"""Tasks over mail conversations (Google-Inbox style): personal, across mailboxes, many-to-many."""

from datetime import timedelta

import pytest
from django.utils import timezone

from mail import models

pytestmark = pytest.mark.django_db(transaction=True)

UPSERT = """
mutation($input: UpsertTaskInput!) {
  upsertTask(input: $input) {
    id title status appClientId threadCount unreadCount latestMessage { subject }
    links { thread { id subject } source confidence reason appClientId }
  }
}
"""
TASKS = 'query($f: TaskFilter) { tasksCount(filters: $f) tasks(filters: $f) { id title } }'


@pytest.fixture
async def inbox(mailbox, greenmail, sync):
    """Two mailboxes of member A with a conversation each, and their thread ids."""
    work, private = await mailbox(name="Work"), await mailbox(name="Private")
    root = greenmail.deliver(work["address"], "Flight booking", "Your flight to Berlin", sender="Airline <air@example.org>")
    greenmail.deliver(work["address"], "Re: Flight booking", "Seat 12A", sender="Airline <air@example.org>", in_reply_to=root, references=[root])
    greenmail.deliver(private["address"], "Hotel confirmation", "Check-in Friday", sender="Hotel <h@example.org>")
    greenmail.wait_for(work["address"], 2)
    greenmail.wait_for(private["address"], 1)
    await sync(work["id"])
    await sync(private["id"])
    flight = await models.Thread.objects.aget(account_id=work["id"])
    hotel = await models.Thread.objects.aget(account_id=private["id"])
    return {"work": work, "private": private, "flight": str(flight.id), "hotel": str(hotel.id)}


async def test_upsert_is_idempotent_and_spans_mailboxes(inbox, aexecute):
    link = {"source": "APP", "confidence": 0.9, "reason": "Travel mail"}
    first = (await aexecute(UPSERT, {"input": {"externalKey": "trip-berlin", "title": "Berlin trip", "threads": [inbox["flight"]], "link": link}})).data["upsertTask"]
    assert first["threadCount"] == 1 and first["unreadCount"] == 2
    assert first["links"][0] == {"thread": {"id": inbox["flight"], "subject": "flight booking"}, "source": "APP", "confidence": 0.9, "reason": "Travel mail", "appClientId": "static"}

    # Sorting again: same task, new title, a second conversation from the *other* mailbox, and the
    # existing link's details updated -- never a duplicate.
    second = (await aexecute(UPSERT, {"input": {"externalKey": "trip-berlin", "title": "Berlin trip (Fri)", "threads": [inbox["flight"], inbox["hotel"]], "link": {**link, "confidence": 0.95}}})).data["upsertTask"]
    assert second["id"] == first["id"] and second["title"] == "Berlin trip (Fri)"
    assert second["threadCount"] == 2 and {l["confidence"] for l in second["links"]} == {0.95}
    assert second["latestMessage"]["subject"] in {"Re: Flight booking", "Hotel confirmation"}
    assert await models.Task.objects.acount() == 1


async def test_many_to_many_and_thread_filters(inbox, aexecute):
    one = (await aexecute(UPSERT, {"input": {"externalKey": "a", "title": "Book seat", "threads": [inbox["flight"]]}})).data["upsertTask"]
    two = (await aexecute(UPSERT, {"input": {"externalKey": "b", "title": "Expense report", "threads": [inbox["flight"], inbox["hotel"]]}})).data["upsertTask"]
    tasks_of_flight = (await aexecute('query($id: ID!) { thread(id: $id) { tasks { title } } }', {"id": inbox["flight"]})).data["thread"]["tasks"]
    assert sorted(t["title"] for t in tasks_of_flight) == ["Book seat", "Expense report"]
    in_one = (await aexecute('query($t: ID!) { threads(filters: {task: $t}) { id } }', {"t": one["id"]})).data["threads"]
    assert in_one == [{"id": inbox["flight"]}]
    assert (await aexecute(TASKS, {"f": {"thread": inbox["hotel"]}})).data["tasks"] == [{"id": two["id"], "title": "Expense report"}]

    # "hasTask: false" hides sorted mail; unlinking brings it back.
    unsorted = 'query { threads(filters: {hasTask: false}) { id } }'
    assert (await aexecute(unsorted)).data["threads"] == []
    await aexecute('mutation($t: ID!, $th: [ID!]!) { unlinkThreads(input: {task: $t, threads: $th}) { id } }', {"t": two["id"], "th": [inbox["hotel"]]})
    assert (await aexecute(unsorted)).data["threads"] == [{"id": inbox["hotel"]}]


