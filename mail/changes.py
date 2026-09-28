"""Changes made here: applied to the rows at once, then queued for the server -- or kept local.

Nothing here opens a connection. A change takes effect in the database in one transaction and
is visible right away; what the server should learn of it becomes a
:class:`~mail.models.MailChange` that :mod:`mail.push` applies later (from ``push_after`` on --
the undo window). What the mailbox is set not to push (``push_seen``, ``push_flagged``,
``push_keywords``) becomes a :class:`~mail.models.LocalPin` instead: it overrides the server for
every copy of the message, and server changes to that flag no longer show.

Queued changes coalesce: one FLAGS change per message, where setting and clearing a flag again
cancel out; a move of a message still waiting to move re-targets the queued move (and a move
back to where the server has it drops it). Moves and deletes cannot stay local (a folder only
exists on the server): with ``push_moves`` off a move is refused, with ``push_deletes`` off a
delete only hides the message here.
"""

import re
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from mail import models, overlay, threads
from mail.errors import MailError, unsupported

#: Flags every IMAP mailbox pushes, whatever its settings: the server needs them to be useful.
ALWAYS_PUSHED = {"\\answered", "\\draft"}
#: The account setting that decides whether a flag is pushed.
TOGGLES = {"\\seen": "push_seen", "\\flagged": "push_flagged"}
KEYWORD_TOGGLE = "push_keywords"

_KEYWORD = re.compile(r"^\$?[A-Za-z0-9_.\-]{1,99}$")

Kind = models.MailChangeKind


def toggle_of(flag: str) -> str | None:
    """The ``push_*`` setting that decides ``flag`` (None: always pushed)."""
    low = flag.lower()
    if low in ALWAYS_PUSHED:
        return None
    if low in TOGGLES:
        return TOGGLES[low]
    return None if flag.startswith("\\") else KEYWORD_TOGGLE


def pushes(account: models.MailAccount, flag: str) -> bool:
    """Whether a change of ``flag`` goes to the server (POP3 keeps every flag here)."""
    if account.protocol != models.Protocol.IMAP:
        return False
    toggle = toggle_of(flag)
    return True if toggle is None else bool(getattr(account, toggle))


def push_after(kind: str) -> datetime:
    """When a change of ``kind`` made now may be pushed (the end of its undo window)."""
    config = settings.KUVERT_WRITEBACK
    seconds = {Kind.FLAGS: config["undo_seconds_flags"], Kind.MOVE: config["undo_seconds_moves"]}.get(kind, config["undo_seconds_deletes"])
    return timezone.now() + timedelta(seconds=int(seconds))


def _without(values: list[str], flag: str) -> list[str]:
    low = flag.lower()
    return [v for v in values if v.lower() != low]


def _record(add: list[str], remove: list[str], flag: str, on: bool, server_has: bool) -> tuple[list[str], list[str]]:
    """Set (``on``) or clear ``flag`` in a queued add/remove pair.

    Undoing a change that is still queued cancels it -- unless the server is not known to be in
    the state it goes back to, then it is queued the other way round.
    """
    had_opposite = any(v.lower() == flag.lower() for v in (remove if on else add))
    add, remove = _without(add, flag), _without(remove, flag)
    if had_opposite and server_has == on:
        return add, remove
    (add if on else remove).append(flag)
    return add, remove


def _lock(account: models.MailAccount, ids: list[int]) -> list[models.Message]:
    """The mailbox's (not deleted) messages ``ids``, locked for this transaction, in id order."""
    return list(models.Message.objects.select_for_update(of=("self",)).filter(account=account, id__in=ids, deleted_at=None).select_related("folder").order_by("id"))


def _reopen(change: models.MailChange) -> None:
    """A changed change is tried afresh."""
    change.state, change.attempts, change.error, change.error_code = models.MailChangeState.PENDING, 0, None, None


def _queue_flags(row: models.Message, ops: list[tuple[str, bool]], user) -> None:  # noqa: ANN001
    change = models.MailChange.objects.select_for_update().filter(message=row, kind=Kind.FLAGS).first()
    add, remove = (list(change.add), list(change.remove)) if change else ([], [])
    server = {f.lower() for f in row.server_flags}
    for flag, on in ops:
        add, remove = _record(add, remove, flag, on, flag.lower() in server)
    if not add and not remove:
        if change is not None:
            change.delete()
        return
    if change is None:
        models.MailChange.objects.create(account_id=row.account_id, message=row, message_key=row.message_key, kind=Kind.FLAGS, add=add, remove=remove, push_after=push_after(Kind.FLAGS), created_by=user)
        return
    change.add, change.remove, change.push_after = add, remove, push_after(Kind.FLAGS)
    _reopen(change)
    change.save()


