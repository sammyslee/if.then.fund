import enum, decimal, copy

from django.db import models, transaction, IntegrityError
from django.conf import settings
from django.utils import timezone

from itfsite.models import User
from contrib.bizlogic import get_pledge_recipients, create_pledge_donation, void_pledge_transaction, HumanReadableValidationError

from jsonfield import JSONField as _JSONField
from enum3field import EnumField, django_enum
from datetime import timedelta

#####################################################################
#
# Utilities
#
#####################################################################

@django_enum
class TextFormat(enum.Enum):
	HTML = 0
	Markdown = 1

class JSONField(_JSONField):
	# turns on sort_keys
    def __init__(self, *args, **kwargs):
        super(_JSONField, self).__init__(*args, dump_kwargs={"sort_keys": True}, **kwargs)

#####################################################################
#
# Triggers
#
# A future event that triggers pledged contributions.
#
#####################################################################

class TriggerType(models.Model):
	"""A class of triggers, like a House vote."""

	key = models.CharField(max_length=64, blank=True, null=True, db_index=True, unique=True, help_text="An opaque look-up key to quickly locate this object.")
	title = models.CharField(max_length=200, help_text="The title for the trigger.")

	created = models.DateTimeField(auto_now_add=True, db_index=True)
	updated = models.DateTimeField(auto_now=True, db_index=True)

	strings = JSONField(default={}, help_text="A dictionary of displayable text.")
	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	def __str__(self):
		return self.key

@django_enum
class TriggerStatus(enum.Enum):
	Draft = 0
	Open = 1
	Paused = 2
	Executed = 3
	Vacated = 4

class Trigger(models.Model):
	"""A future event that triggers a camapaign contribution, such as a roll call vote in Congress."""

	key = models.CharField(max_length=64, blank=True, null=True, db_index=True, unique=True, help_text="An opaque look-up key to quickly locate this object.")

	title = models.CharField(max_length=200, help_text="The title for the trigger.")
	owner = models.ForeignKey(User, blank=True, null=True, on_delete=models.PROTECT, help_text="The user which created the trigger and can update it.")
	trigger_type = models.ForeignKey(TriggerType, on_delete=models.PROTECT, help_text="The type of the trigger, which determines how it is described in text.")

	created = models.DateTimeField(auto_now_add=True, db_index=True)
	updated = models.DateTimeField(auto_now=True, db_index=True)

	slug = models.SlugField(max_length=200, help_text="The URL slug for this trigger.")
	description = models.TextField(help_text="Description text in the format given by description_format.")
	description_format = EnumField(TextFormat, help_text="The format of the description text.")
	status = EnumField(TriggerStatus, default=TriggerStatus.Draft, help_text="The current status of the trigger: Open (accepting pledges), Paused (not accepting pledges), Executed (funds distributed), Vacated (existing pledges invalidated).")
	outcomes = JSONField(default=[], help_text="An array (order matters!) of information for each possible outcome of the trigger, e.g. ['Voted Yes', 'Voted No'].")

	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	pledge_count = models.IntegerField(default=0, help_text="A cached count of the number of pledges made.")
	total_pledged = models.DecimalField(max_digits=6, decimal_places=2, default=0, db_index=True, help_text="A cached total amount of pledges, i.e. prior to execution.")

	def __str__(self):
		return "%s [%d]" % (self.key, self.id)

	def get_absolute_url(self):
		return "/a/%d/%s" % (self.id, self.slug)

	def get_short_url(self):
		# Used in the ref_code of Democracy Engine transactions, which is provided
		# to campaigns, as well as in emails to users with links back to the site.
		return settings.SITE_ROOT_URL + ("/a/%d" % self.id)

	def get_minimum_pledge(self):
		alg = Pledge.current_algorithm()
		m1 = alg['min_contrib']
		m2 = 0
		if 'max_split' in self.extra:
			# The minimum pledge is one cent to all possible recipients, plus fees.
			m2 = decimal.Decimal('0.01') * self.extra['max_split'] * (1 + alg['fees_percent']) + alg['fees_fixed']
			m2 = m2.quantize(decimal.Decimal('.01'), rounding=decimal.ROUND_UP)
		return max(m1, m2)

	# Execute.
	@transaction.atomic
	def execute(self, action_time, actor_outcomes, description, description_format, extra):
		# Executes the trigger.

		# Lock the trigger to prevent race conditions and make sure the Trigger
		# is either Open or Paused.
		trigger = Trigger.objects.select_for_update().filter(id=self.id).first()
		if trigger.status not in (TriggerStatus.Open, TriggerStatus.Paused):
			raise ValueError("Trigger is in state %s." % str(trigger.status))

		# Create TriggerExecution object.
		te = TriggerExecution()
		te.trigger = trigger
		te.cycle = settings.CURRENT_ELECTION_CYCLE
		te.action_time = action_time
		te.description = description
		te.description_format = description_format
		te.extra = extra
		te.save()

		# Create Action objects which represent what each Actor did.
		# actor_outcomes is a dict mapping Actors to outcome indexes
		# or None if the Actor didn't properly participate or a string
		# meaning the Actor didn't participate and the string gives
		# the reason_for_no_outcome value.
		for actor, outcome in actor_outcomes.items():
			ac = Action.create(te, actor, outcome)

		# Mark as executed.
		trigger.status = TriggerStatus.Executed
		trigger.save()

	# Vacate, meaning we do not expect the action to ever occur.
	@transaction.atomic
	def vacate(self):
		trigger = Trigger.objects.select_for_update().filter(id=self.id).first()
		if trigger.status not in (TriggerStatus.Open, TriggerStatus.Paused):
			raise ValueError("Trigger is in state %s." % str(trigger.status))

		# Mark as vacated.
		trigger.status = TriggerStatus.Vacated
		trigger.save()

		# Mark all pledges as vacated.
		pledges = trigger.pledges.select_for_update()
		for p in pledges:
			if p.status != PledgeStatus.Open:
				raise ValueError("Pledge %s is in state %s." % (repr(p), str(p.status)))
			p.status = PledgeStatus.Vacated
			p.save()


