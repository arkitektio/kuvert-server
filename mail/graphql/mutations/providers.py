"""Setting up the organization's providers. Admins only (the schema gates every mutation here).

A provider's credentials only ever travel inwards: a client secret is taken as text, stored
encrypted, and never returned. A server an admin fixes passes the same checks a member's does
(public host, TLS unless the deployment allows otherwise).
"""

from typing import Optional

import strawberry
from django.db import IntegrityError, transaction
from kante.errors import ValidationError
from kante.types import Info
from pydantic import BaseModel
from pydantic import ValidationError as SettingsError

from mail import crypto, enums, models, providers, types
from mail.graphql.errors import translated
from mail.graphql.mutations.accounts import ServerInput, check_server
from mail.graphql.utils import get_or_404

__all__ = [
    "CreateServerProviderInput",
    "UpdateServerProviderInput",
    "CreateOAuthProviderInput",
    "UpdateOAuthProviderInput",
    "UpdateProviderInput",
    "create_server_provider",
    "update_server_provider",
    "create_oauth_provider",
    "update_oauth_provider",
    "update_provider",
    "delete_provider",
]


@strawberry.input(description="A new IMAP provider. With `incoming` (and `protocol`) its servers are fixed and members only give their login; without, each member names the servers (`mailPresets` fills in well-known ones).")
class CreateServerProviderInput:
    name: str = strawberry.field(description="What to call it, unique in the organization.")
    protocol: Optional[enums.Protocol] = strawberry.field(default=None, description="With fixed servers: how incoming mail is read.")
    incoming: Optional[ServerInput] = strawberry.field(default=None, description="The fixed IMAP or POP3 server; omitted: members name their own.")
    smtp: Optional[ServerInput] = strawberry.field(default=None, description="The fixed SMTP server (only with `incoming`); omitted with fixed servers: its mailboxes cannot send.")
    save_sent_copy: bool = strawberry.field(default=True, description="Whether its mailboxes append sent mail to Sent (off when the server files it itself).")
    capabilities: Optional[list[enums.ProviderCapability]] = strawberry.field(default=None, description="What to switch on; everything the kind can do by default (see `providerKinds`).")


@strawberry.input(description="Changes to an IMAP provider; omitted fields stay as they are. Fixed servers may change and its mailboxes follow; while it has mailboxes, its protocol and whether its servers are fixed stay.")
class UpdateServerProviderInput:
    id: strawberry.ID
    name: Optional[str] = strawberry.UNSET
    enabled: Optional[bool] = strawberry.UNSET
    protocol: Optional[enums.Protocol] = strawberry.UNSET
    incoming: Optional[ServerInput] = strawberry.UNSET
    smtp: Optional[ServerInput] = strawberry.UNSET
    save_sent_copy: Optional[bool] = strawberry.UNSET
    capabilities: Optional[list[enums.ProviderCapability]] = strawberry.UNSET


@strawberry.input(description="A new Gmail or Microsoft provider: one OAuth client (a Google Cloud OAuth client, an Entra app registration).")
class CreateOAuthProviderInput:
    kind: enums.Provider = strawberry.field(description="GMAIL or MICROSOFT.")
    name: str = strawberry.field(description="What to call it, unique in the organization.")
    client_id: str = strawberry.field(description="The OAuth client id.")
    client_secret: Optional[str] = strawberry.field(default=None, description="The client secret (a public client with PKCE has none). Stored encrypted and never returned.")
    redirect_urls: list[str] = strawberry.field(description="The redirect URLs registered for the client, the default first.")
    tenant: Optional[str] = strawberry.field(default=None, description="Microsoft: the Entra tenant (id or domain); `common` (any account) by default.")
    capabilities: Optional[list[enums.ProviderCapability]] = strawberry.field(default=None, description="What to switch on; everything the kind can do by default (see `providerKinds`).")


@strawberry.input(description="Changes to a Gmail or Microsoft provider; omitted fields stay as they are. Its client id stays while mailboxes go through it (their refresh tokens belong to that client).")
class UpdateOAuthProviderInput:
    id: strawberry.ID
    name: Optional[str] = strawberry.UNSET
    enabled: Optional[bool] = strawberry.UNSET
    client_id: Optional[str] = strawberry.UNSET
    client_secret: Optional[str] = strawberry.field(default=strawberry.UNSET, description="A new client secret. Omitted or empty: the stored one is kept.")
    redirect_urls: Optional[list[str]] = strawberry.UNSET
    tenant: Optional[str] = strawberry.UNSET
    capabilities: Optional[list[enums.ProviderCapability]] = strawberry.UNSET


