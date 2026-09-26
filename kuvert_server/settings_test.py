from .settings import *  # noqa
from .settings import AUTHENTIKATE, DATABASES, KUVERT_MAIL, KUVERT_SYNC
import logging
import os

# The test stack (tests/integration/docker-compose.yaml) publishes its services on *ephemeral*
# host ports: `tests/conftest.py` overwrites PORT with what docker picked before pytest-django
# creates the test database. This value is only the fallback for running a `tmanage.py` command
# against a stack you started by hand -- set KUVERT_TEST_DB_PORT to `docker compose port db 5432`.
DATABASES["default"] = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "testdb",
    "USER": "test",
    "PASSWORD": "test",
    "HOST": "localhost",
    "PORT": os.environ.get("KUVERT_TEST_DB_PORT", "5432"),
}

# Django forces DEBUG=False under the test runner, and authentikate refuses static tokens when
# DEBUG is False. These are deliberate test fixtures, so opt in explicitly.
AUTHENTIKATE = {
    **AUTHENTIKATE,
    "allow_static_tokens_in_production": True,
    "static_tokens": {
        "test": {"sub": "1", "roles": ["editor"]},
        # Another member of the same organization, for visibility and owner-only checks.
        "colleague": {"sub": "2", "roles": ["editor"]},
        # A user in a different organization, for cross-tenant scoping tests.
        "othertest": {"sub": "9", "org": "other_org", "roles": ["editor"]},
    },
}

# `conftest.fernet_key` writes a key generated for the run and points this at it.
KUVERT_SECRETS = {"key_path": "/nonexistent/set-by-conftest.fernet"}

# The test mail server (GreenMail) runs on localhost with a self-signed certificate.
KUVERT_MAIL = {**KUVERT_MAIL, "allowed_private_hosts": ["localhost"], "tls_verify": False}
KUVERT_SYNC = {**KUVERT_SYNC, "connect_timeout_seconds": 10}

# `conftest.oauth` points both providers at the fake OAuth server of the test stack.
KUVERT_OAUTH = {"google": None, "microsoft": None, "link_expires_seconds": 900}

# Disable logging during tests to reduce noise
logging.disable(logging.CRITICAL)

# Use in-memory channel layer for tests instead of Redis
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# No datalayer by default (messages keep their text and attachment metadata only);
# `conftest.datalayer` switches it on against the stack's RustFS for the tests that need it.
DATALAYER = {}
_DATALAYER_TEST = {
    "access_key": "set-by-conftest",
    "secret_key": "set-by-conftest",
    "host": "localhost",
    "port": 0,
    "protocol": "http",
    "region": "us-east-1",
    "role_arn": "arn:aws:iam::000000000000:role/datalayer",
    "bigfile": {"bucket": "kuvert-mail"},
    "upload_roles": ["admin", "editor", "bot"],
}
