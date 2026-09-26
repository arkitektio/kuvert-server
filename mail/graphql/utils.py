"""Helpers every resolver uses: scoped fetches that fail as NOT_FOUND, and owner checks."""

from typing import TypeVar

from django.db import models as django_models
from kante.errors import NotFound, PermissionDenied
from kante.types import Info

from mail import models
from mail.scoping import aget_for_org, for_org, get_for_org

M = TypeVar("M", bound=django_models.Model)


def get_or_404(model: type[M], info: Info, id: object) -> M:
    """``model`` ``id`` as the caller may see it; a row they may not see is NOT_FOUND too."""
    try:
        return get_for_org(model, info, id=id)  # type: ignore[return-value]
    except (model.DoesNotExist, ValueError):  # type: ignore[attr-defined]
        raise NotFound(f"{model.__name__} {id} does not exist.") from None


async def aget_or_404(model: type[M], info: Info, id: object) -> M:
    """Async :func:`get_or_404`."""
    try:
        return await aget_for_org(model, info, id=id)  # type: ignore[return-value]
    except (model.DoesNotExist, ValueError):  # type: ignore[attr-defined]
        raise NotFound(f"{model.__name__} {id} does not exist.") from None


def get_many(model: type[M], info: Info, ids: list | None, *, select: tuple[str, ...] = ()) -> list[M]:
    """Several rows by id as the caller may see them; any id not visible is NOT_FOUND."""
    if not ids:
        return []
    rows = list(for_org(model, info).filter(id__in=ids).select_related(*select))
    if len(rows) != len(set(str(i) for i in ids)):
        raise NotFound(f"Some {model.__name__} ids do not exist.")
    return rows


def require_owner(account: models.MailAccount, info: Info) -> None:
    """Only the member who linked a mailbox changes its credentials, its sharing, or deletes it."""
    if account.creator_id != info.context.request.user.id:
        raise PermissionDenied("Only the member who linked this mailbox can do this.")
