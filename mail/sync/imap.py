"""One IMAP sync pass of a mailbox: folders, then per folder new mail, backfill, flags and expunges.

Identity is (folder, UIDVALIDITY, UID). Per folder and run:

1. **UIDVALIDITY** -- if the server changed it, every stored UID is meaningless: the folder's rows
   are dropped and it is synced from scratch.
2. **Expunges** -- stored UIDs missing from ``UID SEARCH ALL`` are deleted.
3. **New mail** -- UIDs above ``last_uid``, oldest first, so ``last_uid`` advances without gaps.
4. **Backfill** -- below ``oldest_uid`` and inside ``sync.backfill_days`` (by INTERNALDATE), newest
   first. The first sync of a folder is all backfill, so the newest mail appears first.
5. **Flags** -- ``CHANGEDSINCE`` the stored HIGHESTMODSEQ when the server has CONDSTORE, else
   the newest ``sync.flag_window`` messages are re-read.

New mail and backfill share ``sync.batch_size`` per folder and run, so a large mailbox fills
in over several runs (``more`` in the result) and no single request runs long.

Local changes (:mod:`mail.changes`) survive all of it: sync writes ``server_flags`` only and
re-materializes the effective ``flags``; a UIDVALIDITY reset keeps rows still waiting for a queued
move or delete (a fetched message adopts them instead of adding a copy, see
:func:`mail.sync.store.write`); and a message a queued move or delete takes out of a folder is
not read in there again while the server still has it.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from mail import models, overlay
from mail.protocols.clients import GuardedIMAPClient
from mail.sync.store import Fetched, delete, max_message_bytes, prepare, write

logger = logging.getLogger(__name__)

#: Messages fetched (and written) per round trip.
CHUNK = 25
#: UIDs per CONDSTORE flag fetch (kept well under servers' command-line limits).
FLAG_FETCH_CHUNK = 1000

SPECIAL_USE = {
    b"\\Sent": models.FolderRole.SENT,
    b"\\Drafts": models.FolderRole.DRAFTS,
    b"\\Trash": models.FolderRole.TRASH,
    b"\\Junk": models.FolderRole.JUNK,
    b"\\Archive": models.FolderRole.ARCHIVE,
    b"\\All": models.FolderRole.ALL,
    b"\\Flagged": models.FolderRole.FLAGGED,
    b"\\Important": models.FolderRole.FLAGGED,  # Gmail's Important: a view, like Starred
}

NAME_ROLES = {
    models.FolderRole.SENT: {"sent", "sent items", "sent mail", "sent messages", "gesendet", "gesendete objekte", "gesendete elemente", "envoyés", "enviados", "inviata"},
    models.FolderRole.DRAFTS: {"drafts", "draft", "entwürfe", "brouillons", "borradores", "bozze"},
    models.FolderRole.TRASH: {"trash", "deleted", "deleted items", "deleted messages", "bin", "papierkorb", "gelöschte objekte", "gelöschte elemente", "corbeille", "papelera", "cestino"},
    models.FolderRole.JUNK: {"junk", "spam", "junk e-mail", "junk email", "bulk mail", "spamverdacht", "courrier indésirable"},
    models.FolderRole.ARCHIVE: {"archive", "archives", "archiv", "all mail"},
}


@dataclass
class ImapResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    folders: int = 0
    more: bool = False
    new_messages: list[int] = field(default_factory=list)
    touched_folders: set[int] = field(default_factory=set)


def guess_role(path: str, flags: tuple[bytes, ...], delimiter: str | None) -> str:
    """A folder's role from its SPECIAL-USE flags, else from its name."""
    if path.upper() == "INBOX":
        return models.FolderRole.INBOX
    for flag in flags:
        if flag in SPECIAL_USE:
            return SPECIAL_USE[flag]
    leaf = (path.rsplit(delimiter, 1)[-1] if delimiter else path).strip().lower()
    for role, names in NAME_ROLES.items():
        if leaf in names:
            return role
    return models.FolderRole.OTHER


