"""Syncing a mailbox: the lease, one protocol pass in a worker thread, and the outcome.

Concurrency (as in bank): a sync first claims the mailbox with a lease (``sync_lease_until``) in
one conditional UPDATE -- exactly one caller wins, on any number of replicas; a crashed sync
frees the mailbox when its lease runs out. The pass itself is blocking network I/O, so it runs in
a worker thread of its own (never the shared thread Django's async ORM calls use), and writes
each chunk of messages in its own short transaction.

Request/response only: a sync runs when a client asks (``syncMailAccount``) or when the hub's
rekuest runs the scheduled ``sync_all_mailboxes`` action; nothing loops or waits here.
"""

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterator

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from django.conf import settings
from django.db import connections
from django.db.models import Q
from django.utils import timezone

from mail import accounts, models
from mail.errors import AlreadySyncing, SyncTooSoon, code_for
from mail.protocols.clients import open_imap, open_pop3

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """What one mailbox sync did."""

    account_id: int
    organization_id: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    folders: int = 0
    more: bool = False
    new_messages: list[int] = field(default_factory=list)


def claim(account_id: int) -> bool:
    """Take the mailbox's lease; False if another sync holds it."""
    now = timezone.now()
    lease = int(settings.KUVERT_SYNC["lease_seconds"])
    return bool(
        models.MailAccount.objects.filter(id=account_id)
        .filter(Q(sync_lease_until__isnull=True) | Q(sync_lease_until__lt=now))
        .update(sync_lease_until=now + timedelta(seconds=lease))
    )


def renew(account_id: int) -> None:
    """Extend the lease of a sync that is still running (it holds the lease, so nobody else does)."""
    lease = int(settings.KUVERT_SYNC["lease_seconds"])
    models.MailAccount.objects.filter(id=account_id).update(sync_lease_until=timezone.now() + timedelta(seconds=lease))


def release(account_id: int) -> None:
    """Give the lease back."""
    models.MailAccount.objects.filter(id=account_id).update(sync_lease_until=None)


def begin(account_id: int) -> None:
    """Check the mailbox may sync now and take its lease (raises MAILBOX_INACTIVE, RATE_LIMITED, SYNC_IN_PROGRESS)."""
    account = models.MailAccount.objects.get(id=account_id)
    accounts.ensure_active(account)
    interval = int(settings.KUVERT_SYNC.get("min_interval_seconds") or 0)
    if interval and account.last_synced_at:
        allowed_at = account.last_synced_at + timedelta(seconds=interval)
        if allowed_at > timezone.now():
            raise SyncTooSoon(allowed_at)
    if not claim(account_id):
        raise AlreadySyncing()


def record_failure(account_id: int, error: BaseException) -> None:
    """Remember why the sync failed; credentials that stopped working set NEEDS_REAUTH."""
    code = code_for(error)
    updates = {"last_error": str(error)[:2000] or type(error).__name__, "last_error_code": code}
    if code in (models.MailErrorCode.AUTH_FAILED, models.MailErrorCode.CONSENT_EXPIRED):
        updates["status"] = models.MailAccountStatus.NEEDS_REAUTH
    models.MailAccount.objects.filter(id=account_id).update(**updates)


def _mark_synced(account_id: int) -> None:
    backfill_done = not models.MailFolder.objects.filter(account_id=account_id, sync_enabled=True, selectable=True, backfill_done=False).exists()
    models.MailAccount.objects.filter(id=account_id).update(last_synced_at=timezone.now(), last_error=None, last_error_code=None, backfill_done=backfill_done)


@contextmanager
def incoming_session(account: models.MailAccount) -> Iterator[object]:
    """A logged-in client of the mailbox's incoming server (IMAP or POP3), logged out afterwards."""
    endpoint, credentials = accounts.incoming_endpoint(account), accounts.incoming_credentials(account)
    if account.protocol == models.Protocol.POP3:
        client = open_pop3(endpoint, credentials)
        try:
            yield client
        finally:
            try:
                client.quit()  # also commits DELEs
            except Exception:
                client.close()
    else:
        client = open_imap(endpoint, credentials)
        try:
            yield client
        finally:
            try:
                client.logout()
            except Exception:
                client.shutdown()


def run(account_id: int, folder_ids: list[int] | None = None) -> SyncResult:
    """One blocking pass over the mailbox (the caller holds the lease; run it :func:`in_worker`)."""
    account = models.MailAccount.objects.get(id=account_id)
    with incoming_session(account) as client:
        if account.protocol == models.Protocol.POP3:
            from mail.sync import pop3

            outcome = pop3.sync(client, account)  # type: ignore[arg-type]
        else:
            from mail.sync import imap

            outcome = imap.sync(client, account, folder_ids)  # type: ignore[arg-type]
    _mark_synced(account_id)
    return SyncResult(
        account_id=account_id,
        organization_id=account.organization_id,
        created=outcome.created,
        updated=outcome.updated,
        deleted=outcome.deleted,
        folders=outcome.folders,
        more=outcome.more,
        new_messages=outcome.new_messages,
    )


def in_worker(fn, *args, **kwargs):  # noqa: ANN001, ANN201
    """Run blocking ``fn`` (network and ORM) in a worker thread of its own, closing its DB connections after."""

    def call():  # noqa: ANN202
        try:
            return fn(*args, **kwargs)
        finally:
            connections.close_all()  # this thread is not one of Django's: close what it opened

    return sync_to_async(call, thread_sensitive=False)()


async def sync_account(account_id: int, folder_ids: list[int] | None = None) -> SyncResult:
    """Sync a mailbox now: claim it, run one pass, record the outcome, tell subscribers."""
    from mail.channels import broadcast_sync

    await database_sync_to_async(begin)(account_id)
    try:
        result = await in_worker(run, account_id, folder_ids)
    except BaseException as error:
        await database_sync_to_async(record_failure)(account_id, error)
        logger.info("Sync of mailbox %s failed: %s", account_id, error)
        raise
    finally:
        await database_sync_to_async(release)(account_id)
    logger.info("Synced mailbox %s: %s new, %s updated, %s deleted", account_id, result.created, result.updated, result.deleted)
    await database_sync_to_async(broadcast_sync)(result)
    return result
