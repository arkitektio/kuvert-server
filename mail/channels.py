"""Realtime channels: ``mailboxSyncs``, ``mailChanges`` and ``authSession``.

Sync and change groups are per organization (:func:`org_group`, :func:`changes_group`); the
subscription resolvers additionally drop events of mailboxes the subscriber may not see. A login's
group is keyed by a hash of its state (:func:`auth_group`); only the member who started the login
may listen to it. Not kante's ``Channel.org_group``: its ``:``-separated names are not valid
channel-layer group names.
"""

import hashlib

from django.db.models import Count
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


class MailChangesSignal(BaseModel):
    """Where a mailbox's queue of changes stands now (something was queued, pushed, failed, retried or undone)."""

    account_id: int = Field(description="The mailbox.")
    pending: int = Field(default=0, description="Changes still waiting.")
    failed: int = Field(default=0, description="Changes that did not reach the server.")
    pushed: int = Field(default=0, description="Changes that reached the server in the pass that caused this event.")


mail_changes_channel = build_channel(MailChangesSignal, name="kuvert_mail_changes")


def changes_group(organization_id: int) -> str:
    """The ``mailChanges`` channel group of one organization."""
    return f"kuvert_mail_changes.org.{organization_id}"


def announce_changes(account_id: int, pushed: int = 0) -> None:
    """Tell the organization's subscribers where the mailbox's queue stands, once the current transaction commits."""
    from mail import models

    organization_id = models.MailAccount.objects.filter(id=account_id).values_list("organization_id", flat=True).first()
    if organization_id is None:
        return  # the mailbox is gone, and its changes with it
    counts = dict(models.MailChange.objects.filter(account_id=account_id).order_by().values_list("state").annotate(n=Count("id")))
    signal = MailChangesSignal(account_id=account_id, pending=counts.get(models.MailChangeState.PENDING, 0), failed=counts.get(models.MailChangeState.FAILED, 0), pushed=pushed)
    mail_changes_channel.broadcast_on_commit(signal, [changes_group(organization_id)])


class AuthSessionSignal(BaseModel):
    """A login changed: the subscriber reads it again."""

    changed: bool = True


auth_session_channel = build_channel(AuthSessionSignal, name="kuvert_auth_sessions")


def auth_group(state: str) -> str:
    """The channel group of one login: a hash of its state, so the state never travels as a group name."""
    return f"kuvert_auth.{hashlib.sha256(state.encode()).hexdigest()[:40]}"


def announce_auth(state: str) -> None:
    """Tell whoever watches the login that it changed."""
    auth_session_channel.broadcast_on_commit(AuthSessionSignal(), [auth_group(state)])
