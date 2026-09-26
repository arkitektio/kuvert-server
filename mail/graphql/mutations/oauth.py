"""Linking Gmail and Microsoft mailboxes through OAuth (see :mod:`mail.oauth.linking`)."""

from typing import Optional

import strawberry
from channels.db import database_sync_to_async
from kante.errors import PermissionDenied
from kante.types import Info

from mail import enums, models, types
from mail.graphql.errors import translated
from mail.graphql.utils import get_or_404, require_owner
from mail.oauth import linking
from mail.sync import in_worker

__all__ = ["StartOAuthLinkInput", "CompleteOAuthLinkInput", "start_oauth_link", "complete_oauth_link", "resume_oauth_link", "cancel_oauth_link"]


@strawberry.input(description="Start linking a mailbox through OAuth, or re-link one (`account`) whose grant ran out.")
class StartOAuthLinkInput:
    provider: enums.Provider
    protocol: enums.Protocol = enums.Protocol.IMAP
    redirect_url: Optional[str] = strawberry.field(default=None, description="One of the deployment's registered redirect URLs; the first by default.")
    name: Optional[str] = strawberry.field(default=None, description="A display name for the new mailbox.")
    account: Optional[strawberry.ID] = strawberry.field(default=None, description="Re-link this mailbox (owner only).")
    login_hint: Optional[str] = strawberry.field(default=None, description="The address to pre-fill at the provider.")


@strawberry.input(description="What the provider's redirect carried.")
class CompleteOAuthLinkInput:
    code: str
    state: str


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


@translated
async def complete_oauth_link(info: Info, input: CompleteOAuthLinkInput) -> types.MailAccount:
    """Finish a login with the code and state the redirect carried; returns the ACTIVE mailbox."""
    request = info.context.request
    account = await in_worker(linking.complete, request.organization, request.user, input.code, input.state)
    return account  # type: ignore[return-value]


def _own_pending(info: Info, state: str) -> models.OAuthLink:
    request = info.context.request
    link = models.OAuthLink.objects.filter(state=state, organization=request.organization).select_related("account").first()
    if link is not None and link.creator_id != request.user.id:
        raise PermissionDenied("Only the member who started this link can resume or cancel it.")
    return linking.pending_of(request.organization, request.user, state)


@translated
async def resume_oauth_link(info: Info, state: str) -> types.AuthSession:
    """The caller's pending login again (after its dialog closed)."""
    return types.AuthSession.of(await database_sync_to_async(_own_pending)(info, state))


@translated
async def cancel_oauth_link(info: Info, state: str) -> str:
    """Drop the caller's pending login."""
    link = await database_sync_to_async(_own_pending)(info, state)
    await link.adelete()
    return state
