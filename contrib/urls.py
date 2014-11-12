from django.conf.urls import patterns, include, url

urlpatterns = patterns('',
	url(r'a/(\d+)(?:/([a-z0-9_-]+))?$', 'contrib.views.trigger', name='trigger'),
	url(r'contrib/_submit$', 'contrib.views.submit', name='contrib_submit'),
	url(r'contrib/(\d+)$', 'contrib.views.show_contrib', name='contrib'),
	url(r'contrib/_defaults$', 'contrib.views.get_user_defaults', name='contrib_defaults'),
)