def discover_folders(client: GuardedIMAPClient, account: models.MailAccount) -> list[models.MailFolder]:
    """Upsert the server's folders; drop (with their messages) the ones it no longer lists."""
    excluded = set(settings.KUVERT_SYNC.get("folders_excluded") or [])
    listed = client.list_folders()
    seen = set()
    for flags, delimiter, path in listed:
        delimiter_text = delimiter.decode() if isinstance(delimiter, bytes) else delimiter
        path = path if isinstance(path, str) else path.decode()
        seen.add(path)
        role = guess_role(path, tuple(flags), delimiter_text)
        selectable = b"\\Noselect" not in flags and b"\\NonExistent" not in flags
        leaf = path.rsplit(delimiter_text, 1)[-1] if delimiter_text else path
        folder, created = models.MailFolder.objects.get_or_create(
            account=account,
            path=path,
            defaults={"name": leaf, "delimiter": delimiter_text, "role": role, "selectable": selectable, "sync_enabled": role not in excluded},
        )
        if not created and (folder.role != role or folder.selectable != selectable or not folder.exists_on_server):
            folder.role, folder.selectable, folder.exists_on_server = role, selectable, True
            folder.save(update_fields=["role", "selectable", "exists_on_server"])
    for gone in models.MailFolder.objects.filter(account=account).exclude(path__in=seen):
        # Moves into it that never reached the server go back to where the server has the message.
        for change in models.MailChange.objects.filter(target_folder=gone, kind=models.MailChangeKind.MOVE).exclude(origin_folder=gone):
            if change.message_id and change.origin_folder_id:
                models.Message.objects.filter(id=change.message_id).update(folder_id=change.origin_folder_id, uid=change.origin_uid, uidvalidity=change.origin_uidvalidity)
                overlay.recount([change.origin_folder_id])
            change.delete()
        delete(gone.messages.all())
        gone.delete()
    return list(models.MailFolder.objects.filter(account=account, sync_enabled=True, selectable=True).order_by("id"))


def _flags(values: tuple) -> list[str]:
    return [value.decode() if isinstance(value, bytes) else str(value) for value in values]


def _fetch_messages(client: GuardedIMAPClient, uids: list[int], uidvalidity: int) -> list[Fetched]:
    """Fetch ``uids`` whole (BODY.PEEK, so they are not marked read) or, when too large, their headers."""
    limit = max_message_bytes()
    meta = client.fetch(uids, ["RFC822.SIZE", "INTERNALDATE", "FLAGS"])
    small = [uid for uid in uids if uid in meta and meta[uid].get(b"RFC822.SIZE", 0) <= limit]
    large = [uid for uid in uids if uid in meta and uid not in small]
    bodies = client.fetch(small, ["BODY.PEEK[]"]) if small else {}
    headers = client.fetch(large, ["BODY.PEEK[HEADER]"]) if large else {}
    out = []
    for uid in uids:
        if uid not in meta:
            continue  # expunged meanwhile
        info = meta[uid]
        raw = (bodies.get(uid) or {}).get(b"BODY[]") if uid in bodies else (headers.get(uid) or {}).get(b"BODY[HEADER]")
        if raw is None:
            continue
        received = info.get(b"INTERNALDATE")
        if received is not None and received.tzinfo is None:
            received = received.replace(tzinfo=timezone.get_current_timezone())
        out.append(Fetched(raw=raw, size=int(info.get(b"RFC822.SIZE", len(raw))), flags=_flags(info.get(b"FLAGS", ())), uid=uid, uidvalidity=uidvalidity, received_at=received, truncated=uid in large))
    return out


def _store(client: GuardedIMAPClient, account: models.MailAccount, folder: models.MailFolder, uids: list[int], uidvalidity: int, result: ImapResult) -> None:
    from mail.sync import renew

    for start in range(0, len(uids), CHUNK):
        renew(account.id)  # a long first sync must not lose its lease halfway
        chunk = uids[start : start + CHUNK]
        prepared = [prepare(account, fetched) for fetched in _fetch_messages(client, chunk, uidvalidity)]
        created = write(account, folder, prepared)
        result.created += len(created)
        result.new_messages.extend(message.id for message in created)


