"""GraphQL types. Every model type is ``OrgScoped``: its reads see only the request's organization
and, within it, only the mailboxes the caller may see."""

import datetime
from typing import List, Optional

import kante
import strawberry
import strawberry_django
from django.conf import settings
from django.db.models import F
from django.utils import timezone
from kante.types import Info
from strawberry.scalars import JSON

from datalayer import types as datalayer_types
from mail import enums, filters, models, sanitize
from mail.types._shared import DESCRIPTORS_DESCRIPTION, OrgScoped, build_prescoped_queryset, resolve_descriptors
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
    "MailChange",
    "Category",
    "PushResult",
    "TaskList",
    "Task",
    "TaskThread",
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
    push_seen: bool = strawberry_django.field(description="Read/unread is pushed to the server (else kept here).")
    push_flagged: bool = strawberry_django.field(description="Flagging is pushed to the server (else kept here).")
    push_keywords: bool = strawberry_django.field(description="Keywords (KEYWORD categories) are pushed to the server (else kept here).")
    push_moves: bool = strawberry_django.field(description="Moves are pushed to the server (else refused).")
    push_deletes: bool = strawberry_django.field(description="Deletes are pushed to the server (else only hidden here).")
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
    categories: List["Category"] = strawberry_django.field(description="The mailbox's categories.")

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

    @strawberry_django.field(description="Unread messages over the synced folders, as they are here.")
    def unread_count(self) -> int:
        return sum(f.unread_count for f in self.folders.all() if f.sync_enabled)  # type: ignore[attr-defined]

    @strawberry_django.field(description="Changes made here that have not reached the server yet.")
    def pending_changes(self) -> int:
        return models.MailChange.objects.filter(account_id=self.id, state=models.MailChangeState.PENDING).count()  # type: ignore[attr-defined]

    @strawberry_django.field(description="Changes made here that did not reach the server (see `mailChanges`).")
    def failed_changes(self) -> int:
        return models.MailChange.objects.filter(account_id=self.id, state=models.MailChangeState.FAILED).count()  # type: ignore[attr-defined]


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
    unread_count: int = strawberry_django.field(description="Unread messages in the folder, as they are here (local changes included).")
    server_unread_count: int = strawberry_django.field(description="Unread messages in the folder, as the server counted them at the last sync.")
    keywords_allowed: bool = strawberry_django.field(description="The folder keeps any keyword on the server (else KEYWORD categories stay local here).")
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
    flags: List[str] = strawberry_django.field(description="The flags and keywords as they are here: the server's with local changes applied.")
    server_flags: List[str] = strawberry_django.field(description="The flags and keywords as the server last had them.")
    has_attachments: bool
    truncated: bool
    created_at: datetime.datetime
    attachments: List[Attachment] = strawberry_django.field(description="Attached files, inline images included (`inline`).")
    descriptors: JSON = kante.django_field(resolver=resolve_descriptors, description=DESCRIPTORS_DESCRIPTION)

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):  # noqa: ANN001, ANN206
        return build_prescoped_queryset(info, queryset).filter(deleted_at=None)

    @strawberry_django.field(description="The categories the message is in.")
    def categories(self) -> List["Category"]:
        return list(models.Category.objects.filter(id__in=self.category_ids))  # type: ignore[attr-defined,return-value]

    @strawberry_django.field(description="Changes made here that have not reached the server (pending or failed).")
    def changes(self) -> List["MailChange"]:
        return list(models.MailChange.objects.filter(message_id=self.id))  # type: ignore[attr-defined,return-value]

    @strawberry_django.field(description="How the message here relates to the server.")
    def sync_state(self) -> enums.SyncState:
        states = set(models.MailChange.objects.filter(message_id=self.id).values_list("state", flat=True))  # type: ignore[attr-defined]
        if models.MailChangeState.FAILED in states:
            return enums.SyncState.FAILED
        if states:
            return enums.SyncState.PENDING
        if self.deleted_at or models.LocalPin.objects.filter(account_id=self.account_id, message_key=self.message_key).exists():  # type: ignore[attr-defined]
            return enums.SyncState.LOCAL
        return enums.SyncState.SYNCED

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


def _thread_messages(thread: object, info: Info, folder: Optional[strawberry.ID] = None, folder_role: Optional[enums.FolderRole] = None):  # noqa: ANN202
    """The conversation's messages the caller may see, optionally within a folder or a folder role."""
    from mail.scoping import scope_queryset

    rows = scope_queryset(models.Message.objects.filter(thread_id=thread.id, deleted_at=None), info)  # type: ignore[attr-defined]
    if folder is not None:
        rows = rows.filter(folder_id=folder)
    if folder_role is not None:
        rows = rows.filter(folder__role=folder_role.value)
    return rows


