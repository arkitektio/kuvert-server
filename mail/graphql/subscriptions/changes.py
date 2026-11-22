"""Realtime queue events and login progress."""

from collections.abc import AsyncGenerator
from contextlib import aclosing

import strawberry
from channels.db import database_sync_to_async
from django.utils import timezone
from kante.types import Info

from mail import enums, models, types
from mail.channels import (
    auth_group,
    auth_session_channel,
    changes_group,
    mail_changes_channel,
)
from mail.graphql.errors import translate
from mail.oauth import linking
from mail.scoping import scope_to

__all__ = ["mail_changes", "auth_session"]

#: A login being watched is read again at least this often, so an expiry (which nothing announces) shows.
AUTH_RECHECK_SECONDS = 10.0


async def mail_changes(
    self, info: Info, account: strawberry.ID | None = None
) -> AsyncGenerator[types.MailChangesEvent, None]:
    """Stream where a visible mailbox's queue of changes stands, whenever it changes (only `account`'s, when given)."""
    request = info.context.request

    def visible(account_id: int) -> bool:
        return scope_to(
            models.MailAccount.objects.filter(id=account_id),
            request.organization,
            request.user,
        ).exists()

    async for signal in mail_changes_channel.listen(
        info.context, [changes_group(request.organization.id)]
    ):
        if account is not None and str(signal.account_id) != str(account):
            continue
        if not await database_sync_to_async(visible)(signal.account_id):
            continue
        yield types.MailChangesEvent(
            account_id=strawberry.ID(str(signal.account_id)),
            pending=signal.pending,
            failed=signal.failed,
            pushed=signal.pushed,
        )


def _key(session: types.AuthSession) -> tuple[object, ...]:
    return (
        session.status,
        session.step,
        session.error_code,
        session.result.id if session.result else None,
    )


async def auth_session(
    self, info: Info, state: str
) -> AsyncGenerator[types.AuthSession, None]:
    """The login as it is now, then again whenever it changes, until it is no longer PENDING. Only its starter may watch it."""
    request = info.context.request

    def read() -> models.OAuthLink:
        try:
            return linking.find(request.organization, request.user, state)
        except Exception as error:
            raise translate(error) from error

    link = await database_sync_to_async(read)()
    session = types.AuthSession.of(link)
    yield session
    seen = _key(session)
    while session.status == enums.AuthStatus.PENDING:
        # Until something is announced, or at the latest until the login's time runs out (nothing announces that).
        left = (link.expires_at - timezone.now()).total_seconds()
        async with aclosing(
            auth_session_channel.listen(
                info.context,
                [auth_group(state)],
                timeout=min(max(left, 0.0) + 0.5, AUTH_RECHECK_SECONDS),
            )
        ) as signals:
            async for _ in signals:
                break
        link = await database_sync_to_async(read)()
        session = types.AuthSession.of(link)
        if _key(session) != seen:
            seen = _key(session)
            yield session
