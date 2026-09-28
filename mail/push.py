"""Pushing queued changes (:class:`~mail.models.MailChange`) to the server.

The caller holds the mailbox's lease (a sync pass, ``flush_mail_changes``, or the push right after
a request), so pushes never race a sync. Due changes go in id order, flags first, then moves,
then expunges. Each one is pushed while its row and the change are locked, so a request that
changes the same message again waits for the push instead of racing it.

A push may run twice (a crash between the server's answer and the commit): each is idempotent.
A flag STORE is anyway; a move or expunge whose message is no longer at its origin is done when
the message is where it should be (or, for an expunge, simply gone).

Where a message is on the server:

* FLAGS -- its row's (folder, UIDVALIDITY, UID), or the origin of a move still queued for it.
  Flag changes that share a folder and the same flags go together: one FETCH to see which UIDs
  are still there, one STORE for them (a "mark 500 read" is two round trips, not a thousand);
  the rest go one at a time;
* MOVE, EXPUNGE -- the change's origin.

When UIDVALIDITY moved on, the message is looked up again by its Message-ID. A failure is retried
with backoff (``writeback.*``); what cannot work (the message is gone, the folder keeps no
keywords, an expunge would take other messages with it) is FAILED at once. The local state stays
either way -- a FAILED change still shows, and can be retried or undone.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from mail import models, overlay
from mail.errors import MailError, code_for
from mail.sync.store import delete as delete_rows

logger = logging.getLogger(__name__)

Kind = models.MailChangeKind
Code = models.MailErrorCode

#: Refusals retrying cannot fix.
PERMANENT = {Code.MESSAGE_GONE, Code.KEYWORDS_NOT_PERMITTED, Code.UNSAFE_EXPUNGE, Code.INVALID_STATE, Code.UNSUPPORTED_BY_PROTOCOL}
#: The session (or the mailbox) is unusable: stop pushing; the caller records it.
FATAL = {Code.AUTH_FAILED, Code.CONSENT_EXPIRED, Code.CONNECTION_FAILED, Code.TLS_FAILED, Code.TLS_REQUIRED, Code.HOST_NOT_ALLOWED, Code.NOT_CONFIGURED}

ORDER = {Kind.FLAGS: 0, Kind.MOVE: 1, Kind.EXPUNGE: 2, Kind.POP_DELE: 2}
#: Flag changes pushed with one STORE at most.
FLAG_BATCH = 500
#: One-at-a-time pushes between two renewals of the lease.
RENEW_EVERY = 50


class Gone(MailError):
    def __init__(self, message: str = "The message is no longer on the server where the change expected it.") -> None:
        super().__init__(message, Code.MESSAGE_GONE)


@dataclass
class PushResult:
    pushed: int = 0
    failed: int = 0
    deferred: int = 0
    touched_folders: set[int] = field(default_factory=set)


def _due(account_id: int):  # noqa: ANN202
    return models.MailChange.objects.filter(account_id=account_id, state=models.MailChangeState.PENDING, push_after__lte=timezone.now())


def has_due(account_id: int) -> bool:
    return _due(account_id).exists()


# --- IMAP helpers --------------------------------------------------------------------------------


def _decode(values) -> list[str]:  # noqa: ANN001
    return [v.decode() if isinstance(v, bytes) else str(v) for v in values or ()]


class _Session:
    """The IMAP client and the folder it has selected (read-write)."""

    def __init__(self, client) -> None:  # noqa: ANN001
        self.client = client
        self.path: str | None = None
        self.uidvalidity: int | None = None

    def select(self, folder: models.MailFolder) -> None:
        if self.path == folder.path:
            return
        info = self.client.select_folder(folder.path)
        self.path, self.uidvalidity = folder.path, int(info[b"UIDVALIDITY"])
        permanent = _decode(info.get(b"PERMANENTFLAGS", ()))
        allowed = "\\*" in permanent
        if permanent != folder.permanent_flags or allowed != folder.keywords_allowed:
            folder.permanent_flags, folder.keywords_allowed = permanent, allowed
            models.MailFolder.objects.filter(id=folder.id).update(permanent_flags=permanent, keywords_allowed=allowed)

    def exists(self, uid: int) -> bool:
        return uid in self.client.fetch([uid], ["FLAGS"])

    def find(self, folder: models.MailFolder, key: str) -> int | None:
        """The UID of the message with Message-ID ``key`` in ``folder`` (the newest, if several)."""
        if not key or key.startswith("h:"):
            return None  # no Message-ID to search by
        self.select(folder)
        found = self.client.search(["HEADER", "Message-ID", f"<{key}>"])
        return max(found) if found else None

    def locate(self, folder: models.MailFolder, uidvalidity: int | None, uid: int | None, key: str) -> int:
        """The message's UID in ``folder`` now: its stored one while that is still valid, else by Message-ID."""
        self.select(folder)
        if uid is not None and uidvalidity == self.uidvalidity and self.exists(uid):
            return uid
        found = self.find(folder, key) if (uid is None or uidvalidity != self.uidvalidity) else None
        if found is None:
            raise Gone()
        return found