def _pin(account: models.MailAccount, keys: set[str], ops: list[tuple[str, bool]]) -> None:
    for key in sorted(keys):
        pin, _ = models.LocalPin.objects.select_for_update().get_or_create(account=account, message_key=key)
        add, remove = list(pin.add), list(pin.remove)
        for flag, on in ops:
            add, remove = _without(add, flag), _without(remove, flag)
            (add if on else remove).append(flag)
        pin.add, pin.remove = add, remove
        pin.save()


def _strip_from_changes(account: models.MailAccount, keys: set[str], flags: list[str]) -> None:
    """A flag now kept local no longer goes to the server from any copy of these messages."""
    for change in models.MailChange.objects.select_for_update().filter(account=account, message_key__in=keys, kind=Kind.FLAGS):
        add, remove = change.add, change.remove
        for flag in flags:
            add, remove = _without(add, flag), _without(remove, flag)
        if not add and not remove:
            change.delete()
        elif (add, remove) != (change.add, change.remove):
            change.add, change.remove = add, remove
            change.save(update_fields=["add", "remove", "updated_at"])


def set_flags(account: models.MailAccount, messages: list[models.Message], add: list[str], remove: list[str], user=None) -> list[models.Message]:  # noqa: ANN001
    """Set and clear flags of messages of one mailbox, here at once; queue or pin them (see the module docstring)."""
    ops = [(flag, True) for flag in add] + [(flag, False) for flag in remove]
    with transaction.atomic():
        rows = _lock(account, [m.id for m in messages])
        keys = {row.message_key for row in rows}
        local = [(flag, on) for flag, on in ops if not pushes(account, flag)]
        pushed = [(flag, on) for flag, on in ops if pushes(account, flag)]
        if local:
            _pin(account, keys, local)
            _strip_from_changes(account, keys, [flag for flag, _ in local])
        if pushed:
            for row in rows:
                _queue_flags(row, pushed, user)
        copies = list(models.Message.objects.filter(account=account, message_key__in=keys)) if local else rows
        overlay.materialize(copies)
        overlay.recount({row.folder_id for row in copies})
    return list(models.Message.objects.filter(id__in=[row.id for row in rows]).order_by("id"))


def move(account: models.MailAccount, messages: list[models.Message], destination: models.MailFolder, user=None, *, policy: str = "push_moves") -> list[models.Message]:  # noqa: ANN001
    """Move messages to another folder of their mailbox, here at once; the move is queued. Rows keep their ids."""
    if account.protocol != models.Protocol.IMAP:
        raise unsupported("move messages between folders")
    if destination.account_id != account.id:
        raise MailError("Messages can only be moved within their mailbox.", models.MailErrorCode.INVALID_STATE)
    if not destination.selectable or not destination.exists_on_server:
        raise MailError(f"The folder {destination.path} cannot hold messages.", models.MailErrorCode.INVALID_STATE)
    if not getattr(account, policy):
        raise MailError("This mailbox is set not to move mail on the server.", models.MailErrorCode.UNSUPPORTED_BY_POLICY)
    with transaction.atomic():
        rows = _lock(account, [m.id for m in messages])
        queued = {c.message_id: c for c in models.MailChange.objects.select_for_update().filter(message__in=rows, kind=Kind.MOVE)}
        touched: set[int] = set()
        for row in rows:
            if row.folder_id == destination.id:
                continue
            touched |= {row.folder_id, destination.id}
            change = queued.get(row.id)
            if change is not None:
                if change.origin_folder_id == destination.id:
                    # Back where the server still has it: nothing to push.
                    row.uid, row.uidvalidity = change.origin_uid, change.origin_uidvalidity
                    change.delete()
                else:
                    change.target_folder, change.push_after = destination, push_after(Kind.MOVE)
                    _reopen(change)
                    change.save()
            else:
                if row.uid is None:
                    raise MailError("A message is still on its way to its folder on the server; sync the mailbox first.", models.MailErrorCode.INVALID_STATE)
                models.MailChange.objects.create(
                    account=account,
                    message=row,
                    message_key=row.message_key,
                    kind=Kind.MOVE,
                    origin_folder_id=row.folder_id,
                    origin_uidvalidity=row.uidvalidity,
                    origin_uid=row.uid,
                    target_folder=destination,
                    push_after=push_after(Kind.MOVE),
                    created_by=user,
                )
                row.uid = row.uidvalidity = None
            row.folder = destination
        models.Message.objects.bulk_update(rows, ["folder", "uid", "uidvalidity"])
        overlay.recount(touched)
    return list(models.Message.objects.filter(id__in=[row.id for row in rows]).order_by("id"))


