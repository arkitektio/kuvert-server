"""Providers: an organization's admin sets one up, every mailbox is linked through one, and it decides what its mailboxes may do.

End to end against GreenMail and fakeoauth (which checks each client's secret). Nothing is mocked.
"""

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from mail import crypto, models
from mail.scheduled import sync_all_mailboxes
from tests.conftest import CREATE, OAUTH_CLIENT, PASSWORD, REDIRECT, make_provider

pytestmark = pytest.mark.django_db(transaction=True)

PROVIDER = "id name kind enabled capabilities mailboxCount oauth { clientId redirectUrls tenant hasSecret } server { fixed protocol incoming { host port security } smtp { host port security } saveSentCopy }"
CREATE_SERVER = f"mutation($input: CreateServerProviderInput!) {{ createServerProvider(input: $input) {{ {PROVIDER} }} }}"
UPDATE_SERVER = f"mutation($input: UpdateServerProviderInput!) {{ updateServerProvider(input: $input) {{ {PROVIDER} }} }}"
CREATE_OAUTH = f"mutation($input: CreateOAuthProviderInput!) {{ createOAuthProvider(input: $input) {{ {PROVIDER} }} }}"
UPDATE_OAUTH = f"mutation($input: UpdateOAuthProviderInput!) {{ updateOAuthProvider(input: $input) {{ {PROVIDER} }} }}"
UPDATE = f"mutation($input: UpdateProviderInput!) {{ updateProvider(input: $input) {{ {PROVIDER} }} }}"
DELETE = "mutation($id: ID!) { deleteProvider(id: $id) }"
ACCOUNT = "query($id: ID!) { mailAccount(id: $id) { id incomingHost incomingPort smtpHost canSend mailProvider { id name } } }"
UPDATE_ACCOUNT = "mutation($input: UpdateMailAccountInput!) { updateMailAccount(input: $input) { id } }"
SYNC = "mutation($id: ID!) { syncMailAccount(id: $id) { created } }"
SEND = 'mutation($a: ID!, $to: String!) { sendMessage(input: {account: $a, to: [{address: $to}], text: "hi"}) { status errorCode } }'
START = "mutation($input: StartOAuthLinkInput!) { startOAuthLink(input: $input) { state openUrl } }"
COMPLETE = "mutation($code: String!, $state: String!) { completeAuth(input: {code: $code, state: $state}) { status errorCode result { id } } }"


def _fixed(backend_stack, **fields) -> dict:
    """An IMAP provider fixed to the stack's GreenMail."""
    return {
        "input": {
            "name": "Company mail",
            "protocol": "IMAP",
            "incoming": {"host": "localhost", "port": backend_stack.imaps_port, "security": "TLS"},
            "smtp": {"host": "localhost", "port": backend_stack.smtps_port, "security": "TLS"},
            **fields,
        }
    }


def _gmail(**fields) -> dict:
    return {"input": {"kind": "GMAIL", "name": "Our Gmail", "clientId": OAUTH_CLIENT["client_id"], "clientSecret": OAUTH_CLIENT["client_secret"], "redirectUrls": [REDIRECT], **fields}}


async def _link_oauth(aexecute, fakeoauth, greenmail, provider: str) -> str:
    session = (await aexecute(START, {"input": {"provider": provider}})).data["startOAuthLink"]
    code = fakeoauth.approve(session["openUrl"], greenmail.user(), access_token=PASSWORD)
    done = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeAuth"]
    assert done["status"] == "DONE", done
    return done["result"]["id"]


# --- what the server can run -------------------------------------------------------------------


async def test_the_kinds_say_how_their_mailboxes_log_in(aexecute):
    kinds = (await aexecute("{ providerKinds { kind label authMethod capabilities { capability label description } } }")).data["providerKinds"]

    by_kind = {k["kind"]: k for k in kinds}
    assert {kind: k["authMethod"] for kind, k in by_kind.items()} == {"GENERIC": "PASSWORD", "GMAIL": "XOAUTH2", "MICROSOFT": "XOAUTH2"}
    assert all([c["capability"] for c in k["capabilities"]] == ["SEND", "SCHEDULED_SYNC"] for k in kinds)
    assert all(c["label"] and c["description"] for k in kinds for c in k["capabilities"])


