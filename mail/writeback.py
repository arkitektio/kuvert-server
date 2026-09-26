"""Changing mail on the server: flags, moves and deletes.

The server is changed first and the rows after: when the server refuses, nothing local changes,
so the database never claims a state the mailbox does not have. Moves and deletes change which
rows exist, so they take the mailbox's sync lease (SYNC_IN_PROGRESS while a sync runs); flag
changes do not need it.

POP3 has no flags or folders on the server: flags are kept locally, a move is
UNSUPPORTED_BY_PROTOCOL, and a delete deletes on the server (DELE) and then locally.

Everything here is blocking; resolvers run it with :func:`mail.sync.in_worker`.
"""

from collections import defaultdict

from django.db import transaction

from mail import models, sync
from mail.errors import AlreadySyncing, MailError, unsupported
from mail.sync import imap as imap_sync
from mail.sync.store import delete as delete_rows

SYSTEM_FLAGS = {"\\Seen", "\\Flagged", "\\Answered", "\\Draft", "\\Deleted"}


def _by_folder(messages: list[models.Message]) -> dict[int, list[models.Message]]:
    groups: dict[int, list[models.Message]] = defaultdict(list)
    for message in messages:
        groups[message.folder_id].append(message)
    return groups


def _select(client, folder: models.MailFolder, messages: list[models.Message]) -> list[int]:  # noqa: ANN001
    """Select ``folder`` read-write and return the messages' UIDs; refuse when UIDVALIDITY moved on."""
    info = client.select_folder(folder.path)
    if int(info[b"UIDVALIDITY"]) != folder.uidvalidity or any(m.uidvalidity != folder.uidvalidity for m in messages):
        raise MailError(f"The folder {folder.path} changed on the server; sync the mailbox first.", models.MailErrorCode.SERVER_ERROR)
    return [m.uid for m in messages if m.uid is not None]


def _apply_flags(messages: list[models.Message], add: list[str], remove: list[str]) -> None:
    for message in messages:
        message.flags = sorted((set(message.flags) | set(add)) - set(remove))
    models.Message.objects.bulk_update(messages, ["flags"])


def _recount(folder: models.MailFolder) -> None:
    folder.unread_count = folder.messages.exclude(flags__contains=["\\Seen"]).count()
    folder.save(update_fields=["unread_count"])


def set_flags(account: models.MailAccount, messages: list[models.Message], add: list[str], remove: list[str]) -> list[models.Message]:
    """Add and remove flags of messages of one mailbox."""
    if account.protocol == models.Protocol.IMAP:
        with sync.incoming_session(account) as client:
            for group in _by_folder(messages).values():
                uids = _select(client, group[0].folder, group)
                if add:
                    client.add_flags(uids, add, silent=True)
                if remove:
                    client.remove_flags(uids, remove, silent=True)
    with transaction.atomic():
        _apply_flags(messages, add, remove)
        for group in _by_folder(messages).values():
            _recount(group[0].folder)
    return messages


def _leased(account: models.MailAccount):  # noqa: ANN202
    from contextlib import contextmanager

    @contextmanager
    def lease():  # noqa: ANN202
        if not sync.claim(account.id):
            raise AlreadySyncing()
        try:
            yield
        finally:
            sync.release(account.id)

    return lease()


def _expunge(client, uids: list[int]) -> None:  # noqa: ANN001
    client.delete_messages(uids, silent=True)
    if client.has_capability("UIDPLUS"):
        client.uid_expunge(uids)
    else:
        client.expunge()


def move(account: models.MailAccount, messages: list[models.Message], destination: models.MailFolder) -> list[models.Message]:
    """Move messages to another folder of their mailbox; returns their rows in the destination."""
    if account.protocol == models.Protocol.POP3:
        raise unsupported("move messages between folders")
    if destination.account_id != account.id:
        raise MailError("Messages can only be moved within their mailbox.", models.MailErrorCode.INVALID_STATE)
    moving = [m for m in messages if m.folder_id != destination.id]
    if not moving:
        return messages
    message_ids = {m.message_id for m in moving if m.message_id}
    with _leased(account), sync.incoming_session(account) as client:
        for group in _by_folder(moving).values():
            uids = _select(client, group[0].folder, group)
            if client.has_capability("MOVE"):
                client.move(uids, destination.path)
            else:
                client.copy(uids, destination.path)
                _expunge(client, uids)
        # The server gave the moved messages new UIDs there: read them in now, so the move is
        # visible at once instead of after the next sync.
        delete_rows(models.Message.objects.filter(id__in=[m.id for m in moving]))
        imap_sync.sync_folder(client, account, models.MailFolder.objects.get(id=destination.id), imap_sync.ImapResult(), condstore=False)
        for group in _by_folder(moving).values():
            _recount(group[0].folder)
    moved = list(models.Message.objects.filter(folder=destination, message_id__in=message_ids)) if message_ids else []
    return moved + [m for m in messages if m.folder_id == destination.id]


def delete(account: models.MailAccount, messages: list[models.Message], permanent: bool = False) -> int:
    """Delete messages: into the Trash folder, or for good (``permanent``, or already in Trash)."""
    if account.protocol == models.Protocol.POP3:
        with _leased(account), sync.incoming_session(account) as client:
            _, lines, _ = client.uidl()
            numbers = {uid.strip(): int(number) for number, uid in (line.decode(errors="replace").split(" ", 1) for line in lines)}
            for message in messages:
                if message.uidl in numbers:
                    client.dele(numbers[message.uidl])
        # The DELEs took effect at QUIT, when the session closed.
        return delete_rows(models.Message.objects.filter(id__in=[m.id for m in messages]))

    trash = models.MailFolder.objects.filter(account=account, role=models.FolderRole.TRASH, exists_on_server=True).first()
    to_trash = [m for m in messages if not permanent and trash is not None and m.folder_id != trash.id]
    for_good = [m for m in messages if m not in to_trash]
    if to_trash:
        move(account, to_trash, trash)  # type: ignore[arg-type]
    if for_good:
        with _leased(account), sync.incoming_session(account) as client:
            for group in _by_folder(for_good).values():
                _expunge(client, _select(client, group[0].folder, group))
            delete_rows(models.Message.objects.filter(id__in=[m.id for m in for_good]))
            for group in _by_folder(for_good).values():
                _recount(group[0].folder)
    return len(messages)
