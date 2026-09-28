"""Flags, moves and deletes reach the server (pushed right after the request: the test settings have no undo windows).

What happens before a push -- the undo window, local-only settings, surviving syncs -- is in
``test_local_first.py``.
"""

import pytest

from mail import models

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
async def box_with_mail(mailbox, greenmail, sync):
    box = await mailbox()
    with greenmail.imap(box["address"]) as imap:
        imap.create_folder("Archive")
        imap.create_folder("Trash")
    greenmail.deliver(box["address"], "One")
    greenmail.deliver(box["address"], "Two")
    greenmail.wait_for(box["address"], 2)
    await sync(box["id"])
    ids = {m.subject: str(m.id) async for m in models.Message.objects.filter(account_id=box["id"])}
    folders = {f.path: str(f.id) async for f in models.MailFolder.objects.filter(account_id=box["id"])}
    return {**box, "messages": ids, "folders": folders}


def _server_flags(greenmail, address: str, folder: str = "INBOX") -> dict[str, set[str]]:
    with greenmail.imap(address) as imap:
        imap.select_folder(folder, readonly=True)
        data = imap.fetch(imap.search("ALL"), ["FLAGS", "BODY.PEEK[HEADER.FIELDS (SUBJECT)]"])
    return {v[b"BODY[HEADER.FIELDS (SUBJECT)]"].decode().split(":", 1)[1].strip(): {f.decode() for f in v[b"FLAGS"]} for v in data.values()}


async def test_flags_are_set_on_the_server(box_with_mail, aexecute, greenmail):
    one = box_with_mail["messages"]["One"]
    result = await aexecute('mutation($ids: [ID!]!) { setMessageFlags(input: {messages: $ids, add: ["\\\\Flagged", "$Work"]}) { flags isFlagged } }', {"ids": [one]})
    assert result.data["setMessageFlags"][0]["isFlagged"] is True
    assert _server_flags(greenmail, box_with_mail["address"])["One"] >= {"\\Flagged", "$Work"}
    await aexecute('mutation($ids: [ID!]!) { markMessagesRead(input: {messages: $ids, read: true}) { isRead } }', {"ids": [one]})
    assert "\\Seen" in _server_flags(greenmail, box_with_mail["address"])["One"]
    unread = (await aexecute('query($a: ID!) { messages(filters: {account: $a, unread: true}) { subject } }', {"a": box_with_mail["id"]})).data["messages"]
    assert unread == [{"subject": "Two"}]


async def test_invalid_flags_are_refused(box_with_mail, aexecute):
    for flag in ["\\Deleted", "bad flag", "a\\b"]:
        result = await aexecute('mutation($ids: [ID!]!, $f: String!) { setMessageFlags(input: {messages: $ids, add: [$f]}) { id } }', {"ids": [box_with_mail["messages"]["One"]], "f": flag}, allow_errors=True)
        assert result.errors[0].extensions["code"] == "VALIDATION_ERROR"


async def test_move_lands_in_the_destination(box_with_mail, aexecute, greenmail):
    one = box_with_mail["messages"]["One"]
    moved = await aexecute(
        'mutation($ids: [ID!]!, $f: ID!) { moveMessages(input: {messages: $ids, folder: $f}) { id subject folder { path } syncState } }',
        {"ids": [one], "f": box_with_mail["folders"]["Archive"]},
    )
    # The row keeps its id, and the push already happened (COPYUID gave it its new UID).
    assert moved.data["moveMessages"] == [{"id": one, "subject": "One", "folder": {"path": "Archive"}, "syncState": "SYNCED"}]
    assert set(_server_flags(greenmail, box_with_mail["address"], "Archive")) == {"One"}
    assert set(_server_flags(greenmail, box_with_mail["address"], "INBOX")) == {"Two"}
    row = await models.Message.objects.aget(id=one)
    assert row.uid is not None
    # A sync reads nothing twice.
    await aexecute('mutation($id: ID!) { syncMailAccount(id: $id) { created } }', {"id": box_with_mail["id"]})
    assert await models.Message.objects.filter(account_id=box_with_mail["id"], subject="One").acount() == 1
    assert await models.Message.objects.filter(account_id=box_with_mail["id"], folder__path="INBOX").acount() == 1


async def test_delete_moves_to_trash_then_expunges(box_with_mail, aexecute, greenmail):
    two = box_with_mail["messages"]["Two"]
    assert (await aexecute('mutation($ids: [ID!]!) { deleteMessages(input: {messages: $ids}) { deleted } }', {"ids": [two]})).data["deleteMessages"] == {"deleted": 1}
    assert set(_server_flags(greenmail, box_with_mail["address"], "Trash")) == {"Two"}
    in_trash = await models.Message.objects.aget(account_id=box_with_mail["id"], folder__path="Trash")
    await aexecute('mutation($ids: [ID!]!) { deleteMessages(input: {messages: $ids}) { deleted } }', {"ids": [str(in_trash.id)]})
    assert _server_flags(greenmail, box_with_mail["address"], "Trash") == {}
    assert not await models.Message.objects.filter(account_id=box_with_mail["id"], subject="Two").aexists()


async def test_a_server_failure_keeps_the_local_change(box_with_mail, aexecute, greenmail, sync):
    """The folder was replaced on the server (new UIDVALIDITY): the flag stays here, the push fails -- and the next sync finds the message again."""
    one = box_with_mail["messages"]["One"]
    await aexecute('mutation($ids: [ID!]!, $f: ID!) { moveMessages(input: {messages: $ids, folder: $f}) { id } }', {"ids": [one], "f": box_with_mail["folders"]["Archive"]})
    with greenmail.imap(box_with_mail["address"]) as imap:
        greenmail.recreate_folder(imap, "Archive", "Archive3")  # "One" now lives in Archive3
    flagged = await aexecute('mutation($ids: [ID!]!) { setMessageFlags(input: {messages: $ids, add: ["\\\\Flagged"]}) { isFlagged syncState changes { state errorCode } } }', {"ids": [one]})
    assert flagged.data["setMessageFlags"] == [{"isFlagged": True, "syncState": "FAILED", "changes": [{"state": "FAILED", "errorCode": "MESSAGE_GONE"}]}]

    # The sync reads Archive3 in; the change moves over to the message there and is pushed.
    await sync(box_with_mail["id"])
    assert "\\Flagged" in _server_flags(greenmail, box_with_mail["address"], "Archive3")["One"]
    rows = [m async for m in models.Message.objects.filter(account_id=box_with_mail["id"], subject="One")]
    assert [(r.folder_id is not None, "\\Flagged" in r.flags) for r in rows] == [(True, True)]
    assert not await models.MailChange.objects.filter(account_id=box_with_mail["id"]).aexists()
