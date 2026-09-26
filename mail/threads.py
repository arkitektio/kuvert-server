"""Grouping messages into conversations (a simplified RFC 5256 REFERENCES).

A new message joins the thread of

1. any message of the mailbox whose Message-ID is in its In-Reply-To or References; else
2. any message that names *its* Message-ID in In-Reply-To/References (a reply synced first); else
3. for a reply (``Re:``-style subject) with no known parent: the newest thread with the same
   normalized subject that had a message in the last 30 days.

Otherwise it starts a thread. Two threads a new message connects are merged. Runs inside the
sync's transaction; the mailbox lease keeps two syncs of one mailbox from racing here.
"""

from datetime import timedelta

from django.db.models import Count, Max, Q

from mail import models
from mail.parse import normalize_subject

SUBJECT_WINDOW = timedelta(days=30)


def _is_reply(subject: str) -> bool:
    return normalize_subject(subject) != " ".join((subject or "").split()).lower()


def assign(message: models.Message) -> models.Thread:
    """Put ``message`` (saved, without a thread) into a thread and return it."""
    account_messages = models.Message.objects.filter(account_id=message.account_id).exclude(pk=message.pk).exclude(thread=None)
    parents = [ref for ref in [message.in_reply_to, *message.references] if ref]
    thread_ids: set[int] = set()
    if parents:
        thread_ids |= set(account_messages.filter(message_id__in=parents).values_list("thread_id", flat=True))
    if message.message_id:
        thread_ids |= set(account_messages.filter(Q(in_reply_to=message.message_id) | Q(references__contains=[message.message_id])).values_list("thread_id", flat=True))
    if message.message_id:
        # A copy of the same message in another folder belongs to the same thread.
        thread_ids |= set(account_messages.filter(message_id=message.message_id).values_list("thread_id", flat=True))

    subject = normalize_subject(message.subject)
    thread: models.Thread | None = None
    if thread_ids:
        threads = list(models.Thread.objects.filter(id__in=thread_ids).order_by("id"))
        thread = threads[0]
        for other in threads[1:]:
            models.Message.objects.filter(thread=other).update(thread=thread)
            other.delete()
    elif subject and _is_reply(message.subject) and message.date:
        thread = (
            models.Thread.objects.filter(account_id=message.account_id, subject=subject, last_message_at__gte=message.date - SUBJECT_WINDOW)
            .order_by("-last_message_at")
            .first()
        )
    if thread is None:
        thread = models.Thread.objects.create(account_id=message.account_id, subject=subject)
    message.thread = thread
    message.save(update_fields=["thread"])
    refresh(thread.id)
    return thread


def refresh(thread_id: int) -> None:
    """Recount a thread; delete it once it holds no message."""
    stats = models.Message.objects.filter(thread_id=thread_id).aggregate(count=Count("id"), last=Max("date"))
    if not stats["count"]:
        models.Thread.objects.filter(id=thread_id).delete()
        return
    models.Thread.objects.filter(id=thread_id).update(message_count=stats["count"], last_message_at=stats["last"])


def refresh_many(thread_ids: set[int]) -> None:
    """:func:`refresh` each thread (after messages were deleted or moved)."""
    for thread_id in thread_ids:
        if thread_id:
            refresh(thread_id)
