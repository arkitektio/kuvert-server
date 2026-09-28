"""Categories of a mailbox: shared by everyone who sees it; LOCAL, or kept on the server as a keyword."""

from typing import Optional

import strawberry
from channels.db import database_sync_to_async
from django.db import IntegrityError
from kante.errors import ValidationError
from kante.types import Info

from mail import changes, enums, models, types
from mail.errors import MailError
from mail.graphql.errors import translated
from mail.graphql.utils import get_or_404
from mail.sync import push_soon

__all__ = ["CreateCategoryInput", "UpdateCategoryInput", "create_category", "update_category", "delete_category"]


@strawberry.input(description="A new category of a mailbox.")
class CreateCategoryInput:
    account: strawberry.ID
    name: str
    color: str = ""
    sync: enums.CategorySync = strawberry.field(default=enums.CategorySync.LOCAL, description="LOCAL keeps it here; KEYWORD keeps it on the server as `keyword`, so other mail clients see it.")
    keyword: Optional[str] = strawberry.field(default=None, description="The IMAP keyword (e.g. $Invoices); derived from the name when not given. A KEYWORD category starts out holding the messages that already carry it.")


@strawberry.input(description="Changes to a category.")
class UpdateCategoryInput:
    id: strawberry.ID
    name: Optional[str] = None
    color: Optional[str] = None
    sync: Optional[enums.CategorySync] = strawberry.field(default=None, description="Move the category between LOCAL and KEYWORD; its messages stay in it.")
    keyword: Optional[str] = strawberry.field(default=None, description="A new keyword; a KEYWORD category's messages are re-keyed on the server.")
    remove_keywords: bool = strawberry.field(default=False, description="KEYWORD → LOCAL: also take the keyword off the messages on the server.")


def _check_name(name: str) -> None:
    if not name.strip() or len(name) > 200:
        raise ValidationError("A category needs a name of at most 200 characters.")


def _create(info: Info, input: CreateCategoryInput) -> models.Category:
    account = get_or_404(models.MailAccount, info, input.account)
    _check_name(input.name)
    try:
        return changes.create_category(account, input.name, input.color, input.sync.value, input.keyword)
    except IntegrityError:
        raise ValidationError("The mailbox already has a category with this name or keyword.") from None
    except MailError as error:
        raise ValidationError(str(error)) from None


@translated
async def create_category(info: Info, input: CreateCategoryInput) -> types.Category:
    """Create a category of a mailbox."""
    return await database_sync_to_async(_create)(info, input)  # type: ignore[return-value]


def _update(info: Info, input: UpdateCategoryInput) -> models.Category:
    category = get_or_404(models.Category, info, input.id)
    if input.name is not None:
        _check_name(input.name)
    try:
        return changes.update_category(category, name=input.name, color=input.color, sync=input.sync.value if input.sync else None, keyword=input.keyword, remove_keywords=input.remove_keywords, user=info.context.request.user)
    except IntegrityError:
        raise ValidationError("The mailbox already has a category with this name or keyword.") from None
    except MailError as error:
        raise ValidationError(str(error)) from None


@translated
async def update_category(info: Info, input: UpdateCategoryInput) -> types.Category:
    """Rename, recolor, re-key a category, or move it between LOCAL and KEYWORD."""
    category = await database_sync_to_async(_update)(info, input)
    await push_soon(category.account_id)
    return category  # type: ignore[return-value]


def _delete(info: Info, id: strawberry.ID, remove_keywords: bool) -> int:
    category = get_or_404(models.Category, info, id)
    account_id = category.account_id
    changes.delete_category(category, remove_keywords, info.context.request.user)
    return account_id


@translated
async def delete_category(info: Info, id: strawberry.ID, remove_keywords: bool = False) -> strawberry.ID:
    """Delete a category. A KEYWORD category's keyword stays on the messages unless `removeKeywords`."""
    account_id = await database_sync_to_async(_delete)(info, id, remove_keywords)
    await push_soon(account_id)
    return id
