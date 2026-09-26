"""Flags, moves and deletes -- on the server first, then here (see :mod:`mail.writeback`)."""

from collections import defaultdict

import strawberry
from channels.db import database_sync_to_async
from kante.errors import ValidationError
from kante.types import Info

from mail import models, types, writeback
from mail.graphql.errors import translated
from mail.graphql.utils import get_many, get_or_404
from mail.sync import in_worker

__all__ = [
    "SetMessageFlagsInput",
    "MarkMessagesInput",
    "MoveMessagesInput",
    "DeleteMessagesInput",
    "set_message_flags",
    "mark_messages_read",
    "move_messages",
    "delete_messages",
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


@translated
async def set_message_flags(info: Info, input: SetMessageFlagsInput) -> list[types.Message]:
    """Add and remove flags (on the server for IMAP; locally for POP3)."""
    _check_flags(input.add + input.remove)
    out: list[models.Message] = []
    for account, messages in (await database_sync_to_async(_load)(info, input.messages)).values():
        out += await in_worker(writeback.set_flags, account, messages, input.add, input.remove)
    return out  # type: ignore[return-value]


@translated
async def mark_messages_read(info: Info, input: MarkMessagesInput) -> list[types.Message]:
    """Mark messages read or unread (\\Seen)."""
    add, remove = (["\\Seen"], []) if input.read else ([], ["\\Seen"])
    out: list[models.Message] = []
    for account, messages in (await database_sync_to_async(_load)(info, input.messages)).values():
        out += await in_worker(writeback.set_flags, account, messages, add, remove)
    return out  # type: ignore[return-value]


@translated
async def move_messages(info: Info, input: MoveMessagesInput) -> list[types.Message]:
    """Move messages to another folder of their mailbox (IMAP only). Returns them in the destination (they get new ids)."""
    folder = await database_sync_to_async(get_or_404)(models.MailFolder, info, input.folder)
    groups = await database_sync_to_async(_load)(info, input.messages)
    if set(groups) - {folder.account_id}:
        raise ValidationError("Messages can only be moved within their own mailbox.")
    account, messages = groups[folder.account_id]
    return await in_worker(writeback.move, account, messages, folder)  # type: ignore[return-value]


@translated
async def delete_messages(info: Info, input: DeleteMessagesInput) -> types.DeleteResult:
    """Delete messages on the server: into Trash (IMAP, when there is one), or for good."""
    deleted = 0
    for account, messages in (await database_sync_to_async(_load)(info, input.messages)).values():
        deleted += await in_worker(writeback.delete, account, messages, input.permanent)
    return types.DeleteResult(deleted=deleted)

