"""``descriptors`` on kuvert's types: what an object says about itself, from its structure's declaration.

One declaration (``kuvert_server.service``) feeds the manifest, the signals and this field, so the
tests hold the three to each other: the field answers what a signal about the object carries, in
the keys the manifest declares.
"""

import pytest
from asgiref.sync import async_to_sync
from authentikate.models import Organization

from kuvert_server.schema import schema
from kuvert_server.service import service
from mail import models
from tests.test_signals import intake  # noqa: F401  the fixture

pytestmark = pytest.mark.django_db(transaction=True)

DESCRIBED = """
    query Described($account: ID!) {
        messages(filters: {account: $account}) { descriptors hasAttachments }
        threads { descriptors messageCount }
        outbox { descriptors status }
    }
"""
SEND = "mutation($input: SendMessageInput!) { sendMessage(input: $input) { id status } }"


async def test_an_object_answers_the_descriptors_its_structure_declares(mailbox, greenmail, sync, aexecute):
    box = await mailbox()
    greenmail.deliver(box["address"], "With file", "see attached", attachments=[("report.csv", "text/csv", b"a,b\n1,2\n")])
    greenmail.wait_for(box["address"], 1)
    await sync(box["id"])
    sent = (await aexecute(SEND, {"input": {"account": box["id"], "to": [{"address": greenmail.user()}], "subject": "Hi", "text": "Hello"}})).data["sendMessage"]
    assert sent["status"] == "SENT"

    data = (await aexecute(DESCRIBED, {"account": box["id"]})).data
    assert data["messages"] == [{"descriptors": {"@kuvert/has_attachments": True}, "hasAttachments": True}]
    assert data["threads"] == [{"descriptors": {"@kuvert/message_count": 1}, "messageCount": 1}]
    assert data["outbox"] == [{"descriptors": {"@kuvert/status": "SENT"}, "status": "SENT"}]

    declared = {s["identifier"]: [d["key"] for d in s["descriptors"]] for s in service.manifest()["structures"]}
    assert list(data["messages"][0]["descriptors"]) == declared["@kuvert/message"]
    assert list(data["threads"][0]["descriptors"]) == declared["@kuvert/thread"]
    assert list(data["outbox"][0]["descriptors"]) == declared["@kuvert/outgoingmessage"]


def test_the_field_answers_what_the_signal_carried(intake, authenticated_context):  # noqa: F811
    account = models.MailAccount.objects.create(
        organization=Organization.objects.get(slug="static_org"), name="Team box", email_address="team@example.org",
        incoming_host="imap.example.org", incoming_port=993, username="team", visibility=models.Visibility.ORGANIZATION,
    )
    thread = models.Thread.objects.create(account=account, message_count=3)

    (received,) = intake.of("@kuvert/thread")
    result = async_to_sync(schema.execute)("query Thread($id: ID!) { thread(id: $id) { descriptors } }", context_value=authenticated_context, variable_values={"id": str(thread.pk)})
    assert not result.errors, result.errors
    assert result.data["thread"]["descriptors"] == received["json"]["descriptors"] == {"@kuvert/message_count": 3}