@kante.django_type(models.Thread, pagination=True, filters=filters.ThreadFilter, ordering=filters.ThreadOrder, description="A conversation: messages linked by In-Reply-To/References, across the mailbox's folders.")
class Thread(OrgScoped):
    id: strawberry.ID
    account: MailAccount
    subject: str
    last_message_at: Optional[datetime.datetime]
    message_count: int
    descriptors: JSON = kante.django_field(resolver=resolve_descriptors, description=DESCRIPTORS_DESCRIPTION)

    @strawberry_django.field(description="The conversation's messages, oldest first.")
    def messages(self, info: Info) -> List[Message]:
        from mail.scoping import scope_queryset

        return list(scope_queryset(models.Message.objects.filter(thread_id=self.id, deleted_at=None), info).order_by("date", "id"))  # type: ignore[attr-defined,return-value]

    @strawberry_django.field(description="Whether a message of the conversation is unread.")
    def unread(self) -> bool:
        return models.Message.objects.filter(thread_id=self.id, deleted_at=None).exclude(flags__contains=["\\Seen"]).exists()  # type: ignore[attr-defined]

    @strawberry_django.field(description="The newest message, optionally only within a folder or a folder role: what a list row shows. Null when the conversation has no message there.")
    def latest_message(self, info: Info, folder: Optional[strawberry.ID] = None, folder_role: Optional[enums.FolderRole] = None) -> Optional[Message]:
        return _thread_messages(self, info, folder, folder_role).order_by(F("date").desc(nulls_last=True), F("received_at").desc(nulls_last=True), F("uid").desc(nulls_last=True), "-id").first()  # type: ignore[return-value]

    @strawberry_django.field(description="Distinct senders, oldest first (\"Anna, Ben & 2 more\").")
    def participants(self, info: Info) -> List[Address]:
        seen: dict[str, Address] = {}
        for name, address in _thread_messages(self, info).order_by(F("date").asc(nulls_last=True), F("received_at").asc(nulls_last=True), F("uid").asc(nulls_last=True), "id").values_list("sender_name", "sender_address"):
            key = address or name
            if key and key not in seen:
                seen[key] = Address(name=name, address=address)
        return list(seen.values())

    @strawberry_django.field(description="Unread messages, optionally only within a folder or a folder role.")
    def unread_count(self, info: Info, folder: Optional[strawberry.ID] = None, folder_role: Optional[enums.FolderRole] = None) -> int:
        return _thread_messages(self, info, folder, folder_role).exclude(flags__contains=["\\Seen"]).count()

    @strawberry_django.field(description="Whether any message of the conversation is flagged.")
    def flagged(self, info: Info) -> bool:
        return _thread_messages(self, info).filter(flags__contains=["\\Flagged"]).exists()

    @strawberry_django.field(description="Whether any message of the conversation has attachments.")
    def has_attachments(self, info: Info) -> bool:
        return _thread_messages(self, info).filter(has_attachments=True).exists()

    @strawberry_django.field(description="The caller's tasks this conversation is in.")
    def tasks(self, info: Info) -> List["Task"]:
        from mail.scoping import scope_queryset

        return list(scope_queryset(models.Task.objects.filter(links__thread_id=self.id), info))  # type: ignore[attr-defined,return-value]


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
    descriptors: JSON = kante.django_field(resolver=resolve_descriptors, description=DESCRIPTORS_DESCRIPTION)

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
    folders: List[strawberry.ID] = strawberry.field(description="The folders whose messages changed, so a client refetches only those lists.")


@strawberry.type(description="What a delete did.")
class DeleteResult:
    deleted: int = strawberry.field(description="Messages deleted or moved to Trash (here at once; on the server after the undo window).")


@kante.django_type(models.MailChange, pagination=True, filters=filters.MailChangeFilter, description="A change made here that has not reached the server yet (a pushed one is gone).")
class MailChange(OrgScoped):
    id: strawberry.ID
    account: MailAccount
    kind: enums.MailChangeKind
    state: enums.MailChangeState
    add: List[str] = strawberry_django.field(description="FLAGS: flags and keywords to add.")
    remove: List[str] = strawberry_django.field(description="FLAGS: flags and keywords to remove.")
    origin_folder: Optional[MailFolder] = strawberry_django.field(description="MOVE/EXPUNGE: the folder the server has the message in.")
    target_folder: Optional[MailFolder] = strawberry_django.field(description="MOVE: the folder it goes to.")
    push_after: datetime.datetime = strawberry_django.field(description="Not pushed before then (the undo window, or the wait after a failure).")
    attempts: int
    error: Optional[str]
    error_code: Optional[enums.MailErrorCode]
    created_by: Optional[User]
    created_at: datetime.datetime

    @strawberry_django.field(description="The message (also one deleted here, while its delete is on its way).")
    def message(self) -> Optional[Message]:
        return models.Message.objects.filter(id=self.message_id).first()  # type: ignore[attr-defined,return-value]

    @strawberry_django.field(description="Whether `undoMailChanges` can still take it back (in its undo window, or FAILED).")
    def undoable(self) -> bool:
        return self.state == models.MailChangeState.FAILED or self.push_after > timezone.now()  # type: ignore[attr-defined]


