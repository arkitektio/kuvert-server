"""Filtering and ordering for the list fields.

``strawberry_django`` turns these into GraphQL inputs the paginated list fields accept, e.g.
``messages(filters: {account: "1", unread: true, search: "invoice"}, ordering: [{date: DESC}])``.
"""

import datetime
import typing

import strawberry
import strawberry_django
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone
from kante.types import Info
from strawberry import auto

from embeddings import search
from embeddings.search import hybrid_search
from mail import enums, models
from mail.scoping import for_org


def _ids(prefix: str, field: str, value: list[strawberry.ID]) -> Q:
    return Q(**{f"{prefix}{field}__in": value})


@strawberry_django.filter_type(models.MailAccount)
class MailAccountFilter:
    """Filtering options for mailboxes."""

    @strawberry_django.filter_field
    def ids(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only these mailboxes."""
        return _ids(prefix, "id", value)

    @strawberry_django.filter_field
    def status(self, value: enums.MailAccountStatus, prefix: str) -> Q:
        """Only mailboxes in this status."""
        return Q(**{f"{prefix}status": value.value})

    @strawberry_django.filter_field
    def search(self, value: str, prefix: str) -> Q:
        """Name, address or sender name contains this (case-insensitive)."""
        return Q(**{f"{prefix}name__icontains": value}) | Q(**{f"{prefix}email_address__icontains": value}) | Q(**{f"{prefix}display_name__icontains": value})

    @strawberry_django.filter_field
    def mine(self, info: Info, value: bool, prefix: str) -> Q:
        """Only mailboxes the caller linked (true), or only ones shared with them (false)."""
        q = Q(**{f"{prefix}creator": info.context.request.user})
        return q if value else ~q


@strawberry_django.filter_type(models.MailFolder)
class MailFolderFilter:
    """Filtering options for folders."""

    @strawberry_django.filter_field
    def account(self, value: strawberry.ID, prefix: str) -> Q:
        """Only folders of this mailbox."""
        return Q(**{f"{prefix}account_id": value})

    @strawberry_django.filter_field
    def role(self, value: enums.FolderRole, prefix: str) -> Q:
        """Only folders with this role."""
        return Q(**{f"{prefix}role": value.value})

    @strawberry_django.filter_field
    def sync_enabled(self, value: bool, prefix: str) -> Q:
        """Only folders that are (not) synced."""
        return Q(**{f"{prefix}sync_enabled": value})


def _with_message(prefix: str, messages: QuerySet) -> Q:
    """A conversation matches when any of its messages is in ``messages``."""
    return Q(**{f"{prefix}id__in": messages.exclude(thread=None).values("thread_id")})


def _live() -> QuerySet:
    """Messages not deleted here."""
    return models.Message.objects.filter(deleted_at=None)


def _sync_state(value: enums.SyncState, prefix: str) -> Q:
    failed = Q(**{f"{prefix}id__in": models.MailChange.objects.filter(state=models.MailChangeState.FAILED).exclude(message=None).values("message_id")})
    pending = Q(**{f"{prefix}id__in": models.MailChange.objects.filter(state=models.MailChangeState.PENDING).exclude(message=None).values("message_id")})
    pinned = models.Message.objects.filter(Exists(models.LocalPin.objects.filter(account_id=OuterRef("account_id"), message_key=OuterRef("message_key"))))
    local = Q(**{f"{prefix}id__in": pinned.values("id")}) | Q(**{f"{prefix}deleted_at__isnull": False})
    if value == enums.SyncState.FAILED:
        return failed
    if value == enums.SyncState.PENDING:
        return pending & ~failed
    if value == enums.SyncState.LOCAL:
        return local & ~pending & ~failed
    return ~failed & ~pending & ~local


def _message_text(value: str, prefix: str = "") -> Q:
    return (
        Q(**{f"{prefix}subject__icontains": value})
        | Q(**{f"{prefix}sender_name__icontains": value})
        | Q(**{f"{prefix}sender_address__icontains": value})
        | Q(**{f"{prefix}text_body__icontains": value})
    )


@strawberry_django.filter_type(models.Message)
class MessageFilter:
    """Filtering options for messages."""

    @strawberry_django.filter_field
    def ids(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only these messages."""
        return _ids(prefix, "id", value)

    @strawberry_django.filter_field
    def account(self, value: strawberry.ID, prefix: str) -> Q:
        """Only messages of this mailbox."""
        return Q(**{f"{prefix}account_id": value})

    @strawberry_django.filter_field
    def folder(self, value: strawberry.ID, prefix: str) -> Q:
        """Only messages in this folder."""
        return Q(**{f"{prefix}folder_id": value})

    @strawberry_django.filter_field
    def folder_role(self, value: enums.FolderRole, prefix: str) -> Q:
        """Only messages in folders with this role (e.g. every INBOX)."""
        return Q(**{f"{prefix}folder__role": value.value})

    @strawberry_django.filter_field
    def thread(self, value: strawberry.ID, prefix: str) -> Q:
        """Only messages of this conversation."""
        return Q(**{f"{prefix}thread_id": value})

    @strawberry_django.filter_field
    def unread(self, value: bool, prefix: str) -> Q:
        """Only unread (true) or read (false) messages."""
        q = Q(**{f"{prefix}flags__contains": ["\\Seen"]})
        return ~q if value else q

    @strawberry_django.filter_field
    def flagged(self, value: bool, prefix: str) -> Q:
        """Only flagged (true) or unflagged (false) messages."""
        q = Q(**{f"{prefix}flags__contains": ["\\Flagged"]})
        return q if value else ~q

    @strawberry_django.filter_field
    def has_flag(self, value: str, prefix: str) -> Q:
        """Only messages with this flag or keyword (e.g. `$Label1`)."""
        return Q(**{f"{prefix}flags__contains": [value]})

    @strawberry_django.filter_field
    def has_attachments(self, value: bool, prefix: str) -> Q:
        """Only messages with (without) attachments."""
        return Q(**{f"{prefix}has_attachments": value})

    @strawberry_django.filter_field
    def category(self, value: strawberry.ID, prefix: str) -> Q:
        """Only messages in this category."""
        return Q(**{f"{prefix}category_ids__contains": [int(value)]})

    @strawberry_django.filter_field
    def sync_state(self, value: enums.SyncState, prefix: str) -> Q:
        """Only messages in this sync state (SYNCED, PENDING, LOCAL, FAILED; as `Message.syncState`)."""
        return _sync_state(value, prefix)

    @strawberry_django.filter_field
    def sender(self, value: str, prefix: str) -> Q:
        """Sender name or address contains this (case-insensitive)."""
        return Q(**{f"{prefix}sender_address__icontains": value}) | Q(**{f"{prefix}sender_name__icontains": value})

    @strawberry_django.filter_field
    def recipient(self, value: str, prefix: str) -> Q:
        """A To or Cc address contains this (case-insensitive)."""
        # JSON arrays of {name, address}: a text match over the serialized list is exact enough.
        return Q(**{f"{prefix}to__icontains": value}) | Q(**{f"{prefix}cc__icontains": value})

    @strawberry_django.filter_field
    def date_from(self, value: datetime.datetime, prefix: str) -> Q:
        """Dated at or after this."""
        return Q(**{f"{prefix}date__gte": value})

    @strawberry_django.filter_field
    def date_to(self, value: datetime.datetime, prefix: str) -> Q:
        """Dated before this."""
        return Q(**{f"{prefix}date__lt": value})

    @strawberry_django.filter_field(description="Search by text: a case-insensitive substring of the subject, sender or text; or semantic similarity to them (\"flight booking\" finds the airline's confirmation). Substring matches rank first, then by similarity; an explicit `ordering` replaces that ranking.")
    def search(self, info: Info, queryset: QuerySet, value: str, prefix: str) -> tuple[QuerySet, Q]:
        return hybrid_search(queryset, prefix, value, _message_text(value, prefix))

    @strawberry_django.filter_field(description="Order by similarity to the given message, nearest first (no cut-off; composes with other filters and pagination). Empty when the message is not visible or has no embedding yet.")
    def similar_to(self, info: Info, queryset: QuerySet, value: strawberry.ID, prefix: str) -> tuple[QuerySet, Q]:
        if prefix:
            return queryset, Q()
        # The anchor through for_org: a message the caller may not see must not steer this query.
        anchor = for_org(models.Message, info).filter(pk=value).values_list("embedding", flat=True).first()
        return search.neighbourhood(queryset, anchor, exclude_pk=value)


@strawberry_django.order_type(models.Message)
class MessageOrder:
    """Ordering options for messages."""

    date: auto
    received_at: auto
    subject: auto
    sender_address: auto
    size: auto


@strawberry_django.filter_type(models.Thread)
class ThreadFilter:
    """Filtering options for conversations."""

    @strawberry_django.filter_field
    def account(self, value: strawberry.ID, prefix: str) -> Q:
        """Only conversations of this mailbox."""
        return Q(**{f"{prefix}account_id": value})

    @strawberry_django.filter_field
    def folder(self, value: strawberry.ID, prefix: str) -> Q:
        """Only conversations with a message in this folder."""
        return Q(**{f"{prefix}id__in": _live().filter(folder_id=value).values("thread_id")})

    @strawberry_django.filter_field
    def ids(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only these conversations."""
        return _ids(prefix, "id", value)

    @strawberry_django.filter_field
    def folder_role(self, value: enums.FolderRole, prefix: str) -> Q:
        """Only conversations with a message in a folder with this role (e.g. every INBOX)."""
        return _with_message(prefix, _live().filter(folder__role=value.value))

    @strawberry_django.filter_field
    def unread(self, value: bool, prefix: str) -> Q:
        """Only conversations with (without) an unread message."""
        q = _with_message(prefix, _live().exclude(flags__contains=["\\Seen"]))
        return q if value else ~q

    @strawberry_django.filter_field
    def flagged(self, value: bool, prefix: str) -> Q:
        """Only conversations with (without) a flagged message."""
        q = _with_message(prefix, _live().filter(flags__contains=["\\Flagged"]))
        return q if value else ~q

    @strawberry_django.filter_field
    def has_attachments(self, value: bool, prefix: str) -> Q:
        """Only conversations with (without) a message with attachments."""
        q = _with_message(prefix, _live().filter(has_attachments=True))
        return q if value else ~q

    @strawberry_django.filter_field
    def category(self, value: strawberry.ID, prefix: str) -> Q:
        """Only conversations with a message in this category."""
        return _with_message(prefix, _live().filter(category_ids__contains=[int(value)]))

    @strawberry_django.filter_field
    def task(self, info: Info, value: strawberry.ID, prefix: str) -> Q:
        """Only conversations in this task (of the caller's)."""
        return Q(**{f"{prefix}id__in": models.TaskThread.objects.filter(task_id=value, task__owner=info.context.request.user).values("thread_id")})

    @strawberry_django.filter_field
    def has_task(self, info: Info, value: bool, prefix: str) -> Q:
        """Only conversations in (not in) any of the caller's open tasks -- e.g. hide what is already sorted."""
        q = Q(**{f"{prefix}id__in": models.TaskThread.objects.filter(task__owner=info.context.request.user, task__status=models.TaskStatus.OPEN).values("thread_id")})
        return q if value else ~q

    @strawberry_django.filter_field(description="Only conversations with a message matching the text: the same substring and meaning search as `messages(filters: {search})`.")
    def search(self, info: Info, queryset: QuerySet, value: str, prefix: str) -> Q:
        # Only the messages of the conversations still in play (already scoped to what the caller sees).
        candidates = _live().filter(thread_id__in=queryset.values("id"))
        candidates, predicate = hybrid_search(candidates, "", value, _message_text(value))
        return Q(**{f"{prefix}id__in": candidates.filter(predicate).order_by().values("thread_id")})


@strawberry_django.order_type(models.Thread)
class ThreadOrder:
    """Ordering options for conversations."""

    last_message_at: auto
    message_count: auto


@strawberry_django.filter_type(models.MailChange)
class MailChangeFilter:
    """Filtering options for queued changes."""

    @strawberry_django.filter_field
    def account(self, value: strawberry.ID, prefix: str) -> Q:
        """Only changes of this mailbox."""
        return Q(**{f"{prefix}account_id": value})

    @strawberry_django.filter_field
    def state(self, value: enums.MailChangeState, prefix: str) -> Q:
        """Only changes in this state."""
        return Q(**{f"{prefix}state": value.value})

    @strawberry_django.filter_field
    def kind(self, value: enums.MailChangeKind, prefix: str) -> Q:
        """Only changes of this kind."""
        return Q(**{f"{prefix}kind": value.value})

    @strawberry_django.filter_field
    def message(self, value: strawberry.ID, prefix: str) -> Q:
        """Only changes of this message."""
        return Q(**{f"{prefix}message_id": value})


@strawberry_django.filter_type(models.Category)
class CategoryFilter:
    """Filtering options for categories."""

    @strawberry_django.filter_field
    def account(self, value: strawberry.ID, prefix: str) -> Q:
        """Only categories of this mailbox."""
        return Q(**{f"{prefix}account_id": value})

    @strawberry_django.filter_field
    def sync(self, value: enums.CategorySync, prefix: str) -> Q:
        """Only LOCAL or KEYWORD categories."""
        return Q(**{f"{prefix}sync": value.value})

    @strawberry_django.filter_field
    def search(self, value: str, prefix: str) -> Q:
        """The name contains this (case-insensitive)."""
        return Q(**{f"{prefix}name__icontains": value})


@strawberry_django.filter_type(models.OutgoingMessage)
class OutgoingMessageFilter:
    """Filtering options for sent mail."""

    @strawberry_django.filter_field
    def account(self, value: strawberry.ID, prefix: str) -> Q:
        """Only mail sent from this mailbox."""
        return Q(**{f"{prefix}account_id": value})

    @strawberry_django.filter_field
    def status(self, value: enums.OutgoingStatus, prefix: str) -> Q:
        """Only mail in this status."""
        return Q(**{f"{prefix}status": value.value})


@strawberry_django.filter_type(models.TaskList)
class TaskListFilter:
    """Filtering options for task lists."""

    @strawberry_django.filter_field
    def ids(self, value: list[strawberry.ID], prefix: str) -> Q:
        """Only these lists."""
        return _ids(prefix, "id", value)

    @strawberry_django.filter_field
    def search(self, value: str, prefix: str) -> Q:
        """The name contains this (case-insensitive)."""
        return Q(**{f"{prefix}name__icontains": value})


@strawberry_django.filter_type(models.Task)
class TaskFilter:
    """Filtering options for tasks."""

    @strawberry_django.filter_field
    def ids(self, value: typing.List[strawberry.ID], prefix: str) -> Q:
        """Only these tasks."""
        return _ids(prefix, "id", value)

    @strawberry_django.filter_field
    def list(self, value: strawberry.ID, prefix: str) -> Q:
        """Only tasks on this list."""
        return Q(**{f"{prefix}list_id": value})

    @strawberry_django.filter_field
    def no_list(self, value: bool, prefix: str) -> Q:
        """Only tasks on no list (false: only tasks on some list)."""
        return Q(**{f"{prefix}list__isnull": value})

    @strawberry_django.filter_field
    def status(self, value: enums.TaskStatus, prefix: str) -> Q:
        """Only tasks in this status."""
        return Q(**{f"{prefix}status": value.value})

    @strawberry_django.filter_field
    def pinned(self, value: bool, prefix: str) -> Q:
        """Only pinned (unpinned) tasks."""
        return Q(**{f"{prefix}pinned": value})

    @strawberry_django.filter_field
    def snoozed(self, value: bool, prefix: str) -> Q:
        """Only tasks snoozed right now (or not)."""
        q = Q(**{f"{prefix}snoozed_until__gt": timezone.now()})
        return q if value else ~q

    @strawberry_django.filter_field
    def active(self, value: bool, prefix: str) -> Q:
        """OPEN and not snoozed right now: the Inbox view (false: everything else)."""
        q = Q(**{f"{prefix}status": models.TaskStatus.OPEN}) & ~Q(**{f"{prefix}snoozed_until__gt": timezone.now()})
        return q if value else ~q

    @strawberry_django.filter_field
    def due_before(self, value: datetime.datetime, prefix: str) -> Q:
        """Due before this."""
        return Q(**{f"{prefix}due_at__lt": value})

    @strawberry_django.filter_field
    def thread(self, value: strawberry.ID, prefix: str) -> Q:
        """Only tasks that contain this conversation."""
        return Q(**{f"{prefix}links__thread_id": value})

    @strawberry_django.filter_field
    def external_key(self, value: str, prefix: str) -> Q:
        """The task an app filed under this key."""
        return Q(**{f"{prefix}external_key": value})

    @strawberry_django.filter_field
    def search(self, value: str, prefix: str) -> Q:
        """Title or notes contain this (case-insensitive)."""
        return Q(**{f"{prefix}title__icontains": value}) | Q(**{f"{prefix}notes__icontains": value})


@strawberry_django.order_type(models.Task)
class TaskOrder:
    """Ordering options for tasks."""

    position: auto
    due_at: auto
    created_at: auto
    updated_at: auto
    title: auto
