"""The kinds of provider an organization's admin sets up, what their settings hold, and the check before one is used.

A *provider* is a row (:class:`~mail.models.MailProvider`): its kind, settings and credentials,
and the capabilities switched on for it. Every mailbox is linked through one:

* **IMAP** (``GENERIC``) -- a password mailbox. The admin either fixes the servers (a company
  mail server: members only give their login) or leaves them open (members name any server,
  filled in from ``mailPresets`` where one matches).
* **Gmail** / **Microsoft** -- an OAuth client (``startOAuthLink``). Its id and redirect URLs are
  settings, its secret is stored encrypted. The provider's endpoints are the deployment's
  (``oauth.google`` / ``oauth.microsoft``), never a row's: the service posts to them.

What a kind *can* do is its ``capabilities``; what a provider *does* is the subset on its row.
Reading mail is what every provider is for. A disabled provider, or a mailbox with none, gets
NOT_CONFIGURED wherever its credentials would be used (:func:`usable`).
"""

import re
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from mail import models
from mail.errors import MailError


class Server(BaseModel):
    """Where a server is reached."""

    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    security: models.Security = models.Security.TLS


class ServerProviderSettings(BaseModel):
    """An IMAP provider: its servers, or none (each member names their own)."""

    protocol: Optional[models.Protocol] = Field(default=None, description="How incoming mail is read, with fixed servers.")
    incoming: Optional[Server] = Field(default=None, description="The fixed IMAP or POP3 server; None leaves the servers to the members.")
    smtp: Optional[Server] = Field(default=None, description="The fixed SMTP server; None with fixed servers: its mailboxes cannot send.")
    save_sent_copy: bool = Field(default=True, description="Whether its mailboxes append sent mail to Sent (off when the server files it itself).")

    @model_validator(mode="after")
    def _fixed_together(self) -> "ServerProviderSettings":
        if (self.protocol is None) != (self.incoming is None):
            raise ValueError("fixed servers need both `protocol` and `incoming`")
        if self.smtp is not None and self.incoming is None:
            raise ValueError("an SMTP server is only fixed together with the incoming one")
        return self

    @property
    def fixed(self) -> bool:
        """Whether the admin fixed the servers (members only give their login)."""
        return self.incoming is not None


TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,99}$")


class OAuthProviderSettings(BaseModel):
    """A Gmail or Microsoft provider: one OAuth client. Its secret is stored apart, encrypted."""

    client_id: str = Field(min_length=1, description="The OAuth client id.")
    redirect_urls: list[str] = Field(min_length=1, description="The redirect URLs registered for the client; the first is the default.")
    tenant: str = Field(default="common", description="Microsoft: the Entra tenant (id or domain) logins go through; `common` takes any account.")

    @field_validator("tenant")
    @classmethod
    def _tenant(cls, value: str) -> str:
        if not TENANT.match(value):
            raise ValueError("a tenant is an id or a domain")
        return value

    @field_validator("redirect_urls")
    @classmethod
    def _redirects(cls, value: list[str]) -> list[str]:
        for url in value:
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
                raise ValueError(f"{url!r} is not a URL")
        return value


@dataclass(frozen=True)
class CapabilityInfo:
    """A capability, as shown to whoever switches it on."""

    capability: models.ProviderCapability
    label: str
    description: str


CAPABILITIES: dict[models.ProviderCapability, CapabilityInfo] = {
    info.capability: info
    for info in (
        CapabilityInfo(models.ProviderCapability.SEND, "Sending", "Send mail through its mailboxes' SMTP servers; off, they only receive."),
        CapabilityInfo(models.ProviderCapability.SCHEDULED_SYNC, "Scheduled sync", "Let the unattended `sync_all_mailboxes` action sync its mailboxes; off, they only sync when a member asks."),
    )
}


@dataclass(frozen=True)
class Kind:
    """A kind of provider: how its mailboxes log in, and what it can do."""

    kind: models.Provider
    label: str
    description: str
    auth_method: models.AuthMethod
    capabilities: frozenset[models.ProviderCapability]
    settings: type[BaseModel]


_ALL = frozenset(CAPABILITIES)

KINDS: dict[models.Provider, Kind] = {
    kind.kind: kind
    for kind in (
        Kind(models.Provider.GENERIC, "IMAP / POP3", "A mail server members log in to with a username and (app) password: fixed by the admin, or named by each member.", models.AuthMethod.PASSWORD, _ALL, ServerProviderSettings),
        Kind(models.Provider.GMAIL, "Gmail", "Gmail and Google Workspace through a Google Cloud OAuth client.", models.AuthMethod.XOAUTH2, _ALL, OAuthProviderSettings),
        Kind(models.Provider.MICROSOFT, "Microsoft", "Outlook.com and Microsoft 365 through an Entra app registration.", models.AuthMethod.XOAUTH2, _ALL, OAuthProviderSettings),
    )
}


def kind_of(kind: str) -> Kind:
    """The kind of ``kind`` (a :class:`~mail.models.Provider` value)."""
    return KINDS[models.Provider(kind)]


def server_settings(provider: models.MailProvider) -> ServerProviderSettings:
    """An IMAP provider's settings."""
    assert provider.kind == models.Provider.GENERIC, provider.kind
    return ServerProviderSettings.model_validate(provider.settings)


def oauth_settings(provider: models.MailProvider) -> OAuthProviderSettings:
    """A Gmail or Microsoft provider's settings."""
    assert kind_of(provider.kind).auth_method == models.AuthMethod.XOAUTH2, provider.kind
    return OAuthProviderSettings.model_validate(provider.settings)


def has(provider: models.MailProvider, capability: models.ProviderCapability) -> bool:
    """Whether ``capability`` is switched on for ``provider``."""
    return capability.value in (provider.capabilities or [])


def usable(provider: models.MailProvider | None, what: str = "This mailbox") -> models.MailProvider:
    """``provider``, if it may be used: NOT_CONFIGURED when there is none or it is disabled."""
    if provider is None:
        raise MailError(f"{what} has no provider: an admin of the organization has to set one up (again).", models.MailErrorCode.NOT_CONFIGURED)
    if not provider.enabled:
        raise MailError(f"The provider {provider.name!r} is disabled.", models.MailErrorCode.NOT_CONFIGURED)
    return provider


def require(provider: models.MailProvider | None, capability: models.ProviderCapability, what: str = "This mailbox") -> models.MailProvider:
    """:func:`usable`, and NOT_CONFIGURED unless ``capability`` is switched on."""
    row = usable(provider, what)
    if not has(row, capability):
        raise MailError(f"{CAPABILITIES[capability].label} is switched off for the provider {row.name!r}.", models.MailErrorCode.NOT_CONFIGURED)
    return row
