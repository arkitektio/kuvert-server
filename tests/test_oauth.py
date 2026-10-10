"""Linking Gmail/Microsoft mailboxes through OAuth (code + PKCE), token refresh, revocation, re-linking.

fakeoauth is strict (code bound to client, redirect URI and PKCE challenge; used once). Sending
goes through GreenMail's SMTP, which accepts XOAUTH2 (with the user's password as bearer token).
"""

import asyncio
import threading
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.utils import timezone

from kuvert_server.schema import schema
from mail import crypto, models
from mail.protocols.clients import xoauth2_string
from tests.conftest import PASSWORD, REDIRECT

pytestmark = pytest.mark.django_db(transaction=True)

#: An auth session, as the external auth flow contract shapes it.
SESSION = "state status finish openUrl expiresAt redirectUrl interval userCode step errorCode errorMessage result { identifier id label }"
START = "mutation($input: StartOAuthLinkInput!) { startOAuthLink(input: $input) { %s } }" % SESSION
COMPLETE = "mutation($code: String, $state: String!, $error: String, $description: String) { completeAuth(input: {code: $code, state: $state, error: $error, errorDescription: $description}) { %s } }" % SESSION
READ = "query($s: String!) { authSession(state: $s) { %s } }" % SESSION
RESUME = "mutation($s: String!) { resumeAuth(state: $s) { %s } }" % SESSION
CANCEL = "mutation($s: String!) { cancelAuth(state: $s) { %s } }" % SESSION
ACCOUNT = "query($id: ID!) { mailAccount(id: $id) { id emailAddress displayName provider authMethod status incomingHost incomingPort saveSentCopy pendingAuth { state status } } }"
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
    done = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeAuth"]
    assert done["status"] == "DONE" and done["result"] == {"identifier": "@kuvert/account", "id": done["result"]["id"], "label": address}, done
    account = (await aexecute(ACCOUNT, {"id": done["result"]["id"]})).data["mailAccount"]
    return account, address


async def test_link_and_send(aexecute, fakeoauth, greenmail, backend_stack):
    assert set((await aexecute("{ oauthProviders }")).data["oauthProviders"]) == {"GMAIL", "MICROSOFT"}
    session = (await aexecute(START, {"input": {"provider": "GMAIL"}})).data["startOAuthLink"]
    query = parse_qs(urlparse(session["openUrl"]).query)
    assert (session["status"], session["finish"], session["redirectUrl"]) == ("PENDING", "REDIRECT", REDIRECT)
    assert session["interval"] is None and session["userCode"] is None and session["step"] is None and session["result"] is None
    assert len(session["state"]) >= 43  # 32 random bytes, url-safe
    assert (await aexecute(READ, {"s": session["state"]})).data["authSession"] == session
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


def _exchanges(fakeoauth, since: int) -> int:
    return len([e for e in fakeoauth.log()[since:] if e["grant_type"] == "authorization_code"])


@pytest.mark.parametrize("who", ["colleague_context", "other_org_context"])
async def test_only_who_started_a_login_reaches_it(aexecute, fakeoauth, greenmail, who, colleague_context, other_org_context):
    """Another member of the organization, and another organization, get what an unknown state gets."""
    stranger = {"colleague_context": colleague_context, "other_org_context": other_org_context}[who]
    session = (await aexecute(START, {"input": {"provider": "GMAIL"}})).data["startOAuthLink"]
    code = fakeoauth.approve(session["openUrl"], greenmail.user(), access_token=PASSWORD)
    seen = len(fakeoauth.log())

    answers = [
        await aexecute(READ, {"s": session["state"]}, context=stranger, allow_errors=True),
        await aexecute(RESUME, {"s": session["state"]}, context=stranger, allow_errors=True),
        await aexecute(CANCEL, {"s": session["state"]}, context=stranger, allow_errors=True),
        await aexecute(COMPLETE, {"code": code, "state": session["state"]}, context=stranger, allow_errors=True),
    ]
    unknown = await aexecute(READ, {"s": "not-a-state"}, allow_errors=True)
    resumed = (await aexecute(RESUME, {"s": session["state"]})).data["resumeAuth"]

    assert [a.errors[0].extensions["code"] for a in answers] == ["INVALID_STATE"] * 4
    assert [a.errors[0].message for a in answers] == [unknown.errors[0].message] * 4
    assert resumed == session and _exchanges(fakeoauth, seen) == 0


