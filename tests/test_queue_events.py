"""The queue of changes as a whole: counts past a page, bulk push and retry, and live events.

Also the login's live progress (``authSession`` subscription). Real GreenMail and fakeoauth.
"""

from datetime import timedelta

import pytest
from authentikate.models import Organization
from django.utils import timezone

from mail import models
from mail.channels import auth_group, changes_group

pytestmark = pytest.mark.django_db(transaction=True)

MOVE = 'mutation($ids: [ID!]!, $f: ID!) { moveMessages(input: {messages: $ids, folder: $f}) { id } }'
READ = 'mutation($ids: [ID!]!) { markMessagesRead(input: {messages: $ids, read: true}) { id } }'
COUNT = 'query($f: MailChangeFilter) { mailChangesCount(filters: $f) }'
PUSH_ALL = 'mutation($a: [ID!]) { pushAllMailChanges(accounts: $a) { account { id } pushed pending failed } }'
CHANGES = 'subscription($a: ID) { mailChanges(account: $a) { accountId pending failed pushed } }'
SESSION = "state status errorCode"
AUTH = "subscription($s: String!) { authSession(state: $s) { %s } }" % SESSION
START = "mutation($input: StartOAuthLinkInput!) { startOAuthLink(input: $input) { %s } }" % SESSION
COMPLETE = "mutation($state: String!, $error: String) { completeAuth(input: {state: $state, error: $error}) { %s } }" % SESSION


async def _box(mailbox, greenmail, sync, subjects=("One",)) -> dict:  # noqa: ANN001
    box = await mailbox()
    with greenmail.imap(box["address"]) as imap:
        imap.create_folder("Archive")
    for subject in subjects:
        greenmail.deliver(box["address"], subject)
    greenmail.wait_for(box["address"], len(subjects))
    await sync(box["id"])
    messages = {m.subject: str(m.id) async for m in models.Message.objects.filter(account_id=box["id"])}
    folders = {f.path: str(f.id) async for f in models.MailFolder.objects.filter(account_id=box["id"])}
    return {**box, "messages": messages, "folders": folders}


async def _org_group() -> str:
    return changes_group((await Organization.objects.aget(identifier="static_issuer#static_org")).id)


async def test_folders_filter_by_name_selectable_and_existence(mailbox, greenmail, sync, aexecute):
    box = await _box(mailbox, greenmail, sync)
    await models.MailFolder.objects.filter(account_id=box["id"], path="Archive").aupdate(exists_on_server=False)
    query = 'query($f: MailFolderFilter) { mailFolders(filters: $f) { path } }'

    async def paths(**filters) -> set[str]:  # noqa: ANN003
        rows = (await aexecute(query, {"f": {"account": box["id"], **filters}})).data["mailFolders"]
        return {r["path"] for r in rows}

    assert await paths(search="arch") == {"Archive"}
    assert await paths(existsOnServer=False) == {"Archive"}
    assert "Archive" not in await paths(existsOnServer=True)
    assert await paths(selectable=False) == set()
    assert {"INBOX", "Archive"} <= await paths(selectable=True)


async def test_counts_and_a_bulk_push_cover_every_mailbox(mailbox, greenmail, sync, aexecute, settings):
    settings.KUVERT_WRITEBACK = {**settings.KUVERT_WRITEBACK, "undo_seconds_moves": 600}
    first, second = await _box(mailbox, greenmail, sync), await _box(mailbox, greenmail, sync)
    for box in (first, second):
        await aexecute(MOVE, {"ids": [box["messages"]["One"]], "f": box["folders"]["Archive"]})

    assert (await aexecute(COUNT, {"f": {"accounts": [first["id"], second["id"]]}})).data["mailChangesCount"] == 2
    assert (await aexecute(COUNT, {"f": {"accounts": [first["id"]], "state": "PENDING"}})).data["mailChangesCount"] == 1

    # Still in their undo window: nothing is pushed, both stay pending.
    waiting = (await aexecute(PUSH_ALL)).data["pushAllMailChanges"]
    assert sorted((r["account"]["id"], r["pushed"], r["pending"]) for r in waiting) == sorted([(first["id"], 0, 1), (second["id"], 0, 1)])

    await models.MailChange.objects.aupdate(push_after=timezone.now() - timedelta(seconds=1))  # the window ran out
    pushed = (await aexecute(PUSH_ALL)).data["pushAllMailChanges"]
    assert sorted((r["account"]["id"], r["pushed"], r["pending"], r["failed"]) for r in pushed) == sorted([(first["id"], 1, 0, 0), (second["id"], 1, 0, 0)])
    assert (await aexecute(PUSH_ALL)).data["pushAllMailChanges"] == []  # nothing pending anywhere
    assert (await aexecute(COUNT)).data["mailChangesCount"] == 0


async def test_a_bulk_push_only_reaches_visible_mailboxes(mailbox, greenmail, sync, aexecute, colleague_context):
    box = await _box(mailbox, greenmail, sync)
    hidden = await aexecute(PUSH_ALL, {"a": [box["id"]]}, context=colleague_context, allow_errors=True)
    assert hidden.errors[0].extensions["code"] == "NOT_FOUND"
    assert (await aexecute(PUSH_ALL, context=colleague_context)).data["pushAllMailChanges"] == []


