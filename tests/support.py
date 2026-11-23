"""The types the suite's fixtures hand to tests (their values are built in ``conftest.py``)."""

import datetime
import json
import pathlib
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeAlias, overload

from graphql import GraphQLError
from joserfc import jwt
from joserfc.jwk import OKPKey
from kante.context import HttpContext, UniversalRequest, WsContext
from strawberry.http.temporal_response import TemporalResponse
from strawberry.types import ExecutionResult

if TYPE_CHECKING:
    from tests.conftest import Subscription

#: A GraphQL object as the schema answered it (strawberry's ``ExecutionResult.data``).
Data: TypeAlias = dict[str, Any]


class Answered(Protocol):
    """A result ``aexecute`` checked: no errors, so it carries data."""

    @property
    def data(self) -> Data: ...

    @property
    def errors(self) -> list[GraphQLError] | None: ...


class AExecute(Protocol):
    """``aexecute``: run a document as member A (or ``context``); GraphQL errors fail the test unless allowed."""

    @overload
    def __call__(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        context: HttpContext | None = None,
        allow_errors: Literal[False] = False,
    ) -> Awaitable[Answered]: ...

    @overload
    def __call__(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        context: HttpContext | None = None,
        *,
        allow_errors: bool,
    ) -> Awaitable[ExecutionResult]: ...


class WsContexts(Protocol):
    """``ws_contexts``: a fresh websocket context, with its own consumer, per call."""

    def __call__(self, token: str = "test") -> Awaitable[WsContext]: ...


class Subscribe(Protocol):
    """``subscribe``: start a subscription as a static token, stopped after the test."""

    def __call__(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        token: str = "test",
        group: str | None = None,
    ) -> Awaitable["Subscription"]: ...


class ProviderFor(Protocol):
    """``provider_for``: the id of the organization's first provider of ``kind``, set up on first use."""

    def __call__(
        self, kind: str = "GENERIC", context: HttpContext | None = None
    ) -> Awaitable[str]: ...


class Mailbox(Protocol):
    """``mailbox``: link a fresh GreenMail user as a mailbox; returns the created account and its address."""

    def __call__(
        self,
        protocol: str = "IMAP",
        context: HttpContext | None = None,
        address: str | None = None,
        **extra: Any,
    ) -> Awaitable[Data]: ...


class Sync(Protocol):
    """``sync``: sync a mailbox through the schema; returns ``syncMailAccount``."""

    def __call__(
        self, account_id: str, context: HttpContext | None = None
    ) -> Awaitable[Data]: ...


#: ``dovecot_mailbox``: a fresh Dovecot user linked as an IMAP mailbox.
DovecotMailbox: TypeAlias = Callable[[], Awaitable[Data]]


def error_code(result: "ExecutionResult | Answered") -> object:
    """The ``code`` extension of ``result``'s first error (the result must have failed)."""
    assert result.errors, "the operation succeeded"
    extensions = result.errors[0].extensions
    assert extensions is not None, result.errors[0]
    return extensions["code"]


#: What rekuest signs task tokens with in the test settings (``AUTHENTIKATE["provenance"]``).
PROVENANCE_KEY = OKPKey.import_key(
    json.loads(
        (pathlib.Path(__file__).parent / "fixtures" / "provenance_key.json").read_text()
    )
)

#: ``guest({key: roles})``: a context presenting a guest's token granted those collaborations, by
#: their key (lok's), with the roles held in each (``[]``: view only).
GuestFactory: TypeAlias = Callable[[Mapping[str, list[str]]], HttpContext]


def task_token(
    rcb: str,
    org: str | None,
    act_sub: str = "1",
    *,
    tsk: str = "2",
    rtk: str = "1",
) -> str:
    """A task token as rekuest signs it for the static test app: task ``tsk`` of the workflow
    ``rtk`` (its root task), assigned by ``rcb``, in the organization ``org``."""
    now = int(datetime.datetime.now(datetime.UTC).timestamp())
    claims: dict[str, object] = {
        "iss": "rekuest",
        "aud": ["kuvert"],
        "sub": act_sub,
        "act": {"sub": act_sub, "cid": "static", "iss": "static_issuer"},
        "iat": now,
        "exp": now + 300,
        "jti": uuid.uuid4().hex,
        "tsk": tsk,
        "ptk": rtk,
        "rtk": rtk,
        "rcb": rcb,
        "ahs": "e3b0c44298fc1c14",
        "aha": "sha256-canonical-v1",
    }
    if org is not None:
        claims["org"] = org
    return jwt.encode(
        {"alg": "Ed25519", "kid": "kuvert-test-provenance"},
        claims,
        PROVENANCE_KEY,
        algorithms=["Ed25519"],
    )


def request_context(bearer: str = "test", task: str | None = None) -> HttpContext:
    """A request presenting the static token ``bearer`` (and a task token), as a client sends it:
    nothing resolved yet."""
    headers = {"Authorization": f"Bearer {bearer}"}
    if task is not None:
        headers["rekuest-task"] = task
    return HttpContext(
        request=UniversalRequest(_extensions={}),
        response=TemporalResponse(),
        headers=headers,
        type="http",
    )
