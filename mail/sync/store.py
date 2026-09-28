"""Writing fetched messages into the database, and removing the ones the server no longer has.

Parsing and the datalayer uploads happen before the transaction (they are slow and touch no
rows); the rows of one chunk are written in one atomic block. A message that is already stored
(same folder, UIDVALIDITY and UID -- or UIDL) is skipped, so a retried chunk is harmless.

A fetched message *adopts* a row of its folder with the same message key that has no valid UID
there -- one moved here locally whose move reached the server without COPYUID, or one kept
through a UIDVALIDITY reset -- instead of adding a second row: the row keeps its id, and whatever
was changed on it here. New rows get their local state (pins, categories, see
:mod:`mail.overlay`) laid over the server's flags right away.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from mail import models, overlay, storage, threads
from mail.parse import ParsedMessage, parse, parse_headers

logger = logging.getLogger(__name__)


@dataclass
class Fetched:
    """One message as a protocol returned it."""

    raw: bytes
    size: int
    flags: list[str]
    uid: int | None = None
    uidvalidity: int | None = None
    uidl: str | None = None
    received_at: datetime | None = None
    truncated: bool = False  # ``raw`` holds the headers only


@dataclass
class Prepared:
    fetched: Fetched
    parsed: ParsedMessage
    raw_store: object | None
    attachment_stores: list[object | None]


def max_message_bytes() -> int:
    """Messages larger than this are stored with their headers only."""
    return int(settings.KUVERT_SYNC["max_message_bytes"])


def prepare(account: models.MailAccount, fetched: Fetched) -> Prepared:
    """Parse ``fetched`` and put its raw bytes and attachments in the datalayer (when there is one)."""
    try:
        parsed = parse_headers(fetched.raw) if fetched.truncated else parse(fetched.raw)
    except Exception:
        logger.warning("Could not parse message uid=%s uidl=%s of mailbox %s; storing it bare.", fetched.uid, fetched.uidl, account.pk, exc_info=True)
        parsed = ParsedMessage()
    raw_store = None
    stores: list[object | None] = []
    if storage.enabled():
        raw_store = storage.store_bytes(account.organization_id, account.creator_id, f"{parsed.message_id or 'message'}.eml", fetched.raw, "message/rfc822")
        for attachment in parsed.attachments:
            stores.append(storage.store_bytes(account.organization_id, account.creator_id, attachment.filename or f"attachment-{attachment.position}", attachment.payload, attachment.content_type))
    return Prepared(fetched, parsed, raw_store, stores)


def write(account: models.MailAccount, folder: models.MailFolder, prepared: list[Prepared]) -> list[models.Message]:
    """Create the rows of one chunk (attachments, threads) in one transaction; returns the new messages."""
    created: list[models.Message] = []
    adopted: list[models.Message] = []
    with transaction.atomic():
        for item in prepared:
            fetched, parsed = item.fetched, item.parsed
            stores = [getattr(item.raw_store, "pk", None), *[getattr(s, "pk", None) for s in item.attachment_stores]]
            existing = models.Message.objects.filter(folder=folder)
            if fetched.uidl is not None:
                existing = existing.filter(uidl=fetched.uidl)
            else:
                existing = existing.filter(uidvalidity=fetched.uidvalidity, uid=fetched.uid)
            if existing.exists():
                storage.orphan(stores)
                continue
            key = overlay.key_parts(parsed.message_id, parsed.sender_address[:320], parsed.date or fetched.received_at, parsed.subject[:10000], fetched.size)
            if fetched.uidl is None:
                waiting = (
                    models.Message.objects.select_for_update(of=("self",))
                    .filter(folder=folder, message_key=key)
                    .filter(Q(uid__isnull=True) | ~Q(uidvalidity=fetched.uidvalidity))
                    .exclude(changes__kind=models.MailChangeKind.MOVE)  # its move is still to be pushed
                    .order_by("id")
                    .first()
                )
                if waiting is not None:
                    waiting.uid, waiting.uidvalidity, waiting.server_flags = fetched.uid, fetched.uidvalidity, fetched.flags
                    models.Message.objects.filter(id=waiting.id).update(uid=fetched.uid, uidvalidity=fetched.uidvalidity, server_flags=fetched.flags)
                    adopted.append(waiting)
                    storage.orphan(stores)
                    continue
            message = models.Message(
                account=account,
                folder=folder,
                uid=fetched.uid,
                uidvalidity=fetched.uidvalidity,
                uidl=fetched.uidl,
                message_id=parsed.message_id,
                message_key=key,
                in_reply_to=parsed.in_reply_to,
                references=parsed.references,
                subject=parsed.subject[:10000],
                sender_name=parsed.sender_name[:500],
                sender_address=parsed.sender_address[:320],
                reply_to=parsed.reply_to,
                to=parsed.to,
                cc=parsed.cc,
                bcc=parsed.bcc,
                date=parsed.date or fetched.received_at,
                received_at=fetched.received_at,
                snippet=parsed.snippet,
                text_body=parsed.text_body,
                html_body=parsed.html_body,
                has_remote_images=parsed.has_remote_images,
                size=fetched.size,
                flags=fetched.flags,
                server_flags=fetched.flags,
                has_attachments=parsed.has_attachments,
                truncated=fetched.truncated,
                raw=item.raw_store,
            )
            message.save()  # embeds subject, sender and text (EmbeddedDescriptionMixin)
            models.Attachment.objects.bulk_create(
                [
                    models.Attachment(
                        message=message,
                        position=attachment.position,
                        filename=attachment.filename,
                        content_type=attachment.content_type,
                        size=attachment.size,
                        content_id=attachment.content_id,
                        inline=attachment.inline,
                        store=item.attachment_stores[index] if index < len(item.attachment_stores) else None,
                    )
                    for index, attachment in enumerate(parsed.attachments)
                ]
            )
            threads.assign(message)
            created.append(message)
        overlay.materialize([*created, *adopted])
    return created


def delete(messages: "models.QuerySet[models.Message]") -> int:
    """Delete messages (gone on the server), orphan their stores and recount their threads."""
    rows = list(messages.values_list("id", "thread_id", "raw_id"))
    if not rows:
        return 0
    ids = [row[0] for row in rows]
    store_ids = [row[2] for row in rows] + list(models.Attachment.objects.filter(message_id__in=ids).values_list("store_id", flat=True))
    with transaction.atomic():
        models.Message.objects.filter(id__in=ids).delete()
        threads.refresh_many({row[1] for row in rows})
        storage.orphan(store_ids)
    return len(rows)
