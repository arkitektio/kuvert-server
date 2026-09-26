"""Service exceptions as GraphQL errors whose ``extensions.code`` is a :class:`~mail.models.MailErrorCode`.

The classification is :func:`mail.errors.code_for`, the same one that fills ``lastErrorCode`` on
a mailbox, so a client handles both with one table.
"""

import functools
import inspect

from kante.errors import KanteError

from mail.errors import SyncTooSoon, code_for


def translate(error: Exception) -> Exception:
    """A GraphQL error for a known failure; anything else (a bug) passes through unchanged."""
    if isinstance(error, KanteError):
        return error
    code = code_for(error)
    if code is None:
        return error
    extensions = {"nextSyncAllowedAt": error.allowed_at.isoformat()} if isinstance(error, SyncTooSoon) else None
    return KanteError(str(error), code=str(code.value), extensions=extensions)


def translated(fn):  # noqa: ANN001, ANN201
    """Decorate a resolver so known failures surface with their code (sync or async)."""
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            try:
                return await fn(*args, **kwargs)
            except Exception as error:
                raise translate(error) from error

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        try:
            return fn(*args, **kwargs)
        except Exception as error:
            raise translate(error) from error

    return wrapper
