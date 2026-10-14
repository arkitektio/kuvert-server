"""``descriptors`` on kuvert's types: what an object says about itself, from its structure's declaration.

One declaration (``kuvert_server.service``) feeds the manifest, the signals and this field, so the
tests hold the three to each other: the field answers what a signal about the object carries, in
the keys the manifest declares. They are stored on the row, so a port's ``requires`` is a filter
on the list query.
"""

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from authentikate.models import Organization
from django.core.management import call_command

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


REQUIRED = """
    query Required($messages: [DescriptorConstraintInput!]!, $threads: [DescriptorConstraintInput!]!, $outbox: [DescriptorConstraintInput!]!) {
        messages(filters: {requires: $messages}) { id }
        threads(filters: {requires: $threads}) { id }
        outbox(filters: {requires: $outbox}) { id }
    }
"""


async def _required(aexecute, messages: list, threads: list, outbox: list) -> dict[str, int]:
    result = await aexecute(REQUIRED, {"messages": messages, "threads": threads, "outbox": outbox})
    assert not result.errors, result.errors
    return {field: len(rows) for field, rows in result.data.items()}


async def test_a_port_requires_is_answered_from_the_stored_descriptors(mailbox, greenmail, sync, aexecute):
    box = await mailbox()
    greenmail.deliver(box["address"], "With file", "see attached", attachments=[("report.csv", "text/csv", b"a,b\n1,2\n")])
    greenmail.deliver(box["address"], "Plain", "nothing attached")
    greenmail.wait_for(box["address"], 2)
    await sync(box["id"])
    sent = (await aexecute(SEND, {"input": {"account": box["id"], "to": [{"address": greenmail.user()}], "subject": "Hi", "text": "Hello"}})).data["sendMessage"]
    assert sent["status"] == "SENT"

    with_files = [{"key": "@kuvert/has_attachments", "operator": "EQUALS", "value": True}]
    # The count of a conversation is recounted in place when a message joins it, and restored with it.
    answered = [{"key": "@kuvert/message_count", "operator": "GTE", "value": 1}]
    failed = [{"key": "@kuvert/status", "operator": "EQUALS", "value": "FAILED"}]
    assert await _required(aexecute, with_files, answered, failed) == {"messages": 1, "threads": 2, "outbox": 0}
    assert await _required(aexecute, [], [{"key": "@kuvert/message_count", "operator": "GTE", "value": 2}], [{"key": "@kuvert/status", "operator": "IN", "value": ["SENT"]}]) == {"messages": 2, "threads": 0, "outbox": 1}

    # A key messages do not declare is refused, not ignored (`aexecute` raises on any error).
    with pytest.raises(AssertionError, match="not a descriptor here"):
        await aexecute(REQUIRED, {"messages": [{"key": "@kuvert/colour", "operator": "EQUALS", "value": "red"}], "threads": [], "outbox": []})


async def test_the_setup_job_gives_a_row_what_it_owes(mailbox, greenmail, sync, aexecute):
    box = await mailbox()
    greenmail.deliver(box["address"], "Old", "from before descriptors were stored")
    greenmail.wait_for(box["address"], 1)
    await sync(box["id"])
    await models.Thread.objects.all().aupdate(stored_descriptors={})
    await sync_to_async(call_command)("restore_descriptors")
    assert [thread.stored_descriptors async for thread in models.Thread.objects.all()] == [{"@kuvert/message_count": 1}]
