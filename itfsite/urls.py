from django.conf.urls import patterns, include, url
from django.contrib import admin
from django.contrib.auth import views as auth_views

urlpatterns = patterns('',
	url(r'^$', 'itfsite.views.homepage', name='homepage'),
	url(r'^(about|about/how-it-works|about/theory-of-change|about/legal|privacy|terms|open)$', 'itfsite.views.simplepage', name='simplepage'),
	url(r'^accounts/welcome$', 'itfsite.accounts.welcome', name='welcome'),
	url(r'^home$', 'itfsite.views.user_home', name='home'),
	url(r'^home/contributions$', 'itfsite.views.user_contribution_details', name='user_contrib_details'),

	url(r'^', include('contrib.urls')),

	url(r'^accounts/login$', auth_views.login),
	url(r'^accounts/logout$', auth_views.logout, { 'next_page': '/' } ),
	url(r'^accounts/password-change$', auth_views.password_change, { 'post_change_redirect': '/home', 'template_name': 'registration/password_change.html' }, name='password_change' ),


	url(r'^admin/', include(admin.site.urls)),

	url(r'^_twostream', include('twostream.urls')),

    url(r'^ev/', include('email_confirm_la.urls')),
)
