"""Linking mailboxes with a password, changing, testing, sharing, syncing and removing them."""

from typing import Optional

import strawberry
from authentikate.models import Membership
from channels.db import database_sync_to_async
from django.db import transaction
from kante.errors import ValidationError
from kante.types import Info

from mail import crypto, enums, models, presets, storage, types
from mail.accounts import incoming_credentials, incoming_endpoint, smtp_credentials, smtp_endpoint
from mail.graphql.errors import translated
from mail.graphql.utils import aget_or_404, get_many, get_or_404, require_owner
from mail.protocols import net
from mail.protocols.clients import open_imap, open_pop3, open_smtp, pop3_capabilities
from mail.sync import in_worker, sync_account

__all__ = [
    "ServerInput",
    "CreateMailAccountInput",
    "UpdateMailAccountInput",
    "ShareMailAccountInput",
    "UpdateMailFolderInput",
    "create_mail_account",
    "update_mail_account",
    "test_mail_account",
    "delete_mail_account",
    "share_mail_account",
    "update_mail_folder",
    "sync_mail_account",
]


@strawberry.input(description="Where a server is reached.")
class ServerInput:
    host: str
    port: int
    security: enums.Security = enums.Security.TLS


@strawberry.input(description="A mailbox to link with a username and (app) password. Servers default to the preset of the address's provider when there is one (`mailPresets`).")
class CreateMailAccountInput:
    email_address: str
    password: str = strawberry.field(description="The (app) password. Stored encrypted, never returned.")
    name: Optional[str] = strawberry.field(default=None, description="A display name for the mailbox; the address by default.")
    display_name: Optional[str] = strawberry.field(default=None, description="The sender name of sent mail.")
    protocol: enums.Protocol = enums.Protocol.IMAP
    incoming: Optional[ServerInput] = strawberry.field(default=None, description="The IMAP or POP3 server; the preset's by default.")
    smtp: Optional[ServerInput] = strawberry.field(default=None, description="The SMTP server; the preset's by default. Without one the mailbox cannot send.")
    username: Optional[str] = strawberry.field(default=None, description="The login; the address by default.")
    smtp_username: Optional[str] = strawberry.field(default=None, description="A separate SMTP login, if the server wants one.")
    smtp_password: Optional[str] = strawberry.field(default=None, description="The separate SMTP password.")
    save_sent_copy: Optional[bool] = strawberry.field(default=None, description="Append sent mail to the Sent folder; the preset's choice by default (off for Gmail/Microsoft).")
    pop_leave_on_server: bool = strawberry.field(default=True, description="POP3: keep downloaded mail on the server.")
    visibility: enums.Visibility = enums.Visibility.PRIVATE


@strawberry.input(description="Changes to a mailbox; omitted fields stay as they are. Changing servers or credentials tests the login first.")
class UpdateMailAccountInput:
    id: strawberry.ID
    name: Optional[str] = strawberry.UNSET
    display_name: Optional[str] = strawberry.UNSET
    password: Optional[str] = strawberry.UNSET
    username: Optional[str] = strawberry.UNSET
    incoming: Optional[ServerInput] = strawberry.UNSET
    smtp: Optional[ServerInput] = strawberry.UNSET
    smtp_username: Optional[str] = strawberry.UNSET
    smtp_password: Optional[str] = strawberry.UNSET
    save_sent_copy: Optional[bool] = strawberry.UNSET
    pop_leave_on_server: Optional[bool] = strawberry.UNSET
    enabled: Optional[bool] = strawberry.field(default=strawberry.UNSET, description="False pauses the mailbox (DISABLED); true makes it ACTIVE again (after a successful login).")


@strawberry.input(description="Who sees a mailbox.")
class ShareMailAccountInput:
    id: strawberry.ID
    visibility: enums.Visibility
    users: Optional[list[strawberry.ID]] = strawberry.field(default=None, description="The members a SHARED mailbox is shared with (replaces the list). Must be members of the organization.")


@strawberry.input(description="Changes to a folder.")
class UpdateMailFolderInput:
    id: strawberry.ID
    sync_enabled: bool = strawberry.field(description="Whether syncs read the folder. Turning it off keeps what is stored.")


def _check_server(value: ServerInput) -> None:
    if not value.host.strip() or not 0 < value.port < 65536:
        raise ValidationError("A server needs a host and a port between 1 and 65535.")
    net.require_security(value.security.value)