# --- only admins set them up -------------------------------------------------------------------


async def test_members_see_providers_but_only_admins_change_them(aexecute, admin_context, backend_stack):
    created = (await aexecute(CREATE_OAUTH, _gmail(), context=admin_context)).data["createOAuthProvider"]
    variables = {"input": {"id": created["id"], "enabled": False}}

    refused = [
        await aexecute(CREATE_SERVER, _fixed(backend_stack), allow_errors=True),
        await aexecute(CREATE_OAUTH, _gmail(name="Mine"), allow_errors=True),
        await aexecute(UPDATE, variables, allow_errors=True),
        await aexecute(UPDATE_OAUTH, variables, allow_errors=True),
        await aexecute(DELETE, {"id": created["id"]}, allow_errors=True),
    ]
    listed = (await aexecute("{ mailProviders { id name oauth { clientId hasSecret } } }")).data["mailProviders"]
    fields = {f["name"] for f in (await aexecute('{ __type(name: "MailProvider") { fields { name } } }')).data["__type"]["fields"]}

    assert all(r.errors for r in refused) and await models.MailProvider.objects.acount() == 1
    assert listed == [{"id": created["id"], "name": "Our Gmail", "oauth": {"clientId": OAUTH_CLIENT["client_id"], "hasSecret": True}}]
    assert "secret" not in fields and "settings" not in fields
    stored = await models.MailProvider.objects.aget(id=created["id"])
    assert stored.secret != OAUTH_CLIENT["client_secret"] and crypto.decrypt(stored.secret) == OAUTH_CLIENT["client_secret"]


async def test_names_are_unique_and_settings_are_checked(aexecute, admin_context, backend_stack):
    await aexecute(CREATE_OAUTH, _gmail(), context=admin_context)

    taken = await aexecute(CREATE_SERVER, {"input": {"name": "Our Gmail"}}, context=admin_context, allow_errors=True)
    internal = await aexecute(CREATE_SERVER, _fixed(backend_stack, incoming={"host": "10.0.0.5", "port": 993, "security": "TLS"}), context=admin_context, allow_errors=True)
    plain = await aexecute(CREATE_SERVER, _fixed(backend_stack, incoming={"host": "localhost", "port": 143, "security": "NONE"}), context=admin_context, allow_errors=True)
    half = await aexecute(CREATE_SERVER, _fixed(backend_stack, protocol=None), context=admin_context, allow_errors=True)
    tenant = await aexecute(CREATE_OAUTH, _gmail(name="Tenant", tenant="contoso.com"), context=admin_context, allow_errors=True)
    nowhere = await aexecute(CREATE_OAUTH, _gmail(name="No redirect", redirectUrls=[]), context=admin_context, allow_errors=True)
    imap_as_oauth = await aexecute(CREATE_OAUTH, _gmail(name="Wrong kind", kind="GENERIC"), context=admin_context, allow_errors=True)
    no_send = await aexecute(CREATE_SERVER, {"input": {"name": "Receive only", "capabilities": []}}, context=admin_context)

    assert "already has a provider named" in taken.errors[0].message
    assert internal.errors[0].extensions["code"] == "HOST_NOT_ALLOWED"
    assert plain.errors[0].extensions["code"] == "TLS_REQUIRED"
    assert "protocol" in half.errors[0].message
    assert "only for Microsoft" in tenant.errors[0].message
    assert "redirect_urls" in nowhere.errors[0].message
    assert "createServerProvider" in imap_as_oauth.errors[0].message
    assert no_send.data["createServerProvider"]["capabilities"] == []
    assert await models.MailProvider.objects.acount() == 2


# --- IMAP providers ----------------------------------------------------------------------------


