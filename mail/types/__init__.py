"""GraphQL types. Every model type is ``OrgScoped``: its reads see only the request's organization
and, within it, only the mailboxes the caller may see."""

import datetime
from typing import List, Optional

import kante
import strawberry
import strawberry_django
from django.conf import settings
from django.utils import timezone
from kante.types import Info

from datalayer import types as datalayer_types
from mail import enums, filters, models, sanitize
from mail.types._shared import OrgScoped
from mail.types.auth import Organization, User

__all__ = [
    "OrgScoped",
    "Organization",
    "User",
    "Address",
    "ServerSettings",
    "MailAccount",
    "MailFolder",
    "Thread",
    "Message",
    "Attachment",
    "OutgoingMessage",
    "RefusedRecipient",
    "AuthSession",
    "MailPreset",
    "SyncResult",
    "MailboxSyncEvent",
    "DeleteResult",
]


@strawberry.type(description="A mail address with its display name.")
class Address:
    name: str = strawberry.field(description="The display name (may be empty).")
    address: str = strawberry.field(description="The address.")


def _addresses(values: list[dict] | None) -> list[Address]:
    return [Address(name=v.get("name") or "", address=v.get("address") or "") for v in values or []]


@strawberry.type(description="Where a server is reached.")
class ServerSettings:
    host: str
    port: int
    security: enums.Security


@kante.django_type(models.MailAccount, pagination=True, filters=filters.MailAccountFilter, description="A linked mailbox. Private to the member who linked it unless shared (`visibility`).")
class MailAccount(OrgScoped):
    id: strawberry.ID
    name: str
    email_address: str
    display_name: str
    provider: enums.Provider
    status: enums.MailAccountStatus
    visibility: enums.Visibility
    protocol: enums.Protocol
    incoming_host: str
    incoming_port: int
    incoming_security: enums.Security
    smtp_host: Optional[str]
    smtp_port: Optional[int]
    smtp_security: enums.Security
    username: str
    auth_method: enums.AuthMethod
    save_sent_copy: bool
    pop_leave_on_server: bool
    capabilities: List[str]
    last_synced_at: Optional[datetime.datetime]
    last_error: Optional[str]
    last_error_code: Optional[enums.MailErrorCode]
    backfill_done: bool
    created_at: datetime.datetime
    organization: Organization
    creator: Optional[User]
    shared_with: List[User] = strawberry_django.field(description="Members who see the mailbox when it is SHARED.")
    folders: List["MailFolder"] = strawberry_django.field(description="The mailbox's folders (a POP3 mailbox has one, its INBOX).")

    @strawberry_django.field(description="Whether the caller linked the mailbox (and so may change its credentials, sharing, or delete it).")
    def is_owner(self, info: Info) -> bool:
        return self.creator_id == info.context.request.user.id  # type: ignore[attr-defined]

    @strawberry_django.field(description="Whether the mailbox can send (it has an SMTP server).")
    def can_send(self) -> bool:
        return bool(self.smtp_host and self.smtp_port)

    @strawberry_django.field(description="Whether folders, moves and flags live on the server (IMAP). On POP3 flags are local and moves are refused.")
    def server_side_folders(self) -> bool:
        return self.protocol == models.Protocol.IMAP

    @strawberry_django.field(description="Whether a sync holds the mailbox right now.")
    def syncing(self) -> bool:
        return bool(self.sync_lease_until and self.sync_lease_until > timezone.now())  # type: ignore[attr-defined]

    @strawberry_django.field(description="Unread messages over the synced folders, as the server counts them.")
    def unread_count(self) -> int:
        return sum(f.unread_count for f in self.folders.all() if f.sync_enabled)  # type: ignore[attr-defined]


@kante.django_type(models.MailFolder, pagination=True, filters=filters.MailFolderFilter, description="A folder of a mailbox.")
class MailFolder(OrgScoped):
    id: strawberry.ID
    account: MailAccount
    path: str
    name: str
    delimiter: Optional[str]
    role: enums.FolderRole
    selectable: bool
    sync_enabled: bool
    exists_on_server: bool
    total_count: int
    unread_count: int
    backfill_done: bool
    last_synced_at: Optional[datetime.datetime]
    messages: List["Message"] = strawberry_django.field(pagination=True, filters=filters.MessageFilter, ordering=filters.MessageOrder, description="The folder's messages.")


