"""Listing conversations directly: row fields, filters, counts and account search."""

from datetime import datetime, timezone

import pytest

from mail import models
from mail.sync import sync_account

pytestmark = pytest.mark.django_db(transaction=True)

ROWS = """
query($f: ThreadFilter) {
  threadsCount(filters: $f)
  threads(filters: $f, ordering: [{lastMessageAt: DESC}]) {
    id subject messageCount unreadCount inboxUnread: unreadCount(folderRole: INBOX) flagged hasAttachments
    participants { name address }
    latestMessage { subject }
    latestInbox: latestMessage(folderRole: INBOX) { subject }
  }
}
"""


@pytest.fixture
async def conversation(mailbox, greenmail, sync):
    """Plans: a question from Anna in INBOX, Ben's answer, and the owner's reply in Sent. Plus an unrelated mail."""
    box = await mailbox()
    root = greenmail.deliver(box["address"], "Plans", "Shall we?", sender="Anna <anna@example.org>", date=datetime(2034, 12, 1, 9, tzinfo=timezone.utc), attachments=[("map.pdf", "application/pdf", b"%PDF")])
    greenmail.deliver(box["address"], "Re: Plans", "Yes!", sender="Ben <ben@example.org>", date=datetime(2034, 12, 2, 9, tzinfo=timezone.utc), in_reply_to=root, references=[root])
    greenmail.deliver(box["address"], "Lunch", "Pizza?", sender="Carl <carl@example.org>")
    greenmail.wait_for(box["address"], 3)
    with greenmail.imap(box["address"]) as imap:
        imap.create_folder("Sent")
        imap.append("Sent", f"From: Me <{box['address']}>\r\nSubject: Re: Plans\r\nMessage-ID: <mine@x>\r\nIn-Reply-To: <{root}>\r\nReferences: <{root}>\r\nDate: Mon, 1 Jan 2035 10:00:00 +0000\r\n\r\nSee you\r\n".encode(), flags=[b"\\Seen"])
        imap.select_folder("INBOX")
        first = min(imap.search("ALL"))
        imap.add_flags([first], [b"\\Flagged"])
    await sync(box["id"])
    plans = await models.Thread.objects.aget(account_id=box["id"], subject="plans")
    return {**box, "plans": str(plans.id)}


async def test_rows(conversation, aexecute):
    data = (await aexecute(ROWS, {"f": {"account": conversation["id"]}})).data
    assert data["threadsCount"] == 2
    plans, lunch = data["threads"]
    assert plans["id"] == conversation["plans"]
    assert plans["messageCount"] == 3
    assert (plans["unreadCount"], plans["inboxUnread"]) == (2, 2)  # the Sent copy is read
    assert plans["flagged"] is True and plans["hasAttachments"] is True
    assert [p["name"] for p in plans["participants"]] == ["Anna", "Ben", "Me"]
    assert plans["latestMessage"] == {"subject": "Re: Plans"}  # the owner's reply, newest overall
    assert plans["latestInbox"] == {"subject": "Re: Plans"}  # Ben's answer, newest in INBOX
    assert lunch["flagged"] is False and lunch["hasAttachments"] is False


@pytest.mark.parametrize(
    "filters,expected",
    [
        ({"folderRole": "SENT"}, ["plans"]),
        ({"folderRole": "INBOX"}, ["lunch", "plans"]),
        ({"flagged": True}, ["plans"]),
        ({"flagged": False}, ["lunch"]),
        ({"hasAttachments": True}, ["plans"]),
        ({"search": "pizza"}, ["lunch"]),
        ({"search": "see you"}, ["plans"]),
        ({"unread": False}, []),
    ],
)
async def test_filters(conversation, aexecute, filters, expected):
    data = (await aexecute(ROWS, {"f": {"account": conversation["id"], **filters}})).data
    assert sorted(t["subject"] for t in data["threads"]) == expected
    assert data["threadsCount"] == len(expected)


async def test_ids_filter_and_folder_scoped_latest(conversation, aexecute):
    sent = await models.MailFolder.objects.aget(account_id=conversation["id"], role="SENT")
    query = 'query($ids: [ID!], $folder: ID) { threads(filters: {ids: $ids}) { latestMessage(folder: $folder) { subject sender { name } } } }'
    data = (await aexecute(query, {"ids": [conversation["plans"]], "folder": str(sent.id)})).data["threads"]
    assert data == [{"latestMessage": {"subject": "Re: Plans", "sender": {"name": "Me"}}}]


async def test_counts_respect_visibility(conversation, aexecute, colleague_context):
    assert (await aexecute("{ threadsCount messagesCount }", context=colleague_context)).data == {"threadsCount": 0, "messagesCount": 0}
    assert (await aexecute("{ messagesCount }")).data["messagesCount"] == 4


async def test_mail_account_search(mailbox, aexecute):
    work = await mailbox(name="Work inbox")
    await mailbox(name="Private")
    found = (await aexecute('{ mailAccounts(filters: {search: "work"}) { id } }')).data["mailAccounts"]
    assert found == [{"id": work["id"]}]
    by_address = (await aexecute('query($q: String!) { mailAccounts(filters: {search: $q}) { id } }', {"q": work["address"][:10]})).data["mailAccounts"]
    assert by_address == [{"id": work["id"]}]


async def test_sync_reports_touched_folders(mailbox, greenmail, sync):
    box = await mailbox()
    with greenmail.imap(box["address"]) as imap:
        imap.create_folder("Quiet")
    greenmail.deliver(box["address"], "Hello")
    greenmail.wait_for(box["address"], 1)
    result = await sync_account(int(box["id"]))
    inbox = await models.MailFolder.objects.aget(account_id=box["id"], path="INBOX")
    assert result.touched_folders == [inbox.id]
    assert (await sync_account(int(box["id"]))).touched_folders == []