def _uid_set(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        first, _, last = part.partition(":")
        a, b = int(first), int(last or first)
        out.extend(range(a, b + 1) if a <= b else range(a, b - 1, -1))
    return out


def _pop_copyuid(client) -> tuple[int, dict[int, int]] | None:  # noqa: ANN001
    """The last COPYUID response code (UIDPLUS): the destination's UIDVALIDITY and old UID → new UID."""
    values = client._imap.untagged_responses.pop("COPYUID", None)
    if not values:
        return None
    raw = values[-1].decode() if isinstance(values[-1], bytes) else str(values[-1])
    try:
        uidvalidity, source, destination = raw.split()
        return int(uidvalidity), dict(zip(_uid_set(source), _uid_set(destination)))
    except ValueError:
        return None


def _expunge(client, uids: list[int]) -> None:  # noqa: ANN001
    """Expunge exactly ``uids``; without UIDPLUS only when no other message of the folder is marked deleted."""
    if client.has_capability("UIDPLUS"):
        client.delete_messages(uids, silent=True)
        client.uid_expunge(uids)
        return
    others = set(client.search(["DELETED"])) - set(uids)
    if others:
        raise MailError("Other messages of the folder are marked deleted; without UIDPLUS they would be expunged too.", Code.UNSAFE_EXPUNGE)
    client.delete_messages(uids, silent=True)
    client.expunge()


def _lock(change: models.MailChange) -> tuple[models.Message | None, models.MailChange | None]:
    """The change's row and the change, locked (row first, as :mod:`mail.changes` locks them); None when it was undone."""
    row = None
    if change.message_id is not None:
        row = models.Message.objects.select_for_update(of=("self",)).select_related("folder").filter(id=change.message_id).first()
    fresh = models.MailChange.objects.select_for_update(of=("self",)).select_related("origin_folder", "target_folder").filter(id=change.id, state=models.MailChangeState.PENDING).first()
    return row, fresh


# --- one change each -----------------------------------------------------------------------------


def _push_flags(session: _Session, change: models.MailChange, result: PushResult) -> None:
    refusal = None
    with transaction.atomic():
        row, change = _lock(change)  # type: ignore[assignment]
        if change is None:
            return
        if row is None:
            raise Gone("The message is no longer here.")
        move = models.MailChange.objects.filter(message=row, kind=Kind.MOVE).select_related("origin_folder").first()
        if move is not None:
            folder, uidvalidity, uid = move.origin_folder, move.origin_uidvalidity, move.origin_uid
        elif row.uid is None:
            result.deferred += 1  # moved without COPYUID: the next sync of its folder finds it
            return
        else:
            folder, uidvalidity, uid = row.folder, row.uidvalidity, row.uid
        if folder is None:
            raise Gone("The message's folder is gone.")
        uid = session.locate(folder, uidvalidity, uid, change.message_key)
        held = [] if folder.keywords_allowed else [f for f in [*change.add, *change.remove] if not f.startswith("\\")]
        add = [f for f in change.add if f not in held]
        remove = [f for f in change.remove if f not in held]
        if add:
            session.client.add_flags([uid], add, silent=True)
        if remove:
            session.client.remove_flags([uid], remove, silent=True)
        row.server_flags = sorted(overlay.apply(row.server_flags, add, remove))
        models.Message.objects.filter(id=row.id).update(server_flags=row.server_flags)
        if held:
            # What was pushed is done; the keywords stay queued (FAILED) and keep showing here.
            change.add, change.remove = [f for f in change.add if f in held], [f for f in change.remove if f in held]
            change.save(update_fields=["add", "remove", "updated_at"])
            refusal = MailError(f"The folder {folder.path} keeps no keywords; {', '.join(held)} stay here only.", Code.KEYWORDS_NOT_PERMITTED)
        else:
            change.delete()
            result.pushed += 1
    if refusal is not None:
        raise refusal


def _push_flag_chunk(session: _Session, folder: models.MailFolder, uidvalidity: int | None, add: list[str], remove: list[str], chunk: list[tuple[models.MailChange, int]], result: PushResult) -> list[models.MailChange]:
    """Push one chunk of flag changes with the same flags in one folder; returns the ones to push one at a time."""
    session.select(folder)
    if session.uidvalidity != uidvalidity or (not folder.keywords_allowed and any(not f.startswith("\\") for f in [*add, *remove])):
        return [change for change, _ in chunk]  # to be found again by Message-ID, or pushed in part
    with transaction.atomic():
        rows = {r.id: r for r in models.Message.objects.select_for_update(of=("self",)).filter(id__in=[c.message_id for c, _ in chunk]).order_by("id")}
        fresh = {c.id: c for c in models.MailChange.objects.select_for_update(of=("self",)).filter(id__in=[c.id for c, _ in chunk], state=models.MailChangeState.PENDING).order_by("id")}
        # Changed by a request meanwhile (or undone): the fresh one goes on its own.
        live = [(c, uid) for c, uid in chunk if c.id in fresh and c.message_id in rows and (fresh[c.id].add, fresh[c.id].remove) == (c.add, c.remove)]
        left = [fresh[c.id] for c, _ in chunk if c.id in fresh and all(c.id != l.id for l, _ in live)]
        present = set(session.client.fetch([uid for _, uid in live], ["FLAGS"])) if live else set()
        pushed = [(c, uid) for c, uid in live if uid in present]
        left += [c for c, uid in live if uid not in present]
        uids = [uid for _, uid in pushed]
        if uids and add:
            session.client.add_flags(uids, add, silent=True)
        if uids and remove:
            session.client.remove_flags(uids, remove, silent=True)
        touched = []
        for change, _ in pushed:
            row = rows[change.message_id]
            row.server_flags = sorted(overlay.apply(row.server_flags, add, remove))
            touched.append(row)
        models.Message.objects.bulk_update(touched, ["server_flags"])
        models.MailChange.objects.filter(id__in=[c.id for c, _ in pushed]).delete()
        result.pushed += len(pushed)
    return left


def _push_flag_batches(session: _Session, changes: list[models.MailChange], result: PushResult, renew) -> list[models.MailChange]:  # noqa: ANN001
    """Push flag changes grouped by folder and flags; returns the ones to push one at a time."""
    rows = models.Message.objects.in_bulk([c.message_id for c in changes if c.message_id])
    moves = {m.message_id: m for m in models.MailChange.objects.filter(message_id__in=list(rows), kind=Kind.MOVE)}
    groups: dict[tuple, list[tuple[models.MailChange, int]]] = defaultdict(list)
    single: list[models.MailChange] = []
    for change in changes:
        row = rows.get(change.message_id)
        move = moves.get(change.message_id)
        where = (move.origin_folder_id, move.origin_uidvalidity, move.origin_uid) if move else (row.folder_id, row.uidvalidity, row.uid) if row else (None, None, None)
        if where[0] is None or where[2] is None:
            single.append(change)
            continue
        groups[(where[0], where[1], tuple(change.add), tuple(change.remove))].append((change, where[2]))
    folders = models.MailFolder.objects.in_bulk({key[0] for key in groups})
    for (folder_id, uidvalidity, add, remove), members in groups.items():
        for start in range(0, len(members), FLAG_BATCH):
            chunk = members[start : start + FLAG_BATCH]
            renew()
            try:
                single += _push_flag_chunk(session, folders[folder_id], uidvalidity, list(add), list(remove), chunk, result)
            except Exception as error:
                code = code_for(error)
                if code is None or code in FATAL:
                    raise
                session.path = None
                for change, _ in chunk:
                    _failed(change, error, code, result)
    return single


def _finish_move(row: models.Message | None, change: models.MailChange, uidvalidity: int | None, uid: int | None) -> None:
    target = change.target_folder
    if row is not None and row.folder_id != target.id:
        # Moved on again while this move was on its way: continue from where the server has it.
        change.origin_folder, change.origin_uidvalidity, change.origin_uid = target, uidvalidity, uid
        change.save(update_fields=["origin_folder", "origin_uidvalidity", "origin_uid", "updated_at"])
        return
    change.delete()
    if row is not None and uid is not None:
        # A copy sync already read in (after a crash) gives way to the row that keeps its id.
        duplicate = models.Message.objects.filter(folder=target, uidvalidity=uidvalidity, uid=uid).exclude(id=row.id)
        if duplicate.exists():
            delete_rows(duplicate)
        models.Message.objects.filter(id=row.id).update(uid=uid, uidvalidity=uidvalidity)


def _push_move(session: _Session, change: models.MailChange, result: PushResult) -> None:
    with transaction.atomic():
        row, change = _lock(change)  # type: ignore[assignment]
        if change is None:
            return
        origin, target = change.origin_folder, change.target_folder
        if origin is None or target is None:
            raise Gone("The folder is gone.")
        result.touched_folders |= {origin.id, target.id}
        try:
            uid = session.locate(origin, change.origin_uidvalidity, change.origin_uid, change.message_key)
        except Gone:
            # Pushed before (a crash lost the answer), or moved by another client: done if it arrived.
            found = session.find(target, change.message_key)
            if found is None:
                raise
            _finish_move(row, change, session.uidvalidity, found)
            result.pushed += 1
            return
        client = session.client
        _pop_copyuid(client)
        if client.has_capability("MOVE"):
            client.move([uid], target.path)
        else:
            client.copy([uid], target.path)
            _expunge(client, [uid])
        mapping = _pop_copyuid(client)
        uidvalidity, new_uid = (mapping[0], mapping[1].get(uid)) if mapping else (None, None)
        _finish_move(row, change, uidvalidity, new_uid)
        result.pushed += 1


def _push_expunge(session: _Session, change: models.MailChange, result: PushResult) -> None:
    with transaction.atomic():
        row, change = _lock(change)  # type: ignore[assignment]
        if change is None:
            return
        if change.origin_folder is not None:
            result.touched_folders.add(change.origin_folder.id)
            try:
                uid = session.locate(change.origin_folder, change.origin_uidvalidity, change.origin_uid, change.message_key)
            except Gone:
                uid = None  # already gone: that is what was asked
            if uid is not None:
                _expunge(session.client, [uid])
        change.delete()
        if row is not None:
            delete_rows(models.Message.objects.filter(id=row.id))
        result.pushed += 1


PUSH = {Kind.FLAGS: _push_flags, Kind.MOVE: _push_move, Kind.EXPUNGE: _push_expunge}


def _failed(change: models.MailChange, error: BaseException, code: str, result: PushResult) -> None:
    config = settings.KUVERT_WRITEBACK
    attempts = change.attempts + 1
    fields = {"attempts": attempts, "error": str(error)[:2000] or type(error).__name__, "error_code": code}
    if code in PERMANENT or attempts >= int(config["max_attempts"]):
        fields["state"] = models.MailChangeState.FAILED
        result.failed += 1
    else:
        wait = min(int(config["backoff_base_seconds"]) * 2 ** (attempts - 1), int(config["backoff_max_seconds"]))
        fields["push_after"] = timezone.now() + timedelta(seconds=wait)
        result.deferred += 1
    models.MailChange.objects.filter(id=change.id).update(**fields)
    logger.info("Pushing %s change %s of mailbox %s failed (%s): %s", change.kind, change.id, change.account_id, code, error)


# --- orphans ---------------------------------------------------------------------------------------


def resolve(account: models.MailAccount, final: bool = False) -> int:
    """Hand FLAGS changes whose row sync replaced to the message's rows now; ``final``: FAILED when it has none."""
    moved = 0
    orphans = models.MailChange.objects.filter(account=account, message=None, kind=Kind.FLAGS, state__in=overlay.OPEN_STATES)
    for orphan in orphans:
        with transaction.atomic():
            rows = list(models.Message.objects.select_for_update(of=("self",)).filter(account=account, message_key=orphan.message_key, deleted_at=None).order_by("id"))
            if not rows:
                if final:
                    models.MailChange.objects.filter(id=orphan.id).update(state=models.MailChangeState.FAILED, error="The message is no longer in any synced folder.", error_code=Code.MESSAGE_GONE)
                continue
            for row in rows:
                change = models.MailChange.objects.select_for_update().filter(message=row, kind=Kind.FLAGS).first()
                if change is None:
                    models.MailChange.objects.create(account=account, message=row, message_key=row.message_key, kind=Kind.FLAGS, add=orphan.add, remove=orphan.remove, push_after=orphan.push_after, created_by_id=orphan.created_by_id)
                else:
                    # The row's own (newer) change wins for the flags both name.
                    named = {f.lower() for f in [*change.add, *change.remove]}
                    change.add = [*change.add, *[f for f in orphan.add if f.lower() not in named]]
                    change.remove = [*change.remove, *[f for f in orphan.remove if f.lower() not in named]]
                    change.save(update_fields=["add", "remove", "updated_at"])
            orphan.delete()
            overlay.materialize(rows)
            moved += 1
    return moved


# --- the pass --------------------------------------------------------------------------------------


def _flush_pop3(client, account: models.MailAccount, changes: list[models.MailChange], result: PushResult) -> None:  # noqa: ANN001
    _, lines, _ = client.uidl()
    numbers = {uidl.strip(): int(number) for number, uidl in (line.decode(errors="replace").split(" ", 1) for line in lines)}
    for change in changes:
        if change.kind != Kind.POP_DELE:
            _failed(change, MailError("POP3 mailboxes keep this here.", Code.UNSUPPORTED_BY_PROTOCOL), Code.UNSUPPORTED_BY_PROTOCOL, result)
            continue
        with transaction.atomic():
            row, fresh = _lock(change)
            if fresh is None:
                continue
            if fresh.origin_uidl in numbers:
                client.dele(numbers[fresh.origin_uidl])  # takes effect at QUIT
            fresh.delete()
            if row is not None:
                result.touched_folders.add(row.folder_id)
                delete_rows(models.Message.objects.filter(id=row.id))
            result.pushed += 1


def flush(client, account: models.MailAccount) -> PushResult:  # noqa: ANN001
    """Push the mailbox's due changes over a logged-in client (the caller holds the lease)."""
    result = PushResult()
    resolve(account)
    changes = sorted(_due(account.id), key=lambda c: (ORDER[c.kind], c.id))
    if not changes:
        return result
    if account.protocol == models.Protocol.POP3:
        _flush_pop3(client, account, changes, result)
        return result
    from mail.sync import renew

    session = _Session(client)
    flags = [c for c in changes if c.kind == Kind.FLAGS]
    single = _push_flag_batches(session, flags, result, lambda: renew(account.id))
    for index, change in enumerate([*single, *(c for c in changes if c.kind in (Kind.MOVE, Kind.EXPUNGE))]):
        if index % RENEW_EVERY == 0:
            renew(account.id)  # a long push must not lose the lease to a sync
        try:
            PUSH[change.kind](session, change, result)
        except Exception as error:
            code = code_for(error)
            if code is None or code in FATAL:
                raise
            session.path = None  # the server may have closed the folder on an error
            _failed(change, error, code, result)
    if result.pushed:
        logger.info("Pushed %s change(s) of mailbox %s (%s failed, %s deferred)", result.pushed, account.id, result.failed, result.deferred)
    return result
