"""OAuth 2.0 (authorization code + PKCE) for Gmail and Microsoft mailboxes, and token refresh.

Blocking HTTP (``urllib``): callers run it in a worker thread, like the mail protocols. Endpoints,
scopes and hosts default to the providers' public ones and can be overridden per provider in the
``oauth`` config block (tests point them at a fake).

Scopes: Google's IMAP/POP/SMTP access needs the full ``https://mail.google.com/`` scope;
Microsoft's are the Outlook resource's ``IMAP.AccessAsUser.All``, ``POP.AccessAsUser.All`` and
``SMTP.Send``, plus ``offline_access`` for a refresh token. ``openid email`` gives an id token
naming the mailbox address.
"""

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from django.conf import settings

from mail.errors import MailError, not_configured
from mail.models import MailErrorCode, Provider


@dataclass(frozen=True)
class ProviderDefaults:
    config_key: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    imap: tuple[str, int, str]
    pop3: tuple[str, int, str]
    smtp: tuple[str, int, str]
    extra_params: tuple[tuple[str, str], ...] = ()


DEFAULTS: dict[str, ProviderDefaults] = {
    Provider.GMAIL: ProviderDefaults(
        config_key="google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=("https://mail.google.com/", "openid", "email"),
        imap=("imap.gmail.com", 993, "TLS"),
        pop3=("pop.gmail.com", 995, "TLS"),
        smtp=("smtp.gmail.com", 465, "TLS"),
        # A refresh token is only issued with offline access, and only again on consent.
        extra_params=(("access_type", "offline"), ("prompt", "consent")),
    ),
    Provider.MICROSOFT: ProviderDefaults(
        config_key="microsoft",
        authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        scopes=(
            "https://outlook.office.com/IMAP.AccessAsUser.All",
            "https://outlook.office.com/POP.AccessAsUser.All",
            "https://outlook.office.com/SMTP.Send",
            "offline_access",
            "openid",
            "email",
        ),
        imap=("outlook.office365.com", 993, "TLS"),
        pop3=("outlook.office365.com", 995, "TLS"),
        smtp=("smtp.office365.com", 587, "STARTTLS"),
        extra_params=(("prompt", "select_account"),),
    ),
}


@dataclass(frozen=True)
class OAuthClient:
    """A configured provider: defaults with the deployment's overrides applied."""

    provider: str
    client_id: str
    client_secret: str | None
    redirect_urls: tuple[str, ...]
    authorize_url: str
    token_url: str
    userinfo_url: str | None
    scopes: tuple[str, ...]
    imap: tuple[str, int, str]
    pop3: tuple[str, int, str]
    smtp: tuple[str, int, str]
    extra_params: tuple[tuple[str, str], ...]
    timeout: float


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    id_token: str | None


class ProviderUnreachable(MailError):
    """The provider gave no answer at all: whatever was asked may still be good."""


class TokenRevoked(MailError):
    """The refresh token was revoked or ran out (``invalid_grant``)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, MailErrorCode.CONSENT_EXPIRED)


def client_for(provider: str) -> OAuthClient:
    """The configured client of ``provider``; NOT_CONFIGURED without one."""
    defaults = DEFAULTS.get(provider)
    if defaults is None:
        raise MailError(f"{provider} mailboxes are not linked with OAuth.", MailErrorCode.INVALID_STATE)
    conf = (settings.KUVERT_OAUTH or {}).get(defaults.config_key)
    if not conf:
        raise not_configured(f"OAuth for {provider}")
    return OAuthClient(
        provider=provider,
        client_id=conf["client_id"],
        client_secret=conf.get("client_secret"),
        redirect_urls=tuple(conf["redirect_urls"]),
        authorize_url=conf.get("authorize_url") or defaults.authorize_url,
        token_url=conf.get("token_url") or defaults.token_url,
        userinfo_url=conf.get("userinfo_url"),
        scopes=tuple(conf.get("scopes") or defaults.scopes),
        imap=(conf.get("imap_host") or defaults.imap[0], conf.get("imap_port") or defaults.imap[1], defaults.imap[2]),
        pop3=defaults.pop3,
        smtp=(conf.get("smtp_host") or defaults.smtp[0], conf.get("smtp_port") or defaults.smtp[1], defaults.smtp[2]),
        extra_params=defaults.extra_params,
        timeout=float(conf.get("timeout_seconds") or 20),
    )


def configured_providers() -> list[str]:
    """The providers this deployment has an OAuth client for."""
    oauth = settings.KUVERT_OAUTH or {}
    return [provider for provider, defaults in DEFAULTS.items() if oauth.get(defaults.config_key)]


def pkce_pair() -> tuple[str, str]:
    """A PKCE (verifier, S256 challenge)."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(client: OAuthClient, state: str, challenge: str, redirect_url: str, login_hint: str | None = None) -> str:
    """Where the user approves access."""
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_url,
        "scope": " ".join(client.scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **dict(client.extra_params),
    }
    if login_hint:
        params["login_hint"] = login_hint
    separator = "&" if "?" in client.authorize_url else "?"
    return f"{client.authorize_url}{separator}{urllib.parse.urlencode(params)}"