class TriggerStatusUpdate(models.Model):
	"""A status update about the Trigger providing further information to users looking at the Trigger that was not known when the Trigger was created."""

	trigger = models.ForeignKey(Trigger, on_delete=models.CASCADE, help_text="The Trigger that this update is about.")
	created = models.DateTimeField(auto_now_add=True, db_index=True)
	updated = models.DateTimeField(auto_now=True)
	text = models.TextField(help_text="Status update text in the format given by text_format.")
	text_format = EnumField(TextFormat, help_text="The format of the text.")

class TriggerExecution(models.Model):
	"""How a Trigger was executed."""

	trigger = models.OneToOneField(Trigger, related_name='execution', on_delete=models.PROTECT, help_text="The Trigger this execution information is about.")

	created = models.DateTimeField(auto_now_add=True, db_index=True)
	updated = models.DateTimeField(auto_now=True, db_index=True)
	action_time = models.DateTimeField(help_text="The date & time the action actually ocurred in the real world.")

	cycle = models.IntegerField(help_text="The election cycle (year) that the trigger was executed in.")

	description = models.TextField(help_text="Once a trigger is executed, additional text added to explain how funds were distributed.")
	description_format = EnumField(TextFormat, help_text="The format of the description text.")

	pledge_count = models.IntegerField(default=0, help_text="A cached count of the number of pledges executed. This counts pledges from unconfirmed email addresses that do not result in contributions. Used to check when a Trigger is done executing.")
	pledge_count_with_contribs = models.IntegerField(default=0, help_text="A cached count of the number of pledges executed with actual contributions made.")
	num_contributions = models.IntegerField(default=0, db_index=True, help_text="A cached total number of campaign contributions executed.")
	total_contributions = models.DecimalField(max_digits=6, decimal_places=2, default=0, db_index=True, help_text="A cached total amount of campaign contributions executed, excluding fees.")

	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	def __str__(self):
		return "%s [exec %s]" % (self.trigger, self.created.strftime("%x"))

	def get_outcomes(self):
		# Get the contribution aggregates by outcome
		# and sort by total amount of contributions.
		outcomes = copy.deepcopy(self.trigger.outcomes)
		for i in range(len(outcomes)):
			outcomes[i]['index'] = i
			outcomes[i]['contribs'] = 0
		for rec in ContributionAggregate.objects.filter(trigger_execution=self, district=None)\
				.exclude(outcome=None)\
				.values('outcome', 'total'):
			outcomes[rec['outcome']]['contribs'] = rec['total']
		outcomes.sort(key = lambda x : x['contribs'], reverse=True)
		return outcomes

#####################################################################
#
# Actors
#
# Elected officials and their official acts.
#
#####################################################################

@django_enum
class ActorParty(enum.Enum):
	Democratic = 1
	Republican = 2
	Independent = 3

	def opposite(self):
		if self == ActorParty.Democratic: return ActorParty.Republican
		if self == ActorParty.Republican: return ActorParty.Democratic
		raise ValueError("%s does not have an opposite party." % str(self))

