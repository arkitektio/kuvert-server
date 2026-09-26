"""Realtime sync events, per organization, filtered to the mailboxes the subscriber may see."""

from typing import AsyncGenerator

import strawberry
from channels.db import database_sync_to_async
from kante.types import Info

from mail import models, types
from mail.channels import mailbox_sync_channel, org_group
from mail.scoping import scope_to

__all__ = ["mailbox_syncs"]


async def mailbox_syncs(self, info: Info) -> AsyncGenerator[types.MailboxSyncEvent, None]:
    """Stream an event whenever a mailbox the caller may see finished syncing."""
    request = info.context.request
    group = org_group(request.organization.id)

    def visible(account_id: int) -> bool:
        return scope_to(models.MailAccount.objects.filter(id=account_id), request.organization, request.user).exists()

    async for signal in mailbox_sync_channel.listen(info.context, [group]):
        if not await database_sync_to_async(visible)(signal.account_id):
            continue
        yield types.MailboxSyncEvent(account_id=strawberry.ID(str(signal.account_id)), created=signal.created, updated=signal.updated, deleted=signal.deleted, more=signal.more)