def _test_login(account: models.MailAccount) -> list[str]:
    """Log in to the incoming (and SMTP) server of an unsaved or saved mailbox; returns the capabilities (blocking)."""
    net.check_host(account.incoming_host, account.incoming_port)
    if account.smtp_host:
        net.check_host(account.smtp_host, account.smtp_port or 0)
    endpoint, credentials = incoming_endpoint(account), incoming_credentials(account)
    if account.protocol == models.Protocol.POP3:
        pop = open_pop3(endpoint, credentials)
        try:
            capabilities = pop3_capabilities(pop)
        finally:
            pop.quit()
    else:
        imap = open_imap(endpoint, credentials)
        try:
            capabilities = sorted(c.decode() if isinstance(c, bytes) else c for c in imap.capabilities())
        finally:
            imap.logout()
    if account.smtp_host and account.smtp_port:
        smtp = open_smtp(smtp_endpoint(account), smtp_credentials(account))
        smtp.quit()
    return capabilities


@translated
async def create_mail_account(info: Info, input: CreateMailAccountInput) -> types.MailAccount:
    """Link a mailbox with a password: the login is tested before anything is stored."""
    request = info.context.request
    address = input.email_address.strip()
    if "@" not in address:
        raise ValidationError("emailAddress is not an email address.")
    preset = presets.for_address(address)
    incoming = input.incoming
    if incoming is None and preset is not None:
        server = preset.pop3 if input.protocol == enums.Protocol.POP3 else preset.imap
        incoming = ServerInput(host=server[0], port=server[1], security=enums.Security(server[2])) if server else None
    if incoming is None:
        raise ValidationError("No known server for this address: give `incoming`.")
    smtp = input.smtp
    if smtp is None and preset is not None and preset.smtp:
        smtp = ServerInput(host=preset.smtp[0], port=preset.smtp[1], security=enums.Security(preset.smtp[2]))
    _check_server(incoming)
    if smtp is not None:
        _check_server(smtp)

    account = models.MailAccount(
        organization=request.organization,
        creator=request.user,
        name=(input.name or address)[:200],
        email_address=address,
        display_name=(input.display_name or "")[:200],
        provider=preset.provider if preset else models.Provider.GENERIC,
        protocol=input.protocol.value,
        incoming_host=incoming.host.strip(),
        incoming_port=incoming.port,
        incoming_security=incoming.security.value,
        smtp_host=smtp.host.strip() if smtp else None,
        smtp_port=smtp.port if smtp else None,
        smtp_security=smtp.security.value if smtp else models.Security.TLS,
        username=(input.username or address).strip(),
        auth_method=models.AuthMethod.PASSWORD,
        secret=crypto.encrypt(input.password),
        smtp_username=input.smtp_username or None,
        smtp_secret=crypto.encrypt_optional(input.smtp_password),
        save_sent_copy=input.save_sent_copy if input.save_sent_copy is not None else (preset.save_sent_copy if preset else True),
        pop_leave_on_server=input.pop_leave_on_server,
        visibility=input.visibility.value,
    )
    if await models.MailAccount.objects.filter(organization=request.organization, creator=request.user, email_address=address, protocol=account.protocol).aexists():
        raise ValidationError("You already linked this mailbox.")
    account.capabilities = await in_worker(_test_login, account)
    await account.asave()
    return account  # type: ignore[return-value]


@translated
async def update_mail_account(info: Info, input: UpdateMailAccountInput) -> types.MailAccount:
    """Change a mailbox (owner only). New servers or credentials are tested before they are stored."""
    account = await aget_or_404(models.MailAccount, info, input.id)
    require_owner(account, info)
    retest = False
    if input.name is not strawberry.UNSET and input.name:
        account.name = input.name[:200]
    if input.display_name is not strawberry.UNSET:
        account.display_name = (input.display_name or "")[:200]
    if input.password is not strawberry.UNSET and input.password:
        if account.auth_method != models.AuthMethod.PASSWORD:
            raise ValidationError("This mailbox logs in with OAuth; link it again instead.")
        account.secret, retest = crypto.encrypt(input.password), True
    if input.username is not strawberry.UNSET and input.username:
        account.username, retest = input.username.strip(), True
    if input.incoming is not strawberry.UNSET and input.incoming is not None:
        _check_server(input.incoming)
        account.incoming_host, account.incoming_port, account.incoming_security = input.incoming.host.strip(), input.incoming.port, input.incoming.security.value
        retest = True
    if input.smtp is not strawberry.UNSET:
        if input.smtp is None:
            account.smtp_host = account.smtp_port = None
        else:
            _check_server(input.smtp)
            account.smtp_host, account.smtp_port, account.smtp_security = input.smtp.host.strip(), input.smtp.port, input.smtp.security.value
            retest = True
    if input.smtp_username is not strawberry.UNSET:
        account.smtp_username, retest = input.smtp_username or None, True
    if input.smtp_password is not strawberry.UNSET:
        account.smtp_secret, retest = crypto.encrypt_optional(input.smtp_password), True
    if input.save_sent_copy is not strawberry.UNSET and input.save_sent_copy is not None:
        account.save_sent_copy = input.save_sent_copy
    if input.pop_leave_on_server is not strawberry.UNSET and input.pop_leave_on_server is not None:
        account.pop_leave_on_server = input.pop_leave_on_server
    if input.enabled is not strawberry.UNSET and input.enabled is not None:
        if not input.enabled:
            account.status = models.MailAccountStatus.DISABLED
        elif account.status != models.MailAccountStatus.ACTIVE:
            retest = True
    if retest:
        account.capabilities = await in_worker(_test_login, account)
        account.status, account.last_error, account.last_error_code = models.MailAccountStatus.ACTIVE, None, None
    await account.asave()
    return account  # type: ignore[return-value]