async def test_status_snooze_pin_and_active_view(inbox, aexecute):
    ids = []
    for key in ("x", "y", "z"):
        ids.append((await aexecute(UPSERT, {"input": {"externalKey": key, "title": key}})).data["upsertTask"]["id"])
    x, y, z = ids
    done = (await aexecute('mutation($ids: [ID!]!) { setTaskStatus(input: {tasks: $ids, status: DONE}) { status completedAt } }', {"ids": [x]})).data["setTaskStatus"][0]
    assert done["status"] == "DONE" and done["completedAt"]
    later = (timezone.now() + timedelta(days=1)).isoformat()
    snoozed = (await aexecute('mutation($ids: [ID!]!, $u: DateTime) { snoozeTasks(input: {tasks: $ids, until: $u}) { snoozed } }', {"ids": [y], "u": later})).data["snoozeTasks"]
    assert snoozed == [{"snoozed": True}]
    await aexecute('mutation($id: ID!) { updateTask(input: {id: $id, pinned: true}) { pinned } }', {"id": z})

    assert (await aexecute(TASKS, {"f": {"active": True}})).data == {"tasksCount": 1, "tasks": [{"id": z, "title": "z"}]}
    assert (await aexecute(TASKS, {"f": {"snoozed": True}})).data["tasksCount"] == 1
    # Waking and reopening.
    await aexecute('mutation($ids: [ID!]!) { snoozeTasks(input: {tasks: $ids, until: null}) { snoozed } }', {"ids": [y]})
    reopened = (await aexecute('mutation($ids: [ID!]!) { setTaskStatus(input: {tasks: $ids, status: OPEN}) { completedAt } }', {"ids": [x]})).data["setTaskStatus"][0]
    assert reopened["completedAt"] is None
    assert (await aexecute(TASKS, {"f": {"active": True}})).data["tasksCount"] == 3


async def test_lists(inbox, aexecute):
    travel = (await aexecute('mutation { createTaskList(input: {name: "Travel", color: "#4f86f7"}) { id } }')).data["createTaskList"]["id"]
    task = (await aexecute('mutation($l: ID!) { createTask(input: {title: "Pack", list: $l}) { id list { name } } }', {"l": travel})).data["createTask"]
    assert task["list"] == {"name": "Travel"}
    await aexecute('mutation { createTask(input: {title: "Loose"}) { id } }')
    lists = (await aexecute('{ taskLists { name openCount tasks { title } } }')).data["taskLists"]
    assert lists == [{"name": "Travel", "openCount": 1, "tasks": [{"title": "Pack"}]}]
    assert (await aexecute(TASKS, {"f": {"noList": True}})).data["tasks"][0]["title"] == "Loose"
    await aexecute('mutation($l: ID!) { deleteTaskList(id: $l) }', {"l": travel})
    assert (await aexecute(TASKS, {"f": {"noList": True}})).data["tasksCount"] == 2


async def test_duplicate_external_key_on_create_is_refused(aexecute):
    await aexecute('mutation { createTask(input: {title: "a", externalKey: "k"}) { id } }')
    again = await aexecute('mutation { createTask(input: {title: "b", externalKey: "k"}) { id } }', allow_errors=True)
    assert again.errors[0].extensions["code"] == "VALIDATION_ERROR"


async def test_links_survive_moves_and_resyncs(inbox, aexecute, greenmail, sync):
    task = (await aexecute(UPSERT, {"input": {"externalKey": "t", "title": "Flight", "threads": [inbox["flight"]]}})).data["upsertTask"]
    work = inbox["work"]
    with greenmail.imap(work["address"]) as imap:
        imap.create_folder("Archive")
    await sync(work["id"])
    archive = await models.MailFolder.objects.aget(account_id=work["id"], path="Archive")
    ids = [str(m.id) async for m in models.Message.objects.filter(thread_id=inbox["flight"])]
    await aexecute('mutation($ids: [ID!]!, $f: ID!) { moveMessages(input: {messages: $ids, folder: $f}) { id } }', {"ids": ids, "f": str(archive.id)})

    # Same thread id, now holding the archived copies; the task still has it.
    thread = await models.Thread.objects.aget(id=inbox["flight"])
    assert thread.message_count == 2
    assert await models.Message.objects.filter(thread=thread, folder=archive).acount() == 2
    data = (await aexecute('query($id: ID!) { task(id: $id) { threads { id } unreadCount } }', {"id": task["id"]})).data["task"]
    assert data == {"threads": [{"id": inbox["flight"]}], "unreadCount": 2}

    # The folder replaced on the server (new UIDVALIDITY): re-read, and still the same thread.
    with greenmail.imap(work["address"]) as imap:
        greenmail.recreate_folder(imap, "Archive", "Archive-old")
        imap.select_folder("Archive-old")
        imap.copy(imap.search("ALL"), "Archive")
    await sync(work["id"])
    assert await models.Message.objects.filter(thread_id=inbox["flight"], folder__path="Archive").acount() == 2
    assert (await aexecute('query($id: ID!) { task(id: $id) { threadCount } }', {"id": task["id"]})).data["task"]["threadCount"] == 1