def _tombstone(rows: list[models.Message]) -> None:
    now = timezone.now()
    for row in rows:
        row.deleted_at = now
    models.Message.objects.bulk_update(rows, ["deleted_at"])
    models.MailChange.objects.filter(message__in=rows, kind=Kind.FLAGS).delete()  # nothing left to flag
    threads.refresh_many({row.thread_id for row in rows})


def delete(account: models.MailAccount, messages: list[models.Message], permanent: bool = False, user=None) -> int:  # noqa: ANN001
    """Delete messages here at once: into Trash (a queued move), or for good (a queued expunge)."""
    if account.protocol == models.Protocol.POP3 or not account.push_deletes:
        with transaction.atomic():
            rows = _lock(account, [m.id for m in messages])
            if account.push_deletes:
                for row in rows:
                    if row.uidl:
                        models.MailChange.objects.create(account=account, message=row, message_key=row.message_key, kind=Kind.POP_DELE, origin_folder_id=row.folder_id, origin_uidl=row.uidl, push_after=push_after(Kind.POP_DELE), created_by=user)
            _tombstone(rows)
            overlay.recount({row.folder_id for row in rows})
        return len(rows)

    trash = models.MailFolder.objects.filter(account=account, role=models.FolderRole.TRASH, exists_on_server=True, selectable=True).first()
    to_trash = [m for m in messages if not permanent and trash is not None and m.folder_id != trash.id]
    for_good = [m for m in messages if m not in to_trash]
    if to_trash:
        move(account, to_trash, trash, user, policy="push_deletes")  # type: ignore[arg-type]
    if for_good:
        with transaction.atomic():
            rows = _lock(account, [m.id for m in for_good])
            queued = {c.message_id: c for c in models.MailChange.objects.select_for_update().filter(message__in=rows, kind=Kind.MOVE)}
            for row in rows:
                change = queued.get(row.id)
                if change is not None:
                    # Still where the queued move started: expunge it there instead.
                    origin = (change.origin_folder_id, change.origin_uidvalidity, change.origin_uid)
                    change.delete()
                elif row.uid is None:
                    raise MailError("A message is still on its way to its folder on the server; sync the mailbox first.", models.MailErrorCode.INVALID_STATE)
                else:
                    origin = (row.folder_id, row.uidvalidity, row.uid)
                models.MailChange.objects.create(account=account, message=row, message_key=row.message_key, kind=Kind.EXPUNGE, origin_folder_id=origin[0], origin_uidvalidity=origin[1], origin_uid=origin[2], push_after=push_after(Kind.EXPUNGE), created_by=user)
            _tombstone(rows)
            overlay.recount({row.folder_id for row in rows})
    return len(messages)


def undo(account: models.MailAccount, changes: list[models.MailChange]) -> list[models.Message]:
    """Take back queued changes that were not pushed yet (still in their undo window, or FAILED); returns the messages as they are now."""
    now = timezone.now()
    rows: dict[int, models.Message] = {}
    folders: set[int] = set()
    with transaction.atomic():
        undoable = models.MailChange.objects.select_for_update(of=("self",)).filter(account=account, id__in=[c.id for c in changes]).filter(Q(push_after__gt=now) | Q(state=models.MailChangeState.FAILED))
        for change in undoable.select_related("message"):
            row = change.message
            if change.kind in (Kind.MOVE, Kind.EXPUNGE) and row is not None:
                if change.origin_folder_id is None:
                    continue  # its folder is gone: there is nowhere to put it back
                folders |= {row.folder_id, change.origin_folder_id}
                row.folder_id, row.uid, row.uidvalidity, row.deleted_at = change.origin_folder_id, change.origin_uid, change.origin_uidvalidity, None
                row.save(update_fields=["folder", "uid", "uidvalidity", "deleted_at"])
            elif change.kind == Kind.POP_DELE and row is not None:
                folders.add(row.folder_id)
                row.deleted_at = None
                row.save(update_fields=["deleted_at"])
            if row is not None:
                rows[row.id] = row
            change.delete()
        threads.refresh_many({row.thread_id for row in rows.values()})
        overlay.materialize(rows.values())
        overlay.recount(folders | {row.folder_id for row in rows.values()})
    return list(models.Message.objects.filter(id__in=list(rows)).order_by("id"))


