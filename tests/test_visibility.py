"""Who sees what: another organization sees nothing, and inside one a mailbox is private to the
member who linked it unless shared. Anything not visible looks exactly like a missing id."""

import pytest

from mail import models

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
async def private_box(mailbox, greenmail, sync):
    box = await mailbox()
    greenmail.deliver(box["address"], "Secret", "Only for me", attachments=[("x.txt", "text/plain", b"x")])
    greenmail.wait_for(box["address"], 1)
    await sync(box["id"])
    message = await models.Message.objects.aget(account_id=box["id"])
    return {
        "account": box["id"],
        "message": str(message.id),
        "folder": str(message.folder_id),
        "thread": str(message.thread_id),
    }


@pytest.fixture(params=["other_org_context", "colleague_context"])
def outsider(request):
    """A member of another organization, and a colleague the mailbox is not shared with."""
    return request.getfixturevalue(request.param)


LISTS = ["mailAccounts { id }", "mailFolders { id }", "messages { id }", "threads { id }", "outbox { id }"]
BY_ID = [
    ('query($id: ID!) { mailAccount(id: $id) { id } }', "account"),
    ('query($id: ID!) { mailFolder(id: $id) { id } }', "folder"),
    ('query($id: ID!) { message(id: $id) { id } }', "message"),
    ('query($id: ID!) { thread(id: $id) { id } }', "thread"),
    ('mutation($id: ID!) { syncMailAccount(id: $id) { created } }', "account"),
    ('mutation($id: ID!) { testMailAccount(id: $id) { id } }', "account"),
    ('mutation($id: ID!) { deleteMailAccount(id: $id) }', "account"),
    ('mutation($id: ID!) { updateMailFolder(input: {id: $id, syncEnabled: false}) { id } }', "folder"),
    ('mutation($id: ID!) { markMessagesRead(input: {messages: [$id]}) { id } }', "message"),
    ('mutation($id: ID!) { deleteMessages(input: {messages: [$id]}) { deleted } }', "message"),
    ('mutation($id: ID!) { sendMessage(input: {account: $id, to: [{address: "x@kuvert.test"}], text: "hi"}) { id } }', "account"),
]


@pytest.mark.parametrize("selection", LISTS)
async def test_lists_hide_private_mailboxes(private_box, aexecute, outsider, selection):
    result = await aexecute(f"query {{ {selection} }}", context=outsider)
    assert list(result.data.values())[0] == []


@pytest.mark.parametrize("document,key", BY_ID)
async def test_by_id_is_not_found(private_box, aexecute, outsider, document, key):
    result = await aexecute(document, {"id": private_box[key]}, context=outsider, allow_errors=True)
    assert result.errors and result.errors[0].extensions["code"] == "NOT_FOUND", result.errors


async def test_similar_to_ignores_invisible_anchor(private_box, aexecute, colleague_context):
    result = await aexecute('query($id: ID!) { messages(filters: {similarTo: $id}) { id } }', {"id": private_box["message"]}, context=colleague_context)
    assert result.data["messages"] == []


SHARE = 'mutation($id: ID!, $v: Visibility!, $users: [ID!]) { shareMailAccount(input: {id: $id, visibility: $v, users: $users}) { visibility sharedWith { sub } } }'


async def test_sharing_with_a_member(private_box, aexecute, colleague_context, other_org_context):
    colleague = colleague_context.request.user
    shared = (await aexecute(SHARE, {"id": private_box["account"], "v": "SHARED", "users": [str(colleague.id)]})).data["shareMailAccount"]
    assert shared == {"visibility": "SHARED", "sharedWith": [{"sub": "2"}]}
    seen = (await aexecute("{ messages { id } mailAccounts { id isOwner } }", context=colleague_context)).data
    assert [m["id"] for m in seen["messages"]] == [private_box["message"]]
    assert seen["mailAccounts"] == [{"id": private_box["account"], "isOwner": False}]
    # A member it is shared with reads and flags, but does not administer it.
    marked = await aexecute('mutation($id: ID!) { markMessagesRead(input: {messages: [$id]}) { isRead } }', {"id": private_box["message"]}, context=colleague_context)
    assert marked.data["markMessagesRead"] == [{"isRead": True}]
    for document in ['mutation($id: ID!) { deleteMailAccount(id: $id) }', 'mutation($id: ID!) { updateMailAccount(input: {id: $id, name: "x"}) { id } }']:
        denied = await aexecute(document, {"id": private_box["account"]}, context=colleague_context, allow_errors=True)
        assert denied.errors[0].extensions["code"] == "PERMISSION_DENIED"
    denied = await aexecute(SHARE, {"id": private_box["account"], "v": "ORGANIZATION"}, context=colleague_context, allow_errors=True)
    assert denied.errors[0].extensions["code"] == "PERMISSION_DENIED"
    # Never beyond the organization.
    outsider = other_org_context.request.user
    refused = await aexecute(SHARE, {"id": private_box["account"], "v": "SHARED", "users": [str(outsider.id)]}, allow_errors=True)
    assert refused.errors[0].extensions["code"] == "VALIDATION_ERROR"


async def test_organization_mailbox_is_seen_by_every_member(private_box, aexecute, colleague_context, other_org_context):
    await aexecute(SHARE, {"id": private_box["account"], "v": "ORGANIZATION"})
    assert len((await aexecute("{ messages { id } }", context=colleague_context)).data["messages"]) == 1
    assert (await aexecute("{ messages { id } }", context=other_org_context)).data["messages"] == []
    # Back to private: gone again.
    await aexecute(SHARE, {"id": private_box["account"], "v": "PRIVATE"})
    assert (await aexecute("{ messages { id } }", context=colleague_context)).data["messages"] == []