async def test_an_unlinked_thread_emptied_is_deleted(inbox, aexecute, greenmail, sync):
    """Only a linked thread is kept without messages; unlinking lets it go."""
    task = (await aexecute(UPSERT, {"input": {"externalKey": "t", "title": "Hotel", "threads": [inbox["hotel"]]}})).data["upsertTask"]
    with greenmail.imap(inbox["private"]["address"]) as imap:
        imap.select_folder("INBOX")
        imap.delete_messages(imap.search("ALL"))
        imap.expunge()
    await sync(inbox["private"]["id"])
    assert await models.Thread.objects.filter(id=inbox["hotel"], message_count=0).aexists()
    await aexecute('mutation($t: ID!, $th: [ID!]!) { unlinkThreads(input: {task: $t, threads: $th}) { id } }', {"t": task["id"], "th": [inbox["hotel"]]})
    assert not await models.Thread.objects.filter(id=inbox["hotel"]).aexists()


async def test_tasks_are_personal(inbox, aexecute, colleague_context, other_org_context):
    task = (await aexecute(UPSERT, {"input": {"externalKey": "t", "title": "Mine", "threads": [inbox["flight"]]}})).data["upsertTask"]
    task_list = (await aexecute('mutation { createTaskList(input: {name: "Mine"}) { id } }')).data["createTaskList"]
    for context in (colleague_context, other_org_context):
        assert (await aexecute("{ tasks { id } taskLists { id } tasksCount }", context=context)).data == {"tasks": [], "taskLists": [], "tasksCount": 0}
        for document, value in [
            ('query($id: ID!) { task(id: $id) { id } }', task["id"]),
            ('query($id: ID!) { taskList(id: $id) { id } }', task_list["id"]),
            ('mutation($id: ID!) { deleteTask(id: $id) }', task["id"]),
            ('mutation($id: ID!) { linkThreads(input: {task: $id, threads: []}) { id } }', task["id"]),
        ]:
            result = await aexecute(document, {"id": value}, context=context, allow_errors=True)
            assert result.errors[0].extensions["code"] == "NOT_FOUND"
    # A colleague's own task cannot take a conversation they cannot see.
    own = (await aexecute('mutation { createTask(input: {title: "Theirs"}) { id } }', context=colleague_context)).data["createTask"]
    stolen = await aexecute('mutation($t: ID!, $th: [ID!]!) { linkThreads(input: {task: $t, threads: $th}) { id } }', {"t": own["id"], "th": [inbox["flight"]]}, context=colleague_context, allow_errors=True)
    assert stolen.errors[0].extensions["code"] == "NOT_FOUND"


async def test_links_follow_mailbox_sharing(mailbox, greenmail, sync, aexecute, colleague_context, authenticated_context):
    """A colleague's task over a shared mailbox's thread loses it when sharing ends, and gets it back."""
    shared = await mailbox()
    greenmail.deliver(shared["address"], "Team request")
    greenmail.wait_for(shared["address"], 1)
    await sync(shared["id"])
    thread = str((await models.Thread.objects.aget(account_id=shared["id"])).id)
    share = 'mutation($id: ID!, $v: Visibility!) { shareMailAccount(input: {id: $id, visibility: $v}) { id } }'
    await aexecute(share, {"id": shared["id"], "v": "ORGANIZATION"})
    task = (await aexecute(UPSERT, {"input": {"externalKey": "t", "title": "Handle it", "threads": [thread]}}, context=colleague_context)).data["upsertTask"]
    read = 'query($id: ID!) { task(id: $id) { threadCount links { id } } }'
    assert (await aexecute(read, {"id": task["id"]}, context=colleague_context)).data["task"]["threadCount"] == 1
    await aexecute(share, {"id": shared["id"], "v": "PRIVATE"})
    assert (await aexecute(read, {"id": task["id"]}, context=colleague_context)).data["task"] == {"threadCount": 0, "links": []}
    await aexecute(share, {"id": shared["id"], "v": "ORGANIZATION"})
    assert (await aexecute(read, {"id": task["id"]}, context=colleague_context)).data["task"]["threadCount"] == 1