async def test_completing_a_finished_login_again_answers_the_same_and_exchanges_nothing(aexecute, fakeoauth, greenmail):
    session = (await aexecute(START, {"input": {"provider": "GMAIL"}})).data["startOAuthLink"]
    code = fakeoauth.approve(session["openUrl"], greenmail.user(), access_token=PASSWORD)
    seen = len(fakeoauth.log())

    first = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeAuth"]
    again = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeAuth"]
    without_code = (await aexecute(COMPLETE, {"state": session["state"]})).data["completeAuth"]
    cancelled = (await aexecute(CANCEL, {"s": session["state"]})).data["cancelAuth"]

    assert first["status"] == "DONE" and again == first and without_code == first and cancelled == first
    assert _exchanges(fakeoauth, seen) == 1
    assert await models.MailAccount.objects.acount() == 1


async def test_two_completions_at_once_exchange_the_code_once(aexecute, authenticated_context, fakeoauth, greenmail):
    """The callback page and a pasted redirect race: a real second thread, parked mid-exchange at the provider."""
    session = (await aexecute(START, {"input": {"provider": "GMAIL"}})).data["startOAuthLink"]
    variables = {"code": fakeoauth.approve(session["openUrl"], greenmail.user(), access_token=PASSWORD), "state": session["state"]}
    seen = len(fakeoauth.log())
    outcome: dict = {}

    def first() -> None:
        from django.db import connection

        try:
            outcome["result"] = asyncio.run(schema.execute(COMPLETE, variable_values=variables, context_value=authenticated_context))
        finally:
            connection.close()

    fakeoauth.hold()
    thread = threading.Thread(target=first)
    thread.start()
    deadline = time.monotonic() + 10
    while fakeoauth.held() != 1:
        assert time.monotonic() < deadline, "the first completion never reached the provider"
        await asyncio.sleep(0.02)

    meanwhile = (await aexecute(COMPLETE, variables)).data["completeAuth"]
    fakeoauth.release()
    await asyncio.to_thread(thread.join)
    winner = outcome["result"]
    after = (await aexecute(COMPLETE, variables)).data["completeAuth"]

    assert meanwhile["status"] == "PENDING" and meanwhile["errorCode"] is None
    assert not winner.errors and winner.data["completeAuth"]["status"] == "DONE"
    assert after == winner.data["completeAuth"]
    assert _exchanges(fakeoauth, seen) == 1
    assert await models.MailAccount.objects.acount() == 1


async def test_a_code_the_provider_refuses_ends_the_login_as_failed(aexecute, fakeoauth):
    session = (await aexecute(START, {"input": {"provider": "GMAIL"}})).data["startOAuthLink"]

    failed = (await aexecute(COMPLETE, {"code": "not-the-code", "state": session["state"]})).data["completeAuth"]
    read = (await aexecute(READ, {"s": session["state"]})).data["authSession"]

    assert (failed["status"], failed["errorCode"]) == ("FAILED", "CONSENT_EXPIRED") and failed["errorMessage"]
    assert failed["result"] is None and read == failed
    assert (await models.OAuthLink.objects.aget(state=session["state"])).code_verifier == ""


async def test_the_providers_refusal_is_recorded_in_its_words(aexecute, fakeoauth, greenmail):
    session = (await aexecute(START, {"input": {"provider": "GMAIL"}})).data["startOAuthLink"]
    seen = len(fakeoauth.log())

    refused = (await aexecute(COMPLETE, {"state": session["state"], "error": "access_denied", "description": "The user denied the request."})).data["completeAuth"]
    code = fakeoauth.approve(session["openUrl"], greenmail.user(), access_token=PASSWORD)
    later = (await aexecute(COMPLETE, {"state": session["state"], "code": code})).data["completeAuth"]

    assert (refused["status"], refused["errorCode"], refused["errorMessage"]) == ("FAILED", "PROVIDER_ERROR", "The user denied the request.")
    assert later == refused and _exchanges(fakeoauth, seen) == 0


async def test_an_unreachable_provider_does_not_end_a_login(aexecute, fakeoauth, greenmail, settings):
    """Not getting an answer is an error of that call, not of the login: the code is still good."""
    session = (await aexecute(START, {"input": {"provider": "GMAIL"}})).data["startOAuthLink"]
    code = fakeoauth.approve(session["openUrl"], greenmail.user(), access_token=PASSWORD)
    reachable = settings.KUVERT_OAUTH
    settings.KUVERT_OAUTH = {**reachable, "google": {**reachable["google"], "token_url": "http://127.0.0.1:9/token"}}

    unreachable = await aexecute(COMPLETE, {"code": code, "state": session["state"]}, allow_errors=True)
    read = (await aexecute(READ, {"s": session["state"]})).data["authSession"]
    settings.KUVERT_OAUTH = reachable
    done = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeAuth"]

    assert unreachable.errors[0].extensions["code"] == "PROVIDER_ERROR"
    assert read["status"] == "PENDING"
    assert done["status"] == "DONE"


