"""Single objects by id, presets and OAuth providers."""

from typing import Optional

import strawberry
from kante.types import Info

from mail import enums, models, presets, types
from mail.graphql.utils import get_or_404
from mail.oauth.providers import configured_providers

__all__ = ["mail_account", "mail_folder", "message", "thread", "outgoing_message", "mail_presets", "oauth_providers"]


def mail_account(info: Info, id: strawberry.ID) -> types.MailAccount:
    return get_or_404(models.MailAccount, info, id)  # type: ignore[return-value]


def mail_folder(info: Info, id: strawberry.ID) -> types.MailFolder:
    return get_or_404(models.MailFolder, info, id)  # type: ignore[return-value]


def message(info: Info, id: strawberry.ID) -> types.Message:
    return get_or_404(models.Message, info, id)  # type: ignore[return-value]


def thread(info: Info, id: strawberry.ID) -> types.Thread:
    return get_or_404(models.Thread, info, id)  # type: ignore[return-value]


def outgoing_message(info: Info, id: strawberry.ID) -> types.OutgoingMessage:
    return get_or_404(models.OutgoingMessage, info, id)  # type: ignore[return-value]


def _server(value: tuple[str, int, str] | None) -> Optional[types.ServerSettings]:
    return types.ServerSettings(host=value[0], port=value[1], security=enums.Security(value[2])) if value else None


def mail_presets(info: Info, address: Optional[str] = strawberry.UNSET) -> list[types.MailPreset]:
    """Known providers' server settings; only the one matching ``address`` when it is given."""
    configured = set(configured_providers())
    chosen = presets.PRESETS
    if address:
        match = presets.for_address(address)
        chosen = (match,) if match else ()
    return [
        types.MailPreset(
            key=p.key,
            name=p.name,
            domains=list(p.domains),
            provider=enums.Provider(p.provider),
            imap=_server(p.imap),
            pop3=_server(p.pop3),
            smtp=_server(p.smtp),
            save_sent_copy=p.save_sent_copy,
            oauth=p.oauth,
            oauth_configured=p.oauth and p.provider in configured,
            note=p.note,
        )
        for p in chosen
    ]


def oauth_providers(info: Info) -> list[enums.Provider]:
    """Providers this deployment can link through OAuth."""
    return [enums.Provider(p) for p in configured_providers()]
