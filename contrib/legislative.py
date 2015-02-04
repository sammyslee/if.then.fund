from contrib.models import Trigger, TextFormat, TriggerType, Actor
from django.conf import settings

def create_trigger_from_bill(bill_id, chamber):
	# split/validate the bill ID
	import re
	m = re.match("^([a-z]+)(\d+)-(\d+)$", bill_id)
	if not m: raise ValueError("'%s' is not a bill ID, e.g. hr1234-114." % bill_id)
	bill_type, bill_number, bill_congress = m.groups()
	bill_type = { "hres": "house_resolution", "s": "senate_bill", "sjres": "senate_joint_resolution", "hr": "house_bill", "hconres": "house_concurrent_resolution", "sconres": "senate_concurrent_resolution", "hjres": "house_joint_resolution", "sres": "senate_resolution" }.get(bill_type)
	if not bill_type: raise ValueError("Not a bill ID, e.g. hr1234-114.")

	# validate chamber
	if chamber not in ('s', 'h'): raise ValueError("Chamber must be one of 'h' or 's'.")
	chamber_name = { 's': 'Senate', 'h': 'House' }[chamber]

	# get bill data from GovTrack
	from contrib.utils import query_json_api
	bill_search = query_json_api("https://www.govtrack.us/api/v2/bill", {
		"bill_type": bill_type, "number": bill_number, "congress": bill_congress })
	if len(bill_search['objects']) == 0: raise ValueError("Not a bill.")
	if len(bill_search['objects']) > 1: raise ValueError("Matched multiple bills?")

	bill = bill_search['objects'][0]
	if not bill['is_alive']: raise ValueError("Bill is not alive.")

	# we're going to cache the bill info, so add a timestamp for the retreival date
	import datetime
	bill['as_of'] = datetime.datetime.now().isoformat()

	# get/create TriggerType
	# (in production the object should always exist, but in testing it
	# needs to be created)
	trigger_type, is_new = TriggerType.objects.get_or_create(
		key = "congress_floorvote_%s" % chamber,
		defaults = {
			"strings": {
			"actor": { 's': 'senator', 'h': 'representative' }[chamber],
			"actors": { 's': 'senators', 'h': 'representatives' }[chamber],
			"action_noun": "vote",
			"action_vb_inf": "vote",
			"action_vb_pres_s": "votes",
			"action_vb_past": "voted",
		}})

	# create object
	t = Trigger()
	t.key = "usbill:" + bill_id + ":" + chamber
	t.title = (chamber_name + " Vote on " + bill['title'])[0:200]
	t.owner = None
	t.trigger_type = trigger_type

	from django.template.defaultfilters import slugify
	t.slug = slugify(t.title)[0:200]

	t.description = "The %s will soon vote on %s." % (chamber_name, bill["title"])
	t.description_format = TextFormat.Markdown

	short_title = bill["display_number"]
	t.outcomes = [
		{ "vote_key": "+", "label": "Yes on %s" % short_title },
		{ "vote_key": "-", "label": "No on %s" % short_title },
	]

	t.extra = {
		"max_split":  { 's': 100, 'h': 435 }[chamber],
		"type": "usbill",
		"bill_id": bill_id,
		"chamber": chamber,
		"govtrack_bill_id": bill["id"],
		"bill_info": bill,
	}

	# save and return
	t.save()
	return t

