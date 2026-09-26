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
