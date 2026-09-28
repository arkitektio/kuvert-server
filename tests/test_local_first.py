"""Local first: changes show here at once, wait out their undo window, survive syncs, and reach the server only as the mailbox's push settings say.

Real GreenMail (UIDPLUS and MOVE, no CONDSTORE); the server is checked behind the service's back.
"""

import threading

import pytest
from asgiref.sync import sync_to_async
from django.db import connections
from django.utils import timezone

from mail import models, sync as mail_sync
from mail.scheduled import flush_mail_changes

pytestmark = pytest.mark.django_db(transaction=True)

READ = 'mutation($ids: [ID!]!, $read: Boolean!) { markMessagesRead(input: {messages: $ids, read: $read}) { id isRead syncState } }'
MOVE = 'mutation($ids: [ID!]!, $f: ID!) { moveMessages(input: {messages: $ids, folder: $f}) { id folder { path } syncState changes { id kind undoable } } }'
DELETE = 'mutation($ids: [ID!]!, $p: Boolean!) { deleteMessages(input: {messages: $ids, permanent: $p}) { deleted } }'
UNDO = 'mutation($ids: [ID!]!) { undoMailChanges(input: {messages: $ids}) { id folder { path } syncState } }'
SETTINGS = 'mutation($input: UpdateMailAccountInput!) { updateMailAccount(input: $input) { pushSeen pushDeletes } }'


@pytest.fixture
def writeback(settings):
    """Change ``writeback.*`` for one test."""

    def _set(**values) -> None:  # noqa: ANN003
        settings.KUVERT_WRITEBACK = {**settings.KUVERT_WRITEBACK, **values}

    return _set


@pytest.fixture
async def box(mailbox, greenmail, sync):
    box = await mailbox()
    with greenmail.imap(box["address"]) as imap:
        imap.create_folder("Archive")
        imap.create_folder("Trash")
    for subject in ("One", "Two", "Three"):
        greenmail.deliver(box["address"], subject, message_id=f"{subject.lower()}.{box['address']}")
    greenmail.wait_for(box["address"], 3)
    await sync(box["id"])
    ids = {m.subject: str(m.id) async for m in models.Message.objects.filter(account_id=box["id"])}
    folders = {f.path: str(f.id) async for f in models.MailFolder.objects.filter(account_id=box["id"])}
    return {**box, "messages": ids, "folders": folders}


def server(greenmail, address: str, folder: str = "INBOX") -> dict[str, set[str]]:
    """Subject → flags of ``folder`` on the server."""
    with greenmail.imap(address) as imap:
        imap.select_folder(folder, readonly=True)
        uids = imap.search("ALL")
        data = imap.fetch(uids, ["FLAGS", "BODY.PEEK[HEADER.FIELDS (SUBJECT)]"]) if uids else {}
    return {v[b"BODY[HEADER.FIELDS (SUBJECT)]"].decode().split(":", 1)[1].strip(): {f.decode() for f in v[b"FLAGS"]} for v in data.values()}


def store_behind(greenmail, address: str, subject: str, add: list[str] = (), remove: list[str] = (), folder: str = "INBOX") -> None:
    """Another mail client changes flags on the server."""
    with greenmail.imap(address) as imap:
        imap.select_folder(folder)
        uids = imap.search(["SUBJECT", subject])
        if add:
            imap.add_flags(uids, list(add))
        if remove:
            imap.remove_flags(uids, list(remove))


