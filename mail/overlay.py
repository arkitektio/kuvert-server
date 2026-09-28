"""A message's effective state: the server's flags with what was changed here laid over them.

Sync only ever writes ``server_flags``; ``flags`` and ``category_ids`` are materialized here from

* the :class:`~mail.models.LocalPin` of the message key (flags the mailbox does not push),
* the message's queued FLAGS :class:`~mail.models.MailChange` (pending or failed), and
* the mailbox's categories: a KEYWORD category by its keyword among the flags, a LOCAL one by
  its assignments to the message key.

::

    flags = ((server - pin.remove) | pin.add) - change.remove | change.add

Pins and assignments are kept under the message key, not the row, so they apply to every copy of
the message and survive what sync does to rows (moves elsewhere, UIDVALIDITY resets).
"""

import hashlib
from collections import defaultdict
from typing import Iterable

from django.db.models import Q

from mail import models

#: Queued changes that still say what the message should look like here.
OPEN_STATES = (models.MailChangeState.PENDING, models.MailChangeState.FAILED)


def key_parts(message_id: str | None, sender_address: str, date, subject: str, size: int) -> str:  # noqa: ANN001
    """The message key: the Message-ID, else a hash of what identifies the message across folders."""
    if message_id:
        return message_id
    stamp = str(int(date.timestamp())) if date else ""
    return "h:" + hashlib.sha256(f"{sender_address}\n{stamp}\n{subject}\n{size}".encode()).hexdigest()[:40]


def key_of(message: models.Message) -> str:
    return key_parts(message.message_id, message.sender_address, message.date, message.subject, message.size)


def apply(flags: Iterable[str], add: Iterable[str], remove: Iterable[str]) -> set[str]:
    """``flags`` with ``remove`` taken out and ``add`` put in (keywords compare case-insensitively)."""
    gone = {f.lower() for f in remove}
    out = {f for f in flags if f.lower() not in gone}
    have = {f.lower() for f in out}
    out |= {f for f in add if f.lower() not in have}
    return out


def _keyword_categories(account_ids: set[int]) -> dict[int, dict[str, int]]:
    by_account: dict[int, dict[str, int]] = defaultdict(dict)
    for category_id, account_id, keyword in models.Category.objects.filter(account_id__in=account_ids, sync=models.CategorySync.KEYWORD).values_list("id", "account_id", "keyword"):
        by_account[account_id][keyword.lower()] = category_id
    return by_account


def materialize(messages: Iterable[models.Message]) -> list[models.Message]:
    """Recompute ``flags`` and ``category_ids`` of ``messages``; saves and returns the rows that changed."""
    rows = [m for m in messages]
    if not rows:
        return []
    account_ids = {m.account_id for m in rows}
    keys = {m.message_key for m in rows if m.message_key}
    pins = {(p.account_id, p.message_key): p for p in models.LocalPin.objects.filter(account_id__in=account_ids, message_key__in=keys)}
    changes = {c.message_id: c for c in models.MailChange.objects.filter(message_id__in=[m.id for m in rows], kind=models.MailChangeKind.FLAGS, state__in=OPEN_STATES)}
    keyword_categories = _keyword_categories(account_ids)
    assigned: dict[tuple[int, str], set[int]] = defaultdict(set)
    for account_id, key, category_id in models.CategoryAssignment.objects.filter(account_id__in=account_ids, message_key__in=keys, category__sync=models.CategorySync.LOCAL).values_list("account_id", "message_key", "category_id"):
        assigned[(account_id, key)].add(category_id)

    dirty = []
    for row in rows:
        flags = set(row.server_flags)
        pin = pins.get((row.account_id, row.message_key))
        if pin is not None:
            flags = apply(flags, pin.add, pin.remove)
        change = changes.get(row.id)
        if change is not None:
            flags = apply(flags, change.add, change.remove)
        by_keyword = keyword_categories.get(row.account_id, {})
        categories = assigned.get((row.account_id, row.message_key), set()) | {by_keyword[f.lower()] for f in flags if f.lower() in by_keyword}
        new_flags, new_categories = sorted(flags), sorted(categories)
        if new_flags != sorted(row.flags) or new_categories != sorted(row.category_ids):
            row.flags, row.category_ids = new_flags, new_categories
            dirty.append(row)
    models.Message.objects.bulk_update(dirty, ["flags", "category_ids"], batch_size=500)
    return dirty


def materialize_keys(account_id: int, keys: Iterable[str]) -> list[models.Message]:
    """:func:`materialize` every row (every copy) of these message keys."""
    keys = [k for k in set(keys) if k]
    if not keys:
        return []
    return materialize(models.Message.objects.filter(account_id=account_id, message_key__in=keys).only("id", "account_id", "message_key", "flags", "server_flags", "category_ids"))


def materialize_category(category: models.Category) -> list[models.Message]:
    """:func:`materialize` every message that is, or by keyword might be, in ``category``."""
    rows = models.Message.objects.filter(account_id=category.account_id).filter(Q(category_ids__contains=[category.id]) | Q(flags__contains=[category.keyword]) | Q(server_flags__contains=[category.keyword]) | Q(message_key__in=category.assignments.values("message_key")))
    return materialize(rows.only("id", "account_id", "message_key", "flags", "server_flags", "category_ids"))


def recount(folder_ids: Iterable[int]) -> None:
    """Unread messages per folder, as they are here (local changes included, deleted ones not)."""
    for folder_id in {f for f in folder_ids if f}:
        unread = models.Message.objects.filter(folder_id=folder_id, deleted_at=None).exclude(flags__contains=["\\Seen"]).count()
        models.MailFolder.objects.filter(id=folder_id).update(unread_count=unread)
