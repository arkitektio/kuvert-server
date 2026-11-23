"""A guest of the organization, granted a collaboration and nothing else: what they read of the mail
put in it, and that they change nothing.

Their token lists their collaborations by key (`collabs`: lok's key, one across the hub), with the
roles they hold in each (conftest `guest`). Member A ("test") links a private mailbox and puts a
conversation, a message or sent mail in a collaboration under a fresh key
(`addManyToCollaboration`, which makes kuvert's row of it). A conversation brings the messages it has
then, a message the bytes of its attachments; a mailbox, its folders and the providers never go in.
"""

import uuid
from contextlib import aclosing

import pytest
from arkitekt_service.collaborations.models import Collaboration, CollaborationItem
from botocore.client import BaseClient
from kante.context import HttpContext
from strawberry.types import ExecutionResult

from kuvert_server.schema import schema
from mail import models
from tests.conftest import GreenMail
from tests.support import (
    Data,
    GuestFactory,
    Mailbox,
    Sync,
    request_context,
)

pytestmark = pytest.mark.django_db(transaction=True)

ADD_MANY = "mutation ($input: AddManyToCollaborationInput!) { addManyToCollaboration(input: $input) { key name } }"
ADD = "mutation ($input: CollaborationObjectInput!) { addToCollaboration(input: $input) { key } }"
THREAD = """
query ($id: ID!) {
  thread(id: $id) {
    id subject unread unreadCount hasAttachments
    participants { address }
    latestMessage { subject }
    messages { id subject }
  }
}
"""
MESSAGE = """
query ($id: ID!) {
  message(id: $id) {
    id subject textBody raw { id }
    attachments { filename store { originalFileName accessGrant { key } } }
  }
}
"""


async def _run(ctx: HttpContext, query: str, **variables: object) -> ExecutionResult:
    return await schema.execute(
        query, context_value=ctx, variable_values=variables or None
    )


def _refused(result: ExecutionResult, reason: str) -> None:
    assert result.errors, "it was let through"
    extensions = result.errors[0].extensions or {}
    assert extensions.get("reason") == reason, result.errors


IDENTIFIERS = {
    models.Thread: "@kuvert/thread",
    models.Message: "@kuvert/message",
    models.OutgoingMessage: "@kuvert/outgoingmessage",
}


async def _collaboration(
    *objects: models.Thread | models.Message | models.OutgoingMessage,
    ctx: HttpContext | None = None,
) -> str:
    """A collaboration of the organization holding ``objects``, as member A puts them in: under a
    fresh key, as lok hands one out, in one ``addManyToCollaboration``. Its key."""
    key = str(uuid.uuid4())
    added = await _run(
        ctx or request_context("test"),
        ADD_MANY,
        input={
            "collaboration": key,
            "name": "Shared",
            "objects": [
                {"identifier": IDENTIFIERS[type(obj)], "object": str(obj.pk)}
                for obj in objects
            ],
        },
    )
    assert not added.errors, added.errors
    return key


@pytest.fixture
async def mail(
    mailbox: Mailbox, greenmail: GreenMail, sync: Sync, datalayer: BaseClient
) -> Data:
    """Member A's private mailbox: a conversation of two messages (the first with a file), and an
    unrelated secret message."""
    box = await mailbox()
    first = greenmail.deliver(
        box["address"],
        "Shared plan",
        "the plan",
        attachments=[("plan.txt", "text/plain", b"step one")],
    )
    greenmail.deliver(
        box["address"],
        "Re: Shared plan",
        "sounds good",
        sender="Bob Reply <bob@example.org>",
        in_reply_to=first,
        references=[first],
    )
    greenmail.deliver(
        box["address"], "Secret", "nobody else", sender="Eve <eve@example.org>"
    )
    greenmail.wait_for(box["address"], 3)
    await sync(box["id"])
    shared = await models.Message.objects.aget(account_id=box["id"], message_id=first)
    secret = await models.Message.objects.aget(account_id=box["id"], subject="Secret")
    return {
        "box": box,
        "thread": await models.Thread.objects.aget(messages=shared),
        "first": shared,
        "secret": secret,
        "first_id": first,
    }