class Actor(models.Model):
	"""A public figure, e.g. elected official with an election campaign, who might take an action."""

	govtrack_id = models.IntegerField(unique=True, help_text="GovTrack's ID for this person.")

	name_long = models.CharField(max_length=128, help_text="The long form of the person's current name, meant for a page title.")
	name_short = models.CharField(max_length=128, help_text="The short form of the person's current name, usually a last name, meant for in-page second references.")
	name_sort = models.CharField(max_length=128, help_text="The sorted list form of the person's current name.")
	party = EnumField(ActorParty, help_text="The current party of the Actor. For Members of Congress, this is based on how the Member caucuses to avoid Independent as much as possible.")
	title = models.CharField(max_length=200, help_text="Descriptive text for the office held by this actor.")
	
	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	challenger = models.OneToOneField('Recipient', unique=True, null=True, blank=True, related_name="challenger_to", help_text="The Recipient that contributions to this Actor's challenger go to. Independents don't have challengers because they have no opposing party.")

	def __str__(self):
		return self.name_sort

class Action(models.Model):
	"""The outcome of an actor taking an act described by a trigger."""

	execution = models.ForeignKey(TriggerExecution, related_name="actions", on_delete=models.CASCADE, help_text="The TriggerExecution that created this object.")
	action_time = models.DateTimeField(db_index=True, help_text="The date & time the action actually ocurred in the real world.")
	actor = models.ForeignKey(Actor, on_delete=models.PROTECT, help_text="The Actor who took this action.")
	outcome = models.IntegerField(blank=True, null=True, help_text="The outcome index that was taken. May be null if the Actor should have participated but didn't (we want to record to avoid counterintuitive missing data).")

	name_long = models.CharField(max_length=128, help_text="The long form of the person's name at the time of the action, meant for a page title.")
	name_short = models.CharField(max_length=128, help_text="The short form of the person's name at the time of the action, usually a last name, meant for in-page second references.")
	name_sort = models.CharField(max_length=128, help_text="The sorted list form of the person's name at the time of the action.")
	party = EnumField(ActorParty, help_text="The party of the Actor at the time of the action.")
	title = models.CharField(max_length=200, help_text="Descriptive text for the office held by this actor at the time of the action.")
	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	challenger = models.ForeignKey('Recipient', null=True, blank=True, help_text="The Recipient that contributions to this Actor's challenger go to, at the time of the Action. Independents don't have challengers because they have no opposing party.")

	total_contributions_for = models.DecimalField(max_digits=6, decimal_places=2, default=0, help_text="A cached total amount of campaign contributions executed with the actor as the recipient (excluding fees).")
	total_contributions_against = models.DecimalField(max_digits=6, decimal_places=2, default=0, help_text="A cached total amount of campaign contributions executed with an opponent of the actor as the recipient (excluding fees).")

	reason_for_no_outcome = models.CharField(blank=True, null=True, max_length=200, help_text="If outcome is null, why. E.g. 'Did not vote.'.")

	class Meta:
		unique_together = [('execution', 'actor')]

	def __str__(self):
		return "%s is %s | %s" % (
			self.actor,
			self.outcome_label(),
			self.execution)

	def has_outcome(self):
		return self.outcome is not None

	def outcome_label(self):
		if self.outcome is not None:
			return self.execution.trigger.outcomes[self.outcome]['label']
		if self.reason_for_no_outcome:
			return self.reason_for_no_outcome
		return "N/A"

	@staticmethod
	def create(execution, actor, outcome):
		# outcome can be an integer giving the Trigger's outcome index
		# that the Actor did . . .
		if isinstance(outcome, int):
			outcome_index = outcome
			reason_for_no_outcome = None

		# Or it can be None or a string giving an explanation for why
		# the Action has no outcome.
		else:
			outcome_index = None
			reason_for_no_outcome = outcome

		# Create the Action instance.
		a = Action()
		a.execution = execution
		a.actor = actor
		a.outcome = outcome_index
		a.action_time = execution.action_time
		a.reason_for_no_outcome = reason_for_no_outcome

		# Copy fields that may change on the Actor but that we want to know what they were
		# at the time this Action ocurred.
		for f in ('name_long', 'name_short', 'name_sort', 'party', 'title', 'extra', 'challenger'):
			setattr(a, f, getattr(actor, f))

		# Save.
		a.save()
		return a


#####################################################################
#
# Pledges
#
# A pledged campaign contribution by a user.
#
#####################################################################

@django_enum
class PledgeStatus(enum.Enum):
	Open = 1
	Executed = 2
	Vacated = 10 # trigger was vacated, pledge is considered vacated

class NoMassDeleteManager(models.Manager):
	class CustomQuerySet(models.QuerySet):
		def delete(self):
			# Can't do a mass delete because it would not update Trigger.total_pledged,
			# in the case of the Pledge model.
			#
			# Instead call delete() on each instance, which handles the constraint.
			for obj in self:
				obj.delete()
	def get_queryset(self):
		return NoMassDeleteManager.CustomQuerySet(self.model, using=self._db)