async def test_a_change_shows_at_once_and_reaches_the_server_later(box, aexecute, greenmail, sync, writeback):
    writeback(push_inline=False)
    one = box["messages"]["One"]
    read = (await aexecute(READ, {"ids": [one], "read": True})).data["markMessagesRead"]
    assert read == [{"id": one, "isRead": True, "syncState": "PENDING"}]
    assert "\\Seen" not in server(greenmail, box["address"])["One"]
    folder = (await aexecute('query($id: ID!) { mailFolder(id: $id) { unreadCount serverUnreadCount } }', {"id": box["folders"]["INBOX"]})).data["mailFolder"]
    assert folder == {"unreadCount": 2, "serverUnreadCount": 3}

    # The scheduled flush (or any sync) pushes it.
    assert (await flush_mail_changes())["pushed"] == 1
    assert "\\Seen" in server(greenmail, box["address"])["One"]
    state = (await aexecute('query($id: ID!) { message(id: $id) { syncState serverFlags } }', {"id": one})).data["message"]
    assert state["syncState"] == "SYNCED" and "\\Seen" in state["serverFlags"]


async def test_a_pending_change_survives_a_sync(box, aexecute, greenmail, sync, writeback):
    """Not yet due (an undo window), so the sync pulls the server's flags -- the local change stays on top."""
    writeback(push_inline=False, undo_seconds_flags=3600)
    one = box["messages"]["One"]
    await aexecute(READ, {"ids": [one], "read": True})
    store_behind(greenmail, box["address"], "One", add=["\\Flagged"])  # another client, meanwhile
    await sync(box["id"])
    row = await models.Message.objects.aget(id=one)
    assert set(row.server_flags) - {"\\Recent"} == {"\\Flagged"}
    assert set(row.flags) - {"\\Recent"} == {"\\Flagged", "\\Seen"}
    assert "\\Seen" not in server(greenmail, box["address"])["One"]


async def test_toggling_back_cancels_the_queued_change(box, aexecute, greenmail, writeback):
    writeback(push_inline=False)
    one = box["messages"]["One"]
    await aexecute(READ, {"ids": [one], "read": True})
    back = (await aexecute(READ, {"ids": [one], "read": False})).data["markMessagesRead"]
    assert back[0]["syncState"] == "SYNCED"
    assert not await models.MailChange.objects.filter(account_id=box["id"]).aexists()


async def test_a_move_keeps_its_id_and_can_be_undone(box, aexecute, greenmail, sync, writeback):
    writeback(undo_seconds_moves=3600)
    one = box["messages"]["One"]
    moved = (await aexecute(MOVE, {"ids": [one], "f": box["folders"]["Archive"]})).data["moveMessages"][0]
    assert moved["id"] == one and moved["folder"] == {"path": "Archive"} and moved["syncState"] == "PENDING"
    assert moved["changes"][0]["kind"] == "MOVE" and moved["changes"][0]["undoable"] is True
    assert set(server(greenmail, box["address"])) == {"One", "Two", "Three"}

    # A sync meanwhile neither reads it into INBOX again nor loses it.
    await sync(box["id"])
    assert await models.Message.objects.filter(account_id=box["id"], subject="One").acount() == 1

    undone = (await aexecute(UNDO, {"ids": [one]})).data["undoMailChanges"]
    assert undone == [{"id": one, "folder": {"path": "INBOX"}, "syncState": "SYNCED"}]
    row = await models.Message.objects.aget(id=one)
    assert row.uid is not None
    assert set(server(greenmail, box["address"], "Archive")) == set()


async def test_a_move_pushed_by_a_crashed_run_is_not_pushed_twice(box, aexecute, greenmail, sync, writeback):
    """The server has the move (the answer was lost): the retry finds the message in the target and adopts it."""
    writeback(push_inline=False)
    one = box["messages"]["One"]
    await aexecute(MOVE, {"ids": [one], "f": box["folders"]["Archive"]})
    with greenmail.imap(box["address"]) as imap:  # what the lost push did
        imap.select_folder("INBOX")
        imap.move(imap.search(["SUBJECT", "One"]), "Archive")
    await sync(box["id"])
    assert set(server(greenmail, box["address"], "Archive")) == {"One"}
    rows = [(str(m.id), m.folder_id, m.uid is not None) async for m in models.Message.objects.filter(account_id=box["id"], subject="One")]
    assert rows == [(one, int(box["folders"]["Archive"]), True)]
    assert not await models.MailChange.objects.filter(account_id=box["id"]).aexists()