@kante.django_type(models.Attachment, description="A file attached to a message.")
class Attachment(OrgScoped):
    id: strawberry.ID
    position: int
    filename: str
    content_type: str
    size: int
    content_id: Optional[str]
    inline: bool

    @strawberry_django.field(description="The bytes in the datalayer (request a read grant from it); null without a datalayer.")
    def store(self) -> Optional[datalayer_types.BigFileStore]:
        return self.store  # type: ignore[attr-defined,return-value]


@kante.django_type(models.Message, pagination=True, filters=filters.MessageFilter, ordering=filters.MessageOrder, description="A message in a folder. A copy in another folder is another message with the same `messageId`.")
class Message(OrgScoped):
    id: strawberry.ID
    account: MailAccount
    folder: MailFolder
    thread: Optional["Thread"]
    uid: Optional[str] = strawberry_django.field(description="IMAP: the UID in the folder (a string: UIDs are 32-bit unsigned).")
    message_id: Optional[str]
    in_reply_to: Optional[str]
    references: List[str]
    subject: str
    sender_name: str
    sender_address: str
    date: Optional[datetime.datetime]
    received_at: Optional[datetime.datetime]
    snippet: str
    text_body: str
    has_remote_images: bool
    size: int
    flags: List[str]
    has_attachments: bool
    truncated: bool
    created_at: datetime.datetime
    attachments: List[Attachment] = strawberry_django.field(description="Attached files, inline images included (`inline`).")

    @strawberry_django.field(description="The sender.")
    def sender(self) -> Address:
        return Address(name=self.sender_name, address=self.sender_address)  # type: ignore[attr-defined]

    @strawberry_django.field(description="Reply-To addresses.")
    def reply_to(self) -> List[Address]:
        return _addresses(self.reply_to)  # type: ignore[attr-defined]

    @strawberry_django.field(description="To addresses.")
    def to(self) -> List[Address]:
        return _addresses(self.to)  # type: ignore[attr-defined]

    @strawberry_django.field(description="Cc addresses.")
    def cc(self) -> List[Address]:
        return _addresses(self.cc)  # type: ignore[attr-defined]

    @strawberry_django.field(description="Bcc addresses (only known on sent mail).")
    def bcc(self) -> List[Address]:
        return _addresses(self.bcc)  # type: ignore[attr-defined]

    @strawberry_django.field(description="Whether the message is read (\\Seen).")
    def is_read(self) -> bool:
        return "\\Seen" in self.flags  # type: ignore[attr-defined]

    @strawberry_django.field(description="Whether the message is flagged (\\Flagged).")
    def is_flagged(self) -> bool:
        return "\\Flagged" in self.flags  # type: ignore[attr-defined]

    @strawberry_django.field(description="Whether the message was answered (\\Answered).")
    def is_answered(self) -> bool:
        return "\\Answered" in self.flags  # type: ignore[attr-defined]

    @strawberry_django.field(description="The HTML body, sanitized: no scripts, styles, event handlers or forms. Remote images are removed unless `allowRemote` (loading one tells the sender the mail was read); inline images keep their `cid:` references (see `attachments.contentId`). Null for a plain-text message.")
    def html(self, allow_remote: bool = False) -> Optional[str]:
        body = self.html_body  # type: ignore[attr-defined]
        if not body:
            return None
        if allow_remote or not settings.KUVERT_MAIL.get("block_remote_images", True):
            return body
        return sanitize.block_remote(body)

    @strawberry_django.field(description="The raw RFC 5322 message in the datalayer; null without a datalayer.")
    def raw(self) -> Optional[datalayer_types.BigFileStore]:
        return self.raw  # type: ignore[attr-defined,return-value]


@kante.django_type(models.Thread, pagination=True, filters=filters.ThreadFilter, ordering=filters.ThreadOrder, description="A conversation: messages linked by In-Reply-To/References, across the mailbox's folders.")
class Thread(OrgScoped):
    id: strawberry.ID
    account: MailAccount
    subject: str
    last_message_at: Optional[datetime.datetime]
    message_count: int

    @strawberry_django.field(description="The conversation's messages, oldest first.")
    def messages(self, info: Info) -> List[Message]:
        from mail.scoping import scope_queryset

        return list(scope_queryset(models.Message.objects.filter(thread_id=self.id), info).order_by("date", "id"))  # type: ignore[attr-defined,return-value]

    @strawberry_django.field(description="Whether a message of the conversation is unread.")
    def unread(self) -> bool:
        return models.Message.objects.filter(thread_id=self.id).exclude(flags__contains=["\\Seen"]).exists()  # type: ignore[attr-defined]


