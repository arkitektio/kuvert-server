"""Flags, moves and deletes change the server first, then the rows."""

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
    moved = await aexecute(
        'mutation($ids: [ID!]!, $f: ID!) { moveMessages(input: {messages: $ids, folder: $f}) { subject folder { path } } }',
        {"ids": [box_with_mail["messages"]["One"]], "f": box_with_mail["folders"]["Archive"]},
    )
    assert moved.data["moveMessages"] == [{"subject": "One", "folder": {"path": "Archive"}}]
    assert set(_server_flags(greenmail, box_with_mail["address"], "Archive")) == {"One"}
    assert set(_server_flags(greenmail, box_with_mail["address"], "INBOX")) == {"Two"}
    assert await models.Message.objects.filter(account_id=box_with_mail["id"], folder__path="INBOX").acount() == 1


async def test_delete_moves_to_trash_then_expunges(box_with_mail, aexecute, greenmail):
    two = box_with_mail["messages"]["Two"]
    assert (await aexecute('mutation($ids: [ID!]!) { deleteMessages(input: {messages: $ids}) { deleted } }', {"ids": [two]})).data["deleteMessages"] == {"deleted": 1}
    assert set(_server_flags(greenmail, box_with_mail["address"], "Trash")) == {"Two"}
    in_trash = await models.Message.objects.aget(account_id=box_with_mail["id"], folder__path="Trash")
    await aexecute('mutation($ids: [ID!]!) { deleteMessages(input: {messages: $ids}) { deleted } }', {"ids": [str(in_trash.id)]})
    assert _server_flags(greenmail, box_with_mail["address"], "Trash") == {}
    assert not await models.Message.objects.filter(account_id=box_with_mail["id"], subject="Two").aexists()


async def test_a_server_failure_changes_nothing_locally(box_with_mail, aexecute, greenmail):
    """The folder was replaced on the server (new UIDVALIDITY): the change is refused and the row stays as it was."""
    with greenmail.imap(box_with_mail["address"]) as imap:
        imap.rename_folder("Archive", "Archive2")
        imap.create_folder("Archive")
        imap.append("Archive", b"Subject: A\r\nMessage-ID: <a@x>\r\n\r\na\r\n")
    moved = await aexecute(
        'mutation($ids: [ID!]!, $f: ID!) { moveMessages(input: {messages: $ids, folder: $f}) { id } }',
        {"ids": [box_with_mail["messages"]["One"]], "f": box_with_mail["folders"]["Archive"]},
    )
    # Moving *into* a replaced folder works (the destination is re-read)...
    assert moved.data["moveMessages"]
    # ...but touching a message whose folder's UIDVALIDITY moved on is refused.
    archived = await models.Message.objects.aget(account_id=box_with_mail["id"], folder__path="Archive", subject="One")
    with greenmail.imap(box_with_mail["address"]) as imap:
        imap.rename_folder("Archive", "Archive3")
        imap.create_folder("Archive")
    result = await aexecute('mutation($ids: [ID!]!) { setMessageFlags(input: {messages: $ids, add: ["\\\\Flagged"]}) { id } }', {"ids": [str(archived.id)]}, allow_errors=True)
    assert result.errors[0].extensions["code"] == "SERVER_ERROR"
    await archived.arefresh_from_db()
    assert "\\Flagged" not in archived.flags
