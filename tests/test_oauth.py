"""Linking Gmail/Microsoft mailboxes through OAuth (code + PKCE), token refresh, revocation, re-linking.

fakeoauth is strict (code bound to client, redirect URI and PKCE challenge; used once). Sending
goes through GreenMail's SMTP, which accepts XOAUTH2 (with the user's password as bearer token).
"""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.utils import timezone

from mail import crypto, models
from mail.protocols.clients import xoauth2_string
from tests.conftest import PASSWORD, REDIRECT

pytestmark = pytest.mark.django_db(transaction=True)

START = 'mutation($input: StartOAuthLinkInput!) { startOAuthLink(input: $input) { state openUrl finish redirectUrl provider expiresAt account { id } } }'
COMPLETE = 'mutation($code: String!, $state: String!) { completeOAuthLink(input: {code: $code, state: $state}) { id emailAddress displayName provider authMethod status incomingHost incomingPort saveSentCopy } }'
SEND = 'mutation($a: ID!, $to: String!) { sendMessage(input: {account: $a, to: [{address: $to}], text: "hi"}) { status errorCode error } }'


def test_xoauth2_initial_response():
    assert xoauth2_string("me@x", "tok") == "user=me@x\x01auth=Bearer tok\x01\x01"


async def test_not_configured_without_a_client(aexecute):
    result = await aexecute(START, {"input": {"provider": "GMAIL"}}, allow_errors=True)
    assert result.errors[0].extensions["code"] == "NOT_CONFIGURED"
    assert (await aexecute("{ oauthProviders }")).data["oauthProviders"] == []


async def _link(aexecute, fakeoauth, greenmail, **extra) -> tuple[dict, str]:
    address = greenmail.user()
    session = (await aexecute(START, {"input": {"provider": "GMAIL", "name": "Work", **extra}})).data["startOAuthLink"]
    code = fakeoauth.approve(session["openUrl"], address, access_token=PASSWORD, name="Work Person")
    account = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeOAuthLink"]
    return account, address


async def test_link_and_send(aexecute, fakeoauth, greenmail, backend_stack):
    assert set((await aexecute("{ oauthProviders }")).data["oauthProviders"]) == {"GMAIL", "MICROSOFT"}
    session = (await aexecute(START, {"input": {"provider": "GMAIL"}})).data["startOAuthLink"]
    query = parse_qs(urlparse(session["openUrl"]).query)
    assert session["finish"] == "REDIRECT" and session["redirectUrl"] == REDIRECT
    assert query["code_challenge_method"] == ["S256"] and query["state"] == [session["state"]]
    assert query["access_type"] == ["offline"] and "https://mail.google.com/" in query["scope"][0]

    account, address = await _link(aexecute, fakeoauth, greenmail)
    assert account["emailAddress"] == address and account["displayName"] == "Work Person"
    assert (account["provider"], account["authMethod"], account["status"]) == ("GMAIL", "XOAUTH2", "ACTIVE")
    assert (account["incomingHost"], account["incomingPort"], account["saveSentCopy"]) == ("localhost", backend_stack.imaps_port, False)
    stored = await models.MailAccount.objects.aget(id=account["id"])
    assert crypto.decrypt(stored.access_token) == PASSWORD and stored.secret and crypto.decrypt(stored.secret) != PASSWORD

    friend = greenmail.user()
    sent = (await aexecute(SEND, {"a": account["id"], "to": friend})).data["sendMessage"]
    assert sent["status"] == "SENT", sent
    greenmail.wait_for(friend, 1)


async def test_state_is_single_use_and_personal(aexecute, fakeoauth, greenmail, colleague_context):
    session = (await aexecute(START, {"input": {"provider": "GMAIL"}})).data["startOAuthLink"]
    code = fakeoauth.approve(session["openUrl"], greenmail.user(), access_token=PASSWORD)
    stolen = await aexecute(COMPLETE, {"code": code, "state": session["state"]}, context=colleague_context, allow_errors=True)
    assert stolen.errors[0].extensions["code"] == "INVALID_STATE"
    resumed = await aexecute('mutation($s: String!) { resumeOAuthLink(state: $s) { state } }', {"s": session["state"]}, context=colleague_context, allow_errors=True)
    assert resumed.errors[0].extensions["code"] == "PERMISSION_DENIED"
    assert (await aexecute('mutation($s: String!) { resumeOAuthLink(state: $s) { state } }', {"s": session["state"]})).data["resumeOAuthLink"]["state"] == session["state"]
    await aexecute(COMPLETE, {"code": code, "state": session["state"]})
    again = await aexecute(COMPLETE, {"code": code, "state": session["state"]}, allow_errors=True)
    assert again.errors[0].extensions["code"] == "INVALID_STATE"


