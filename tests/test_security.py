"""Where the service may connect: no internal addresses, TLS required, credentials never exposed."""

import pytest

from mail import crypto, models
from tests.conftest import CREATE, PASSWORD

pytestmark = pytest.mark.django_db(transaction=True)


def _payload(address: str, host: str, port: int, security: str = "TLS") -> dict:
    return {"emailAddress": address, "password": PASSWORD, "incoming": {"host": host, "port": port, "security": security}}


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "db", "0.0.0.0"])
async def test_internal_hosts_are_refused(aexecute, host):
    result = await aexecute(CREATE, {"input": _payload("x@kuvert.test", host, 993)}, allow_errors=True)
    assert result.errors[0].extensions["code"] in ("HOST_NOT_ALLOWED", "CONNECTION_FAILED"), result.errors
    if host != "db":  # unresolvable outside the stack's network
        assert result.errors[0].extensions["code"] == "HOST_NOT_ALLOWED"
    assert not await models.MailAccount.objects.aexists()


async def test_plaintext_is_refused(aexecute, backend_stack):
    result = await aexecute(CREATE, {"input": _payload("x@kuvert.test", "localhost", backend_stack.smtp_port, "NONE")}, allow_errors=True)
    assert result.errors[0].extensions["code"] == "TLS_REQUIRED"


async def test_wrong_password_is_refused_before_storing(aexecute, greenmail, backend_stack):
    address = greenmail.user()
    payload = {**_payload(address, "localhost", backend_stack.imaps_port), "password": "wrong"}
    result = await aexecute(CREATE, {"input": payload}, allow_errors=True)
    assert result.errors[0].extensions["code"] == "AUTH_FAILED"
    assert not await models.MailAccount.objects.aexists()


async def test_credentials_are_encrypted_and_not_in_the_schema(mailbox, aexecute):
    box = await mailbox()
    account = await models.MailAccount.objects.aget(id=box["id"])
    assert account.secret != PASSWORD and crypto.decrypt(account.secret) == PASSWORD
    fields = (await aexecute('{ __type(name: "MailAccount") { fields { name } } }')).data["__type"]["fields"]
    names = {f["name"] for f in fields}
    assert not names & {"secret", "password", "accessToken", "smtpSecret"}


async def test_failed_sync_records_the_error_and_needs_reauth(mailbox, sync, aexecute):
    box = await mailbox()
    await models.MailAccount.objects.filter(id=box["id"]).aupdate(secret=crypto.encrypt("changed-elsewhere"))
    result = await aexecute('mutation($id: ID!) { syncMailAccount(id: $id) { created } }', {"id": box["id"]}, allow_errors=True)
    assert result.errors[0].extensions["code"] == "AUTH_FAILED"
    account = (await aexecute('query($id: ID!) { mailAccount(id: $id) { status lastErrorCode syncing } }', {"id": box["id"]})).data["mailAccount"]
    assert account == {"status": "NEEDS_REAUTH", "lastErrorCode": "AUTH_FAILED", "syncing": False}
    again = await aexecute('mutation($id: ID!) { syncMailAccount(id: $id) { created } }', {"id": box["id"]}, allow_errors=True)
    assert again.errors[0].extensions["code"] == "MAILBOX_INACTIVE"
    # A new password brings it back.
    updated = await aexecute('mutation($id: ID!, $pw: String!) { updateMailAccount(input: {id: $id, password: $pw}) { status lastError } }', {"id": box["id"], "pw": PASSWORD})
    assert updated.data["updateMailAccount"] == {"status": "ACTIVE", "lastError": None}