class Pledge(models.Model):
	"""A user's pledge of a contribution."""

	user = models.ForeignKey(User, blank=True, null=True, on_delete=models.PROTECT, help_text="The user making the pledge. When an anonymous user makes a pledge, this is null, the user's email address is stored, and the pledge should be considered unconfirmed/provisional and will not be executed.")
	email = models.EmailField(max_length=254, blank=True, null=True, help_text="When an anonymous user makes a pledge, their email address is stored here and we send a confirmation email.")
	trigger = models.ForeignKey(Trigger, related_name="pledges", on_delete=models.PROTECT, help_text="The Trigger that this Pledge is for.")

	# When a Pledge is cancelled, the object is deleted. The three fields above
	# are archived, plus the fields listed in this list:
	cancel_archive_fields = ('created', 'updated', 'algorithm', 'desired_outcome', 'amount', 'cclastfour')

	created = models.DateTimeField(auto_now_add=True, db_index=True)
	updated = models.DateTimeField(auto_now=True)
	algorithm = models.IntegerField(default=0, help_text="In case we change our terms & conditions, or our explanation of how things work, an integer indicating the terms and expectations at the time the user made the pledge.")
	status = EnumField(PledgeStatus, default=PledgeStatus.Open, help_text="The current status of the pledge.")

	desired_outcome = models.IntegerField(help_text="The outcome index that the user desires.")
	amount = models.DecimalField(max_digits=6, decimal_places=2, help_text="The pledge amount in dollars (including fees). The credit card charge may be less in the event that we have to round to the nearest penny-donation.")
	incumb_challgr = models.FloatField(help_text="A float indicating how to split the pledge: -1 (to challenger only) <=> 0 (evenly split between incumbends and challengers) <=> +1 (to incumbents only)")
	filter_party = EnumField(ActorParty, blank=True, null=True, help_text="Contributions only go to candidates whose party matches this party. Independent is not an allowed value here.")
	filter_competitive = models.BooleanField(default=False, help_text="Whether to filter contributions to competitive races.")

	cclastfour = models.CharField(max_length=4, blank=True, null=True, db_index=True, help_text="The last four digits of the user's credit card number, stored & indexed for fast look-up in case we need to find a pledge from a credit card number.")

	pre_execution_email_sent_at = models.DateTimeField(blank=True, null=True, help_text="The date and time when the user was sent an email letting them know that their pledge is about to be executed.")
	post_execution_email_sent_at = models.DateTimeField(blank=True, null=True, help_text="The date and time when the user was sent an email letting them know that their pledge was executed.")

	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	class Meta:
		unique_together = [('trigger', 'user'), ('trigger', 'email')]

	objects = NoMassDeleteManager()

	ENFORCE_EXECUTION_EMAIL_DELAY = True # can disable for testing

	@transaction.atomic
	def save(self, *args, **kwargs):
		# Override .save() so on the INSERT of a new Pledge we increment
		# counters on the Trigger.
		is_new = (not self.id) # if the pk evaluates to false, Django does an INSERT

		# Actually save().
		super(Pledge, self).save(*args, **kwargs)

		# For a new object, increment the trigger's pledge_count and total_pledged
		# fields (atomically).
		if is_new:
			from django.db import models
			t = self.trigger
			t.pledge_count = models.F('pledge_count') + 1
			t.total_pledged = models.F('total_pledged') + self.amount
			t.save(update_fields=['pledge_count', 'total_pledged'])

	@transaction.atomic
	def delete(self):
		if self.status != PledgeStatus.Open:
			raise ValueError("Cannot cancel a Pledge with status %s." % self.status)

		# Decrement the Trigger's pledge_count and total_pledged.
		self.trigger.pledge_count = models.F('pledge_count') - 1
		self.trigger.total_pledged = models.F('total_pledged') - self.amount
		self.trigger.save(update_fields=['pledge_count', 'total_pledged'])

		# Archive as a cancelled pledge.
		cp = CancelledPledge.from_pledge(self)

		# Remove record. Will raise an exception and abort the transaction if
		# the pledge has been executed and a PledgeExecution object refers to this.
		super(Pledge, self).delete()	

	@staticmethod
	def current_algorithm():
		return {
			"id": 1, # a sequence number so we can track changes to our fee structure, etc.
			"min_contrib": 1, # dollars
			"max_contrib": 500, # dollars
			"fees_fixed": decimal.Decimal("0.20"), # 20 cents, convert from string so it is exact
			"fees_percent": decimal.Decimal("0.09"), # 0.09 means 9%, convert from string so it is exact
			"pre_execution_warn_time": (timedelta(days=1), "this time tomorrow"),
		}

	def __str__(self):
		return self.get_email() + " => " + str(self.trigger)

	def get_email(self):
		if self.user:
			return self.user.email
		else:
			return self.email

	@property
	def desired_outcome_label(self):
		return self.trigger.outcomes[self.desired_outcome]["label"]

	@property
	def antidesired_outcome_label(self):
		if len(self.trigger.outcomes) != 2:
			raise ValueError("Trigger has more than two options.")
		return self.trigger.outcomes[1 - self.desired_outcome]["label"]

	@property
	def get_nice_status(self):
		if self.status != PledgeStatus.Executed:
			return self.status.name
		elif self.execution.problem == PledgeExecutionProblem.NoProblem:
			return "Finished"
		else:
			return "Failed"

	@property
	def targets_summary(self):
		# This is mirrored in pledge_form.html.

		desired_outcome_label = self.desired_outcome_label
		antidesired_outcome_label = self.antidesired_outcome_label

		party_filter = ""
		if self.filter_party is not None:
			party_filter = self.filter_party.name + " "

		noun = self.trigger.trigger_type.strings['actors']
		verb = self.trigger.trigger_type.strings['action_vb_inf' if self.status != PledgeStatus.Executed else "action_vb_past"]

		if self.incumb_challgr == 1:
			# "keep em in"
			return "%s%s who %s %s" \
				% (party_filter, noun, verb, desired_outcome_label)
		elif self.incumb_challgr == -1:
			# "throw em out"
			return "the %sopponents in the next general election of %s who %s %s" \
				% (party_filter, noun, verb, antidesired_outcome_label)
		elif party_filter == "":
			# goes to incumbents and challengers, no party filter
			if self.status != PledgeStatus.Executed:
				count = "all %s" % self.trigger.extra['max_split']
			else:
				count = str(self.execution.contributions.count())
			return "%s %s, each getting a part of your contribution if they %s %s, but if they %s %s their part of your contribution will go to their next general election opponent" \
				% (count, noun, verb, desired_outcome_label,
				   verb, antidesired_outcome_label)
		else:
			# goes to incumbents and challengers, with a party filter
			return "%s%s who %s %s and the %sopponents in the next general election of %s who %s %s" \
				% (party_filter, noun, verb, desired_outcome_label,
				   party_filter, noun, verb, antidesired_outcome_label)


	@transaction.atomic
	def confirm_email(self, user):
		# Can't confirm twice, but this might be called twice. In order
		# to prevent a race condition, use select_for_update which locks
		# the row until the transaction ends.
		pledge = Pledge.objects.select_for_update().filter(id=self.id).first()
		if pledge.user: return False

		# Move the anonymous pledge to the user's account.
		pledge.user = user
		pledge.email = None
		pledge.save()

		return True

	@staticmethod
	def find_from_billing(cc_number):
		# Returns an interator that yields matchinig Pledge instances.
		# Must be in parallel to how the view function creates the pledge.
		from django.contrib.auth.hashers import check_password
		cc_number = cc_number.replace(' ', '')
		for p in Pledge.objects.filter(cclastfour=cc_number[-4:]):
			if check_password(cc_number, p.extra['billing']['cc_num_hashed']):
				yield p

	@transaction.atomic
	def execute(self):
		# Lock the Pledge and the Trigger to prevent race conditions.
		pledge = Pledge.objects.select_for_update().filter(id=self.id).first()
		trigger = Trigger.objects.select_for_update().filter(id=pledge.trigger.id).first()
		trigger_execution = trigger.execution

		# Validate state.
		if pledge.status != PledgeStatus.Open:
			raise ValueError("Pledge cannot be executed in status %s." % pledge.status)
		if trigger.status != TriggerStatus.Executed:
			raise ValueError("Pledge cannot be executed when trigger is in status %s." % trigger.status)
		if pledge.algorithm != Pledge.current_algorithm()['id']:
			raise ValueError("Pledge has an invalid algorithm.")

		# Default values.
		problem = PledgeExecutionProblem.NoProblem
		exception = None
		recip_contribs = []
		fees = 0
		total_charge = 0
		de_don = None


		if pledge.user is None:
			# We do not make contributions for pledges from unconfirmed email
			# addresses, since we can't let them know that we're about to
			# execute the pledge. But we execute it so that our data model
			# is consistent: All Pledges associated with an executed Trigger
			# are executed.
			problem = PledgeExecutionProblem.EmailUnconfirmed

		else:
			# Get the actual recipients of the pledge, as a list of tuples of
			# (Recipient, Action). The pledge filters may result in there being
			# no actual recipients.
			recipients = get_pledge_recipients(trigger, pledge)

			if len(recipients) == 0:
				# If there are no matching recipients, we don't make a credit card chage.
				problem = PledgeExecutionProblem.FiltersExcludedAll

			else:
				# Additional checks that don't apply to failed executions for reasons above.
				if pledge.pre_execution_email_sent_at is None:
					raise ValueError("User %s has not yet been sent the pre-execution email." % pledge.user)
				elif (timezone.now() - pledge.pre_execution_email_sent_at) < Pledge.current_algorithm()['pre_execution_warn_time'][0] \
						and not settings.DEBUG and Pledge.ENFORCE_EXECUTION_EMAIL_DELAY:
					raise ValueError("User %s has not yet been given enough time to cancel the pledge." % pledge.user)

				# Make the donation (an authorization).
				#
				# (The transaction records created by the donation are not immediately
				# available, so we know success but can't get further details.)
				try:
					recip_contribs, fees, total_charge, de_don = \
						create_pledge_donation(pledge, recipients)

				# Catch typical exceptions and log them in the PledgeExecutionObject.
				except HumanReadableValidationError as e:
					problem = PledgeExecutionProblem.TransactionFailed
					exception = str(e)

		# From here on, if there is a problem then we need to print DE API donation
		# information before we lose track of it, since nothing will be written to
		# the database on an error.
		try:
			# Sanity check.
			if len(recip_contribs) == 0 and problem == PledgeExecutionProblem.NoProblem:
				raise ValueError("Pledge executing with no recipients but no problem.")

			# Create PledgeExecution object.
			pe = PledgeExecution()
			pe.pledge = pledge
			pe.trigger_execution = trigger_execution
			pe.problem = problem
			pe.charged = total_charge
			pe.fees = fees
			pe.extra = {
				"donation": de_don, # donation record, which refers to transactions
				"exception": exception, 
			}
			pe.save()

			# Create Contribution objects.
			for recipient, action, amount in recip_contribs:
				c = Contribution()
				c.pledge_execution = pe
				c.action = action
				c.recipient = recipient
				c.amount = amount
				c.de_id = recipient.de_id
				c.save()

				# Increment the TriggerExecution and Action's total_contributions.
				c.inc_action_contrib_total()

			# Mark pledge as executed.
			pledge.status = PledgeStatus.Executed
			pledge.save()

			# Increment TriggerExecution's pledge_count so that we know how many pledges
			# have been or have not yet been executed.
			trigger_execution.pledge_count = models.F('pledge_count') + 1
			if len(recip_contribs) > 0:
				trigger_execution.pledge_count_with_contribs = models.F('pledge_count_with_contribs') + 1
			trigger_execution.save(update_fields=['pledge_count', 'pledge_count_with_contribs'])

		except:
			import sys, rtyaml
			print("Problem during the following transaction:", file=sys.stderr)
			print("", file=sys.stderr)
			try:
				print(rtyaml.dump(de_don))
			except:
				print(de_don)
			print("", file=sys.stderr)
			print("The database transaction was rolled back so the PledgeExecution instance is gone. Transaction information isn't immediately available, so someone will need to void the transaction manually.", file=sys.stderr)
			raise