async def test_a_delete_waits_for_its_undo_window(box, aexecute, greenmail, sync, writeback):
    writeback(undo_seconds_deletes=3600)
    two = box["messages"]["Two"]
    assert (await aexecute(DELETE, {"ids": [two], "p": True})).data["deleteMessages"] == {"deleted": 1}
    listed = (await aexecute('query($a: ID!) { messages(filters: {account: $a}) { subject } }', {"a": box["id"]})).data["messages"]
    assert {m["subject"] for m in listed} == {"One", "Three"}
    await sync(box["id"])  # the server still has it: not read in again
    assert "Two" in server(greenmail, box["address"])
    await aexecute(UNDO, {"ids": [two]})
    listed = (await aexecute('query($a: ID!) { messages(filters: {account: $a}) { subject } }', {"a": box["id"]})).data["messages"]
    assert {m["subject"] for m in listed} == {"One", "Two", "Three"}

    writeback(undo_seconds_deletes=0)
    await aexecute(DELETE, {"ids": [two], "p": True})
    assert "Two" not in server(greenmail, box["address"])
    assert not await models.Message.objects.filter(account_id=box["id"], subject="Two").aexists()


async def test_push_seen_off_keeps_read_state_here(box, aexecute, greenmail, sync):
    await aexecute(SETTINGS, {"input": {"id": box["id"], "pushSeen": False}})
    one, two = box["messages"]["One"], box["messages"]["Two"]
    read = (await aexecute(READ, {"ids": [one], "read": True})).data["markMessagesRead"]
    assert read[0]["isRead"] is True and read[0]["syncState"] == "LOCAL"

    store_behind(greenmail, box["address"], "Two", add=["\\Seen"])  # untouched here: the server's state shows
    store_behind(greenmail, box["address"], "One", add=["\\Flagged"])  # another flag of the pinned one: shows too
    await sync(box["id"])
    assert "\\Seen" not in server(greenmail, box["address"])["One"]
    assert set((await models.Message.objects.aget(id=one)).flags) - {"\\Recent"} == {"\\Seen", "\\Flagged"}
    assert "\\Seen" in (await models.Message.objects.aget(id=two)).flags

    # A pinned value ignores the server: marking it read there and unread here keeps it unread.
    await aexecute(READ, {"ids": [two], "read": False})
    await sync(box["id"])
    assert "\\Seen" not in (await models.Message.objects.aget(id=two)).flags

    # Turning pushing on pushes what was kept.
    await aexecute(SETTINGS, {"input": {"id": box["id"], "pushSeen": True}})
    await mail_sync.push_account(int(box["id"]))
    flags = server(greenmail, box["address"])
    assert "\\Seen" in flags["One"] and "\\Seen" not in flags["Two"]
    assert not await models.LocalPin.objects.filter(account_id=box["id"]).aexists()


async def test_revert_to_server_drops_local_state(box, aexecute, greenmail):
    await aexecute(SETTINGS, {"input": {"id": box["id"], "pushSeen": False}})
    one = box["messages"]["One"]
    await aexecute(READ, {"ids": [one], "read": True})
    reverted = (await aexecute('mutation($ids: [ID!]!) { revertMessagesToServer(messages: $ids) { isRead syncState } }', {"ids": [one]})).data["revertMessagesToServer"]
    assert reverted == [{"isRead": False, "syncState": "SYNCED"}]


async def test_push_deletes_off_hides_mail_here_only(box, aexecute, greenmail, sync):
    await aexecute(SETTINGS, {"input": {"id": box["id"], "pushDeletes": False}})
    three = box["messages"]["Three"]
    await aexecute(DELETE, {"ids": [three], "p": False})
    await sync(box["id"])
    assert "Three" in server(greenmail, box["address"])
    listed = (await aexecute('query($a: ID!) { messages(filters: {account: $a}) { subject } }', {"a": box["id"]})).data["messages"]
    assert {m["subject"] for m in listed} == {"One", "Two"}
    assert await models.Message.objects.filter(account_id=box["id"], subject="Three").acount() == 1