@strawberry.input(description="Changes any provider takes, whatever its kind; omitted fields stay as they are.")
class UpdateProviderInput:
    id: strawberry.ID
    name: Optional[str] = strawberry.UNSET
    enabled: Optional[bool] = strawberry.UNSET
    capabilities: Optional[list[enums.ProviderCapability]] = strawberry.UNSET


OAUTH_KINDS = (models.Provider.GMAIL, models.Provider.MICROSOFT)


def _capabilities(kind: str, chosen: list[enums.ProviderCapability] | None) -> list[str]:
    """The chosen capabilities as stored, or everything the kind can do; refuses one the kind lacks."""
    info = providers.kind_of(kind)
    if chosen is None:
        return sorted(capability.value for capability in info.capabilities)
    unsupported = sorted({c.value for c in chosen} - {c.value for c in info.capabilities})
    if unsupported:
        raise ValidationError(f"{info.label} cannot do {', '.join(unsupported)}; see `providerKinds` for what it can.")
    return sorted({c.value for c in chosen})


def _apply_common(provider: models.MailProvider, name: object, enabled: object, capabilities: object) -> None:
    """Write the fields every kind has onto ``provider`` (unsaved); ``UNSET`` leaves one alone."""
    if isinstance(name, str):
        provider.name = name.strip()
        if not provider.name:
            raise ValidationError("A provider needs a name.")
    if isinstance(enabled, bool):
        provider.enabled = enabled
    if isinstance(capabilities, list):
        provider.capabilities = _capabilities(provider.kind, capabilities)


def _validated(model: type[BaseModel], what: str, **values: object) -> dict:
    """``values`` as ``model`` stores them; a validation error naming what is wrong."""
    try:
        return model.model_validate(values).model_dump(mode="json")
    except SettingsError as error:
        problems = "; ".join(f"{'.'.join(str(part) for part in problem['loc']) or 'settings'}: {problem['msg']}" for problem in error.errors())
        raise ValidationError(f"Invalid {what} settings ({problems}).") from error


def _server(value: ServerInput | None) -> dict | None:
    if value is None:
        return None
    check_server(value)
    return {"host": value.host.strip(), "port": value.port, "security": value.security.value}


def _save(provider: models.MailProvider) -> models.MailProvider:
    try:
        with transaction.atomic():
            provider.save()
    except IntegrityError as error:
        raise ValidationError(f"The organization already has a provider named {provider.name!r}.") from error
    return provider


def _new(info: Info, kind: models.Provider, name: str, capabilities: list[enums.ProviderCapability] | None) -> models.MailProvider:
    request = info.context.request
    provider = models.MailProvider(organization=request.organization, creator=request.user, kind=kind)  # type: ignore[misc]  # kante's request types are authentikate's rows
    _apply_common(provider, name, True, strawberry.UNSET)
    provider.capabilities = _capabilities(kind, capabilities)
    return provider


def _mailboxes(provider: models.MailProvider) -> int:
    return models.MailAccount.objects.filter(mail_provider=provider).count()


@translated
def create_server_provider(info: Info, input: CreateServerProviderInput) -> types.MailProvider:
    """Set up an IMAP provider. Fixed servers are checked (a public host, TLS) before anything is stored."""
    provider = _new(info, models.Provider.GENERIC, input.name, input.capabilities)
    provider.settings = _validated(
        providers.ServerProviderSettings,
        "IMAP",
        protocol=input.protocol.value if input.protocol else None,
        incoming=_server(input.incoming),
        smtp=_server(input.smtp),
        save_sent_copy=input.save_sent_copy,
    )
    return _save(provider)  # type: ignore[return-value]


@translated
def update_server_provider(info: Info, input: UpdateServerProviderInput) -> types.MailProvider:
    """Change an IMAP provider. New fixed servers are checked first, and its mailboxes move to them (their logins are not tested again)."""
    provider = get_or_404(models.MailProvider, info, input.id)
    if provider.kind != models.Provider.GENERIC:
        raise ValidationError("Not an IMAP provider: change it with `updateOAuthProvider` or `updateProvider`.")
    _apply_common(provider, input.name, input.enabled, input.capabilities)
    current = providers.server_settings(provider)
    values = current.model_dump(mode="json")
    if input.protocol is not strawberry.UNSET:
        values["protocol"] = input.protocol.value if input.protocol else None
    if input.incoming is not strawberry.UNSET:
        values["incoming"] = _server(input.incoming)
    if input.smtp is not strawberry.UNSET:
        values["smtp"] = _server(input.smtp)
    if isinstance(input.save_sent_copy, bool):
        values["save_sent_copy"] = input.save_sent_copy
    provider.settings = _validated(providers.ServerProviderSettings, "IMAP", **values)
    changed = providers.server_settings(provider)
    with transaction.atomic():
        if _mailboxes(provider) and (changed.fixed != current.fixed or changed.protocol != current.protocol):
            raise ValidationError("Mailboxes go through this provider: its protocol and whether its servers are fixed cannot change. Set up another provider instead.")
        _save(provider)
        if changed.fixed and (changed.incoming, changed.smtp) != (current.incoming, current.smtp):
            assert changed.incoming is not None
            smtp = changed.smtp
            models.MailAccount.objects.filter(mail_provider=provider).update(
                incoming_host=changed.incoming.host,
                incoming_port=changed.incoming.port,
                incoming_security=changed.incoming.security.value,
                smtp_host=smtp.host if smtp else None,
                smtp_port=smtp.port if smtp else None,
                smtp_security=smtp.security.value if smtp else models.Security.TLS,
            )
    return provider  # type: ignore[return-value]