def execute_trigger_from_vote(trigger, govtrack_url):
	import requests, lxml.etree

	# Map vote keys '+' and '-' to outcome indexes.
	outcome_index = { }
	for i, outcome in enumerate(trigger.outcomes):
		outcome_index[outcome['vote_key']] = i

	# Get vote metadata from GovTrack's API, via the undocumented
	# '.json' extension added to vote pages.
	vote = requests.get(govtrack_url+'.json').json()

	# Parse the date, which is in US Eastern time. Must make it
	# timezone-aware to store in our database.
	from django.utils.timezone import make_aware
	import dateutil.parser, dateutil.tz
	when = dateutil.parser.parse(vote['created'])
	z = dateutil.tz.tzfile('/usr/share/zoneinfo/EST5EDT')
	when = make_aware(when, z)

	# Then get how Members of Congress voted via the XML, which conveniently
	# includes everything without limit/offset. The congress project vote
	# JSON doesn't use GovTrack IDs, so it's more convenient to use GovTrack
	# data.
	r = requests.get(govtrack_url+'/export/xml').content
	dom = lxml.etree.fromstring(r)
	actor_outcomes = { }
	for voter in dom.findall('voter'):
		# Validate.
		if not voter.get('id'):
			 # VP tiebreaker
			if voter.get('VP'):
				continue
			raise Exception("Missing data in GovTrack XML.")

		# Get the Actor.
		try:
			actor = Actor.objects.get(govtrack_id=voter.get('id'))
		except Actor.DoesNotExist:
			if settings.DEBUG:
				print("No Actor instance exists here for Member of Congress with GovTrack ID %d." % int(voter.get('id')))
				continue
				
			raise Exception("No Actor instance exists here for Member of Congress with GovTrack ID %d." % int(voter.get('id')))

		# Map vote keys '+' and '-' to outcome indexes.
		# Treat not voting (0 and P) as a null outcome, meaning the Actor didn't
		# take action for our purposes but should be recorded as not participating.
		outcome = outcome_index.get(voter.get('vote'))

		if outcome is None:
			if voter.get('vote') == "0":
				outcome = "Did not vote."
			elif voter.get('vote') == "P":
				outcome = "Voted 'present'."
			else:
				raise ValueError("Invalid vote option key: " + str(voter.get('vote')))

		actor_outcomes[actor] = outcome

	# Make a textual description of what happened.
	description = """The {chamber} voted on this on {date}. For more details, see the [vote record on GovTrack.us]({link}).
	""".format(
		chamber=vote['chamber_label'],
		date=when.strftime("%b. %d, %Y").replace(" 0", ""),
		link=vote['link'],
	)

	# Execute.
	trigger.execute(
		when,
		actor_outcomes,
		description,
		TextFormat.Markdown,
		{
			"govtrack_url": govtrack_url,
			"govtrack_vote": vote,
		})

def geocode(address):
	# Geocodes an address, returning a tuple of (DISTRICT, { metadata }).
	# DISTRICT is in the form of XX00, or UNKN for a geocoder failure.
	# Metadata is other arbitrary info so we have more details about
	# the API calls we're making for later.

	# Use the CDYNE Postal Address Verification API.

	import requests, urllib.parse, lxml.etree, json
	from django.conf import settings

		# http version: "http://pav3.cdyne.com/PavService.svc/VerifyAddressAdvanced"
	r = requests.post("https://pav3.cdyne.com/PavService.svc/rest_s/VerifyAddressAdvanced",
		data=json.dumps({
			"PrimaryAddressLine": address[0],
			"CityName": address[1],
			"State": address[2],
			"ZipCode": address[3],
			"LicenseKey": settings.CDYNE_API_KEY,
			"ReturnCensusInfo": True,
			"ReturnGeoLocation": True,
			"ReturnLegislativeInfo": True,
		}),
		headers={ "Content-Type": "application/json", "Accept": "application/json" }
		)

	# Raise an exception for non-200 OK responses.
	r.raise_for_status()

	# Parse XML.
	r = r.json()
	retcode = int(r["ReturnCode"])
	if retcode in (1, 2):
		raise Exception("CDYNE returned error code %d. See http://wiki.cdyne.com/index.php/PAV_VerifyAddressAdvanced_Output." % retcode)

	if retcode in (10,):
		# Input Address is Not Found.
		return (None, r)

	# Add a timestamp to the response in case we need to know later.
	from django.utils.timezone import now
	r['timestamp'] = now().isoformat()

	# Return the Congressional district and the whole CDYNE response
	# in case we want to re-parse it later.
	return (r['StateAbbreviation'] + r['LegislativeInfo']['CongressionalDistrictNumber'], r)

