"""Linking Gmail and Microsoft mailboxes through OAuth (see :mod:`mail.oauth.linking`)."""

from typing import Optional

import strawberry
from channels.db import database_sync_to_async
from kante.types import Info

from mail import enums, models, types
from mail.graphql.errors import translated
from mail.graphql.utils import get_or_404, require_owner
from mail.oauth import linking
from mail.sync import in_worker

__all__ = ["StartOAuthLinkInput", "CompleteAuthInput", "start_oauth_link", "complete_auth", "resume_auth", "cancel_auth", "auth_session"]


@strawberry.input(description="Start linking a mailbox through OAuth, or re-link one (`account`) whose grant ran out.")
class StartOAuthLinkInput:
    provider: enums.Provider
    protocol: enums.Protocol = enums.Protocol.IMAP
    redirect_url: Optional[str] = strawberry.field(default=None, description="One of the deployment's registered redirect URLs; the first by default.")
    name: Optional[str] = strawberry.field(default=None, description="A display name for the new mailbox.")
    account: Optional[strawberry.ID] = strawberry.field(default=None, description="Re-link this mailbox (owner only).")
    login_hint: Optional[str] = strawberry.field(default=None, description="The address to pre-fill at the provider.")


@strawberry.input(description="Finish (REDIRECT) or advance (POLL) a started login.")
class CompleteAuthInput:
    state: str
    code: Optional[str] = strawberry.field(default=None, description="REDIRECT: the `code` query parameter of the redirect. POLL: omitted.")
    error: Optional[str] = strawberry.field(default=None, description="REDIRECT: the provider's `error` / `error_description`, when it refused.")
    error_description: Optional[str] = None


def _start(info: Info, input: StartOAuthLinkInput) -> models.OAuthLink:
    request = info.context.request
    account = None
    if input.account:
        account = get_or_404(models.MailAccount, info, input.account)
        require_owner(account, info)
    link = linking.start(request.organization, request.user, input.provider.value, input.protocol.value, input.redirect_url, name=input.name or "", account=account, login_hint=input.login_hint)
    return models.OAuthLink.objects.select_related("account").get(pk=link.pk)


@translated
async def start_oauth_link(info: Info, input: StartOAuthLinkInput) -> types.AuthSession:
    """A login to open at the provider."""
    return types.AuthSession.of(await database_sync_to_async(_start)(info, input))


def _own_login(info: Info, state: str) -> models.OAuthLink:
    request = info.context.request
    return linking.find(request.organization, request.user, state)


def _complete(info: Info, input: CompleteAuthInput) -> models.OAuthLink:
    return linking.complete(_own_login(info, input.state), input.code, input.error, input.error_description)


@translated
async def complete_auth(info: Info, input: CompleteAuthInput) -> types.AuthSession:
    """REDIRECT: finish with the code. POLL: advance one step; call until not PENDING.

    A login that is settled (DONE, FAILED, EXPIRED, CANCELLED) is answered again as it is: nothing is exchanged twice.
    """
    return types.AuthSession.of(await in_worker(_complete, info, input))


@translated
async def resume_auth(info: Info, state: str) -> types.AuthSession:
    """The same login again (a fresh openUrl if the old one cannot be reused)."""
    return types.AuthSession.of(await database_sync_to_async(_own_login)(info, state))


@translated
async def cancel_auth(info: Info, state: str) -> types.AuthSession:
    """Drop a login that will not be finished. Idempotent."""
    return types.AuthSession.of(await database_sync_to_async(lambda: linking.cancel(_own_login(info, state)))())


@translated
async def auth_session(info: Info, state: str) -> types.AuthSession:
    """Where a login is. No side effect."""
    return types.AuthSession.of(await database_sync_to_async(_own_login)(info, state))