class CancelledPledge(models.Model):
	"""Records when a user cancels a Pledge."""

	created = models.DateTimeField(auto_now_add=True, db_index=True)

	trigger = models.ForeignKey(Trigger, on_delete=models.CASCADE, help_text="The Trigger that the pledge was for.")
	user = models.ForeignKey(User, blank=True, null=True, on_delete=models.CASCADE, help_text="The user who made the pledge, if not anonymous.")
	email = models.EmailField(max_length=254, blank=True, null=True, help_text="The email address of an unconfirmed pledge.")

	pledge = JSONField(blank=True, help_text="The original Pledge information.")

	@staticmethod
	def from_pledge(pledge):
		cp = CancelledPledge()
		cp.trigger = pledge.trigger
		cp.user = pledge.user
		cp.email = pledge.email
		cp.pledge = { k: getattr(pledge, k) for k in Pledge.cancel_archive_fields }
		cp.pledge['amount'] = float(cp.pledge['amount']) # can't JSON-serialize a Decimal
		cp.pledge['created'] = cp.pledge['created'].isoformat() # can't JSON-serialize a DateTime
		cp.pledge['updated'] = cp.pledge['updated'].isoformat() # can't JSON-serialize a DateTime
		cp.save()

@django_enum
class PledgeExecutionProblem(enum.Enum):
	NoProblem = 0
	EmailUnconfirmed = 1 # email address on the pledge was not confirmed
	FiltersExcludedAll = 2 # no recipient matched filters
	TransactionFailed = 3 # problems making the donation in the DE api