async def test_a_local_delete_survives_a_uidvalidity_reset(box, aexecute, greenmail, sync):
    """The folder is replaced with the same message in it: the refetch adopts the hidden row instead of showing the mail again."""
    await aexecute(MOVE, {"ids": [box["messages"]["Three"]], "f": box["folders"]["Archive"]})
    await aexecute(SETTINGS, {"input": {"id": box["id"], "pushDeletes": False}})
    await aexecute(DELETE, {"ids": [box["messages"]["Three"]], "p": True})
    with greenmail.imap(box["address"]) as imap:
        greenmail.recreate_folder(imap, "Archive", "Archive-old")
        imap.append("Archive", f"Subject: Three\r\nMessage-ID: <three.{box['address']}>\r\n\r\nagain\r\n".encode())
    await sync(box["id"])
    rows = [(str(m.id), m.deleted_at is not None) async for m in models.Message.objects.filter(account_id=box["id"], folder__path="Archive")]
    assert rows == [(box["messages"]["Three"], True)]


CATEGORY = 'mutation($input: CreateCategoryInput!) { createCategory(input: $input) { id keyword sync } }'
CATEGORIZE = 'mutation($ids: [ID!]!, $add: [ID!]!) { categorizeMessages(input: {messages: $ids, add: $add}) { id categories { name } } }'


async def test_categories_local_and_as_keywords(box, aexecute, greenmail, sync):
    work = (await aexecute(CATEGORY, {"input": {"account": box["id"], "name": "Work", "sync": "KEYWORD"}})).data["createCategory"]
    ideas = (await aexecute(CATEGORY, {"input": {"account": box["id"], "name": "Ideas"}})).data["createCategory"]
    assert work["keyword"] == "$Work" and ideas["sync"] == "LOCAL"
    one = box["messages"]["One"]
    done = (await aexecute(CATEGORIZE, {"ids": [one], "add": [work["id"], ideas["id"]]})).data["categorizeMessages"]
    assert {c["name"] for c in done[0]["categories"]} == {"Work", "Ideas"}
    flags = server(greenmail, box["address"])
    assert "$Work" in flags["One"] and not any("Ideas" in f for f in flags["One"])

    # Another client puts $Work on Two: it joins the category.
    store_behind(greenmail, box["address"], "Two", add=["$Work"])
    await sync(box["id"])
    in_work = (await aexecute('query($c: ID!) { messages(filters: {category: $c}) { subject } }', {"c": work["id"]})).data["messages"]
    assert {m["subject"] for m in in_work} == {"One", "Two"}

    # A move keeps both (the keyword travels with MOVE; the local one is kept under the message key).
    await aexecute(MOVE, {"ids": [one], "f": box["folders"]["Archive"]})
    await sync(box["id"])
    row = await models.Message.objects.aget(id=one)
    assert set(row.category_ids) == {int(work["id"]), int(ideas["id"])}

    # Going LOCAL keeps the members; with removeKeywords the server forgets them.
    await aexecute('mutation($id: ID!) { updateCategory(input: {id: $id, sync: LOCAL, removeKeywords: true}) { sync } }', {"id": work["id"]})
    assert "$Work" not in server(greenmail, box["address"])["Two"]
    in_work = (await aexecute('query($c: ID!) { messages(filters: {category: $c}) { subject } }', {"c": work["id"]})).data["messages"]
    assert {m["subject"] for m in in_work} == {"One", "Two"}


