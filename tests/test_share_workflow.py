"""Sharing a workflow: the mail a root task sent through kuvert, put in a collaboration as it is now.

A workflow is a root task (koherent's ``Task.root_task_id``). Its tasks call kuvert with a task
token rekuest signed (``tests/support.task_token``, ``rtk`` naming the workflow), and koherent
records the task on the CREATE row of the sent mail's history. Messages and conversations are
synced from the mail server and record no task: they are reported unknown, and nothing of them is
put in. A provider is never put in, whatever made it. Only mail sent through a mailbox the member
sharing sees goes in: a colleague who knows the workflow's id shares nothing of a private mailbox.
"""

import uuid

import pytest
from arkitekt_service.collaborations.models import Collaboration
from kante.context import HttpContext
from strawberry.types import ExecutionResult

from kuvert_server.schema import schema
from mail import models
from tests.conftest import GreenMail
from tests.support import Data, GuestFactory, Mailbox, request_context, task_token

pytestmark = pytest.mark.django_db(transaction=True)

ORG = "static_issuer#static_org"

SHARE = """
mutation ($input: ShareWorkflowInput!) {
  shareWorkflow(input: $input) {
    collaboration
    dryRun
    added { identifier object }
    covered
    skipped { identifier object }
    unknown
  }
}
"""
SEND = 'mutation ($id: ID!, $to: String!, $subject: String!) { sendMessage(input: {account: $id, to: [{address: $to}], subject: $subject, text: "hi"}) { id status } }'


async def _run(ctx: HttpContext, query: str, **variables: object) -> ExecutionResult:
    return await schema.execute(
        query, context_value=ctx, variable_values=variables or None
    )


async def _as(ctx: HttpContext, query: str, **variables: object) -> dict:
    result = await _run(ctx, query, **variables)
    assert not result.errors, result.errors
    assert result.data is not None
    return result.data


async def _send(box: Data, subject: str, task: str | None = None) -> str:
    sent = await _as(
        request_context("test", task=task),
        SEND,
        id=box["id"],
        to=box["friend"],
        subject=subject,
    )
    assert sent["sendMessage"]["status"] == "SENT", sent
    return str(sent["sendMessage"]["id"])


@pytest.fixture
async def workflow(mailbox: Mailbox, greenmail: GreenMail) -> Data:
    """Member A's private mailbox; a workflow (two tasks under one root) that sent two mails
    through it, and a mail sent outside it."""
    box = {**await mailbox(), "friend": greenmail.user()}
    root = f"root-{uuid.uuid4().hex}"
    first = task_token("1", ORG, tsk=f"task-{uuid.uuid4().hex}", rtk=root)
    second = task_token("1", ORG, tsk=f"task-{uuid.uuid4().hex}", rtk=root)
    return {
        "root": root,
        "box": box,
        "made": {
            await _send(box, "From the first task", first),
            await _send(box, "From the second task", second),
        },
        "unrelated": await _send(box, "By hand"),
    }


async def test_a_dry_run_says_what_would_go_in_and_writes_nothing(
    workflow: Data,
) -> None:
    key = str(uuid.uuid4())
    report = (
        await _as(
            request_context("test"),
            SHARE,
            input={"collaboration": key, "rootTask": workflow["root"], "dryRun": True},
        )
    )["shareWorkflow"]
    assert report["dryRun"] is True
    assert {a["object"] for a in report["added"]} == workflow["made"]
    assert {a["identifier"] for a in report["added"]} == {"@kuvert/outgoingmessage"}
    assert report["skipped"] == [] and report["covered"] == 0
    # Synced from the mail server: nothing says which workflow made them.
    assert sorted(report["unknown"]) == ["@kuvert/message", "@kuvert/thread"]
    assert not await Collaboration.objects.filter(key=key).aexists(), (
        "a dry run makes no row of the collaboration here"
    )


async def test_the_workflows_sent_mail_goes_in_and_a_guest_reads_it(
    workflow: Data, guest: GuestFactory
) -> None:
    key = str(uuid.uuid4())
    report = (
        await _as(
            request_context("test"),
            SHARE,
            input={"collaboration": key, "rootTask": workflow["root"]},
        )
    )["shareWorkflow"]
    assert {a["object"] for a in report["added"]} == workflow["made"]

    ctx = guest({key: []})
    outbox = await _as(ctx, "{ outbox { id subject } }")
    assert {row["id"] for row in outbox["outbox"]} == workflow["made"]
    assert (
        await _run(
            ctx,
            "query ($id: ID!) { outgoingMessage(id: $id) { id } }",
            id=workflow["unrelated"],
        )
    ).errors


async def test_a_colleague_shares_nothing_of_a_private_mailbox(
    workflow: Data,
) -> None:
    key = str(uuid.uuid4())
    report = (
        await _as(
            request_context("colleague"),
            SHARE,
            input={"collaboration": key, "rootTask": workflow["root"]},
        )
    )["shareWorkflow"]
    assert report["added"] == []

    # Once the mailbox is the organization's, it is theirs to share as well.
    await models.MailAccount.objects.filter(id=workflow["box"]["id"]).aupdate(
        visibility=models.Visibility.ORGANIZATION
    )
    report = (
        await _as(
            request_context("colleague"),
            SHARE,
            input={"collaboration": key, "rootTask": workflow["root"], "dryRun": True},
        )
    )["shareWorkflow"]
    assert {a["object"] for a in report["added"]} == workflow["made"]


async def test_lists_filter_by_the_workflow_and_by_collaboration(
    workflow: Data,
) -> None:
    member = request_context("test")
    created = await _as(
        member,
        "query ($f: OutgoingMessageFilter) { outbox(filters: $f) { id } }",
        f={"createdUnder": workflow["root"]},
    )
    assert {row["id"] for row in created["outbox"]} == workflow["made"]

    key = str(uuid.uuid4())
    await _as(member, SHARE, input={"collaboration": key, "rootTask": workflow["root"]})
    shared = await _as(
        member,
        "query ($f: OutgoingMessageFilter) { outbox(filters: $f) { id } }",
        f={"inCollaboration": key},
    )
    assert {row["id"] for row in shared["outbox"]} == workflow["made"]
    nothing = await _as(
        member,
        "query ($f: OutgoingMessageFilter) { outbox(filters: $f) { id } }",
        f={"createdUnder": f"root-{uuid.uuid4().hex}"},
    )
    assert nothing["outbox"] == []