async def test_an_open_provider_takes_any_server(aexecute, admin_context, mailbox):
    open_ = (await aexecute(CREATE_SERVER, {"input": {"name": "Any server"}}, context=admin_context)).data["createServerProvider"]
    assert open_["server"] == {"fixed": False, "protocol": None, "incoming": None, "smtp": None, "saveSentCopy": True}

    box = await mailbox()  # the organization's first IMAP provider: this one
    account = (await aexecute(ACCOUNT, {"id": box["id"]})).data["mailAccount"]
    assert account["mailProvider"] == {"id": open_["id"], "name": "Any server"} and account["canSend"]


async def test_a_fixed_provider_takes_only_the_login_and_its_mailboxes_follow_it(aexecute, admin_context, greenmail, backend_stack):
    fixed = (await aexecute(CREATE_SERVER, _fixed(backend_stack), context=admin_context)).data["createServerProvider"]
    address = greenmail.user()
    login = {"provider": fixed["id"], "emailAddress": address, "password": PASSWORD}

    overridden = await aexecute(CREATE, {"input": {**login, "incoming": {"host": "imap.example.org", "port": 993, "security": "TLS"}}}, allow_errors=True)
    other_protocol = await aexecute(CREATE, {"input": {**login, "protocol": "POP3"}}, allow_errors=True)
    created = (await aexecute(CREATE, {"input": login})).data["createMailAccount"]
    moved = await aexecute(UPDATE_ACCOUNT, {"input": {"id": created["id"], "incoming": {"host": "imap.example.org", "port": 993, "security": "TLS"}}}, allow_errors=True)
    account = (await aexecute(ACCOUNT, {"id": created["id"]})).data["mailAccount"]

    assert "fixes the servers" in overridden.errors[0].message and "reads mail with IMAP" in other_protocol.errors[0].message
    assert "fixes its servers" in moved.errors[0].message
    assert (account["incomingHost"], account["incomingPort"], account["smtpHost"], account["canSend"]) == ("localhost", backend_stack.imaps_port, "localhost", True)

    # The admin drops the SMTP server: the mailbox follows, and can no longer send.
    changed = (await aexecute(UPDATE_SERVER, {"input": {"id": fixed["id"], "smtp": None}}, context=admin_context)).data["updateServerProvider"]
    account = (await aexecute(ACCOUNT, {"id": created["id"]})).data["mailAccount"]
    assert changed["server"]["smtp"] is None and changed["mailboxCount"] == 1
    assert (account["smtpHost"], account["canSend"]) == (None, False)

    # What the mailboxes were linked as cannot change under them.
    opened = await aexecute(UPDATE_SERVER, {"input": {"id": fixed["id"], "protocol": None, "incoming": None}}, context=admin_context, allow_errors=True)
    assert "cannot change" in opened.errors[0].message


async def test_an_oauth_provider_does_not_take_passwords(aexecute, provider_for, greenmail):
    result = await aexecute(CREATE, {"input": {"provider": await provider_for("GMAIL"), "emailAddress": greenmail.user(), "password": PASSWORD}}, allow_errors=True)
    assert "startOAuthLink" in result.errors[0].message and not await models.MailAccount.objects.aexists()


# --- what a provider allows --------------------------------------------------------------------