def _sync_flags(client: GuardedIMAPClient, folder: models.MailFolder, uidvalidity: int, condstore: bool, highest_modseq: int | None, stored: dict[int, int]) -> int:
    """Re-read the server's flags of stored messages (``server_flags``); returns how many changed."""
    if not stored:
        return 0
    if condstore and folder.highest_modseq and highest_modseq:
        if highest_modseq == folder.highest_modseq:
            return 0
        # Explicit UIDs, chunked: imapclient cannot fetch a range ("1:*" -- it filters its answer by
        # the ids it was given), and a long UID list must still fit a server's command line.
        uids = sorted(stored)
        changed = {}
        for start in range(0, len(uids), FLAG_FETCH_CHUNK):
            changed.update(client.fetch(uids[start : start + FLAG_FETCH_CHUNK], ["FLAGS"], modifiers=[f"CHANGEDSINCE {folder.highest_modseq}"]))
    else:
        window = sorted(stored, reverse=True)[: int(settings.KUVERT_SYNC["flag_window"])]
        changed = client.fetch(window, ["FLAGS"]) if window else {}
    by_uid = {uid: _flags(data.get(b"FLAGS", ())) for uid, data in changed.items() if uid in stored}
    if not by_uid:
        return 0
    rows = list(models.Message.objects.filter(folder=folder, uidvalidity=uidvalidity, uid__in=list(by_uid)).only("id", "uid", "account_id", "message_key", "flags", "server_flags", "category_ids"))
    dirty = []
    for row in rows:
        flags = by_uid[row.uid]
        if sorted(flags) != sorted(row.server_flags):
            row.server_flags = flags
            dirty.append(row)
    models.Message.objects.bulk_update(dirty, ["server_flags"], batch_size=500)
    overlay.materialize(dirty)
    return len(dirty)


_LEAVING = (models.MailChangeKind.MOVE, models.MailChangeKind.EXPUNGE)


def _leaving(folder: models.MailFolder, uidvalidity: int) -> set[int]:
    """UIDs a queued move or delete takes out of ``folder``: the server still has them there, not for long."""
    return set(models.MailChange.objects.filter(origin_folder=folder, origin_uidvalidity=uidvalidity, kind__in=_LEAVING, state__in=overlay.OPEN_STATES).exclude(origin_uid=None).values_list("origin_uid", flat=True))


def _relocate_leaving(client: GuardedIMAPClient, folder: models.MailFolder, uidvalidity: int) -> None:
    """After a UIDVALIDITY change: find the messages queued moves and deletes start from again, by Message-ID."""
    for change in models.MailChange.objects.filter(origin_folder=folder, kind__in=_LEAVING, state__in=overlay.OPEN_STATES).exclude(origin_uidvalidity=uidvalidity):
        uid = None
        if change.message_key and not change.message_key.startswith("h:"):
            found = client.search(["HEADER", "Message-ID", f"<{change.message_key}>"])
            uid = max(found) if found else None
        models.MailChange.objects.filter(id=change.id).update(origin_uidvalidity=uidvalidity, origin_uid=uid)


