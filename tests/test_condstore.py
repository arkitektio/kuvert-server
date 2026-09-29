"""Against Dovecot, which has CONDSTORE (GreenMail does not): flags are re-read with CHANGEDSINCE, and pushes work there too."""

import pytest

from mail import models

pytestmark = pytest.mark.django_db(transaction=True)


def _append(dovecot, user: str, *subjects: str) -> None:
    with dovecot.imap(user) as imap:
        for subject in subjects:
            imap.append("INBOX", f"Subject: {subject}\r\nMessage-ID: <{subject}.{user}@x>\r\n\r\n{subject}\r\n".encode())


def _flags(dovecot, user: str, folder: str = "INBOX") -> dict[str, set[str]]:
    with dovecot.imap(user) as imap:
        imap.select_folder(folder, readonly=True)
        uids = imap.search("ALL")
        data = imap.fetch(uids, ["FLAGS", "BODY.PEEK[HEADER.FIELDS (SUBJECT)]"]) if uids else {}
    return {v[b"BODY[HEADER.FIELDS (SUBJECT)]"].decode().split(":", 1)[1].strip(): {f.decode() for f in v[b"FLAGS"]} for v in data.values()}


async def test_flag_changes_are_read_with_changedsince(dovecot_mailbox, dovecot, sync):
    box = await dovecot_mailbox()
    assert "CONDSTORE" in box["capabilities"]
    _append(dovecot, box["user"], "one", "two")
    await sync(box["id"])
    inbox = await models.MailFolder.objects.aget(account_id=box["id"], path="INBOX")
    assert inbox.highest_modseq  # the CONDSTORE path is the one taken from now on

    with dovecot.imap(box["user"]) as imap:  # another client
        imap.select_folder("INBOX")
        imap.add_flags(imap.search(["SUBJECT", "two"]), ["\\Seen", "$Work"])
    assert (await sync(box["id"]))["updated"] == 1
    two = await models.Message.objects.aget(account_id=box["id"], subject="two")
    assert {"\\Seen", "$Work"} <= set(two.server_flags) and {"\\Seen", "$Work"} <= set(two.flags)
    assert (await sync(box["id"]))["updated"] == 0  # nothing changed since


async def test_local_changes_reach_dovecot(dovecot_mailbox, dovecot, sync, aexecute):
    box = await dovecot_mailbox()
    with dovecot.imap(box["user"]) as imap:
        imap.create_folder("Archive")
    _append(dovecot, box["user"], "one", "two")
    await sync(box["id"])
    one = await models.Message.objects.aget(account_id=box["id"], subject="one")
    archive = await models.MailFolder.objects.aget(account_id=box["id"], path="Archive")

    await aexecute('mutation($ids: [ID!]!) { markMessagesRead(input: {messages: $ids}) { isRead } }', {"ids": [str(one.id)]})
    moved = await aexecute('mutation($ids: [ID!]!, $f: ID!) { moveMessages(input: {messages: $ids, folder: $f}) { id syncState } }', {"ids": [str(one.id)], "f": str(archive.id)})
    assert moved.data["moveMessages"] == [{"id": str(one.id), "syncState": "SYNCED"}]
    assert "\\Seen" in _flags(dovecot, box["user"], "Archive")["one"]
    await sync(box["id"])
    assert await models.Message.objects.filter(account_id=box["id"], subject="one").acount() == 1