async def test_retry_takes_every_failed_change_of_a_mailbox(mailbox, greenmail, sync, aexecute):
    box = await _box(mailbox, greenmail, sync, subjects=("One", "Two"))
    ids = [box["messages"]["One"], box["messages"]["Two"]]
    await aexecute(MOVE, {"ids": ids, "f": box["folders"]["Archive"]})
    with greenmail.imap(box["address"]) as imap:
        greenmail.recreate_folder(imap, "Archive", "Archive3")  # the moved mail now lives elsewhere
    await aexecute('mutation($ids: [ID!]!) { setMessageFlags(input: {messages: $ids, add: ["\\\\Flagged"]}) { id } }', {"ids": ids})
    failed = [c async for c in models.MailChange.objects.filter(account_id=box["id"], state=models.MailChangeState.FAILED)]
    assert len(failed) == 2

    retried = (await aexecute('mutation($a: ID) { retryMailChanges(account: $a) { id attempts } }', {"a": box["id"]})).data["retryMailChanges"]
    # Both were queued again and pushed at once; the message is still gone, so each failed once more (attempts start over).
    assert sorted(r["id"] for r in retried) == sorted(str(c.id) for c in failed)
    assert all(r["attempts"] == 1 for r in retried)

    nothing = await aexecute("mutation { retryMailChanges { id } }", allow_errors=True)
    assert nothing.errors[0].extensions["code"] == "VALIDATION_ERROR"


async def test_queue_events_reach_the_mailboxs_watchers_only(mailbox, greenmail, sync, aexecute, subscribe, settings):
    settings.KUVERT_WRITEBACK = {**settings.KUVERT_WRITEBACK, "undo_seconds_moves": 600}
    box, other = await _box(mailbox, greenmail, sync), await _box(mailbox, greenmail, sync)
    group = await _org_group()
    mine = await subscribe(CHANGES, {"a": box["id"]}, group=group)
    everything = await subscribe(CHANGES, group=group)
    colleague = await subscribe(CHANGES, token="colleague", group=group)  # same organization, but the mailboxes are private

    # Queued in its undo window: pending, nothing pushed.
    await aexecute(MOVE, {"ids": [box["messages"]["One"]], "f": box["folders"]["Archive"]})
    assert (await mine.next())["mailChanges"] == {"accountId": box["id"], "pending": 1, "failed": 0, "pushed": 0}
    assert (await everything.next())["mailChanges"]["accountId"] == box["id"]

    # Another mailbox: only the unfiltered watcher hears of it.
    await aexecute(READ, {"ids": [other["messages"]["One"]]})
    assert (await everything.next())["mailChanges"] == {"accountId": other["id"], "pending": 0, "failed": 0, "pushed": 1}
    assert await mine.silent()

    # Undone: the queue is empty again.
    await aexecute('mutation($ids: [ID!]!) { undoMailChanges(input: {messages: $ids}) { id } }', {"ids": [box["messages"]["One"]]})
    assert (await mine.next())["mailChanges"] == {"accountId": box["id"], "pending": 0, "failed": 0, "pushed": 0}
    assert await colleague.silent()


async def test_a_failed_push_is_announced(mailbox, greenmail, sync, aexecute, subscribe):
    box = await _box(mailbox, greenmail, sync)
    await aexecute(MOVE, {"ids": [box["messages"]["One"]], "f": box["folders"]["Archive"]})
    with greenmail.imap(box["address"]) as imap:
        greenmail.recreate_folder(imap, "Archive", "Archive3")
    watcher = await subscribe(CHANGES, {"a": box["id"]}, group=await _org_group())
    await aexecute('mutation($ids: [ID!]!) { setMessageFlags(input: {messages: $ids, add: ["\\\\Flagged"]}) { id } }', {"ids": [box["messages"]["One"]]})
    assert (await watcher.next())["mailChanges"] == {"accountId": box["id"], "pending": 0, "failed": 1, "pushed": 0}


async def test_a_login_is_watched_until_it_settles(aexecute, fakeoauth, subscribe):
    session = (await aexecute(START, {"input": {"provider": fakeoauth.gmail}})).data["startOAuthLink"]
    watcher = await subscribe(AUTH, {"s": session["state"]}, group=auth_group(session["state"]))
    assert (await watcher.next())["authSession"] == {"state": session["state"], "status": "PENDING", "errorCode": None}

    await aexecute(COMPLETE, {"state": session["state"], "error": "access_denied"})
    assert (await watcher.next())["authSession"] == {"state": session["state"], "status": "FAILED", "errorCode": "PROVIDER_ERROR"}
    await watcher.task  # settled: the stream ends


async def test_an_expiring_login_reads_expired_without_an_announcement(aexecute, fakeoauth, subscribe):
    session = (await aexecute(START, {"input": {"provider": fakeoauth.gmail}})).data["startOAuthLink"]
    await models.OAuthLink.objects.filter(state=session["state"]).aupdate(expires_at=timezone.now() + timedelta(seconds=1))
    watcher = await subscribe(AUTH, {"s": session["state"]})
    assert (await watcher.next())["authSession"]["status"] == "PENDING"
    assert (await watcher.next(timeout=5))["authSession"] == {"state": session["state"], "status": "EXPIRED", "errorCode": "CODE_EXPIRED"}


async def test_only_the_starter_watches_a_login(aexecute, fakeoauth, subscribe):
    session = (await aexecute(START, {"input": {"provider": fakeoauth.gmail}})).data["startOAuthLink"]
    for token in ("colleague", "othertest"):
        stranger = await subscribe(AUTH, {"s": session["state"]}, token=token)
        assert (await stranger.error()).extensions["code"] == "INVALID_STATE"