def sync_folder(client: GuardedIMAPClient, account: models.MailAccount, folder: models.MailFolder, result: ImapResult, condstore: bool) -> None:
    """One pass over one folder (see the module docstring)."""
    info = client.select_folder(folder.path, readonly=True)
    uidvalidity = int(info[b"UIDVALIDITY"])
    highest_modseq = int(info[b"HIGHESTMODSEQ"]) if condstore and b"HIGHESTMODSEQ" in info else None
    first = folder.uidvalidity != uidvalidity
    if first and folder.uidvalidity is not None:
        logger.info("UIDVALIDITY of %s changed (%s → %s); resyncing it", folder.path, folder.uidvalidity, uidvalidity)
        # Rows already in the new epoch (a pushed move's COPYUID), waiting for a queued move (no
        # UID) or deleted here stay; the refetch adopts the ones without a valid UID.
        result.deleted += delete(folder.messages.exclude(uid=None).exclude(uidvalidity=uidvalidity).filter(deleted_at=None).exclude(changes__kind=models.MailChangeKind.MOVE))
        _relocate_leaving(client, folder, uidvalidity)
    if first:
        folder.uidvalidity, folder.last_uid, folder.oldest_uid, folder.backfill_done, folder.highest_modseq = uidvalidity, 0, None, False, None

    leaving = _leaving(folder, uidvalidity)
    on_server = sorted(client.search("ALL"))
    server_set = set(on_server)
    stored = dict(folder.messages.filter(uidvalidity=uidvalidity).exclude(uid=None).values_list("uid", "id"))
    gone = [uid for uid in stored if uid not in server_set]
    if gone:
        result.deleted += delete(folder.messages.filter(uidvalidity=uidvalidity, uid__in=gone))
        for uid in gone:
            stored.pop(uid)

    result.updated += _sync_flags(client, folder, uidvalidity, condstore, highest_modseq, stored)

    budget = min(int(settings.KUVERT_SYNC["batch_size"]), max(0, int(settings.KUVERT_SYNC["max_messages_per_run"]) - result.created))
    if not first:
        new = [uid for uid in on_server if uid > folder.last_uid and uid not in stored and uid not in leaving][:budget]
        if new:
            _store(client, account, folder, new, uidvalidity, result)
            folder.last_uid = max(folder.last_uid, max(new))
            budget -= len(new)
        if any(uid > folder.last_uid for uid in on_server):
            result.more = True
    else:
        folder.last_uid = on_server[-1] if on_server else 0

    if not folder.backfill_done:
        days = settings.KUVERT_SYNC.get("backfill_days")
        window = server_set if not days else set(client.search(["SINCE", (timezone.now() - timedelta(days=int(days))).date()]))
        pool = sorted((uid for uid in window if uid not in stored and uid not in leaving and (folder.oldest_uid is None or uid < folder.oldest_uid) and uid <= folder.last_uid), reverse=True)
        take = pool[: max(budget, 0)]
        if take:
            _store(client, account, folder, take, uidvalidity, result)
            folder.oldest_uid = min(take) if folder.oldest_uid is None else min(folder.oldest_uid, min(take))
        if len(pool) > len(take):
            result.more = True
        else:
            folder.backfill_done = True

    status = client.folder_status(folder.path, [b"MESSAGES", b"UNSEEN"])
    folder.total_count = int(status.get(b"MESSAGES", len(on_server)))
    folder.server_unread_count = int(status.get(b"UNSEEN", 0))
    folder.highest_modseq = highest_modseq
    folder.last_synced_at = timezone.now()
    folder.save()
    overlay.recount([folder.id])


def sync(client: GuardedIMAPClient, account: models.MailAccount, folder_ids: list[int] | None = None) -> ImapResult:
    """Sync the mailbox's folders (or only ``folder_ids``) over a logged-in client."""
    capabilities = sorted(cap.decode() if isinstance(cap, bytes) else cap for cap in client.capabilities())
    condstore = "CONDSTORE" in capabilities or "QRESYNC" in capabilities
    if condstore and "ENABLE" in capabilities:
        try:
            client.enable("CONDSTORE")
        except Exception:
            condstore = False
    if account.capabilities != capabilities:
        account.capabilities = capabilities
        models.MailAccount.objects.filter(pk=account.pk).update(capabilities=capabilities)

    result = ImapResult()
    folders = discover_folders(client, account)
    if folder_ids is not None:
        folders = [folder for folder in folders if folder.id in set(folder_ids)]
    for folder in folders:
        before = (result.created, result.updated, result.deleted)
        sync_folder(client, account, folder, result, condstore)
        if (result.created, result.updated, result.deleted) != before:
            result.touched_folders.add(folder.id)
        result.folders += 1
    return result
