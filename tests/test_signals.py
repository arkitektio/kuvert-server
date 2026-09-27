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

from rekuest_service import trust
from kuvert_server.service import service

EXPECTED = {
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
    settings.REKUEST_HOOK = {"REKUEST_URL": server.url, "SERVICE": "kuvert"}
    settings.INSTANCE = {
        "PRIVATE_KEY": KEY.as_pem(private=True).decode(),
        "TRUST_JWKS": {"keys": [{**trust.public_jwk(KEY), "service": "live.arkitekt.kuvert"}]},
    }
    yield server
    server.server.shutdown()


def _organization():
    from authentikate.models import Organization

    return Organization.objects.get_or_create(slug="signals-test-org")[0]


def test_the_manifest_declares_every_model_signal():
    assert {s["identifier"]: s["kinds"] for s in service.manifest()["signals"]} == EXPECTED


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
    assert trust.verify("POST", received["path"], received["body"], received["headers"]["Authorization"], audience="live.arkitekt.rekuest").issuer == "live.arkitekt.kuvert"


@pytest.mark.django_db(transaction=True)
def test_private_mail_is_never_signalled(intake):
    _thread("PRIVATE")
    _thread("SHARED")
    assert intake.of("@kuvert/thread", timeout=1) == []
