"""Shared pytest fixtures for the kuvert service.

The suite runs against a real stack brought up by dokker (``tests/integration``): postgres,
GreenMail -- a real SMTP/IMAP/POP3 server with TLS and authentication --, RustFS (S3) and
``fakeoauth``, a strict stand-in for Google's and Microsoft's token endpoints. Nothing in the
service is mocked: a test seeds mail over SMTP and reads it back through the GraphQL schema.

* ``backend_stack`` — brings the stack up once per session; yields its ephemeral ports.
* ``greenmail`` — creates mail users, delivers mail, and pokes at mailboxes over IMAP directly.
* ``authenticated_context`` / ``colleague_context`` / ``other_org_context`` — two members of one
  organization and one of another, for visibility and tenancy tests.
* ``mailbox`` — a GreenMail user linked as a mailbox (IMAP or POP3) through ``createMailAccount``.
* ``oauth`` / ``fakeoauth`` — both OAuth providers pointed at ``fakeoauth``.
* ``datalayer`` — switches the datalayer on against the stack's RustFS.
"""

import json
import poplib
import smtplib
import ssl
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

import psycopg
import pytest
from authentikate.models import Client, Membership, Organization, User
from dokker import testing
from imapclient import IMAPClient
from kante.context import HttpContext, UniversalRequest
from strawberry.http.temporal_response import TemporalResponse

from kuvert_server.schema import schema

MAIL_DOMAIN = "kuvert.test"
PASSWORD = "secret-pw"


@dataclass
class Stack:
    """Where the test stack's services landed on this host."""

    db_port: int
    smtp_port: int
    smtps_port: int
    imaps_port: int
    pop3s_port: int
    mail_api_url: str
    fakeoauth_url: str
    rustfs_port: int


def _wait(check, what: str, timeout: float = 60) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while True:
        try:
            check()
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{what} did not come up in {timeout}s")
            time.sleep(0.2)


@pytest.fixture(scope="session")
def backend_stack():
    """Bring up the stack and yield the host ports docker gave it (none are pinned)."""
    compose = Path(__file__).parent / "integration" / "docker-compose.yaml"
    with testing(str(compose)) as e:
        e.up()
        ports: dict[str, int] = {}

        def port(service: str, inner: int) -> int:
            key = f"{service}:{inner}"
            if key not in ports:
                ports[key] = e.get_port(service, inner)
            return ports[key]

        def db_ready() -> None:
            with psycopg.connect(dbname="testdb", user="test", password="test", host="localhost", port=port("db", 5432), connect_timeout=1) as connection:
                connection.execute("SELECT 1")

        def mail_ready() -> None:
            urllib.request.urlopen(f"http://localhost:{port('mail', 8080)}/api/service/readiness", timeout=1).read()
            # Every listener greets (the API answers before the TLS ones do).
            with IMAPClient("localhost", port("mail", 3993), ssl=True, ssl_context=_tls(), timeout=2):
                pass
            with smtplib.SMTP_SSL("localhost", port("mail", 3465), context=_tls(), timeout=2, local_hostname="kuvert.test"):
                pass
            with smtplib.SMTP("localhost", port("mail", 3025), timeout=2, local_hostname="kuvert.test"):
                pass
            pop = poplib.POP3_SSL("localhost", port("mail", 3995), context=_tls(), timeout=2)
            pop.quit()

        def fakeoauth_ready() -> None:
            urllib.request.urlopen(f"http://localhost:{port('fakeoauth', 8000)}/_admin/health", timeout=1).read()

        def rustfs_ready() -> None:
            try:
                urllib.request.urlopen(f"http://localhost:{port('rustfs', 9000)}/", timeout=1)
            except urllib.error.HTTPError:
                pass  # any S3 answer (403 to an anonymous request) means it is up

        _wait(db_ready, "postgres")
        _wait(mail_ready, "greenmail", timeout=120)
        _wait(rustfs_ready, "rustfs")
        _wait(fakeoauth_ready, "fakeoauth", timeout=180)  # the first run builds its image
        yield Stack(
            db_port=port("db", 5432),
            smtp_port=port("mail", 3025),
            smtps_port=port("mail", 3465),
            imaps_port=port("mail", 3993),
            pop3s_port=port("mail", 3995),
            mail_api_url=f"http://localhost:{port('mail', 8080)}",
            fakeoauth_url=f"http://localhost:{port('fakeoauth', 8000)}",
            rustfs_port=port("rustfs", 9000),
        )


@pytest.fixture(scope="session", autouse=True)
def embedding_model_warm():
    """Load the embedding model once per session, outside any test's DB transaction."""
    from embeddings import engine

    engine.warm_up()
    yield