def revert_to_server(account: models.MailAccount, messages: list[models.Message]) -> list[models.Message]:
    """Drop what was changed here on these messages and not pushed: pins, queued flag changes, local-only deletes."""
    ids = [m.id for m in messages]
    with transaction.atomic():
        rows = list(models.Message.objects.select_for_update(of=("self",)).filter(account=account, id__in=ids))
        keys = {row.message_key for row in rows}
        models.LocalPin.objects.filter(account=account, message_key__in=keys).delete()
        models.MailChange.objects.filter(message__in=rows, kind=Kind.FLAGS).delete()
        pending_deletes = set(models.MailChange.objects.filter(message__in=rows, kind__in=[Kind.EXPUNGE, Kind.POP_DELE]).values_list("message_id", flat=True))
        models.Message.objects.filter(id__in=[r.id for r in rows if r.deleted_at and r.id not in pending_deletes]).update(deleted_at=None)
        copies = list(models.Message.objects.filter(account=account, message_key__in=keys))
        overlay.materialize(copies)
        overlay.recount({row.folder_id for row in copies})
    return list(models.Message.objects.filter(id__in=ids).order_by("id"))


def apply_policy(account: models.MailAccount, before: dict[str, bool], user=None) -> None:  # noqa: ANN001
    """After ``push_*`` settings changed: pins of a flag now pushed are queued, queued changes of a flag now kept local are pinned."""
    toggles = (*TOGGLES.values(), KEYWORD_TOGGLE)
    turned_on = {t for t in toggles if not before[t] and getattr(account, t)}
    turned_off = {t for t in toggles if before[t] and not getattr(account, t)}
    if account.protocol != models.Protocol.IMAP or not (turned_on or turned_off):
        return
    with transaction.atomic():
        keys: set[str] = set()
        if turned_off:
            for change in models.MailChange.objects.select_for_update().filter(account=account, kind=Kind.FLAGS):
                ops = [(f, True) for f in change.add if toggle_of(f) in turned_off] + [(f, False) for f in change.remove if toggle_of(f) in turned_off]
                if ops:
                    _pin(account, {change.message_key}, ops)
                    _strip_from_changes(account, {change.message_key}, [f for f, _ in ops])
                    keys.add(change.message_key)
        if turned_on:
            for pin in models.LocalPin.objects.select_for_update().filter(account=account):
                ops = [(f, True) for f in pin.add if toggle_of(f) in turned_on] + [(f, False) for f in pin.remove if toggle_of(f) in turned_on]
                if not ops:
                    continue
                for row in models.Message.objects.select_for_update(of=("self",)).filter(account=account, message_key=pin.message_key, deleted_at=None):
                    _queue_flags(row, ops, user)
                moved = {f.lower() for f, _ in ops}
                pin.add, pin.remove = [f for f in pin.add if f.lower() not in moved], [f for f in pin.remove if f.lower() not in moved]
                if pin.add or pin.remove:
                    pin.save()
                else:
                    pin.delete()
                keys.add(pin.message_key)
        overlay.materialize_keys(account.id, keys)
        overlay.recount(set(models.Message.objects.filter(account=account, message_key__in=keys).values_list("folder_id", flat=True)))


# --- categories ----------------------------------------------------------------------------------


def check_keyword(keyword: str) -> str:
    """``keyword`` if it is a usable IMAP keyword (an atom, no system flag)."""
    if not _KEYWORD.match(keyword or ""):
        raise MailError(f"{keyword!r} is not a valid keyword (letters, digits, $ _ . -).", models.MailErrorCode.INVALID_STATE)
    return keyword


def keyword_for(account: models.MailAccount, name: str) -> str:
    """A free keyword for a category called ``name`` ($Invoices, $Invoices2, …)."""
    base = "$" + ("".join(ch for ch in name.title() if ch.isascii() and (ch.isalnum() or ch in "_-")) or "Category")[:60]
    taken = {k.lower() for k in models.Category.objects.filter(account=account).values_list("keyword", flat=True)}
    keyword, n = base, 1
    while keyword.lower() in taken:
        n += 1
        keyword = f"{base}{n}"
    return keyword


def _copies(account: models.MailAccount, keys) -> list[models.Message]:  # noqa: ANN001
    return list(models.Message.objects.filter(account=account, message_key__in=keys, deleted_at=None))