async def test_a_guest_reads_the_conversation_put_in_and_nothing_else(
    mail: Data, guest: GuestFactory
) -> None:
    thread: models.Thread = mail["thread"]
    ctx = guest({await _collaboration(thread): []})

    listed = await _run(ctx, "{ threads { id } messages { id subject } }")
    assert not listed.errors, listed.errors
    assert listed.data is not None
    assert [t["id"] for t in listed.data["threads"]] == [str(thread.pk)]
    assert sorted(m["subject"] for m in listed.data["messages"]) == [
        "Re: Shared plan",
        "Shared plan",
    ]

    read = await _run(ctx, THREAD, id=str(thread.pk))
    assert not read.errors, read.errors
    assert read.data is not None
    assert read.data["thread"]["participants"] == [
        {"address": "alice@example.org"},
        {"address": "bob@example.org"},
    ]
    assert read.data["thread"]["unreadCount"] == 2
    assert read.data["thread"]["hasAttachments"] is True

    # The secret message, the mailbox, its folders and its provider are none of theirs.
    assert (await _run(ctx, MESSAGE, id=str(mail["secret"].pk))).errors
    nothing = await _run(
        ctx,
        "{ mailAccounts { id } mailFolders { id } mailProviders { id } outbox { id } tasks { id } taskLists { id } categories { id } mailChanges { id } }",
    )
    assert not nothing.errors, nothing.errors
    assert nothing.data is not None
    assert all(rows == [] for rows in nothing.data.values()), nothing.data
    assert (
        await _run(
            ctx,
            "query ($id: ID!) { mailAccount(id: $id) { id } }",
            id=mail["box"]["id"],
        )
    ).errors


async def test_a_guest_opens_the_attachments_of_a_shared_message_never_its_raw_source(
    mail: Data, guest: GuestFactory
) -> None:
    ctx = guest({await _collaboration(mail["first"]): []})

    read = await _run(ctx, MESSAGE, id=str(mail["first"].pk))
    assert not read.errors, read.errors
    assert read.data is not None
    message = read.data["message"]
    assert message["textBody"].strip() == "the plan"
    assert message["raw"] is None  # stored, but not brought along
    [attachment] = message["attachments"]
    assert attachment["filename"] == "plan.txt"
    assert attachment["store"]["originalFileName"] == "plan.txt"
    assert attachment["store"]["accessGrant"]["key"]


async def test_mail_threaded_in_later_stays_out_and_counts_for_nothing(
    mail: Data, guest: GuestFactory, greenmail: GreenMail, sync: Sync
) -> None:
    thread: models.Thread = mail["thread"]
    ctx = guest({await _collaboration(thread): []})
    greenmail.deliver(
        mail["box"]["address"],
        "Re: Shared plan",
        "a private aside",
        sender="Mallory <mallory@example.org>",
        in_reply_to=mail["first_id"],
        references=[mail["first_id"]],
    )
    greenmail.wait_for(mail["box"]["address"], 4)
    await sync(mail["box"]["id"])
    later = await models.Message.objects.aget(
        account_id=mail["box"]["id"], sender_address="mallory@example.org"
    )
    assert await models.Thread.objects.filter(messages=later, pk=thread.pk).aexists()

    read = await _run(ctx, THREAD, id=str(thread.pk))
    assert not read.errors, read.errors
    assert read.data is not None
    seen = read.data["thread"]
    assert str(later.pk) not in {m["id"] for m in seen["messages"]}
    assert "mallory@example.org" not in {p["address"] for p in seen["participants"]}
    assert seen["unreadCount"] == 2
    assert seen["latestMessage"] == {"subject": "Re: Shared plan"}
    assert (await _run(ctx, MESSAGE, id=str(later.pk))).errors

    # The member, who sees the whole mailbox, sees all three.
    member = await _run(request_context("test"), THREAD, id=str(thread.pk))
    assert member.data is not None
    assert member.data["thread"]["unreadCount"] == 3


async def test_no_guest_changes_anything_not_even_an_editor(
    mail: Data, guest: GuestFactory
) -> None:
    thread: models.Thread = mail["thread"]
    first: models.Message = mail["first"]
    key = await _collaboration(thread)
    changes_before = await models.MailChange.objects.acount()

    for ctx in (guest({key: []}), guest({key: ["kuvert_editor"]})):
        for document, variables in [
            (
                "mutation ($id: ID!) { markMessagesRead(input: {messages: [$id]}) { id } }",
                {"id": str(first.pk)},
            ),
            (
                'mutation ($id: ID!) { setMessageFlags(input: {messages: [$id], add: ["\\\\Flagged"]}) { id } }',
                {"id": str(first.pk)},
            ),
            (
                "mutation ($id: ID!) { deleteMessages(input: {messages: [$id]}) { deleted } }",
                {"id": str(first.pk)},
            ),
            (
                "mutation ($id: ID!) { syncMailAccount(id: $id) { created } }",
                {"id": mail["box"]["id"]},
            ),
            (
                'mutation ($id: ID!) { sendMessage(input: {account: $id, to: [{address: "x@kuvert.test"}], text: "hi"}) { id } }',
                {"id": mail["box"]["id"]},
            ),
            (
                'mutation ($id: ID!) { createTask(input: {title: "mine", threads: [$id]}) { id } }',
                {"id": str(thread.pk)},
            ),
            (
                ADD,
                {
                    "input": {
                        "collaboration": key,
                        "identifier": "@kuvert/message",
                        "object": str(mail["secret"].pk),
                    }
                },
            ),
            (
                "mutation ($input: ShareWorkflowInput!) { shareWorkflow(input: $input) { dryRun } }",
                {"input": {"collaboration": key, "rootTask": "any", "dryRun": True}},
            ),
            (
                "mutation ($key: ID!) { deleteCollaboration(key: $key) }",
                {"key": key},
            ),
        ]:
            _refused(await _run(ctx, document, **variables), "SCOPED_READ_ONLY")

    # Nothing was queued for the mail server, nothing changed here.
    assert await models.MailChange.objects.acount() == changes_before
    await first.arefresh_from_db()
    assert "\\Seen" not in first.flags and first.deleted_at is None
    assert await CollaborationItem.objects.filter(collaboration__key=key).acount() == (
        # the conversation, its two messages, the first one's attachment
        4
    )