def _oauth_settings(kind: str, client_id: str, redirect_urls: list[str], tenant: str | None) -> dict:
    if kind == models.Provider.GMAIL and tenant not in (None, "", "common"):
        raise ValidationError("A tenant is only for Microsoft providers.")
    return _validated(providers.OAuthProviderSettings, "OAuth", client_id=client_id.strip(), redirect_urls=redirect_urls, tenant=tenant or "common")


def _adopt(provider: models.MailProvider) -> None:
    """The organization's OAuth mailboxes of this kind that belong to no provider attach to its first one.

    Their refresh tokens belong to the OAuth client the deployment was configured with before
    providers existed, which the first provider an admin sets up is expected to be.
    """
    others = models.MailProvider.objects.filter(organization_id=provider.organization_id, kind=provider.kind).exclude(id=provider.id)
    if not others.exists():
        models.MailAccount.objects.filter(organization_id=provider.organization_id, provider=provider.kind, auth_method=models.AuthMethod.XOAUTH2, mail_provider__isnull=True).update(mail_provider=provider)


@translated
def create_oauth_provider(info: Info, input: CreateOAuthProviderInput) -> types.MailProvider:
    """Set up a Gmail or Microsoft OAuth client. The organization's mailboxes of that kind linked before providers existed attach to its first one."""
    if input.kind.value not in OAUTH_KINDS:
        raise ValidationError("An OAuth provider is GMAIL or MICROSOFT; a mail server is set up with `createServerProvider`.")
    provider = _new(info, models.Provider(input.kind.value), input.name, input.capabilities)
    provider.settings = _oauth_settings(provider.kind, input.client_id, input.redirect_urls, input.tenant)
    provider.secret = crypto.encrypt_optional((input.client_secret or "").strip() or None)
    with transaction.atomic():
        _save(provider)
        _adopt(provider)
    return provider  # type: ignore[return-value]


@translated
def update_oauth_provider(info: Info, input: UpdateOAuthProviderInput) -> types.MailProvider:
    """Change a Gmail or Microsoft provider. Its client id cannot change while mailboxes go through it."""
    provider = get_or_404(models.MailProvider, info, input.id)
    if provider.kind not in OAUTH_KINDS:
        raise ValidationError("Not an OAuth provider: change it with `updateServerProvider` or `updateProvider`.")
    _apply_common(provider, input.name, input.enabled, input.capabilities)
    current = providers.oauth_settings(provider)
    client_id = input.client_id.strip() if isinstance(input.client_id, str) else current.client_id
    redirect_urls = input.redirect_urls if isinstance(input.redirect_urls, list) else current.redirect_urls
    tenant = input.tenant if input.tenant is not strawberry.UNSET else current.tenant
    provider.settings = _oauth_settings(provider.kind, client_id, redirect_urls, tenant)
    if isinstance(input.client_secret, str) and input.client_secret.strip():
        provider.secret = crypto.encrypt(input.client_secret.strip())
    with transaction.atomic():
        if client_id != current.client_id and _mailboxes(provider):
            raise ValidationError("Mailboxes go through this provider, and their refresh tokens belong to its client: set up another provider for the new client and re-link them through it.")
        _save(provider)
    return provider  # type: ignore[return-value]


@translated
def update_provider(info: Info, input: UpdateProviderInput) -> types.MailProvider:
    """Rename, enable or disable a provider of any kind, or change what is switched on for it."""
    provider = get_or_404(models.MailProvider, info, input.id)
    _apply_common(provider, input.name, input.enabled, input.capabilities)
    return _save(provider)  # type: ignore[return-value]


@translated
def delete_provider(info: Info, id: strawberry.ID) -> strawberry.ID:
    """Remove a provider and its stored secret; returns its id. Refused while mailboxes go through it (disable it instead)."""
    provider = get_or_404(models.MailProvider, info, id)
    with transaction.atomic():
        count = _mailboxes(provider)
        if count:
            raise ValidationError(f"{count} mailbox(es) still go through this provider: unlink them first, or disable the provider instead.")
        provider.delete()
    return id