class PledgeExecution(models.Model):
	"""How a user's pledge was executed. Each pledge has a single PledgeExecution when the Trigger is executed, and immediately many Contribution objects are created."""

	pledge = models.OneToOneField(Pledge, related_name="execution", on_delete=models.PROTECT, help_text="The Pledge this execution information is about.")
	trigger_execution = models.ForeignKey(TriggerExecution, related_name="pledges", on_delete=models.PROTECT, help_text="The TriggerExecution this execution information is about.")

	created = models.DateTimeField(auto_now_add=True, db_index=True)

	problem = EnumField(PledgeExecutionProblem, default=PledgeExecutionProblem.NoProblem, help_text="A problem code associated with a failure to make any contributions for the pledge.")
	charged = models.DecimalField(max_digits=6, decimal_places=2, help_text="The amount the user's account was actually charged, in dollars and including fees. It may differ from the pledge amount to ensure that contributions of whole-cent amounts could be made to candidates.")
	fees = models.DecimalField(max_digits=6, decimal_places=2, help_text="The fees the user was charged, in dollars.")
	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	district = models.CharField(max_length=4, blank=True, null=True, db_index=True, help_text="The congressional district of the user (at the time of the pledge), in the form of XX00.")

	objects = NoMassDeleteManager()

	def __str__(self):
		return str(self.pledge)

	@transaction.atomic
	def delete(self, allow_credit=False):
		# Delete the contributions explicitly so that .delete() gets called (by our manager).
		self.contributions.all().delete()

		# Return the Pledge to the open state so we can try to execute again.
		self.pledge.status = PledgeStatus.Open
		self.pledge.save()

		# Decrement the TriggerExecution's pledge_count.
		te = self.pledge.trigger.execution
		te.pledge_count = models.F('pledge_count') - 1
		if self.problem == PledgeExecutionProblem.NoProblem:
			te.pledge_count_with_contribs = models.F('pledge_count_with_contribs') - 1
		te.save(update_fields=['pledge_count', 'pledge_count_with_contribs'])

		# Delete record.
		super(PledgeExecution, self).delete()	

		# Void or refund the transaction. There should be only one, but
		# just in case get a list of all mentioned transactions for the
		# donation. Do this last so that if the void succeeds no other
		# error can follow. The execution may not have resulted in a
		# transaction, so of course only do this if there was a donation
		# record.
		if self.extra['donation']:
			txns = set(item['transaction_guid'] for item in self.extra['donation']['line_items'])
			for txn in txns:
				# Raises an exception on failure.
				void_pledge_transaction(txn, allow_credit=allow_credit)

	def show_txn(self):
		import rtyaml
		from contrib.bizlogic import DemocracyEngineAPI
		txns = set(item['transaction_guid'] for item in self.extra['donation']['line_items'])
		for txn in txns:
			print(rtyaml.dump(DemocracyEngineAPI.get_transaction(txn)))

	@property
	def problem_text(self):
		if self.problem == PledgeExecutionProblem.EmailUnconfirmed:
			return "Your contribution was not made because you did not confirm your email address prior to the %s." \
				% self.pledge.trigger.trigger_type.strings['action_noun']
		if self.problem == PledgeExecutionProblem.TransactionFailed:
			return "There was a problem charging your credit card and making the contribution: %s. Your contribution could not be made." \
				% self.pledge.execution.extra['exception']
		if self.problem == PledgeExecutionProblem.FiltersExcludedAll:
			return "Your contribution was not made because there were no %s that met your criteria of %s." \
				% (self.pledge.trigger.trigger_type.strings['actors'], self.pledge.targets_summary)

	@transaction.atomic
	def update_district(self, district, other):
		# lock so we don't overwrite
		self = PledgeExecution.objects.filter(id=self.id).select_for_update().get()

		# temporarily decrement all of the contributions from the aggregates
		for c in self.contributions.all():
			c.inc_action_contrib_total(factor=-1)

		self.district = district
		self.extra['geocode'] = other
		self.save(update_fields=['district', 'extra'])

		# re-increment now that the district is set
		for c in self.contributions.all():
			c.inc_action_contrib_total(factor=1)