async def test_a_disabled_provider_links_syncs_and_sends_nothing(aexecute, admin_context, mailbox, provider_for, greenmail):
    box = await mailbox()
    provider = await provider_for("GENERIC")
    await aexecute(UPDATE, {"input": {"id": provider, "enabled": False}}, context=admin_context)

    synced = await aexecute(SYNC, {"id": box["id"]}, allow_errors=True)
    sent = await aexecute(SEND, {"a": box["id"], "to": greenmail.user()}, allow_errors=True)
    tested = await aexecute("mutation($id: ID!) { testMailAccount(id: $id) { id } }", {"id": box["id"]}, allow_errors=True)
    linked = await aexecute(CREATE, {"input": {"provider": provider, "emailAddress": greenmail.user(), "password": PASSWORD, "incoming": {"host": "localhost", "port": 1, "security": "TLS"}}}, allow_errors=True)
    scheduled = await sync_all_mailboxes("static_org")
    account = (await aexecute(ACCOUNT, {"id": box["id"]})).data["mailAccount"]

    assert [r.errors[0].extensions["code"] for r in (synced, sent, tested, linked)] == ["NOT_CONFIGURED"] * 4
    assert scheduled == {"synced": 0, "skipped": 0, "failed": 0, "created": 0}
    assert not account["canSend"] and (await models.MailAccount.objects.aget(id=box["id"])).status == models.MailAccountStatus.ACTIVE


async def test_capabilities_decide_sending_and_scheduled_syncs(aexecute, admin_context, mailbox, provider_for, greenmail):
    box = await mailbox()
    provider = await provider_for("GENERIC")
    greenmail.deliver(box["address"], "Hello")
    greenmail.wait_for(box["address"], 1)

    receive_only = (await aexecute(UPDATE, {"input": {"id": provider, "capabilities": []}}, context=admin_context)).data["updateProvider"]
    can_send = (await aexecute(ACCOUNT, {"id": box["id"]})).data["mailAccount"]["canSend"]
    sent = await aexecute(SEND, {"a": box["id"], "to": greenmail.user()}, allow_errors=True)
    skipped = await sync_all_mailboxes("static_org")
    by_hand = (await aexecute(SYNC, {"id": box["id"]})).data["syncMailAccount"]
    await aexecute(UPDATE, {"input": {"id": provider, "capabilities": ["SEND", "SCHEDULED_SYNC"]}}, context=admin_context)

    assert receive_only["capabilities"] == [] and not can_send
    assert sent.errors[0].extensions["code"] == "NOT_CONFIGURED" and "Sending" in sent.errors[0].message
    assert skipped["synced"] == 0 and by_hand["created"] == 1
    assert (await sync_all_mailboxes("static_org"))["synced"] == 1
    assert (await aexecute(SEND, {"a": box["id"], "to": greenmail.user()})).data["sendMessage"]["status"] == "SENT"


async def test_a_provider_with_mailboxes_cannot_be_deleted(aexecute, admin_context, mailbox, provider_for):
    box = await mailbox()
    provider = await provider_for("GENERIC")

    refused = await aexecute(DELETE, {"id": provider}, context=admin_context, allow_errors=True)
    await aexecute("mutation($id: ID!) { deleteMailAccount(id: $id) }", {"id": box["id"]})
    deleted = (await aexecute(DELETE, {"id": provider}, context=admin_context)).data["deleteProvider"]

    assert "1 mailbox(es) still go through" in refused.errors[0].message
    assert deleted == provider and not await models.MailProvider.objects.aexists()


# --- OAuth providers ---------------------------------------------------------------------------


