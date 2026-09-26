"""The actions rekuest schedules, run as rekuest would (one pass each)."""

import pytest

from mail import models
from mail.scheduled import purge_orphaned_stores, reembed_stale, sync_all_mailboxes
from rekuest_service import registered

pytestmark = pytest.mark.django_db(transaction=True)


def test_actions_are_registered_with_defaults(settings):
    actions = registered()
    assert {"sync_all_mailboxes", "reembed_stale", "purge_orphaned_stores"} <= set(actions)
    assert actions["sync_all_mailboxes"].default_interval == 300


async def test_sync_all_mailboxes(mailbox, greenmail):
    one, two = await mailbox(), await mailbox()
    greenmail.deliver(one["address"], "a")
    greenmail.deliver(two["address"], "b")
    greenmail.wait_for(one["address"], 1)
    greenmail.wait_for(two["address"], 1)
    await models.MailAccount.objects.filter(id=two["id"]).aupdate(status=models.MailAccountStatus.DISABLED)
    result = await sync_all_mailboxes()
    assert result == {"synced": 1, "skipped": 0, "failed": 0, "created": 1}
    # A mailbox a user is syncing right now is skipped, not failed.
    await models.MailAccount.objects.filter(id=two["id"]).aupdate(status=models.MailAccountStatus.ACTIVE)
    from asgiref.sync import sync_to_async

    from mail.sync import claim

    assert await sync_to_async(claim)(int(two["id"]))
    result = await sync_all_mailboxes()
    assert result["skipped"] == 1 and result["synced"] == 1


async def test_reembed_and_purge_are_noops_when_nothing_is_stale():
    assert await reembed_stale() == {"reembedded": 0}
    assert await purge_orphaned_stores() == {"purged": 0}