@pytest.fixture(scope="session")
def django_db_modify_db_settings(backend_stack):
    """Point Django at the stack's postgres before pytest-django creates the test database."""
    from django.conf import settings

    settings.DATABASES["default"]["PORT"] = str(backend_stack.db_port)
    yield


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Kill the connections of worker threads before the test database is dropped."""
    yield
    from django.db import connections

    with django_db_blocker.unblock():
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
        connections.close_all()


@pytest.fixture(scope="session", autouse=True)
def fernet_key(tmp_path_factory):
    """A Fernet key generated for the run; nothing is committed."""
    from cryptography.fernet import Fernet
    from django.conf import settings

    from mail import crypto

    key = tmp_path_factory.mktemp("secrets") / "kuvert.fernet"
    key.write_bytes(Fernet.generate_key() + b"\n")
    settings.KUVERT_SECRETS = {"key_path": str(key)}
    crypto._fernet.cache_clear()
    yield key


def _post(url: str, body: dict) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"{}")


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read())


def _tls() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class GreenMail:
    """Users, delivery and direct IMAP access to the stack's GreenMail."""

    def __init__(self, stack: Stack) -> None:
        self.stack = stack

    def user(self, password: str = PASSWORD) -> str:
        """A fresh mail user (unique per call); returns its address."""
        address = f"u{uuid.uuid4().hex[:12]}@{MAIL_DOMAIN}"
        _post(self.stack.mail_api_url + "/api/user", {"email": address, "login": address, "password": password})
        return address

    def deliver(self, to: str, subject: str = "Hello", body: str = "Hi there", *, sender: str = "Alice Sender <alice@example.org>", html: str | None = None, message_id: str | None = None, in_reply_to: str | None = None, references: list[str] | None = None, attachments: list[tuple[str, str, bytes]] | None = None, date=None) -> str:  # noqa: ANN001
        """Deliver a message to ``to`` over plain SMTP; returns its Message-ID (without brackets)."""
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject
        mid = message_id or make_msgid(domain="example.org").strip("<>")
        msg["Message-ID"] = f"<{mid}>"
        if date is not None:
            msg["Date"] = format_datetime(date)
        if in_reply_to:
            msg["In-Reply-To"] = f"<{in_reply_to}>"
        if references:
            msg["References"] = " ".join(f"<{r}>" for r in references)
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        for filename, kind, payload in attachments or []:
            maintype, subtype = kind.split("/", 1)
            msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
        with smtplib.SMTP("localhost", self.stack.smtp_port, timeout=10) as smtp:
            smtp.send_message(msg, from_addr="alice@example.org", to_addrs=[to])
        return mid

    def imap(self, address: str, password: str = PASSWORD) -> IMAPClient:
        """A direct IMAP session as ``address`` (to change the mailbox behind the service's back)."""
        client = IMAPClient("localhost", self.stack.imaps_port, ssl=True, ssl_context=_tls(), timeout=10)
        client.login(address, password)
        return client

    def wait_for(self, address: str, count: int, folder: str = "INBOX", timeout: float = 10) -> None:
        """Until ``folder`` of ``address`` holds ``count`` messages (delivery is asynchronous)."""
        deadline = time.monotonic() + timeout
        with self.imap(address) as client:
            while True:
                client.select_folder(folder, readonly=True)
                if len(client.search("ALL")) >= count:
                    return
                if time.monotonic() > deadline:
                    raise AssertionError(f"{folder} of {address} did not reach {count} messages")
                time.sleep(0.1)


@pytest.fixture(scope="session")
def greenmail(backend_stack) -> GreenMail:
    return GreenMail(backend_stack)


def _context(token: str, sub: str, org_slug: str) -> HttpContext:
    user, _ = User.objects.get_or_create(sub=sub, iss="static_issuer", defaults={"username": f"static_issuer_{sub}"})
    client, _ = Client.objects.get_or_create(client_id="oinsoins")
    org, _ = Organization.objects.get_or_create(slug=org_slug)
    membership, _ = Membership.objects.get_or_create(user=user, organization=org)
    request = UniversalRequest(_extensions={"token": token}, _client=client, _user=user, _organization=org)  # type: ignore[arg-type]
    request.set_membership(membership)  # type: ignore[arg-type]
    return HttpContext(request=request, response=TemporalResponse(), headers={"Authorization": f"Bearer {token}", "User-Agent": "kuvert-tests"}, type="http")


@pytest.fixture
def authenticated_context(transactional_db) -> HttpContext:
    """Member A of ``static_org`` (the static ``test`` token)."""
    return _context("test", "1", "static_org")


@pytest.fixture
def colleague_context(transactional_db) -> HttpContext:
    """Member B of ``static_org``: same organization, different user."""
    return _context("colleague", "2", "static_org")


@pytest.fixture
def other_org_context(transactional_db) -> HttpContext:
    """A member of ``other_org``."""
    return _context("othertest", "9", "other_org")


@pytest.fixture
def aexecute(authenticated_context):
    """Run a GraphQL document as member A (or ``context``); fails the test on GraphQL errors unless ``allow_errors``."""

    async def _run(query: str, variables: dict | None = None, context: HttpContext | None = None, allow_errors: bool = False):
        result = await schema.execute(query, variable_values=variables or {}, context_value=context or authenticated_context)
        if not allow_errors:
            assert not result.errors, result.errors
        return result

    return _run