@strawberry.type(description="A recipient the SMTP server refused.")
class RefusedRecipient:
    address: str
    code: int
    message: str


@kante.django_type(models.OutgoingMessage, pagination=True, filters=filters.OutgoingMessageFilter, description="A message sent through a mailbox's SMTP server.")
class OutgoingMessage(OrgScoped):
    id: strawberry.ID
    account: MailAccount
    creator: Optional[User]
    status: enums.OutgoingStatus
    subject: str
    text_body: str
    html_body: str
    in_reply_to: Optional[Message]
    message_id: str
    saved_to_sent: bool
    error: Optional[str]
    error_code: Optional[enums.MailErrorCode]
    created_at: datetime.datetime
    sent_at: Optional[datetime.datetime]
    attachments: List[datalayer_types.BigFileStore] = strawberry_django.field(description="The files attached.")

    @strawberry_django.field(description="To addresses.")
    def to(self) -> List[Address]:
        return _addresses(self.to)  # type: ignore[attr-defined]

    @strawberry_django.field(description="Cc addresses.")
    def cc(self) -> List[Address]:
        return _addresses(self.cc)  # type: ignore[attr-defined]

    @strawberry_django.field(description="Bcc addresses.")
    def bcc(self) -> List[Address]:
        return _addresses(self.bcc)  # type: ignore[attr-defined]

    @strawberry_django.field(description="Recipients the SMTP server refused while accepting the rest.")
    def refused(self) -> List[RefusedRecipient]:
        return [RefusedRecipient(address=address, code=int(value[0]), message=str(value[1])) for address, value in (self.refused or {}).items()]  # type: ignore[attr-defined]


@strawberry.type(description="A started OAuth login: open `openUrl`; the provider redirects to `redirectUrl` with `?code&state`; call `completeOAuthLink` with them.")
class AuthSession:
    state: str
    open_url: str
    expires_at: datetime.datetime
    finish: str = strawberry.field(description="How the login finishes: REDIRECT (catch the redirect, then `completeOAuthLink`).")
    redirect_url: str
    provider: enums.Provider
    account: Optional[MailAccount] = strawberry.field(description="The mailbox this login re-links, if it does.")

    @classmethod
    def of(cls, link: models.OAuthLink) -> "AuthSession":
        return cls(state=link.state, open_url=link.auth_url, expires_at=link.expires_at, finish="REDIRECT", redirect_url=link.redirect_url, provider=enums.Provider(link.provider), account=link.account)  # type: ignore[arg-type]


@strawberry.type(description="Server settings of a well-known mail provider, to fill in a new mailbox.")
class MailPreset:
    key: str
    name: str
    domains: List[str]
    provider: enums.Provider
    imap: Optional[ServerSettings]
    pop3: Optional[ServerSettings]
    smtp: Optional[ServerSettings]
    save_sent_copy: bool
    oauth: bool = strawberry.field(description="The provider is linked through OAuth (`startOAuthLink`).")
    oauth_configured: bool = strawberry.field(description="This deployment has an OAuth client for it.")
    note: str


@strawberry.type(description="What one mailbox sync did.")
class SyncResult:
    account: MailAccount
    created: int = strawberry.field(description="New messages.")
    updated: int = strawberry.field(description="Messages whose flags changed.")
    deleted: int = strawberry.field(description="Messages gone from the server.")
    folders: int = strawberry.field(description="Folders synced.")
    more: bool = strawberry.field(description="More mail is waiting (the backfill or a burst of new mail continues on the next sync).")


@strawberry.type(description="A mailbox finished syncing.")
class MailboxSyncEvent:
    account_id: strawberry.ID
    created: int
    updated: int
    deleted: int
    more: bool


@strawberry.type(description="What a delete did.")
class DeleteResult:
    deleted: int = strawberry.field(description="Messages deleted or moved to Trash.")
