"""kuvert's model signals: what it declares to the hub's rekuest, and that a save reaches it signed.

The save goes to a real local HTTP server standing in for rekuest's signal intake, and is
checked the way rekuest checks it (the instance-key JWT, the body).
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from joserfc.jwk import OKPKey

from arkitekt_service import trust
from kuvert_server.service import service

EXPECTED = {
    "@kuvert/mailprovider": [
        "CREATED",
        "UPDATED",
        "DELETED"
    ],
    "@kuvert/message": [
        "CREATED"
    ],
    "@kuvert/thread": [
        "CREATED",
        "UPDATED"
    ],
    "@kuvert/outgoingmessage": [
        "CREATED",
        "UPDATED"
    ]
}

KEY = OKPKey.generate_key("Ed25519")


class _Intake:
    def __init__(self) -> None:
        self.received: list[dict] = []
        intake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                intake.received.append({"path": self.path, "headers": dict(self.headers), "body": body, "json": json.loads(body)})
                self.send_response(202)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def of(self, identifier: str, count: int = 1, timeout: float = 10) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [r for r in self.received if r["json"]["identifier"] == identifier]
            if len(found) >= count:
                return found
            time.sleep(0.05)
        return [r for r in self.received if r["json"]["identifier"] == identifier]


@pytest.fixture
def intake(settings):
    server = _Intake()
    settings.REKUEST_SERVICE = {"REKUEST_URL": server.url, "SERVICE": "kuvert"}
    settings.INSTANCE = {
        "PRIVATE_KEY": KEY.as_pem(private=True).decode(),
        "TRUST_JWKS": {"keys": [{**trust.public_jwk(KEY), "service": "kuvert"}]},
    }
    yield server
    server.server.shutdown()
    # Closed, not only stopped: a signal still queued for this intake is refused at once
    # instead of holding the one sender thread until its timeout.
    server.server.server_close()


def _organization():
    from authentikate.models import Organization

    return Organization.objects.get_or_create(identifier="static_issuer#signals-test-org")[0]


def test_the_manifest_declares_every_model_signal():
    assert {s["identifier"]: s["kinds"] for s in service.manifest()["signals"]} == EXPECTED


def test_the_manifest_lists_what_kuvert_hosts_with_its_descriptors():
    hosted = {s["identifier"]: s for s in service.manifest()["structures"]}
    # Everything hosted is signalled (mail for team mailboxes only); nothing else is declared.
    assert set(hosted) == set(EXPECTED)
    assert hosted["@kuvert/outgoingmessage"]["label"] == "Outgoing Email"
    assert hosted["@kuvert/message"]["descriptors"] == [{"key": "@kuvert/has_attachments", "type": "BOOL", "description": "Whether the email has attachments (attached files), not counting inline images"}]
    assert [(d["key"], d["type"]) for d in hosted["@kuvert/thread"]["descriptors"]] == [("@kuvert/message_count", "INT")]
    assert [(d["key"], d["type"]) for d in hosted["@kuvert/outgoingmessage"]["descriptors"]] == [("@kuvert/status", "STRING")]
    assert [(d["key"], d["type"]) for d in hosted["@kuvert/mailprovider"]["descriptors"]] == [("@kuvert/provider_kind", "STRING"), ("@kuvert/enabled", "BOOL"), ("@kuvert/capabilities", "LIST")]
    # The signal of a structure carries exactly the keys the structure declares.
    for signal in service.manifest()["signals"]:
        assert signal["descriptors"] == [d["key"] for d in hosted[signal["identifier"]]["descriptors"]]


def _thread(visibility: str):
    from mail.models import MailAccount, Thread

    account = MailAccount.objects.create(
        organization=_organization(), name=f"{visibility} box", email_address=f"{visibility.lower()}@example.org",
        incoming_host="imap.example.org", incoming_port=993, username=visibility.lower(), visibility=visibility,
    )
    return Thread.objects.create(account=account)


@pytest.mark.django_db(transaction=True)
def test_team_mailbox_threads_are_signalled(intake):
    thread = _thread("ORGANIZATION")
    (received,) = intake.of("@kuvert/thread")
    assert (received["json"]["kind"], received["json"]["object"]) == ("CREATED", str(thread.pk))
    assert trust.verify("POST", received["path"], received["body"], received["headers"]["Authorization"], audience="rekuest").issuer == "kuvert"


@pytest.mark.django_db(transaction=True)
def test_a_provider_change_is_signalled_without_its_credentials(intake):
    from tests.conftest import make_provider

    provider = make_provider(_organization(), "GMAIL", name="Gmail")
    provider.enabled = False
    provider.save()
    created, updated = intake.of("@kuvert/mailprovider", count=2)
    assert [created["json"]["kind"], updated["json"]["kind"]] == ["CREATED", "UPDATED"]
    assert updated["json"]["descriptors"] == {"@kuvert/provider_kind": "GMAIL", "@kuvert/enabled": False, "@kuvert/capabilities": ["SCHEDULED_SYNC", "SEND"]}
    assert "kuvert-test-secret" not in updated["body"].decode() and "secret" not in updated["json"]


@pytest.mark.django_db(transaction=True)
def test_private_mail_is_never_signalled(intake):
    _thread("PRIVATE")
    _thread("SHARED")
    assert intake.of("@kuvert/thread", timeout=1) == []
