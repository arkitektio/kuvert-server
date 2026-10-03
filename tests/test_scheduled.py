"""The actions of kuvert's HookAgent, run as rekuest would: one pass each, for one organization."""

from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from datalayer.models import BigFileStore
from kuvert_server.hook_agent import agent
from kuvert_server.service import service
from mail import models
from mail.scheduled import purge_orphaned_stores, reembed_stale, sync_all_mailboxes

pytestmark = pytest.mark.django_db(transaction=True)

MINE, THEIRS = "static_org", "other_org"


def test_actions_are_offered_and_wired_to_nothing(settings):
    actions = agent.actions
    assert {"sync_all_mailboxes", "flush_mail_changes", "reembed_stale", "purge_orphaned_stores"} <= set(actions)
    # An organization schedules them for itself, so each is handed the organization it runs for.
    assert all(action.takes_organization for action in actions.values())
    assert all(set(action) == {"interface", "name", "description"} for action in agent.manifest()["actions"])


def test_the_service_and_the_agent_are_separate_declarations():
    assert "actions" not in service.manifest() and not hasattr(service, "action")


async def test_sync_all_mailboxes(mailbox, greenmail):
    one, two = await mailbox(), await mailbox()
    greenmail.deliver(one["address"], "a")
    greenmail.deliver(two["address"], "b")
    greenmail.wait_for(one["address"], 1)
    greenmail.wait_for(two["address"], 1)
    await models.MailAccount.objects.filter(id=two["id"]).aupdate(status=models.MailAccountStatus.DISABLED)
    result = await sync_all_mailboxes(MINE)
    assert result == {"synced": 1, "skipped": 0, "failed": 0, "created": 1}
    # A mailbox a user is syncing right now is skipped, not failed.
    await models.MailAccount.objects.filter(id=two["id"]).aupdate(status=models.MailAccountStatus.ACTIVE)
    from mail.sync import claim

    assert await sync_to_async(claim)(int(two["id"]))
    result = await sync_all_mailboxes(MINE)
    assert result["skipped"] == 1 and result["synced"] == 1


async def test_reembed_and_purge_are_noops_when_nothing_is_stale():
    assert await reembed_stale(MINE) == {"reembedded": 0}
    assert await purge_orphaned_stores(MINE) == {"purged": 0}


# --- One organization's run does one organization's work -------------------------------------


@pytest.fixture
async def boxes(mailbox, greenmail, other_org_context):
    """A mailbox with one message waiting on the server, in each of two organizations."""
    mine, theirs = await mailbox(), await mailbox(context=other_org_context)
    for box in (mine, theirs):
        greenmail.deliver(box["address"], "Hello")
        greenmail.wait_for(box["address"], 1)
    return mine, theirs


def _messages(box: dict):
    return models.Message.objects.filter(account_id=box["id"])


async def test_a_sync_for_one_organization_touches_only_its_mailboxes(boxes):
    mine, theirs = boxes

    assert await sync_all_mailboxes("nobody") == {"synced": 0, "skipped": 0, "failed": 0, "created": 0}
    assert await sync_all_mailboxes(THEIRS) == {"synced": 1, "skipped": 0, "failed": 0, "created": 1}
    assert await _messages(theirs).acount() == 1
    assert await _messages(mine).acount() == 0

    assert await sync_all_mailboxes(MINE) == {"synced": 1, "skipped": 0, "failed": 0, "created": 1}
    assert await _messages(mine).acount() == 1


async def test_a_reembed_for_one_organization_embeds_only_its_messages(boxes):
    mine, theirs = boxes
    await sync_all_mailboxes(MINE)
    await sync_all_mailboxes(THEIRS)
    await models.Message.objects.all().aupdate(embedding_model="another-model")

    assert await reembed_stale(THEIRS) == {"reembedded": 1}
    assert (await _messages(mine).aget()).embedding_model == "another-model"
    assert (await _messages(theirs).aget()).embedding_model != "another-model"

    assert await reembed_stale(MINE) == {"reembedded": 1}
    assert (await _messages(mine).aget()).embedding_model != "another-model"


async def test_a_purge_for_one_organization_deletes_only_its_stores(boxes, datalayer):
    mine, theirs = boxes
    await sync_all_mailboxes(MINE)
    await sync_all_mailboxes(THEIRS)
    my_raw, their_raw = (await _messages(mine).aget()).raw_id, (await _messages(theirs).aget()).raw_id
    assert my_raw and their_raw
    # Both messages are gone for longer than the grace period.
    await models.Message.objects.all().adelete()
    await BigFileStore.objects.filter(id__in=[my_raw, their_raw]).aupdate(orphaned_at=timezone.now() - timedelta(days=2))

    assert await purge_orphaned_stores(THEIRS) == {"purged": 1}
    assert not await BigFileStore.objects.filter(id=their_raw).aexists()
    assert await BigFileStore.objects.filter(id=my_raw).aexists()

    assert await purge_orphaned_stores(MINE) == {"purged": 1}
    assert not await BigFileStore.objects.filter(id=my_raw).aexists()