async def test_a_guest_subscribes_to_nothing_and_counts_nothing(
    mail: Data, guest: GuestFactory
) -> None:
    ctx = guest({await _collaboration(mail["thread"]): []})

    subscribed = await schema.subscribe(
        "subscription { mailboxSyncs { accountId } }", context_value=ctx
    )
    if not isinstance(subscribed, ExecutionResult):
        async with aclosing(subscribed) as stream:
            subscribed = await anext(stream)
    _refused(subscribed, "SCOPED_READ_ONLY")

    # Root fields handing out no objects (counts, presets, kinds of provider) are closed to guests.
    for field in (
        "threadsCount",
        "messagesCount",
        "tasksCount",
        "mailChangesCount",
        "providerKinds { kind }",
        "mailPresets { name }",
    ):
        _refused(await _run(ctx, f"{{ {field} }}"), "COLLABORATION_DOES_NOT_REACH")


async def test_a_provider_never_goes_in_and_a_member_shares_only_what_they_see(
    mail: Data,
    guest: GuestFactory,
    colleague_context: HttpContext,
) -> None:
    provider = await models.MailProvider.objects.aget(accounts__id=mail["box"]["id"])
    key = await _collaboration(mail["first"])

    put = await _run(
        request_context("test"),
        ADD,
        input={
            "collaboration": key,
            "identifier": "@kuvert/mailprovider",
            "object": str(provider.pk),
        },
    )
    assert put.errors and "never put in a collaboration" in put.errors[0].message, (
        put.errors
    )

    # The colleague does not see member A's private mailbox: its mail is no more theirs to share.
    by_colleague = await _run(
        request_context("colleague"),
        ADD,
        input={
            "collaboration": str(uuid.uuid4()),
            "identifier": "@kuvert/thread",
            "object": str(mail["thread"].pk),
        },
    )
    assert by_colleague.errors, "a colleague shared mail they cannot see"
    assert not await Collaboration.objects.exclude(key=key).aexists()
    assert (
        await CollaborationItem.objects.filter(collaboration__key=key).acount() == 2
    )  # the message and its attachment's bytes

    ctx = guest({key: []})
    listed = await _run(ctx, "{ mailProviders { id } }")
    assert listed.data == {"mailProviders": []}


async def test_a_member_lists_what_is_in_a_collaboration(
    mail: Data, guest: GuestFactory
) -> None:
    key = await _collaboration(mail["thread"])
    await _collaboration(mail["secret"])  # another, not granted

    member = request_context("test")
    listed = await _run(
        member,
        "query ($key: ID!, $in: ThreadFilter, $msgs: MessageFilter) { collaboration(key: $key) { key itemCount(brought: false) } threads(filters: $in) { id } messages(filters: $msgs) { subject } }",
        key=key,
        **{"in": {"inCollaboration": key}, "msgs": {"inCollaboration": key}},
    )
    assert not listed.errors, listed.errors
    assert listed.data is not None
    assert listed.data["collaboration"] == {"key": key, "itemCount": 1}
    assert listed.data["threads"] == [{"id": str(mail["thread"].pk)}]
    assert sorted(m["subject"] for m in listed.data["messages"]) == [
        "Re: Shared plan",
        "Shared plan",
    ]

    granted = await _run(guest({key: []}), "{ collaborations { key name } }")
    assert not granted.errors, granted.errors
    assert granted.data == {"collaborations": [{"key": key, "name": "Shared"}]}


def _type_line(sdl: str, name: str) -> str:
    return next(line for line in sdl.splitlines() if line.startswith(f"type {name} "))


def test_the_schema_says_what_each_type_is_and_opens_no_write_to_guests() -> None:
    sdl = str(schema)
    assert '@identified(by: "@kuvert/thread")' in _type_line(sdl, "Thread")
    assert '@identified(by: "@kuvert/message")' in _type_line(sdl, "Message")
    assert "allow_scoped: true" not in sdl
