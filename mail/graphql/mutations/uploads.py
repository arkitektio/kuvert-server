"""Finishing an attachment upload, limited to the caller's own uploads.

The vendored datalayer finishes any store of the organization by id and returns it with its
read grant. Here the same table also holds every synced raw message and attachment (sequential
ids), so that would hand any member the bytes of another member's private mail. Only a store the
caller requested themselves, and that no message references, can be finished.
"""

from datalayer import inputs as datalayer_inputs
from datalayer import types as datalayer_types
from datalayer.datalayer import get_current_datalayer
from datalayer.models import BigFileStore
from django.db.models import Q
from kante.errors import NotFound
from kante.types import Info

from mail import models

__all__ = ["finish_bigfile_upload"]


def _mail_stores() -> Q:
    return Q(id__in=models.Message.objects.exclude(raw=None).values("raw_id")) | Q(id__in=models.Attachment.objects.exclude(store=None).values("store_id"))


def finish_bigfile_upload(info: Info, input: datalayer_inputs.FinishBigFileUploadInput) -> datalayer_types.BigFileStore:
    """Mark the caller's uploaded store as populated after the client wrote the object."""
    request = info.context.request
    payload = input.to_pydantic()
    own = BigFileStore.objects.filter(id=payload.store_id, organization=request.organization, creator=request.user).exclude(_mail_stores())
    if not own.exists():
        raise NotFound(f"BigFileStore {payload.store_id} does not exist.")
    return get_current_datalayer().finish_bigfile_upload(request.organization.id, payload)  # type: ignore[return-value]
