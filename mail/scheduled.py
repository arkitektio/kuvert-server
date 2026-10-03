"""The actions of kuvert's hook agent: work the hub's rekuest can ask for (vendored ``rekuest_hook``).

Every organization has the agent, so each action is handed the slug of the organization the run
is for and does that organization's share of the work, nothing else:

* ``sync_all_mailboxes`` -- one sync pass of every ACTIVE mailbox of the organization. A mailbox a
  user is syncing right now is skipped; its lease is the same one.
* ``flush_mail_changes`` -- push the organization's queued local changes that are due; only
  mailboxes that have some are connected to.
* ``reembed_stale`` -- embed the organization's messages whose vector is missing or from another
  model.
* ``purge_orphaned_stores`` -- delete the organization's raw messages and attachments nothing
  references any more.

The actions are only offered. Nothing here schedules them: whether and how often one runs is the
organization's own automation (a schedule its users set up). Nothing here loops or waits: each
run is one pass, started by rekuest, and a run lost to a crash is simply followed by the next one.
"""

import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone

from mail import models
from mail.errors import AlreadySyncing, SyncTooSoon
from mail.sync import push_account, sync_account
from kuvert_server.hook_agent import agent

logger = logging.getLogger(__name__)


def _active_accounts(organization: str) -> list[int]:
    return list(models.MailAccount.objects.filter(organization__slug=organization, status=models.MailAccountStatus.ACTIVE).order_by("last_synced_at", "id").values_list("id", flat=True))


@agent.action(
    interface="sync_all_mailboxes",
    name="Sync all mailboxes",
    description="Sync every active mailbox of the organization once: new mail, flags, deletions, and the next part of a backfill.",
)
async def sync_all_mailboxes(organization: str) -> dict:
    synced = skipped = failed = created = 0
    for account_id in await sync_to_async(_active_accounts)(organization):
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


def _due_accounts(organization: str) -> list[int]:
    due = models.MailChange.objects.filter(account__organization__slug=organization, state=models.MailChangeState.PENDING, push_after__lte=timezone.now(), account__status=models.MailAccountStatus.ACTIVE)
    return sorted(set(due.values_list("account_id", flat=True)))


@agent.action(
    interface="flush_mail_changes",
    name="Push local changes",
    description="Push the organization's changes made here (read state, flags, categories, moves, deletes) that are due to their mailboxes' servers.",
)
async def flush_mail_changes(organization: str) -> dict:
    pushed = skipped = failed = 0
    for account_id in await sync_to_async(_due_accounts)(organization):
        try:
            result = await push_account(account_id)
        except Exception as error:  # recorded on the mailbox by push_now; one line here
            failed += 1
            logger.warning("Pushing changes of mailbox %s failed: %s", account_id, error)
            continue
        if result is None:
            skipped += 1
        else:
            pushed += result.pushed
    return {"pushed": pushed, "skipped": skipped, "failed": failed}


def _reembed(organization: str) -> int:
    from embeddings import engine
    from embeddings.healer import reembed_all

    if not engine.enabled():
        return 0
    return reembed_all([models.Message], max_batches=50, organization=organization)


@agent.action(
    interface="reembed_stale",
    name="Re-embed stale messages",
    description="Embed the organization's messages whose vector is missing or came from another model (after a model change, or when the model was unavailable at sync time).",
)
async def reembed_stale(organization: str) -> dict:
    return {"reembedded": await sync_to_async(_reembed)(organization)}


@agent.action(
    interface="purge_orphaned_stores",
    name="Purge orphaned files",
    description="Delete the organization's stored raw messages and attachments whose messages are gone for more than a day.",
)
async def purge_orphaned_stores(organization: str) -> dict:
    from mail import storage

    return {"purged": await sync_to_async(storage.purge_orphans)(organization=organization)}