async def test_unregistered_redirect_is_refused(aexecute, fakeoauth):
    result = await aexecute(START, {"input": {"provider": "MICROSOFT", "redirectUrl": "https://evil.example/cb"}}, allow_errors=True)
    assert result.errors[0].extensions["code"] == "INVALID_STATE"


async def test_expired_link_is_refused(aexecute, fakeoauth, greenmail):
    session = (await aexecute(START, {"input": {"provider": "GMAIL"}})).data["startOAuthLink"]
    await models.OAuthLink.objects.filter(state=session["state"]).aupdate(expires_at=timezone.now() - timedelta(seconds=1))
    result = await aexecute(COMPLETE, {"code": "whatever", "state": session["state"]}, allow_errors=True)
    assert result.errors[0].extensions["code"] == "CODE_EXPIRED"


async def test_refresh_then_revocation_then_relink(aexecute, fakeoauth, greenmail):
    account, address = await _link(aexecute, fakeoauth, greenmail)
    friend = greenmail.user()

    # An expired access token is refreshed before use.
    await models.MailAccount.objects.filter(id=account["id"]).aupdate(token_expires_at=timezone.now() - timedelta(minutes=1))
    assert (await aexecute(SEND, {"a": account["id"], "to": friend})).data["sendMessage"]["status"] == "SENT"
    assert [e["grant_type"] for e in fakeoauth.log()][-1] == "refresh_token"

    # The user revoked access at the provider: the mailbox needs to be linked again.
    fakeoauth.revoke(address)
    await models.MailAccount.objects.filter(id=account["id"]).aupdate(token_expires_at=timezone.now() - timedelta(minutes=1))
    failed = (await aexecute(SEND, {"a": account["id"], "to": friend})).data["sendMessage"]
    assert (failed["status"], failed["errorCode"]) == ("FAILED", "CONSENT_EXPIRED")
    assert (await models.MailAccount.objects.aget(id=account["id"])).status == "NEEDS_REAUTH"

    # Re-link the same mailbox: same row, ACTIVE again.
    session = (await aexecute(START, {"input": {"provider": "GMAIL", "account": account["id"]}})).data["startOAuthLink"]
    assert session["account"] == {"id": account["id"]}
    code = fakeoauth.approve(session["openUrl"], address, access_token=PASSWORD)
    relinked = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeOAuthLink"]
    assert relinked["id"] == account["id"] and relinked["status"] == "ACTIVE"
    assert (await aexecute(SEND, {"a": account["id"], "to": friend})).data["sendMessage"]["status"] == "SENT"


async def test_relink_must_approve_the_same_address(aexecute, fakeoauth, greenmail):
    account, _ = await _link(aexecute, fakeoauth, greenmail)
    session = (await aexecute(START, {"input": {"provider": "GMAIL", "account": account["id"]}})).data["startOAuthLink"]
    code = fakeoauth.approve(session["openUrl"], greenmail.user(), access_token=PASSWORD)
    result = await aexecute(COMPLETE, {"code": code, "state": session["state"]}, allow_errors=True)
    assert result.errors[0].extensions["code"] == "INVALID_STATE"


async def test_rotated_refresh_tokens_are_stored(aexecute, fakeoauth, greenmail):
    fakeoauth.config(rotate_refresh=True)
    account, _ = await _link(aexecute, fakeoauth, greenmail)
    before = crypto.decrypt((await models.MailAccount.objects.aget(id=account["id"])).secret)
    await models.MailAccount.objects.filter(id=account["id"]).aupdate(token_expires_at=timezone.now() - timedelta(minutes=1))
    assert (await aexecute(SEND, {"a": account["id"], "to": greenmail.user()})).data["sendMessage"]["status"] == "SENT"
    after = crypto.decrypt((await models.MailAccount.objects.aget(id=account["id"])).secret)
    assert after != before
