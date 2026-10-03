from strawberry.scalars import JSON

from mail.scoping import scope_queryset


def build_prescoped_queryset(info, queryset):  # noqa: ANN001, ANN201
    """Limit ``queryset`` to the request's organization and to the mailboxes its user may see.

    Every type backing a Query field must route its ``get_queryset`` through here:
    strawberry_django runs it for single ``x: T = field()`` fetches as well as lists and nested
    relations, and nothing else in the stack scopes them.
    """
    return scope_queryset(queryset, info)


class OrgScoped:
    """Mixin that scopes a type's reads (as mikro's and bank's do), resolved via MRO."""

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):  # noqa: ANN001, ANN206
        return build_prescoped_queryset(info, queryset)


DESCRIPTORS_DESCRIPTION = (
    "This object's descriptors, a flat mapping of key to value: the facts about it that an action's port can `require` and a trigger can test "
    "(e.g. `@kuvert/message_count`). The keys are the ones kuvert declares for this structure, and the values are the ones a signal about the object carries. "
    "Empty for a structure that declares none"
)


def resolve_descriptors(root) -> JSON:  # noqa: ANN001 - the model instance behind any hosted type
    """The descriptors of a hosted object, from its structure's declaration (``kuvert_server.service``)."""
    from kuvert_server.service import service  # the declaration imports mail.models

    return service.describe(root)