def _keyword_members(category: models.Category) -> list[models.Message]:
    low = category.keyword.lower()
    rows = models.Message.objects.filter(account_id=category.account_id, deleted_at=None).filter(Q(category_ids__contains=[category.id]) | Q(flags__contains=[category.keyword]))
    return [row for row in rows if any(f.lower() == low for f in row.flags)]


def categorize(account: models.MailAccount, messages: list[models.Message], add: list[models.Category], remove: list[models.Category], user=None) -> list[models.Message]:  # noqa: ANN001
    """Put messages into categories and take them out: every copy of each message, here at once."""
    if any(c.account_id != account.id for c in [*add, *remove]):
        raise MailError("Categories belong to one mailbox; use that mailbox's.", models.MailErrorCode.INVALID_STATE)
    keys = {m.message_key for m in messages}
    with transaction.atomic():
        for category in add:
            if category.sync == models.CategorySync.LOCAL:
                models.CategoryAssignment.objects.bulk_create([models.CategoryAssignment(category=category, account=account, message_key=key, created_by=user) for key in keys], ignore_conflicts=True)
        for category in remove:
            if category.sync == models.CategorySync.LOCAL:
                models.CategoryAssignment.objects.filter(category=category, message_key__in=keys).delete()
        keyword_add = [c.keyword for c in add if c.sync == models.CategorySync.KEYWORD]
        keyword_remove = [c.keyword for c in remove if c.sync == models.CategorySync.KEYWORD]
        if keyword_add or keyword_remove:
            set_flags(account, _copies(account, keys), keyword_add, keyword_remove, user)
        overlay.materialize_keys(account.id, keys)
    return list(models.Message.objects.filter(id__in=[m.id for m in messages]).order_by("id"))


def create_category(account: models.MailAccount, name: str, color: str = "", sync: str = models.CategorySync.LOCAL, keyword: str | None = None) -> models.Category:
    """A new category; a KEYWORD one already holds the messages that carry its keyword."""
    with transaction.atomic():
        category = models.Category.objects.create(account=account, name=name.strip(), color=color, sync=sync, keyword=check_keyword(keyword) if keyword else keyword_for(account, name))
        overlay.materialize_category(category)
    return category


def update_category(category: models.Category, *, name: str | None = None, color: str | None = None, sync: str | None = None, keyword: str | None = None, remove_keywords: bool = False, user=None) -> models.Category:  # noqa: ANN001
    """Rename, recolor, re-key or move a category between LOCAL and KEYWORD, keeping its messages."""
    account = category.account
    with transaction.atomic():
        members_by_keyword = _keyword_members(category) if category.sync == models.CategorySync.KEYWORD else []
        old_keyword, old_sync = category.keyword, category.sync
        if name is not None:
            category.name = name.strip()
        if color is not None:
            category.color = color
        if keyword is not None and keyword != old_keyword:
            category.keyword = check_keyword(keyword)
        if sync is not None:
            category.sync = sync
        category.save()

        if old_sync == models.CategorySync.KEYWORD and category.sync == models.CategorySync.KEYWORD and category.keyword != old_keyword:
            set_flags(account, members_by_keyword, [category.keyword], [old_keyword], user)
        elif old_sync == models.CategorySync.LOCAL and category.sync == models.CategorySync.KEYWORD:
            keys = set(category.assignments.values_list("message_key", flat=True))
            if keys:
                set_flags(account, _copies(account, keys), [category.keyword], [], user)
            category.assignments.all().delete()
        elif old_sync == models.CategorySync.KEYWORD and category.sync == models.CategorySync.LOCAL:
            keys = {row.message_key for row in members_by_keyword}
            models.CategoryAssignment.objects.bulk_create([models.CategoryAssignment(category=category, account=account, message_key=key, created_by=user) for key in keys], ignore_conflicts=True)
            if remove_keywords and members_by_keyword:
                set_flags(account, members_by_keyword, [], [old_keyword], user)
        overlay.materialize_category(category)
    return category


def delete_category(category: models.Category, remove_keywords: bool = False, user=None) -> None:  # noqa: ANN001
    """Delete a category; a KEYWORD one's keyword stays on the messages unless ``remove_keywords``."""
    with transaction.atomic():
        if category.sync == models.CategorySync.KEYWORD and remove_keywords:
            members = _keyword_members(category)
            if members:
                set_flags(category.account, members, [], [category.keyword], user)
        rows = list(models.Message.objects.filter(account_id=category.account_id, category_ids__contains=[category.id]))
        category.delete()
        overlay.materialize(rows)