#####################################################################
#
# Recipients and Contributions
#
# Actual campaign contributions made.
#
#####################################################################

class Recipient(models.Model):
	"""A contribution recipient, with the current Democracy Engine recipient ID, which is either an Actor (an incumbent) or a logically specified general election candidate by office sought and party."""

	de_id = models.CharField(max_length=64, unique=True, help_text="The Democracy Engine ID that we have assigned to this recipient.")
	active = models.BooleanField(default=True, help_text="Whether this Recipient can currently receive funds.")

	actor = models.ForeignKey(Actor, blank=True, null=True, unique=True, help_text="The Actor that this recipient corresponds to (i.e. this Recipient is an incumbent).")

	office_sought = models.CharField(max_length=7, blank=True, null=True, help_text="For challengers, a code specifying the office sought in the form of 'S-NY-I' (New York class 1 senate seat) or 'H-TX-30' (Texas 30th congressional district). Unique with party.")
	party = EnumField(ActorParty, blank=True, null=True, help_text="The party of the challenger, or null if this Recipient is for an incumbent. Unique with office_sought.")

	class Meta:
		unique_together = [('office_sought', 'party')]

	def __str__(self):
		if self.actor:
			# is an incumbent
			return str(self.actor)
		else:
			try:
				# is a currently challenger of someone
				return self.party.name + " Challenger to " + str(self.challenger_to) + " (" + self.office_sought + ")"
			except:
				# is not a current challenger of someone, so just use office/party designation
				return self.office_sought + ":" + str(self.party)

	@property
	def is_challenger(self):
		return self.actor is None