@kante.django_type(models.Category, pagination=True, filters=filters.CategoryFilter, description="A category of a mailbox, shared by everyone who sees the mailbox: LOCAL, or kept on the server as an IMAP keyword (KEYWORD).")
class Category(OrgScoped):
    id: strawberry.ID
    account: MailAccount
    name: str
    color: str
    sync: enums.CategorySync
    keyword: str = strawberry_django.field(description="The IMAP keyword a KEYWORD category is kept as.")
    created_at: datetime.datetime

    @strawberry_django.field(description="Messages in the category (every copy counts).")
    def message_count(self) -> int:
        return models.Message.objects.filter(account_id=self.account_id, deleted_at=None, category_ids__contains=[self.id]).count()  # type: ignore[attr-defined]


@strawberry.type(description="What pushing a mailbox's changes did.")
class PushResult:
    account: MailAccount
    pushed: int = strawberry.field(description="Changes that reached the server.")
    pending: int = strawberry.field(description="Changes still waiting (in their undo window, backing off, or a sync held the mailbox).")
    failed: int = strawberry.field(description="Changes that did not reach the server.")


def _visible_links(task: object, info: Info):  # noqa: ANN202
    """The task's links whose thread the caller can still see."""
    from mail.scoping import scope_queryset

    return scope_queryset(models.TaskThread.objects.filter(task_id=task.id), info)  # type: ignore[attr-defined]


@kante.django_type(models.TaskList, pagination=True, filters=filters.TaskListFilter, description="A member's list of tasks (an Inbox bundle or project). Only its owner sees it.")
class TaskList(OrgScoped):
    id: strawberry.ID
    name: str
    color: str
    position: float
    created_at: datetime.datetime
    tasks: List["Task"] = strawberry_django.field(pagination=True, filters=filters.TaskFilter, ordering=filters.TaskOrder, description="The tasks on the list.")

    @strawberry_django.field(description="How many tasks on the list are OPEN.")
    def open_count(self) -> int:
        return self.tasks.filter(status=models.TaskStatus.OPEN).count()  # type: ignore[attr-defined]


@kante.django_type(models.TaskThread, description="A conversation in a task: who put it there, and (for an app) how sure it was and why.")
class TaskThread(OrgScoped):
    id: strawberry.ID
    task: "Task"
    thread: Thread
    source: enums.TaskLinkSource
    confidence: Optional[float]
    reason: str
    position: float
    created_at: datetime.datetime

    @strawberry_django.field(description="The client id of the app the link was made from, if any.")
    def app_client_id(self) -> Optional[str]:
        return self.client.client_id if self.client_id else None  # type: ignore[attr-defined]


@kante.django_type(models.Task, pagination=True, filters=filters.TaskFilter, ordering=filters.TaskOrder, description="Something to do, made of mail conversations from any mailbox its owner can see. Only its owner sees it. Its status is independent of the mail: finishing a task changes no message.")
class Task(OrgScoped):
    id: strawberry.ID
    title: str
    notes: str
    status: enums.TaskStatus
    pinned: bool
    due_at: Optional[datetime.datetime]
    snoozed_until: Optional[datetime.datetime]
    position: float
    external_key: Optional[str]
    list: Optional[TaskList]
    completed_at: Optional[datetime.datetime]
    created_at: datetime.datetime
    updated_at: datetime.datetime

    @strawberry_django.field(description="Whether the task is snoozed right now.")
    def snoozed(self) -> bool:
        return bool(self.snoozed_until and self.snoozed_until > timezone.now())  # type: ignore[attr-defined]

    @strawberry_django.field(description="The client id of the app that created the task, if one did.")
    def app_client_id(self) -> Optional[str]:
        return self.client.client_id if self.client_id else None  # type: ignore[attr-defined]

    @strawberry_django.field(description="The task's conversations with who put them there; only those in mailboxes the caller can still see.")
    def links(self, info: Info) -> List[TaskThread]:
        return list(_visible_links(self, info).select_related("thread", "client"))  # type: ignore[return-value]

    @strawberry_django.field(description="The task's conversations, newest first; only those in mailboxes the caller can still see.")
    def threads(self, info: Info) -> List[Thread]:
        ids = _visible_links(self, info).values("thread_id")
        return list(models.Thread.objects.filter(id__in=ids).order_by(F("last_message_at").desc(nulls_last=True), "-id"))  # type: ignore[return-value]

    @strawberry_django.field(description="How many conversations the task has (visible ones).")
    def thread_count(self, info: Info) -> int:
        return _visible_links(self, info).count()

    @strawberry_django.field(description="Unread messages over the task's conversations.")
    def unread_count(self, info: Info) -> int:
        ids = _visible_links(self, info).values("thread_id")
        return models.Message.objects.filter(thread_id__in=ids).exclude(flags__contains=["\\Seen"]).count()

    @strawberry_django.field(description="The newest message over the task's conversations.")
    def latest_message(self, info: Info) -> Optional[Message]:
        ids = _visible_links(self, info).values("thread_id")
        return models.Message.objects.filter(thread_id__in=ids).order_by(F("date").desc(nulls_last=True), "-id").first()  # type: ignore[return-value]
