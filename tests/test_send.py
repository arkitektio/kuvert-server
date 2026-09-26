"""Sending through the mailbox's own SMTP server: delivery, the Sent copy, replies and refusals."""

import email
from email import policy

import pytest

from mail import models

pytestmark = pytest.mark.django_db(transaction=True)

SEND = """
mutation($input: SendMessageInput!) {
  sendMessage(input: $input) { id status subject messageId savedToSent error errorCode inReplyTo { id } to { address } }
}
"""


def _inbox(greenmail, address: str) -> list[email.message.EmailMessage]:
    with greenmail.imap(address) as imap:
        imap.select_folder("INBOX", readonly=True)
        return [email.message_from_bytes(v[b"BODY[]"], policy=policy.default) for v in imap.fetch(imap.search("ALL"), ["BODY.PEEK[]"]).values()]


async def test_send_delivers_and_saves_a_copy(mailbox, greenmail, aexecute):
    box = await mailbox(displayName="Me Myself")
    with greenmail.imap(box["address"]) as imap:
        imap.create_folder("Sent")
    await aexecute('mutation($id: ID!) { syncMailAccount(id: $id) { folders } }', {"id": box["id"]})
    friend = greenmail.user()

    sent = (await aexecute(SEND, {"input": {"account": box["id"], "to": [{"address": friend, "name": "Friend"}], "bcc": [{"address": "hidden@kuvert.test"}], "subject": "Hi", "text": "Hello friend", "html": "<p>Hello <b>friend</b></p>"}})).data["sendMessage"]
    assert sent["status"] == "SENT" and sent["savedToSent"] is True

    greenmail.wait_for(friend, 1)
    [received] = _inbox(greenmail, friend)
    assert received["Subject"] == "Hi"
    assert received["From"] == "Me Myself <%s>" % box["address"]
    assert received["Message-ID"].strip("<>") == sent["messageId"]
    assert "Bcc" not in received
    assert received.get_body(("plain",)).get_content().strip() == "Hello friend"

    # The Sent copy was read in at once, in the same thread the reply will join.
    copy = await models.Message.objects.aget(account_id=box["id"], folder__role="SENT")
    assert copy.message_id == sent["messageId"] and "\\Seen" in copy.flags
    outbox = (await aexecute("{ outbox { id status } }")).data["outbox"]
    assert outbox == [{"id": sent["id"], "status": "SENT"}]


async def test_reply_threads_and_answers(mailbox, greenmail, aexecute, sync):
    box = await mailbox(saveSentCopy=False)
    original = greenmail.deliver(box["address"], "Question", "Can you?", sender=f"Asker <{greenmail.user()}>")
    greenmail.wait_for(box["address"], 1)
    await sync(box["id"])
    message = await models.Message.objects.aget(account_id=box["id"])
    asker = message.sender_address

    sent = (await aexecute(SEND, {"input": {"account": box["id"], "to": [{"address": asker}], "text": "Yes", "inReplyTo": str(message.id)}})).data["sendMessage"]
    assert sent["subject"] == "Re: Question" and sent["inReplyTo"] == {"id": str(message.id)}
    greenmail.wait_for(asker, 1)
    [reply] = _inbox(greenmail, asker)
    assert reply["In-Reply-To"] == f"<{original}>"
    assert original in reply["References"]
    await message.arefresh_from_db()
    assert "\\Answered" in message.flags


async def test_refusals_come_back_failed_not_raised(mailbox, aexecute):
    box = await mailbox()
    bad = await aexecute(SEND, {"input": {"account": box["id"], "to": [{"address": "not an address"}], "text": "x"}}, allow_errors=True)
    assert bad.errors[0].extensions["code"] == "SEND_REJECTED"
    none = await aexecute(SEND, {"input": {"account": box["id"], "text": "x"}}, allow_errors=True)
    assert none.errors[0].extensions["code"] == "SEND_REJECTED"

    # A wrong SMTP password: the send is recorded FAILED and the mailbox needs new credentials.
    await models.MailAccount.objects.filter(id=box["id"]).aupdate(smtp_username=box["address"], smtp_secret=__import__("mail.crypto", fromlist=["x"]).encrypt("wrong"))
    failed = (await aexecute(SEND, {"input": {"account": box["id"], "to": [{"address": "x@kuvert.test"}], "text": "x"}})).data["sendMessage"]
    assert failed["status"] == "FAILED" and failed["errorCode"] == "AUTH_FAILED"
    account = await models.MailAccount.objects.aget(id=box["id"])
    assert account.status == "NEEDS_REAUTH"
    inactive = await aexecute(SEND, {"input": {"account": box["id"], "to": [{"address": "x@kuvert.test"}], "text": "x"}})
    assert inactive.data["sendMessage"]["errorCode"] == "MAILBOX_INACTIVE"