class Contribution(models.Model):
	"""A fully executed campaign contribution."""

	pledge_execution = models.ForeignKey(PledgeExecution, related_name="contributions", on_delete=models.PROTECT, help_text="The PledgeExecution this execution information is about.")
	action = models.ForeignKey(Action, on_delete=models.PROTECT, help_text="The Action this contribution was made in reaction to.")
	recipient = models.ForeignKey(Recipient, related_name="contributions", on_delete=models.PROTECT, help_text="The Recipient this contribution was sent to.")
	amount = models.DecimalField(max_digits=6, decimal_places=2, help_text="The amount of the contribution, in dollars.")
	refunded_time = models.DateTimeField(blank=True, null=True, help_text="If the contribution was refunded to the user, the time that happened.")

	de_id = models.CharField(max_length=64, help_text="The Democracy Engine ID that the contribution was assigned to.")

	extra = JSONField(blank=True, help_text="Additional information about the contribution.")

	objects = NoMassDeleteManager()

	class Meta:
		unique_together = [('pledge_execution', 'action'), ('pledge_execution', 'recipient')]

	def __str__(self):
		return "$%0.2f to %s for %s" % (self.amount, self.recipient, self.pledge_execution)

	def name_long(self):
		if not self.recipient.is_challenger:
			# is an incumbent
			return self.action.name_long
		else:
			# is a challenger, but who it was a challenger to may be different
			# from who the recipient is a challenger to now, so use the action
			# to get the name of the incumbent.
			return self.recipient.party.name + " Challenger to " + self.action.name_long

	@transaction.atomic
	def delete(self):
		# Delete this object. You almost certainly do NOT want to do this
		# since the transaction line item will remain on the Democracy
		# Engine side.

		# Decrement the TriggerExecution and Action's total_pledged fields.
		self.inc_action_contrib_total(factor=-1)

		# Remove record.
		super(Contribution, self).delete()	

	def inc_action_contrib_total(self, factor=1):
		# Increment the totals on the Action instance. This excludes fees because
		# this is based on transaction line items.
		if not self.recipient.is_challenger:
			# Contribution was to the Actor.
			field = 'total_contributions_for'
		else:
			# Contribution was to the Actor's opponent.
			field = 'total_contributions_against'
		setattr(self.action, field, models.F(field) + self.amount*factor)
		self.action.save(update_fields=[field])

		# Increment the TriggerExecution's total_contributions. Likewise, it
		# excludes fees.
		self.action.execution.total_contributions = models.F('total_contributions') + self.amount*factor
		self.action.execution.num_contributions = models.F('num_contributions') + 1*factor
		self.action.execution.save(update_fields=['total_contributions', 'num_contributions'])

		# Increment the cached ContributionAggregate for the desired outcome.
		# Note that Pledge.district is None before we've done the look-up,
		# and we'll just omit those from the aggregates.
		slices = []
		for outcome in (None, self.pledge_execution.pledge.desired_outcome):
			for district in set([None, self.pledge_execution.district]):
				slices.append({ "outcome": outcome, "district": district })
		for slce in slices:
			agg, is_new = ContributionAggregate.objects.get_or_create(
				trigger_execution=self.action.execution,
				**slce)
			agg.total = models.F('total') + self.amount*factor
			agg.save(update_fields=['total'])

class ContributionAggregate(models.Model):
	"""Aggregate totals for various slices of contributions."""

	trigger_execution = models.ForeignKey(TriggerExecution, related_name='contribution_aggregates', on_delete=models.CASCADE, help_text="The TriggerExecution that these cached statistics are about.")
	updated = models.DateTimeField(auto_now=True, db_index=True)

	outcome = models.IntegerField(blank=True, null=True, help_text="The outcome index that was taken. Null if the slice encompasses all outcomes.")
	district = models.CharField(max_length=4, blank=True, null=True, help_text="The congressional district of the user (at the time of the pledge), in the form of XX00. Null if the slice encompasses all district.")

	total = models.DecimalField(max_digits=6, decimal_places=2, default=0, db_index=True, help_text="A cached total amount of campaign contributions executed, excluding fees.")

	class Meta:
		unique_together = ('trigger_execution', 'outcome', 'district')
