"""IMAP sync against a real server: new mail, backfill in batches, flags, expunges, UIDVALIDITY."""

import pytest
from django.conf import settings as django_settings

from mail import models

pytestmark = pytest.mark.django_db(transaction=True)

MESSAGES = """
query($account: ID!) {
  messages(filters: {account: $account}, ordering: [{date: DESC}]) {
    id subject snippet textBody isRead isFlagged flags hasAttachments
    sender { name address } to { address } folder { path role } thread { id messageCount }
    attachments { filename contentType size inline }
  }
}
"""


async def test_first_sync_reads_inbox(mailbox, greenmail, sync, aexecute):
    box = await mailbox()
    greenmail.deliver(box["address"], "First", "Hello first")
    greenmail.deliver(box["address"], "Second", "Hello second", attachments=[("a.pdf", "application/pdf", b"%PDF-1.4 data")])
    greenmail.wait_for(box["address"], 2)

    result = await sync(box["id"])
    assert result["created"] == 2
    assert result["account"]["backfillDone"] is True

    messages = (await aexecute(MESSAGES, {"account": box["id"]})).data["messages"]
    assert {m["subject"] for m in messages} == {"First", "Second"}
    second = next(m for m in messages if m["subject"] == "Second")
    assert second["hasAttachments"] is True
    assert second["attachments"] == [{"filename": "a.pdf", "contentType": "application/pdf", "size": 13, "inline": False}]
    assert second["sender"] == {"name": "Alice Sender", "address": "alice@example.org"}
    assert second["folder"] == {"path": "INBOX", "role": "INBOX"}
    assert second["isRead"] is False  # fetched with BODY.PEEK

    again = await sync(box["id"])
    assert again["created"] == 0


async def test_new_mail_after_first_sync(mailbox, greenmail, sync):
    box = await mailbox()
    greenmail.deliver(box["address"], "Old")
    greenmail.wait_for(box["address"], 1)
    await sync(box["id"])
    greenmail.deliver(box["address"], "New")
    greenmail.wait_for(box["address"], 2)
    assert (await sync(box["id"]))["created"] == 1


async def test_backfill_in_batches(mailbox, greenmail, sync, settings):
    settings.KUVERT_SYNC = {**django_settings.KUVERT_SYNC, "batch_size": 3}
    box = await mailbox()
    for n in range(7):
        greenmail.deliver(box["address"], f"Mail {n}")
    greenmail.wait_for(box["address"], 7)

    first = await sync(box["id"])
    assert (first["created"], first["more"], first["account"]["backfillDone"]) == (3, True, False)
    # Newest first: the first batch holds the three newest.
    subjects = set(await _subjects(box["id"]))
    assert subjects == {"Mail 4", "Mail 5", "Mail 6"}
    second = await sync(box["id"])
    assert second["created"] == 3 and second["more"] is True
    third = await sync(box["id"])
    assert third["created"] == 1 and third["more"] is False and third["account"]["backfillDone"] is True


async def _subjects(account_id: str) -> list[str]:
    return [s async for s in models.Message.objects.filter(account_id=account_id).values_list("subject", flat=True)]


async def test_flags_and_expunges_follow_the_server(mailbox, greenmail, sync):
    box = await mailbox()
    greenmail.deliver(box["address"], "Keep")
    greenmail.deliver(box["address"], "Drop")
    greenmail.wait_for(box["address"], 2)
    await sync(box["id"])

    with greenmail.imap(box["address"]) as imap:
        imap.select_folder("INBOX")
        keep, drop = sorted(imap.search("ALL"))
        imap.add_flags([keep], [b"\\Seen", b"\\Flagged"])
        imap.delete_messages([drop])
        imap.expunge()

    result = await sync(box["id"])
    assert result["updated"] == 1 and result["deleted"] == 1
    message = await models.Message.objects.aget(account_id=box["id"])
    assert message.subject == "Keep"
    assert set(message.flags) == {"\\Seen", "\\Flagged"}