async def test_an_unreachable_server_keeps_the_change_queued(box, aexecute, greenmail):
    one = box["messages"]["One"]
    await models.MailAccount.objects.filter(id=box["id"]).aupdate(incoming_port=1)  # nothing listens there
    read = (await aexecute(READ, {"ids": [one], "read": True})).data["markMessagesRead"]
    assert read[0]["isRead"] is True and read[0]["syncState"] == "PENDING"
    account = await models.MailAccount.objects.aget(id=box["id"])
    assert account.last_error_code == "CONNECTION_FAILED"
    change = await models.MailChange.objects.aget(account_id=box["id"])
    assert change.attempts == 0  # the connection, not the change, failed
    assert change.push_after > timezone.now()  # the mailbox backs off: the next request does not reconnect
    assert await mail_sync.push_account(int(box["id"])) is None

    await models.MailAccount.objects.filter(id=box["id"]).aupdate(incoming_port=greenmail.stack.imaps_port)
    await models.MailChange.objects.filter(account_id=box["id"]).aupdate(push_after=timezone.now())  # the backoff passed
    assert (await mail_sync.push_account(int(box["id"]))).pushed == 1
    assert "\\Seen" in server(greenmail, box["address"])["One"]
    assert (await models.MailAccount.objects.aget(id=box["id"])).last_error_code is None


async def test_a_held_lease_defers_the_push(box, aexecute, greenmail, writeback):
    """A sync holds the mailbox: pushing now does nothing (that sync pushes); once it is free, the push goes through."""
    writeback(push_inline=False)
    await aexecute(READ, {"ids": [box["messages"]["One"]], "read": True})
    assert await sync_to_async(mail_sync.claim)(int(box["id"]))
    assert await mail_sync.push_account(int(box["id"])) is None
    assert (await models.MailChange.objects.aget(account_id=box["id"])).state == "PENDING"
    await sync_to_async(mail_sync.release)(int(box["id"]))
    assert (await mail_sync.push_account(int(box["id"]))).pushed == 1
    assert "\\Seen" in server(greenmail, box["address"])["One"]


async def test_two_pushes_at_once_push_once(box, aexecute, greenmail, writeback):
    """Real threads: whichever gets the lease pushes; the other finds the mailbox held or nothing left."""
    writeback(push_inline=False)
    await aexecute(READ, {"ids": [box["messages"]["One"], box["messages"]["Two"]], "read": True})
    results: list = []

    def push() -> None:
        try:
            results.append(mail_sync.push_now(int(box["id"])))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=push) for _ in range(2)]
    await sync_to_async(lambda: ([t.start() for t in threads], [t.join() for t in threads]))()
    assert sum(r.pushed for r in results if r is not None) == 2
    flags = server(greenmail, box["address"])
    assert "\\Seen" in flags["One"] and "\\Seen" in flags["Two"]


async def test_sync_state_filter_stays_in_its_mailbox(box, aexecute, mailbox, greenmail, sync, other_org_context):
    """The same mail in another organization's mailbox, kept read there only, changes nothing here."""
    other = await mailbox(context=other_org_context)
    greenmail.deliver(other["address"], "One", message_id=f"one.{box['address']}")
    greenmail.wait_for(other["address"], 1)
    await sync(other["id"], context=other_org_context)
    await aexecute(SETTINGS, {"input": {"id": other["id"], "pushSeen": False}}, context=other_org_context)
    theirs = [str(m.id) async for m in models.Message.objects.filter(account_id=other["id"])]
    await aexecute(READ, {"ids": theirs, "read": True}, context=other_org_context)

    local = (await aexecute('query($a: ID!) { messages(filters: {account: $a, syncState: LOCAL}) { subject } }', {"a": box["id"]})).data["messages"]
    assert local == []
    synced = (await aexecute('query($a: ID!) { messages(filters: {account: $a, syncState: SYNCED}) { subject } }', {"a": box["id"]})).data["messages"]
    assert {m["subject"] for m in synced} == {"One", "Two", "Three"}
