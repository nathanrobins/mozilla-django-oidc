import base64
import hashlib
import json
import logging

import requests
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import ImproperlyConfigured, SuspiciousOperation
from django.urls import reverse
from django.utils.encoding import force_bytes, smart_bytes, smart_str
from django.utils.module_loading import import_string
from josepy.b64 import b64decode
from josepy.jwk import JWK
from josepy.jws import JWS, Header
from requests.auth import HTTPBasicAuth

from mozilla_django_oidc.utils import absolutify, import_from_settings

LOGGER = logging.getLogger(__name__)


def default_username_algo(email):
    """Generate username for the Django user.

    :arg str/unicode email: the email address to use to generate a username

    :returns: str/unicode

    """
    # bluntly stolen from django-browserid
    # store the username as a base64 encoded sha224 of the email address
    # this protects against data leakage because usernames are often
    # treated as public identifiers (so we can't use the email address).
    username = base64.urlsafe_b64encode(
        hashlib.sha1(force_bytes(email)).digest()
    ).rstrip(b"=")

    return smart_str(username)


class OIDCAuthenticationBackend(ModelBackend):
    """Override Django's authentication."""

    def __init__(self, *args, **kwargs):
        """Initialize settings."""
        self.OIDC_OP_TOKEN_ENDPOINT = self.get_settings("OIDC_OP_TOKEN_ENDPOINT")
        self.OIDC_OP_USER_ENDPOINT = self.get_settings("OIDC_OP_USER_ENDPOINT")
        self.OIDC_OP_JWKS_ENDPOINT = self.get_settings("OIDC_OP_JWKS_ENDPOINT", None)
        self.OIDC_RP_CLIENT_ID = self.get_settings("OIDC_RP_CLIENT_ID")
        self.OIDC_RP_CLIENT_SECRET = self.get_settings("OIDC_RP_CLIENT_SECRET")
        self.OIDC_RP_SIGN_ALGO = self.get_settings("OIDC_RP_SIGN_ALGO", "HS256")
        self.OIDC_RP_IDP_SIGN_KEY = self.get_settings("OIDC_RP_IDP_SIGN_KEY", None)
        self.OIDC_OP_CONFIGURATIONS = self.get_settings("OIDC_OP_CONFIGURATIONS", {})

        if self.OIDC_RP_SIGN_ALGO.startswith("RS") and (
            self.OIDC_RP_IDP_SIGN_KEY is None and self.OIDC_OP_JWKS_ENDPOINT is None
        ):
            msg = "{} alg requires OIDC_RP_IDP_SIGN_KEY or OIDC_OP_JWKS_ENDPOINT to be configured."
            raise ImproperlyConfigured(msg.format(self.OIDC_RP_SIGN_ALGO))

        self.UserModel = get_user_model()

    @staticmethod
    def get_settings(attr, *args):
        return import_from_settings(attr, *args)

    def describe_user_by_claims(self, claims):
        email = claims.get("email")
        return "email {}".format(email)

    def filter_users_by_claims(self, claims):
        """Return all users matching the specified email."""
        email = claims.get("email")
        if not email:
            return self.UserModel.objects.none()
        return self.UserModel.objects.filter(email__iexact=email)

    def verify_claims(self, claims):
        """Verify the provided claims to decide if authentication should be allowed."""

        # Verify claims required by default configuration
        scopes = self.get_settings("OIDC_RP_SCOPES", "openid email")
        if "email" in scopes.split():
            return "email" in claims

        LOGGER.warning(
            "Custom OIDC_RP_SCOPES defined. "
            "You need to override `verify_claims` for custom claims verification."
        )

        return True

    def create_user(self, claims):
        """Return object for a newly created user account."""
        email = claims.get("email")
        username = self.get_username(claims)
        return self.UserModel.objects.create_user(username, email=email)

    def get_username(self, claims):
        """Generate username based on claims."""
        # bluntly stolen from django-browserid
        # https://github.com/mozilla/django-browserid/blob/master/django_browserid/auth.py
        username_algo = self.get_settings("OIDC_USERNAME_ALGO", None)

        if username_algo:
            if isinstance(username_algo, str):
                username_algo = import_string(username_algo)
            return username_algo(claims.get("email"))

        return default_username_algo(claims.get("email"))

    def update_user(self, user, claims):
        """Update existing user with new claims, if necessary save, and return user"""
        return user

    def _verify_jws(self, payload, key, request):
        """Verify the given JWS payload with the given key and return the payload"""
        jws = JWS.from_compact(payload)

        try:
            alg = jws.signature.combined.alg.name
        except KeyError:
            msg = "No alg value found in header"
            raise SuspiciousOperation(msg)
        REQUEST_BASE = request.build_absolute_uri('/')
        REQUEST_OIDC_SETTINGS = self.OIDC_OP_CONFIGURATIONS.get(REQUEST_BASE, {})
        REQUEST_OIDC_RP_SIGN_ALGO = REQUEST_OIDC_SETTINGS.get('SIGN_ALGO', self.OIDC_RP_SIGN_ALGO)
        if alg != REQUEST_OIDC_RP_SIGN_ALGO:
            msg = (
                "The provider algorithm {!r} does not match the client's "
                "OIDC_RP_SIGN_ALGO.".format(alg)
            )
            raise SuspiciousOperation(msg)

        if isinstance(key, str):
            # Use smart_bytes here since the key string comes from settings.
            jwk = JWK.load(smart_bytes(key))
        else:
            # The key is a json returned from the IDP JWKS endpoint.
            jwk = JWK.from_json(key)

        if not jws.verify(jwk):
            msg = "JWS token verification failed."
            raise SuspiciousOperation(msg)

        return jws.payload

    def retrieve_matching_jwk(self, token, request):
        """Get the signing key by exploring the JWKS endpoint of the OP."""
        REQUEST_BASE = request.build_absolute_uri('/')
        REQUEST_OIDC_SETTINGS = self.OIDC_OP_CONFIGURATIONS.get(REQUEST_BASE, {})
        REQUEST_OIDC_OP_JWKS_ENDPOINT = REQUEST_OIDC_SETTINGS.get('JWKS_ENDPOINT', self.OIDC_OP_JWKS_ENDPOINT)

        response_jwks = requests.get(
            REQUEST_OIDC_OP_JWKS_ENDPOINT,
            verify=self.get_settings("OIDC_VERIFY_SSL", True),
            timeout=self.get_settings("OIDC_TIMEOUT", None),
            proxies=self.get_settings("OIDC_PROXY", None),
        )
        response_jwks.raise_for_status()
        jwks = response_jwks.json()

        # Compute the current header from the given token to find a match
        jws = JWS.from_compact(token)
        json_header = jws.signature.protected
        header = Header.json_loads(json_header)

        key = None
        for jwk in jwks["keys"]:
            if import_from_settings("OIDC_VERIFY_KID", True) and jwk[
                "kid"
            ] != smart_str(header.kid):
                continue
            if "alg" in jwk and jwk["alg"] != smart_str(header.alg):
                continue
            key = jwk
        if key is None:
            raise SuspiciousOperation("Could not find a valid JWKS.")
        return key

    def get_payload_data(self, token, key, request):
        """Helper method to get the payload of the JWT token."""
        if self.get_settings("OIDC_ALLOW_UNSECURED_JWT", False):
            header, payload_data, signature = token.split(b".")
            header = json.loads(smart_str(b64decode(header)))

            # If config allows unsecured JWTs check the header and return the decoded payload
            if "alg" in header and header["alg"] == "none":
                return b64decode(payload_data)

        # By default fallback to verify JWT signatures
        return self._verify_jws(token, key, request)

    def verify_token(self, token, request, **kwargs):
        """Validate the token signature."""
        nonce = kwargs.get("nonce")

        token = force_bytes(token)
        REQUEST_BASE = request.build_absolute_uri('/')
        REQUEST_OIDC_SETTINGS = self.OIDC_OP_CONFIGURATIONS.get(REQUEST_BASE, {})
        REQUEST_OIDC_RP_SIGN_ALGO = REQUEST_OIDC_SETTINGS.get('SIGN_ALGO', self.OIDC_RP_SIGN_ALGO)
        REQUEST_OIDC_RP_IDP_SIGN_KEY = REQUEST_OIDC_SETTINGS.get('IDP_SIGN_KEY', self.OIDC_RP_IDP_SIGN_KEY )
        REQUEST_OIDC_RP_CLIENT_SECRET = REQUEST_OIDC_SETTINGS.get('CLIENT_SECRET', self.OIDC_RP_CLIENT_SECRET)
        if REQUEST_OIDC_RP_SIGN_ALGO.startswith("RS"):
            if self.OIDC_RP_IDP_SIGN_KEY is not None:
                key = REQUEST_OIDC_RP_IDP_SIGN_KEY
            else:
                key = self.retrieve_matching_jwk(token, request)
        else:
            key = REQUEST_OIDC_RP_CLIENT_SECRET

        payload_data = self.get_payload_data(token, key, request)

        # The 'token' will always be a byte string since it's
        # the result of base64.urlsafe_b64decode().
        # The payload is always the result of base64.urlsafe_b64decode().
        # In Python 3 and 2, that's always a byte string.
        # In Python3.6, the json.loads() function can accept a byte string
        # as it will automagically decode it to a unicode string before
        # deserializing https://bugs.python.org/issue17909
        payload = json.loads(payload_data.decode("utf-8"))
        token_nonce = payload.get("nonce")

        if self.get_settings("OIDC_USE_NONCE", True) and nonce != token_nonce:
            msg = "JWT Nonce verification failed."
            raise SuspiciousOperation(msg)
        return payload

    def get_token(self, payload, request):
        """Return token object as a dictionary."""

        auth = None
        if self.get_settings("OIDC_TOKEN_USE_BASIC_AUTH", False):
            # When Basic auth is defined, create the Auth Header and remove secret from payload.
            user = payload.get("client_id")
            pw = payload.get("client_secret")

            auth = HTTPBasicAuth(user, pw)
            del payload["client_secret"]
        REQUEST_BASE = request.build_absolute_uri('/')
        REQUEST_OIDC_SETTINGS = self.OIDC_OP_CONFIGURATIONS.get(REQUEST_BASE, {})
        REQUEST_OIDC_OP_TOKEN_ENDPOINT = REQUEST_OIDC_SETTINGS.get('TOKEN_ENDPOINT', self.OIDC_OP_TOKEN_ENDPOINT)
        response = requests.post(
            REQUEST_OIDC_OP_TOKEN_ENDPOINT,
            data=payload,
            auth=auth,
            verify=self.get_settings("OIDC_VERIFY_SSL", True),
            timeout=self.get_settings("OIDC_TIMEOUT", None),
            proxies=self.get_settings("OIDC_PROXY", None),
        )
        response.raise_for_status()
        return response.json()

    def get_userinfo(self, access_token, id_token, payload, request):
        """Return user details dictionary. The id_token and payload are not used in
        the default implementation, but may be used when overriding this method"""
        REQUEST_BASE = request.build_absolute_uri('/')
        REQUEST_OIDC_SETTINGS = self.OIDC_OP_CONFIGURATIONS.get(REQUEST_BASE, {})
        REQUEST_OIDC_OP_USER_ENDPOINT = REQUEST_OIDC_SETTINGS.get('USER_ENDPOINT', self.OIDC_OP_USER_ENDPOINT)
        user_response = requests.get(
            REQUEST_OIDC_OP_USER_ENDPOINT,
            headers={"Authorization": "Bearer {0}".format(access_token)},
            verify=self.get_settings("OIDC_VERIFY_SSL", True),
            timeout=self.get_settings("OIDC_TIMEOUT", None),
            proxies=self.get_settings("OIDC_PROXY", None),
        )
        user_response.raise_for_status()
        return user_response.json()

    def authenticate(self, request, **kwargs):
        """Authenticates a user based on the OIDC code flow."""

        self.request = request
        if not self.request:
            return None

        state = self.request.GET.get("state")
        code = self.request.GET.get("code")
        nonce = kwargs.pop("nonce", None)
        code_verifier = kwargs.pop("code_verifier", None)

        if not code or not state:
            return None

        reverse_url = self.get_settings(
            "OIDC_AUTHENTICATION_CALLBACK_URL", "oidc_authentication_callback"
        )
        # Replace OIDC_RP_CLIENT_ID and OIDC_RP_CLIENT_SECRET based on request
        token_payload = {
            "client_id": self.OIDC_RP_CLIENT_ID,
            "client_secret": self.OIDC_RP_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": absolutify(self.request, reverse(reverse_url)),
        }

        # Send code_verifier with token request if using PKCE
        if code_verifier is not None:
            token_payload.update({"code_verifier": code_verifier})

        # Get the token
        token_info = self.get_token(token_payload, request)
        id_token = token_info.get("id_token")
        access_token = token_info.get("access_token")

        # Validate the token
        payload = self.verify_token(id_token, nonce=nonce, request=request)

        if payload:
            self.store_tokens(access_token, id_token)
            try:
                return self.get_or_create_user(access_token, id_token, payload, request)
            except SuspiciousOperation as exc:
                LOGGER.warning("failed to get or create user: %s", exc)
                return None

        return None

    def store_tokens(self, access_token, id_token):
        """Store OIDC tokens."""
        session = self.request.session

        if self.get_settings("OIDC_STORE_ACCESS_TOKEN", False):
            session["oidc_access_token"] = access_token

        if self.get_settings("OIDC_STORE_ID_TOKEN", False):
            session["oidc_id_token"] = id_token

    def get_or_create_user(self, access_token, id_token, payload, request):
        """Returns a User instance if 1 user is found. Creates a user if not found
        and configured to do so. Returns nothing if multiple users are matched."""

        user_info = self.get_userinfo(access_token, id_token, payload, request)

        claims_verified = self.verify_claims(user_info)
        if not claims_verified:
            msg = "Claims verification failed"
            raise SuspiciousOperation(msg)

        # email based filtering
        users = self.filter_users_by_claims(user_info)

        if len(users) == 1:
            return self.update_user(users[0], user_info)
        elif len(users) > 1:
            # In the rare case that two user accounts have the same email address,
            # bail. Randomly selecting one seems really wrong.
            msg = "Multiple users returned"
            raise SuspiciousOperation(msg)
        elif self.get_settings("OIDC_CREATE_USER", True):
            user = self.create_user(user_info)
            return user
        else:
            LOGGER.debug(
                "Login failed: No user with %s found, and " "OIDC_CREATE_USER is False",
                self.describe_user_by_claims(user_info),
            )
            return None

    def get_user(self, user_id):
        """Return a user based on the id."""

        try:
            return self.UserModel.objects.get(pk=user_id)
        except self.UserModel.DoesNotExist:
            return None