def _post(client: OAuthClient, form: dict[str, str]) -> dict:
    if client.client_secret:
        form = {**form, "client_secret": client.client_secret}
    request = urllib.request.Request(
        client.token_url,
        data=urllib.parse.urlencode({"client_id": client.client_id, **form}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=client.timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read() or b"{}")
        except ValueError:
            body = {}
        kind = body.get("error") or f"HTTP {error.code}"
        description = body.get("error_description") or ""
        if kind == "invalid_grant":
            raise TokenRevoked(f"The provider refused the grant: {description or kind}") from error
        failure = ProviderUnreachable if error.code >= 500 or error.code == 429 else MailError  # no answer about the request itself
        raise failure(f"The OAuth provider answered {kind}: {description}".strip(": "), MailErrorCode.PROVIDER_ERROR) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ProviderUnreachable(f"The OAuth provider could not be reached: {error}", MailErrorCode.PROVIDER_ERROR) from error


def _tokens(body: dict) -> TokenSet:
    if not body.get("access_token"):
        raise MailError("The OAuth provider returned no access token.", MailErrorCode.PROVIDER_ERROR)
    expires = body.get("expires_in")
    return TokenSet(body["access_token"], body.get("refresh_token"), int(expires) if expires is not None else None, body.get("id_token"))


def exchange_code(client: OAuthClient, code: str, verifier: str, redirect_url: str) -> TokenSet:
    """Tokens for an approved authorization code."""
    return _tokens(_post(client, {"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": redirect_url}))


def refresh(client: OAuthClient, refresh_token: str) -> TokenSet:
    """A new access token (and, for providers that rotate them, a new refresh token)."""
    return _tokens(_post(client, {"grant_type": "refresh_token", "refresh_token": refresh_token, "scope": " ".join(client.scopes)}))


def _claims(id_token: str) -> dict:
    """The claims of an id token straight from the token endpoint.

    Not signature-checked: it was received directly from the provider's token endpoint over TLS
    in the code exchange, which OpenID Connect Core (3.1.3.7) accepts as validation.
    """
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def mailbox_address(client: OAuthClient, tokens: TokenSet) -> tuple[str, str]:
    """The (address, display name) the tokens belong to."""
    claims = _claims(tokens.id_token) if tokens.id_token else {}
    if not claims.get("email") and client.userinfo_url:
        request = urllib.request.Request(client.userinfo_url, headers={"Authorization": f"Bearer {tokens.access_token}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=client.timeout) as response:
                claims = {**claims, **json.loads(response.read())}
        except (urllib.error.URLError, ValueError, OSError) as error:
            raise MailError(f"Could not read the mailbox address: {error}", MailErrorCode.PROVIDER_ERROR) from error
    address = claims.get("email") or claims.get("preferred_username") or claims.get("upn")
    if not address or "@" not in address:
        raise MailError("The provider did not say which mailbox was approved (no email claim).", MailErrorCode.PROVIDER_ERROR)
    return address.lower(), claims.get("name") or ""
