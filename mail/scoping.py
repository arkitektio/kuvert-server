"""Organization and mailbox-visibility scoping.

Up to three filters apply to every read and write, in this order:

1. **Organization** (as in bank): a row belongs to the request's organization, through its own
   ``organization`` column or a chain of required FKs (:func:`organization_path`).
2. **Visibility**: a row that belongs to a mailbox (:class:`~mail.models.MailAccount`) is only
   visible to the members who may see that mailbox (:func:`visible_accounts_q`): its creator,
   everyone for an ORGANIZATION mailbox, and the members it is shared with for a SHARED one.
3. **Owner**: a row that belongs to a member personally (a task, a task list: an ``owner``) is
   only visible to that member. A task's link to a thread passes 2 *and* 3: the owner must still
   see the thread's mailbox.

List fields are scoped through ``OrgScoped.get_queryset`` on the GraphQL types; everything that
fetches a row by id (mutations, single-object queries, subscriptions) goes through
:func:`for_org`, :func:`get_for_org` or :func:`aget_for_org`. A row the caller cannot see is
NOT_FOUND exactly like a missing one (:mod:`mail.graphql.utils`).
"""

from functools import cache

from django.core.exceptions import FieldDoesNotExist
from django.db import models as django_models
from django.db.models import Q
from kante.types import Info

# Models with no organization anywhere in their non-nullable FK graph. Keep this list short and
# visible — it is the tenancy escape hatch. Currently every model is scoped.
UNSCOPED_MODELS: frozenset[str] = frozenset()

_MAX_PATH_DEPTH = 3


def _find_path(model: type[django_models.Model], depth: int, target: str, is_target) -> str | None:  # noqa: ANN001
    if is_target(model):
        return ""
    try:
        field = model._meta.get_field(target)
        if field.is_relation:
            return target
    except FieldDoesNotExist:
        pass

    if depth == 0:
        return None

    # Only follow required FKs: a nullable path would silently hide rows whose FK is NULL
    # instead of scoping them.
    for field in model._meta.get_fields():
        if not isinstance(field, django_models.ForeignKey) or field.null:
            continue
        if field.related_model is model:
            continue
        sub_path = _find_path(field.related_model, depth - 1, target, is_target)
        if sub_path is not None:
            return f"{field.name}__{sub_path}" if sub_path else field.name
    return None


@cache
def organization_path(model: type[django_models.Model]) -> str | None:
    """The ORM lookup path from ``model`` to its organization, if any."""
    return _find_path(model, _MAX_PATH_DEPTH, "organization", lambda m: False)


@cache
def account_path(model: type[django_models.Model]) -> str | None:
    """The ORM lookup path from ``model`` to its mailbox: ``""`` for a mailbox itself, None when it has none."""
    from mail.models import MailAccount

    return _find_path(model, _MAX_PATH_DEPTH, "account", lambda m: m is MailAccount)


@cache
def owner_path(model: type[django_models.Model]) -> str | None:
    """The ORM lookup path from ``model`` to the member it personally belongs to, if any."""
    return _find_path(model, _MAX_PATH_DEPTH, "owner", lambda m: False)


def visible_accounts_q(user, prefix: str = "") -> Q:  # noqa: ANN001 - authentikate User
    """The mailboxes ``user`` may see (the organization is filtered separately)."""
    from mail.models import Visibility

    return (
        Q(**{f"{prefix}creator": user})
        | Q(**{f"{prefix}visibility": Visibility.ORGANIZATION})
        | (Q(**{f"{prefix}visibility": Visibility.SHARED}) & Q(**{f"{prefix}shared_with": user}))
    )


def scope_to(queryset: django_models.QuerySet, organization, user) -> django_models.QuerySet:  # noqa: ANN001
    """Narrow ``queryset`` to ``organization`` and to the mailboxes ``user`` may see."""
    model = queryset.model
    path = organization_path(model)
    if path is None:
        if model.__name__ not in UNSCOPED_MODELS:
            raise LookupError(f"{model.__name__} has no path to an organization and is not registered in mail.scoping.UNSCOPED_MODELS")
        return queryset
    queryset = queryset.filter(**{path: organization})
    to_account = account_path(model)
    if to_account is not None:
        from mail.models import MailAccount

        # `shared_with` is a to-many join: select the visible ids in a subquery, so a row is
        # never duplicated when several conditions hold.
        visible = MailAccount.objects.filter(visible_accounts_q(user)).values("id")
        queryset = queryset.filter(**{f"{to_account}__in" if to_account else "id__in": visible})
    to_owner = owner_path(model)
    if to_owner is not None:
        queryset = queryset.filter(**{to_owner: user})
    return queryset


def scope_queryset(queryset: django_models.QuerySet, info: Info) -> django_models.QuerySet:
    """Narrow ``queryset`` to what the request's user may see in its organization."""
    request = info.context.request
    return scope_to(queryset, request.organization, request.user)


def for_org(model: type[django_models.Model], info: Info) -> django_models.QuerySet:
    """``model``'s queryset limited to what the request may see."""
    return scope_queryset(model.objects.all(), info)


def get_for_org(model: type[django_models.Model], info: Info, **kwargs) -> django_models.Model:
    """``Model.objects.get`` limited to what the request may see."""
    return for_org(model, info).get(**kwargs)


async def aget_for_org(model: type[django_models.Model], info: Info, **kwargs) -> django_models.Model:
    """Async ``Model.objects.aget`` limited to what the request may see."""
    return await for_org(model, info).aget(**kwargs)
