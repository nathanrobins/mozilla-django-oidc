from django.conf.urls import url
from django.utils.module_loading import import_string

from mozilla_django_oidc import views
from mozilla_django_oidc.utils import import_from_settings

DEFAULT_CALLBACK_CLASS = 'mozilla_django_oidc.views.OIDCAuthenticationCallbackView'
CALLBACK_CLASS_PATH = import_from_settings('OIDC_CALLBACK_CLASS', DEFAULT_CALLBACK_CLASS)

OIDCCallbackClass = import_string(CALLBACK_CLASS_PATH)

urlpatterns = [
    url(r'^callback/$', OIDCCallbackClass.as_view(),
        name='oidc_authentication_callback'),
    url(r'^authenticate/$', views.OIDCAuthenticationRequestView.as_view(),
        name='oidc_authentication_init'),
]
