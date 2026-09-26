"""Realtime channel for the ``mailboxSyncs`` subscription.

Groups are per organization (:func:`org_group`); the subscription resolver additionally drops
events of mailboxes the subscriber may not see. Not kante's ``Channel.org_group``: its
``:``-separated names are not valid channel-layer group names.
"""

from kante.channel import build_channel
from pydantic import BaseModel, Field


class MailboxSyncSignal(BaseModel):
    """A mailbox finished syncing."""

    account_id: int = Field(description="The mailbox that was synced.")
    created: int = Field(default=0, description="New messages.")
    updated: int = Field(default=0, description="Messages whose flags changed.")
    deleted: int = Field(default=0, description="Messages gone from the server.")
    more: bool = Field(default=False, description="More mail is waiting for the next sync (a large mailbox fills in over several).")
    folder_ids: list[int] = Field(default_factory=list, description="The folders whose messages changed.")


mailbox_sync_channel = build_channel(MailboxSyncSignal, name="kuvert_mailbox_syncs")


def org_group(organization_id: int) -> str:
    """The channel group of one organization."""
    return f"kuvert_mailbox_syncs.org.{organization_id}"


def broadcast_sync(result: object) -> None:
    """Tell the organization's subscribers a mailbox was synced."""
    signal = MailboxSyncSignal(account_id=result.account_id, created=result.created, updated=result.updated, deleted=result.deleted, more=result.more, folder_ids=result.touched_folders)  # type: ignore[attr-defined]
    mailbox_sync_channel.broadcast(signal, [org_group(result.organization_id)])  # type: ignore[attr-defined]
