"""Sending mail through a mailbox (see :mod:`mail.send`)."""

from typing import Optional

import strawberry
from channels.db import database_sync_to_async
from kante.errors import ValidationError
from kante.types import Info

from datalayer.models import BigFileStore
from mail import models, send, types
from mail.graphql.errors import translated
from mail.graphql.utils import get_or_404
from mail.sync import in_worker

__all__ = ["RecipientInput", "SendMessageInput", "send_message"]


@strawberry.input(description="A recipient.")
class RecipientInput:
    address: str
    name: Optional[str] = None


@strawberry.input(description="A message to send from a mailbox. A reply (`inReplyTo`) gets In-Reply-To/References and, without a subject, 'Re: ' + the original's.")
class SendMessageInput:
    account: strawberry.ID
    to: list[RecipientInput] = strawberry.field(default_factory=list)
    cc: list[RecipientInput] = strawberry.field(default_factory=list)
    bcc: list[RecipientInput] = strawberry.field(default_factory=list)
    subject: str = ""
    text: str = strawberry.field(default="", description="The plain-text body.")
    html: Optional[str] = strawberry.field(default=None, description="An HTML alternative, sent as given.")
    in_reply_to: Optional[strawberry.ID] = strawberry.field(default=None, description="The message this answers (from any visible folder of the same mailbox).")
    attachments: list[str] = strawberry.field(default_factory=list, description="Stores uploaded with `requestBigfileUpload` / `finishBigfileUpload` (by the caller).")


def _recipients(values: list[RecipientInput]) -> list[dict]:
    return [{"name": (r.name or "").strip(), "address": r.address.strip()} for r in values]


def _prepare(info: Info, input: SendMessageInput) -> models.OutgoingMessage:
    request = info.context.request
    account = get_or_404(models.MailAccount, info, input.account)
    parent = None
    if input.in_reply_to:
        parent = get_or_404(models.Message, info, input.in_reply_to)
        if parent.account_id != account.id:
            raise ValidationError("A reply is sent from the mailbox of the message it answers.")
    stores = []
    if input.attachments:
        # Only files the caller uploaded: another member's upload is not theirs to send.
        stores = list(BigFileStore.objects.filter(id__in=input.attachments, organization=request.organization, creator=request.user, populated=True))
        if len(stores) != len(set(input.attachments)):
            raise ValidationError("Some attachments are not uploaded (or not by you).")
    return send.prepare(
        account,
        request.user,
        to=_recipients(input.to),
        cc=_recipients(input.cc),
        bcc=_recipients(input.bcc),
        subject=input.subject,
        text=input.text,
        html=input.html or "",
        in_reply_to=parent,
        attachments=stores,
    )


@translated
async def send_message(info: Info, input: SendMessageInput) -> types.OutgoingMessage:
    """Send a message now. A refused send is not an error: it comes back FAILED with `error` / `errorCode`."""
    outgoing = await database_sync_to_async(_prepare)(info, input)
    outgoing = await in_worker(lambda: send.send(models.OutgoingMessage.objects.select_related("account", "in_reply_to").get(pk=outgoing.pk)))
    return outgoing  # type: ignore[return-value]