@translated
async def test_mail_account(info: Info, id: strawberry.ID) -> types.MailAccount:
    """Log in to the mailbox's servers now. A failure is recorded (`lastError`) and raised; success clears it."""
    account = await aget_or_404(models.MailAccount, info, id)
    try:
        capabilities = await in_worker(_test_login, account)
    except Exception as error:
        from mail.sync import record_failure

        await database_sync_to_async(record_failure)(account.id, error)
        raise
    fields = {"capabilities": capabilities, "last_error": None, "last_error_code": None}
    if account.status == models.MailAccountStatus.NEEDS_REAUTH:
        fields["status"] = models.MailAccountStatus.ACTIVE
    await models.MailAccount.objects.filter(pk=account.pk).aupdate(**fields)
    return await models.MailAccount.objects.aget(pk=account.pk)  # type: ignore[return-value]


def _delete_account(account: models.MailAccount) -> None:
    store_ids = list(account.messages.exclude(raw=None).values_list("raw_id", flat=True))
    store_ids += list(models.Attachment.objects.filter(message__account=account).exclude(store=None).values_list("store_id", flat=True))
    with transaction.atomic():
        account.delete()
        storage.orphan(store_ids)


@translated
async def delete_mail_account(info: Info, id: strawberry.ID) -> strawberry.ID:
    """Unlink a mailbox (owner only): its stored mail is deleted here, never on the server."""
    account = await aget_or_404(models.MailAccount, info, id)
    require_owner(account, info)
    await database_sync_to_async(_delete_account)(account)
    return id


def _share(info: Info, input: ShareMailAccountInput) -> models.MailAccount:
    account = get_or_404(models.MailAccount, info, input.id)
    require_owner(account, info)
    request = info.context.request
    with transaction.atomic():
        account.visibility = input.visibility.value
        account.save(update_fields=["visibility"])
        if input.users is not None:
            members = list(Membership.objects.filter(organization=request.organization, user_id__in=input.users).values_list("user_id", flat=True))
            if len(set(members)) != len({str(u) for u in input.users}):
                raise ValidationError("Mailboxes can only be shared with members of the organization.")
            account.shared_with.set(members)
    return account


@translated
async def share_mail_account(info: Info, input: ShareMailAccountInput) -> types.MailAccount:
    """Set who sees a mailbox (owner only): PRIVATE, SHARED with `users`, or the whole ORGANIZATION."""
    return await database_sync_to_async(_share)(info, input)  # type: ignore[return-value]


@translated
def update_mail_folder(info: Info, input: UpdateMailFolderInput) -> types.MailFolder:
    """Turn syncing a folder on or off."""
    folder = get_or_404(models.MailFolder, info, input.id)
    folder.sync_enabled = input.sync_enabled
    folder.save(update_fields=["sync_enabled"])
    return folder  # type: ignore[return-value]


@translated
async def sync_mail_account(info: Info, id: strawberry.ID, folders: Optional[list[strawberry.ID]] = None) -> types.SyncResult:
    """Sync a mailbox now (or only some of its folders): new mail, flags, deletions, and the next part of the backfill."""
    account = await aget_or_404(models.MailAccount, info, id)
    folder_ids = None
    if folders:
        folder_ids = [f.id for f in await database_sync_to_async(get_many)(models.MailFolder, info, folders)]
    result = await sync_account(account.id, folder_ids)
    account = await models.MailAccount.objects.aget(pk=account.pk)
    return types.SyncResult(account=account, created=result.created, updated=result.updated, deleted=result.deleted, folders=result.folders, more=result.more)  # type: ignore[arg-type]