CREATE = """
mutation Create($input: CreateMailAccountInput!) {
  createMailAccount(input: $input) { id emailAddress status protocol capabilities isOwner canSend }
}
"""
SYNC = """
mutation Sync($id: ID!) { syncMailAccount(id: $id) { created updated deleted folders more account { id backfillDone lastError } } }
"""


@pytest.fixture
def mailbox(aexecute, greenmail, backend_stack):
    """Link a fresh GreenMail user as a mailbox (as member A, or ``context``); returns ``{id, address, …}``."""

    async def _link(protocol: str = "IMAP", context: HttpContext | None = None, address: str | None = None, **extra) -> dict:  # noqa: ANN003
        address = address or greenmail.user()
        port = backend_stack.pop3s_port if protocol == "POP3" else backend_stack.imaps_port
        payload = {
            "emailAddress": address,
            "password": PASSWORD,
            "protocol": protocol,
            "incoming": {"host": "localhost", "port": port, "security": "TLS"},
            "smtp": {"host": "localhost", "port": backend_stack.smtps_port, "security": "TLS"},
            **extra,
        }
        created = (await aexecute(CREATE, {"input": payload}, context=context)).data["createMailAccount"]
        return {**created, "address": address}

    return _link


@pytest.fixture
def sync(aexecute):
    """Sync a mailbox through the schema; returns ``syncMailAccount``."""

    async def _sync(account_id: str, context: HttpContext | None = None) -> dict:
        return (await aexecute(SYNC, {"id": account_id}, context=context)).data["syncMailAccount"]

    return _sync


# --- OAuth -------------------------------------------------------------------------------------

OAUTH_CLIENT = {"client_id": "kuvert-test-client", "client_secret": "kuvert-test-secret"}
REDIRECT = "https://kuvert.test/callback"


class FakeOAuth:
    """Drives fakeoauth's /_admin endpoints."""

    def __init__(self, url: str) -> None:
        self.url = url

    def approve(self, auth_url: str, email: str, access_token: str | None = None, name: str = "") -> str:
        """The user approves at the provider; returns the code the redirect would carry."""
        from urllib.parse import parse_qs, urlparse

        query = {k: v[0] for k, v in parse_qs(urlparse(auth_url).query).items()}
        return _post(
            self.url + "/_admin/approve",
            {"client_id": query["client_id"], "redirect_uri": query["redirect_uri"], "code_challenge": query["code_challenge"], "scope": query.get("scope", ""), "email": email, "name": name, "access_token": access_token},
        )["code"]

    def revoke(self, email: str) -> None:
        _post(self.url + "/_admin/revoke", {"email": email})

    def config(self, **values) -> None:  # noqa: ANN003
        _post(self.url + "/_admin/config", values)

    def log(self) -> list[dict]:
        return _get(self.url + "/_admin/log")["log"]


@pytest.fixture
def fakeoauth(backend_stack, settings) -> FakeOAuth:
    """Both OAuth providers pointed at fakeoauth; Google's mail servers are the stack's GreenMail."""
    _post(backend_stack.fakeoauth_url + "/_admin/clients", OAUTH_CLIENT)
    provider = {
        **OAUTH_CLIENT,
        "redirect_urls": [REDIRECT, "https://other.test/callback"],
        "authorize_url": "https://accounts.example/authorize",
        "token_url": backend_stack.fakeoauth_url + "/token",
        "imap_host": "localhost",
        "imap_port": backend_stack.imaps_port,
        "smtp_host": "localhost",
        "smtp_port": backend_stack.smtps_port,
        "timeout_seconds": 10,
    }
    settings.KUVERT_OAUTH = {"google": provider, "microsoft": {**provider}, "link_expires_seconds": 900}
    fake = FakeOAuth(backend_stack.fakeoauth_url)
    fake.config(expires_in=3600, rotate_refresh=False)
    yield fake
    fake.config(expires_in=3600, rotate_refresh=False)


# --- datalayer ---------------------------------------------------------------------------------


@pytest.fixture
def datalayer(backend_stack, settings):
    """The datalayer on, against the stack's RustFS (root keys, as the deployment's service user)."""
    import boto3
    from botocore.config import Config

    import datalayer.datalayer as dl_module
    from kuvert_server import settings_test

    settings.DATALAYER = {**settings_test._DATALAYER_TEST, "access_key": "kuverttestroot", "secret_key": "kuverttestrootsecret", "port": backend_stack.rustfs_port}
    dl_module.GLOBAL_DL = None
    s3 = boto3.client(
        "s3", endpoint_url=f"http://localhost:{backend_stack.rustfs_port}", aws_access_key_id="kuverttestroot", aws_secret_access_key="kuverttestrootsecret", region_name="us-east-1", config=Config(signature_version="s3v4")
    )
    bucket = settings.DATALAYER["bigfile"]["bucket"]
    if bucket not in {b["Name"] for b in s3.list_buckets().get("Buckets", [])}:
        s3.create_bucket(Bucket=bucket)
    yield s3
    dl_module.GLOBAL_DL = None
