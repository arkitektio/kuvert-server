"""Flags, categories, moves and deletes -- here at once, then queued for the server (see :mod:`mail.changes`).

Every mutation answers with the messages as they are here now; the server learns of the change
right after the request (``writeback.push_inline``), after its undo window, or with the next sync.
``Message.syncState`` and ``changes`` say where a change is.
"""

from collections import defaultdict
from typing import Optional

import strawberry
from channels.db import database_sync_to_async
from kante.errors import ValidationError
from kante.types import Info

from mail import changes, models, types
from mail.graphql.errors import translated
from mail.graphql.utils import aget_or_404, get_many, get_or_404
from mail.sync import push_account, push_soon

__all__ = [
    "SetMessageFlagsInput",
    "MarkMessagesInput",
    "MoveMessagesInput",
    "DeleteMessagesInput",
    "CategorizeMessagesInput",
    "UndoMailChangesInput",
    "set_message_flags",
    "mark_messages_read",
    "move_messages",
    "delete_messages",
    "categorize_messages",
    "undo_mail_changes",
    "revert_messages_to_server",
    "retry_mail_changes",
    "push_mail_changes",
]

_FLAG_CHARS = set("\\$abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


@strawberry.input(description="Flags to add to and remove from messages (\\Seen, \\Flagged, \\Answered, \\Draft, or keywords like $Label1).")
class SetMessageFlagsInput:
    messages: list[strawberry.ID]
    add: list[str] = strawberry.field(default_factory=list)
    remove: list[str] = strawberry.field(default_factory=list)


@strawberry.input(description="Mark messages read or unread.")
class MarkMessagesInput:
    messages: list[strawberry.ID]
    read: bool = True


@strawberry.input(description="Move messages to another folder of their mailbox.")
class MoveMessagesInput:
    messages: list[strawberry.ID]
    folder: strawberry.ID


@strawberry.input(description="Delete messages: into Trash, or for good.")
class DeleteMessagesInput:
    messages: list[strawberry.ID]
    permanent: bool = strawberry.field(default=False, description="Expunge instead of moving to Trash (messages already in Trash are always expunged).")


@strawberry.input(description="Put messages into categories of their mailbox and take them out (every copy of each message).")
class CategorizeMessagesInput:
    messages: list[strawberry.ID]
    add: list[strawberry.ID] = strawberry.field(default_factory=list, description="Categories to put the messages into.")
    remove: list[strawberry.ID] = strawberry.field(default_factory=list, description="Categories to take the messages out of.")


@strawberry.input(description="Take back changes that have not reached the server: by change, or every one of some messages.")
class UndoMailChangesInput:
    changes: Optional[list[strawberry.ID]] = None
    messages: Optional[list[strawberry.ID]] = None


def _check_flags(flags: list[str]) -> None:
    for flag in flags:
        if not flag or len(flag) > 100 or not set(flag) <= _FLAG_CHARS or flag.count("\\") > 1 or ("\\" in flag and not flag.startswith("\\")):
            raise ValidationError(f"{flag!r} is not a valid flag or keyword.")
        if flag == "\\Deleted":
            raise ValidationError("Use deleteMessages to delete.")
        if flag == "\\Recent":
            raise ValidationError("\\Recent is set by the server.")


def _load(info: Info, ids: list[strawberry.ID]) -> dict[int, tuple[models.MailAccount, list[models.Message]]]:
    """The messages by mailbox (NOT_FOUND for any the caller may not see)."""
    if not ids:
        raise ValidationError("No messages given.")
    groups: dict[int, tuple[models.MailAccount, list[models.Message]]] = {}
    lists: dict[int, list[models.Message]] = defaultdict(list)
    for message in get_many(models.Message, info, ids, select=("folder", "account")):
        lists[message.account_id].append(message)
        groups[message.account_id] = (message.account, lists[message.account_id])
    return groups


def _user(info: Info):  # noqa: ANN202
    return info.context.request.user


async def _flags(info: Info, ids: list[strawberry.ID], add: list[str], remove: list[str]) -> list[models.Message]:
    out: list[models.Message] = []
    groups = await database_sync_to_async(_load)(info, ids)
    for account, messages in groups.values():
        out += await database_sync_to_async(changes.set_flags)(account, messages, add, remove, _user(info))
    for account_id in groups:
        await push_soon(account_id)
    return out


@translated
async def set_message_flags(info: Info, input: SetMessageFlagsInput) -> list[types.Message]:
    """Add and remove flags: here at once, on the server as the mailbox's push settings say."""
    _check_flags(input.add + input.remove)
    return await _flags(info, input.messages, input.add, input.remove)  # type: ignore[return-value]


@translated
async def mark_messages_read(info: Info, input: MarkMessagesInput) -> list[types.Message]:
    """Mark messages read or unread (\\Seen)."""
    add, remove = (["\\Seen"], []) if input.read else ([], ["\\Seen"])
    return await _flags(info, input.messages, add, remove)  # type: ignore[return-value]


@translated
async def move_messages(info: Info, input: MoveMessagesInput) -> list[types.Message]:
    """Move messages to another folder of their mailbox (IMAP only): here at once, keeping their ids; the server follows after the undo window."""
    folder = await database_sync_to_async(get_or_404)(models.MailFolder, info, input.folder)
    groups = await database_sync_to_async(_load)(info, input.messages)
    if set(groups) - {folder.account_id}:
        raise ValidationError("Messages can only be moved within their own mailbox.")
    account, messages = groups[folder.account_id]
    moved = await database_sync_to_async(changes.move)(account, messages, folder, _user(info))
    await push_soon(account.id)
    return moved  # type: ignore[return-value]


@translated
async def delete_messages(info: Info, input: DeleteMessagesInput) -> types.DeleteResult:
    """Delete messages: gone here at once; into Trash or expunged on the server after the undo window."""
    deleted = 0
    groups = await database_sync_to_async(_load)(info, input.messages)
    for account, messages in groups.values():
        deleted += await database_sync_to_async(changes.delete)(account, messages, input.permanent, _user(info))
    for account_id in groups:
        await push_soon(account_id)
    return types.DeleteResult(deleted=deleted)


def _categorize(info: Info, input: CategorizeMessagesInput) -> tuple[list[models.Message], list[int]]:
    groups = _load(info, input.messages)
    add = get_many(models.Category, info, input.add)
    remove = get_many(models.Category, info, input.remove)
    if any(c.account_id not in groups for c in [*add, *remove]):
        raise ValidationError("A category belongs to one mailbox; use the categories of the messages' mailboxes.")
    out: list[models.Message] = []
    for account, messages in groups.values():
        out += changes.categorize(account, messages, [c for c in add if c.account_id == account.id], [c for c in remove if c.account_id == account.id], _user(info))
    return out, list(groups)


@translated
async def categorize_messages(info: Info, input: CategorizeMessagesInput) -> list[types.Message]:
    """Put messages into categories and take them out: here at once; a KEYWORD category also on the server."""
    out, accounts = await database_sync_to_async(_categorize)(info, input)
    for account_id in accounts:
        await push_soon(account_id)
    return out  # type: ignore[return-value]


def _undo(info: Info, input: UndoMailChangesInput) -> list[models.Message]:
    if not input.changes and not input.messages:
        raise ValidationError("Give changes or messages.")
    rows = get_many(models.MailChange, info, input.changes or [])
    if input.messages:
        messages = get_many(models.Message, info, input.messages)
        rows += list(models.MailChange.objects.filter(message__in=messages))
    by_account: dict[int, list[models.MailChange]] = defaultdict(list)
    for change in rows:
        by_account[change.account_id].append(change)
    out: list[models.Message] = []
    for account_id, account_changes in by_account.items():
        out += changes.undo(models.MailAccount.objects.get(id=account_id), account_changes)
    return out


@translated
async def undo_mail_changes(info: Info, input: UndoMailChangesInput) -> list[types.Message]:
    """Take back changes that have not reached the server (in their undo window, or FAILED); returns the messages as they are now."""
    return await database_sync_to_async(_undo)(info, input)  # type: ignore[return-value]


def _revert(info: Info, messages: list[strawberry.ID]) -> list[models.Message]:
    out: list[models.Message] = []
    for account, rows in _load(info, messages).values():
        out += changes.revert_to_server(account, rows)
    return out


@translated
async def revert_messages_to_server(info: Info, messages: list[strawberry.ID]) -> list[types.Message]:
    """Drop what was changed here on messages and has not reached the server: local-only flags, queued flag changes, local-only deletes."""
    return await database_sync_to_async(_revert)(info, messages)  # type: ignore[return-value]


def _retry(info: Info, ids: list[strawberry.ID]) -> tuple[list[models.MailChange], set[int]]:
    from django.utils import timezone

    rows = get_many(models.MailChange, info, ids)
    models.MailChange.objects.filter(id__in=[r.id for r in rows], state=models.MailChangeState.FAILED).update(state=models.MailChangeState.PENDING, attempts=0, error=None, error_code=None, push_after=timezone.now())
    return list(models.MailChange.objects.filter(id__in=[r.id for r in rows])), {r.account_id for r in rows}


@translated
async def retry_mail_changes(info: Info, changes: list[strawberry.ID]) -> list[types.MailChange]:
    """Queue FAILED changes again, due now."""
    rows, accounts = await database_sync_to_async(_retry)(info, changes)
    for account_id in accounts:
        await push_soon(account_id)
    return [row async for row in models.MailChange.objects.filter(id__in=[r.id for r in rows])]  # type: ignore[return-value]


@translated
async def push_mail_changes(info: Info, account: strawberry.ID) -> types.PushResult:
    """Push a mailbox's due changes now (without syncing). Pushes nothing while a sync holds the mailbox -- that sync pushes them."""
    mailbox = await aget_or_404(models.MailAccount, info, account)
    result = await push_account(mailbox.id)
    pending = await models.MailChange.objects.filter(account=mailbox, state=models.MailChangeState.PENDING).acount()
    failed = await models.MailChange.objects.filter(account=mailbox, state=models.MailChangeState.FAILED).acount()
    return types.PushResult(account=mailbox, pushed=result.pushed if result else 0, pending=pending, failed=failed)  # type: ignore[arg-type]