async def test_an_admins_oauth_client_links_mailboxes_and_refreshes_their_tokens(aexecute, admin_context, fakeoauth, greenmail):
    ours = (await aexecute(CREATE_OAUTH, _gmail(), context=admin_context)).data["createOAuthProvider"]
    # Another Gmail provider with a client fakeoauth does not know: never used for this mailbox.
    await aexecute(CREATE_OAUTH, _gmail(name="Broken", clientSecret="wrong"), context=admin_context)

    account_id = await _link_oauth(aexecute, fakeoauth, greenmail, ours["id"])
    assert (await aexecute(ACCOUNT, {"id": account_id})).data["mailAccount"]["mailProvider"]["id"] == ours["id"]

    await models.MailAccount.objects.filter(id=account_id).aupdate(token_expires_at=timezone.now() - timedelta(minutes=1))
    assert (await aexecute(SEND, {"a": account_id, "to": greenmail.user()})).data["sendMessage"]["status"] == "SENT"

    # The provider's secret is what the refresh presents: a wrong one is refused by the provider.
    await aexecute(UPDATE_OAUTH, {"input": {"id": ours["id"], "clientSecret": "rotated-wrongly"}}, context=admin_context)
    await models.MailAccount.objects.filter(id=account_id).aupdate(token_expires_at=timezone.now() - timedelta(minutes=1))
    failed = (await aexecute(SEND, {"a": account_id, "to": greenmail.user()})).data["sendMessage"]
    assert (failed["status"], failed["errorCode"]) == ("FAILED", "PROVIDER_ERROR")

    # An empty secret keeps the stored one; the client id stays while mailboxes go through it.
    kept = (await aexecute(UPDATE_OAUTH, {"input": {"id": ours["id"], "clientSecret": "", "name": "Renamed"}}, context=admin_context)).data["updateOAuthProvider"]
    other_client = await aexecute(UPDATE_OAUTH, {"input": {"id": ours["id"], "clientId": "another-client"}}, context=admin_context, allow_errors=True)
    assert kept["name"] == "Renamed" and crypto.decrypt((await models.MailProvider.objects.aget(id=ours["id"])).secret) == "rotated-wrongly"
    assert "another provider" in other_client.errors[0].message


async def test_a_relink_can_move_a_mailbox_to_another_provider_of_its_kind(aexecute, fakeoauth, greenmail, provider_for):
    account_id = await _link_oauth(aexecute, fakeoauth, greenmail, fakeoauth.gmail)
    address = (await models.MailAccount.objects.aget(id=account_id)).email_address
    second = str((await models.MailProvider.objects.acreate(**{**{f: getattr(await models.MailProvider.objects.aget(id=fakeoauth.gmail), f) for f in ("organization_id", "kind", "settings", "secret", "capabilities")}, "name": "Second Gmail"})).id)

    microsoft = await aexecute(START, {"input": {"provider": fakeoauth.microsoft, "account": account_id}}, allow_errors=True)
    session = (await aexecute(START, {"input": {"provider": second, "account": account_id}})).data["startOAuthLink"]
    code = fakeoauth.approve(session["openUrl"], address, access_token=PASSWORD)
    done = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeAuth"]

    assert "GMAIL provider" in microsoft.errors[0].message
    assert done["result"]["id"] == account_id
    assert str((await models.MailAccount.objects.aget(id=account_id)).mail_provider_id) == second


async def test_mailboxes_from_before_providers_attach_to_the_first_one_of_their_kind(aexecute, admin_context, authenticated_context):
    from channels.db import database_sync_to_async

    def _loose() -> models.MailAccount:
        return models.MailAccount.objects.create(
            organization=authenticated_context.request.organization, creator=authenticated_context.request.user, name="Old", email_address="old@gmail.com",
            provider=models.Provider.GMAIL, auth_method=models.AuthMethod.XOAUTH2, incoming_host="imap.gmail.com", incoming_port=993, username="old@gmail.com",
        )

    old = await database_sync_to_async(_loose)()
    first = (await aexecute(CREATE_OAUTH, _gmail(), context=admin_context)).data["createOAuthProvider"]
    await aexecute(CREATE_OAUTH, _gmail(name="Second"), context=admin_context)

    assert str((await models.MailAccount.objects.aget(id=old.id)).mail_provider_id) == first["id"]


def test_rotating_the_key_reencrypts_provider_secrets(settings, tmp_path, authenticated_context):
    from cryptography.fernet import Fernet

    provider = make_provider(authenticated_context.request.organization, "GMAIL")
    before = provider.secret
    keys = tmp_path / "keys"
    keys.write_text(Fernet.generate_key().decode() + "\n" + open(settings.KUVERT_SECRETS["key_path"]).read())
    settings.KUVERT_SECRETS = {**settings.KUVERT_SECRETS, "key_path": str(keys)}

    call_command("rotate_secrets")
    provider.refresh_from_db()

    assert provider.secret != before and crypto.decrypt(provider.secret) == OAUTH_CLIENT["client_secret"]
