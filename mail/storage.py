"""Raw messages and attachments in the datalayer, written by the service itself.

The vendored datalayer creates stores for *client* uploads (a grant, a PUT, a finish). A sync
writes with the service's own credentials instead: :func:`store_bytes` puts the object and
creates its finished :class:`~datalayer.models.BigFileStore` in one step. Without a ``datalayer``
config block nothing is stored and every function here is a no-op returning None.

Stores are never deleted with the rows that reference them: :func:`orphan` marks them, and the
``purge_orphaned_stores`` rekuest action (:mod:`mail.scheduled`) deletes those still unreferenced
after a grace period.
"""

import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from datalayer.datalayer import get_current_datalayer
from datalayer.models import BigFileStore

logger = logging.getLogger(__name__)

BUCKET_KEY = "bigfile"


def enabled() -> bool:
    """Whether a datalayer is configured."""
    return bool(getattr(settings, "DATALAYER", None))


def store_bytes(organization_id: int, creator_id: int | None, filename: str, payload: bytes, content_type: str) -> BigFileStore | None:
    """Put ``payload`` in the bucket and return its finished store (None without a datalayer)."""
    if not enabled():
        return None
    layer = get_current_datalayer()
    key = uuid.uuid4().hex
    layer.put_file(BUCKET_KEY, key, payload, content_type)
    return BigFileStore.objects.create(
        organization_id=organization_id,
        creator_id=creator_id,
        key=key,
        bucket=BUCKET_KEY,
        path=layer.build_store_path(BUCKET_KEY, key),
        original_file_name=filename[:1000] or None,
        content_type=content_type[:255],
        populated=True,
        size_bytes=len(payload),
    )


def read_bytes(store: BigFileStore) -> bytes:
    """The whole object of ``store``."""
    return get_current_datalayer().read_object(BUCKET_KEY, store.key)


def orphan(store_ids: list[int]) -> None:
    """Mark stores whose referencing rows are gone as candidates for the purge."""
    ids = [store_id for store_id in store_ids if store_id]
    if ids:
        BigFileStore.objects.filter(id__in=ids, orphaned_at__isnull=True).update(orphaned_at=timezone.now())


def _referenced() -> Q:
    from mail import models

    return (
        Q(id__in=models.Message.objects.exclude(raw=None).values("raw_id"))
        | Q(id__in=models.Attachment.objects.exclude(store=None).values("store_id"))
        | Q(id__in=models.OutgoingMessage.attachments.through.objects.values("bigfilestore_id"))
    )


def purge_orphans(grace: timedelta = timedelta(days=1), limit: int = 500, organization: str | None = None) -> int:
    """Delete (bytes, then row) stores orphaned longer than ``grace`` that nothing references again.

    Every organization's, or only those of ``organization`` (its slug): a store always belongs to
    one. What counts as referenced is not narrowed, so a reference from anywhere keeps a store.
    """
    if not enabled():
        return 0
    cutoff = timezone.now() - grace
    candidates = BigFileStore.objects.filter(orphaned_at__lt=cutoff)
    if organization is not None:
        candidates = candidates.filter(organization__slug=organization)
    # Re-attached in the meantime (a message moved back): clear the mark instead.
    candidates.filter(_referenced()).update(orphaned_at=None)
    purged = 0
    for store in candidates.exclude(_referenced())[:limit]:
        try:
            store.delete()
            purged += 1
        except Exception:
            logger.warning("Could not purge store %s", store.pk, exc_info=True)
    return purged