async def test_cancel_is_idempotent_and_the_login_can_still_be_read(aexecute, fakeoauth, greenmail):
    session = (await aexecute(START, {"input": {"provider": "GMAIL"}})).data["startOAuthLink"]
    code = fakeoauth.approve(session["openUrl"], greenmail.user(), access_token=PASSWORD)
    seen = len(fakeoauth.log())

    cancelled = (await aexecute(CANCEL, {"s": session["state"]})).data["cancelAuth"]
    again = (await aexecute(CANCEL, {"s": session["state"]})).data["cancelAuth"]
    read = (await aexecute(READ, {"s": session["state"]})).data["authSession"]
    completed = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeAuth"]

    assert cancelled["status"] == "CANCELLED" and again == cancelled and read == cancelled and completed == cancelled
    assert _exchanges(fakeoauth, seen) == 0 and not await models.MailAccount.objects.aexists()


async def test_unregistered_redirect_is_refused(aexecute, fakeoauth):
    result = await aexecute(START, {"input": {"provider": "MICROSOFT", "redirectUrl": "https://evil.example/cb"}}, allow_errors=True)
    assert result.errors[0].extensions["code"] == "INVALID_STATE"


async def test_expired_link_is_refused(aexecute, fakeoauth, greenmail):
    session = (await aexecute(START, {"input": {"provider": "GMAIL"}})).data["startOAuthLink"]
    await models.OAuthLink.objects.filter(state=session["state"]).aupdate(expires_at=timezone.now() - timedelta(seconds=1))
    seen = len(fakeoauth.log())

    read = (await aexecute(READ, {"s": session["state"]})).data["authSession"]
    resumed = (await aexecute(RESUME, {"s": session["state"]})).data["resumeAuth"]
    completed = (await aexecute(COMPLETE, {"code": "whatever", "state": session["state"]})).data["completeAuth"]

    assert (read["status"], read["errorCode"]) == ("EXPIRED", "CODE_EXPIRED") and resumed == read
    assert completed["status"] == "EXPIRED" and _exchanges(fakeoauth, seen) == 0


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
    # A re-link names its mailbox from the start, and the mailbox offers the login until it is finished.
    assert session["status"] == "PENDING" and session["result"] == {"identifier": "@kuvert/account", "id": account["id"], "label": address}
    assert (await aexecute(ACCOUNT, {"id": account["id"]})).data["mailAccount"]["pendingAuth"] == {"state": session["state"], "status": "PENDING"}
    code = fakeoauth.approve(session["openUrl"], address, access_token=PASSWORD)
    done = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeAuth"]
    relinked = (await aexecute(ACCOUNT, {"id": done["result"]["id"]})).data["mailAccount"]
    assert done["status"] == "DONE" and relinked["id"] == account["id"] and relinked["status"] == "ACTIVE" and relinked["pendingAuth"] is None
    assert (await aexecute(SEND, {"a": account["id"], "to": friend})).data["sendMessage"]["status"] == "SENT"


async def test_relink_must_approve_the_same_address(aexecute, fakeoauth, greenmail):
    account, _ = await _link(aexecute, fakeoauth, greenmail)
    session = (await aexecute(START, {"input": {"provider": "GMAIL", "account": account["id"]}})).data["startOAuthLink"]
    code = fakeoauth.approve(session["openUrl"], greenmail.user(), access_token=PASSWORD)
    failed = (await aexecute(COMPLETE, {"code": code, "state": session["state"]})).data["completeAuth"]
    assert (failed["status"], failed["errorCode"]) == ("FAILED", "INVALID_STATE") and account["emailAddress"] in failed["errorMessage"]


async def test_rotated_refresh_tokens_are_stored(aexecute, fakeoauth, greenmail):
    fakeoauth.config(rotate_refresh=True)
    account, _ = await _link(aexecute, fakeoauth, greenmail)
    before = crypto.decrypt((await models.MailAccount.objects.aget(id=account["id"])).secret)
    await models.MailAccount.objects.filter(id=account["id"]).aupdate(token_expires_at=timezone.now() - timedelta(minutes=1))
    assert (await aexecute(SEND, {"a": account["id"], "to": greenmail.user()})).data["sendMessage"]["status"] == "SENT"
    after = crypto.decrypt((await models.MailAccount.objects.aget(id=account["id"])).secret)
    assert after != before
