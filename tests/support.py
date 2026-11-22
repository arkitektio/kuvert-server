"""The types the suite's fixtures hand to tests (their values are built in ``conftest.py``)."""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeAlias, overload

from graphql import GraphQLError
from kante.context import HttpContext, WsContext
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
