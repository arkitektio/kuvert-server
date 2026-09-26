"""POP3 mailboxes: one INBOX, identity by UIDL, local flags, no folders, DELE on delete."""

import pytest

from mail import models

pytestmark = pytest.mark.django_db(transaction=True)


def _server_count(greenmail, address: str) -> int:
    with greenmail.imap(address) as imap:  # GreenMail's IMAP sees the same INBOX
        imap.select_folder("INBOX", readonly=True)
        return len(imap.search("ALL"))


async def test_pop3_sync_and_mirror(mailbox, greenmail, sync, aexecute):
    box = await mailbox(protocol="POP3")
    assert "UIDL" in box["capabilities"]
    greenmail.deliver(box["address"], "Pop one")
    greenmail.deliver(box["address"], "Pop two")
    greenmail.wait_for(box["address"], 2)
    assert (await sync(box["id"]))["created"] == 2
    assert (await sync(box["id"]))["created"] == 0

    folders = (await aexecute('query($a: ID!) { mailFolders(filters: {account: $a}) { path role } }', {"a": box["id"]})).data["mailFolders"]
    assert folders == [{"path": "INBOX", "role": "INBOX"}]

    # Flags are local: marking read works without touching the server.
    message = await models.Message.objects.filter(account_id=box["id"], subject="Pop one").afirst()
    read = await aexecute('mutation($ids: [ID!]!) { markMessagesRead(input: {messages: $ids}) { isRead } }', {"ids": [str(message.id)]})
    assert read.data["markMessagesRead"] == [{"isRead": True}]

    # Moves are IMAP-only.
    inbox = await models.MailFolder.objects.aget(account_id=box["id"])
    moved = await aexecute('mutation($ids: [ID!]!, $f: ID!) { moveMessages(input: {messages: $ids, folder: $f}) { id } }', {"ids": [str(message.id)], "f": str(inbox.id)}, allow_errors=True)
    assert moved.errors[0].extensions["code"] == "UNSUPPORTED_BY_PROTOCOL"

    # Delete: DELE on the server, then the row.
    await aexecute('mutation($ids: [ID!]!) { deleteMessages(input: {messages: $ids}) { deleted } }', {"ids": [str(message.id)]})
    assert _server_count(greenmail, box["address"]) == 1
    assert await models.Message.objects.filter(account_id=box["id"]).acount() == 1

    # Deleted on the server by another client: mirrored (leave-on-server).
    with greenmail.imap(box["address"]) as imap:
        imap.select_folder("INBOX")
        imap.delete_messages(imap.search("ALL"))
        imap.expunge()
    assert (await sync(box["id"]))["deleted"] == 1


async def test_pop3_without_leave_on_server_downloads_and_deletes(mailbox, greenmail, sync):
    box = await mailbox(protocol="POP3", popLeaveOnServer=False)
    greenmail.deliver(box["address"], "Take me")
    greenmail.wait_for(box["address"], 1)
    assert (await sync(box["id"]))["created"] == 1
    assert _server_count(greenmail, box["address"]) == 0
    # The local copy is now the only one and survives the next sync.
    assert (await sync(box["id"]))["deleted"] == 0
    assert await models.Message.objects.filter(account_id=box["id"]).acount() == 1


async def test_pop3_cannot_move_between_folders(mailbox, greenmail, sync, aexecute):
    imap_box = await mailbox()
    box = await mailbox(protocol="POP3", address=imap_box["address"])
    greenmail.deliver(box["address"], "Stay")
    greenmail.wait_for(box["address"], 1)
    await sync(box["id"])
    await sync(imap_box["id"])
    pop_message = await models.Message.objects.aget(account_id=box["id"])
    imap_inbox = await models.MailFolder.objects.aget(account_id=imap_box["id"], path="INBOX")
    result = await aexecute('mutation($ids: [ID!]!, $f: ID!) { moveMessages(input: {messages: $ids, folder: $f}) { id } }', {"ids": [str(pop_message.id)], "f": str(imap_inbox.id)}, allow_errors=True)
    assert result.errors[0].extensions["code"] == "VALIDATION_ERROR"  # never across mailboxes