async def test_folders_are_discovered_with_roles(mailbox, greenmail, sync, aexecute):
    box = await mailbox()
    with greenmail.imap(box["address"]) as imap:
        imap.create_folder("Sent")
        imap.create_folder("Trash")
        imap.create_folder("Receipts")
        imap.append("Receipts", b"From: shop@example.org\r\nSubject: Receipt\r\nMessage-ID: <r1@example.org>\r\n\r\nThanks\r\n")
    result = await sync(box["id"])
    assert result["folders"] == 4 and result["created"] == 1
    folders = (await aexecute('query($a: ID!) { mailFolders(filters: {account: $a}) { path role totalCount } }', {"a": box["id"]})).data["mailFolders"]
    assert {(f["path"], f["role"]) for f in folders} == {("INBOX", "INBOX"), ("Sent", "SENT"), ("Trash", "TRASH"), ("Receipts", "OTHER")}


async def test_uidvalidity_change_resyncs_the_folder(mailbox, greenmail, sync):
    box = await mailbox()
    with greenmail.imap(box["address"]) as imap:
        imap.create_folder("Box")
        imap.append("Box", b"Subject: one\r\nMessage-ID: <one@x>\r\n\r\n1\r\n")
    await sync(box["id"])
    folder = await models.MailFolder.objects.aget(account_id=box["id"], path="Box")
    old_validity = folder.uidvalidity
    old_id = (await models.Message.objects.aget(folder=folder)).id

    with greenmail.imap(box["address"]) as imap:
        greenmail.recreate_folder(imap, "Box", "Old")
        imap.append("Box", b"Subject: one\r\nMessage-ID: <one@x>\r\n\r\n1\r\n")
        imap.append("Box", b"Subject: two\r\nMessage-ID: <two@x>\r\n\r\n2\r\n")
    await sync(box["id"])
    folder = await models.MailFolder.objects.aget(account_id=box["id"], path="Box")
    rows = [m async for m in models.Message.objects.filter(folder=folder)]
    assert await models.Message.objects.filter(folder__path="Old", account_id=box["id"]).acount() == 1
    assert folder.uidvalidity != old_validity
    assert sorted(m.subject for m in rows) == ["one", "two"]
    assert old_id not in {m.id for m in rows}
    assert all(m.uidvalidity == folder.uidvalidity for m in rows)


async def test_threads_join_replies(mailbox, greenmail, sync, aexecute):
    box = await mailbox()
    root = greenmail.deliver(box["address"], "Plans", "Shall we?")
    greenmail.deliver(box["address"], "Re: Plans", "Yes", in_reply_to=root, references=[root])
    greenmail.deliver(box["address"], "Other", "Unrelated")
    greenmail.wait_for(box["address"], 3)
    await sync(box["id"])
    threads = (await aexecute('query($a: ID!) { threads(filters: {account: $a}) { subject messageCount messages { subject } } }', {"a": box["id"]})).data["threads"]
    assert sorted((t["subject"], t["messageCount"]) for t in threads) == [("other", 1), ("plans", 2)]


async def test_one_run_is_capped_over_all_folders(mailbox, greenmail, sync, settings):
    settings.KUVERT_SYNC = {**django_settings.KUVERT_SYNC, "batch_size": 5, "max_messages_per_run": 4}
    box = await mailbox()
    with greenmail.imap(box["address"]) as imap:
        imap.create_folder("Other")
        for n in range(3):
            imap.append("Other", f"Subject: o{n}\r\nMessage-ID: <o{n}@x>\r\n\r\n.\r\n".encode())
    for n in range(3):
        greenmail.deliver(box["address"], f"i{n}")
    greenmail.wait_for(box["address"], 3)
    first = await sync(box["id"])
    assert first["created"] == 4 and first["more"] is True
    second = await sync(box["id"])
    assert second["created"] == 2 and second["more"] is False


async def test_junk_is_discovered_but_not_synced(mailbox, greenmail, sync, aexecute):
    box = await mailbox()
    with greenmail.imap(box["address"]) as imap:
        imap.create_folder("Spam")
        imap.append("Spam", b"Subject: buy now\r\nMessage-ID: <spam@x>\r\n\r\n.\r\n")
    assert (await sync(box["id"]))["created"] == 0
    spam = (await aexecute('query($a: ID!) { mailFolders(filters: {account: $a, role: JUNK}) { id syncEnabled } }', {"a": box["id"]})).data["mailFolders"][0]
    assert spam["syncEnabled"] is False
    await aexecute('mutation($id: ID!) { updateMailFolder(input: {id: $id, syncEnabled: true}) { id } }', {"id": spam["id"]})
    assert (await sync(box["id"]))["created"] == 1
