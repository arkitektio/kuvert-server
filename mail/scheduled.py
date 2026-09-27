"""The work the hub's rekuest schedules (vendored ``rekuest_service``).

* ``sync_all_mailboxes`` -- one sync pass of every ACTIVE mailbox (default every
  ``sync.scheduled_every_seconds``). A mailbox a user is syncing right now is skipped; its lease
  is the same one.
* ``reembed_stale`` -- embed messages whose vector is missing or from another model.
* ``purge_orphaned_stores`` -- delete raw messages and attachments nothing references any more.

Nothing here loops or waits: each run is one pass, started by rekuest, and a run lost to a crash
is simply followed by the next one.
"""

import logging

from asgiref.sync import sync_to_async
from django.conf import settings

from mail import models
from mail.errors import AlreadySyncing, SyncTooSoon
from mail.sync import sync_account
from kuvert_server.service import service

logger = logging.getLogger(__name__)


def _active_accounts() -> list[int]:
    return list(models.MailAccount.objects.filter(status=models.MailAccountStatus.ACTIVE).order_by("last_synced_at", "id").values_list("id", flat=True))


@service.action(
    interface="sync_all_mailboxes",
    name="Sync all mailboxes",
    description="Sync every active mailbox once: new mail, flags, deletions, and the next part of a backfill.",
    default_interval=settings.KUVERT_SYNC.get("scheduled_every_seconds"),
)
async def sync_all_mailboxes() -> dict:
    synced = skipped = failed = created = 0
    for account_id in await sync_to_async(_active_accounts)():
        try:
            result = await sync_account(account_id)
            synced += 1
            created += result.created
        except (AlreadySyncing, SyncTooSoon):
            skipped += 1
        except Exception as error:  # recorded on the mailbox by sync_account; one line here
            failed += 1
            logger.warning("Scheduled sync of mailbox %s failed: %s", account_id, error)
    return {"synced": synced, "skipped": skipped, "failed": failed, "created": created}


def _reembed() -> int:
    from embeddings import engine
    from embeddings.healer import reembed_all

    if not engine.enabled():
        return 0
    return reembed_all([models.Message], max_batches=50)


def _reembed_interval() -> int | None:
    embeddings = getattr(settings, "EMBEDDINGS", {})
    return embeddings.get("SWEEP_INTERVAL") if embeddings.get("ENABLED", True) else None


@service.action(
    interface="reembed_stale",
    name="Re-embed stale messages",
    description="Embed messages whose vector is missing or came from another model (after a model change, or when the model was unavailable at sync time).",
    default_interval=_reembed_interval(),
)
async def reembed_stale() -> dict:
    return {"reembedded": await sync_to_async(_reembed)()}


@service.action(
    interface="purge_orphaned_stores",
    name="Purge orphaned files",
    description="Delete stored raw messages and attachments whose messages are gone for more than a day.",
    default_interval=6 * 3600 if getattr(settings, "DATALAYER", None) else None,
)
async def purge_orphaned_stores() -> dict:
    from mail import storage

    return {"purged": await sync_to_async(storage.purge_orphans)()}
