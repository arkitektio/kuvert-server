"""kuvert as the hub's rekuest sees it: the actions it offers (``mail/scheduled.py``) and the
signals it emits (vendored ``rekuest_service``).

Mail is personal by default. A signal reaches every member of the organization who can set a
trigger, so only mailboxes shared with the whole organization (``visibility = ORGANIZATION``,
a team mailbox) are announced; private and shared-with-some mail never is. Tasks and mail
accounts are personal too and get no signals.
"""

from rekuest_service import Service, organization_of

from mail import models

service = Service("kuvert", description="Mail: team mailboxes, their threads and outgoing mail.")

on_account = organization_of("account.organization")


def _team_mailbox(obj, kind: str) -> bool:
    """Only mail of a mailbox every member of the organization sees."""
    return obj.account.visibility == models.Visibility.ORGANIZATION


service.model_signal(
    models.Message, "@kuvert/message", kinds=("CREATED",), organization=on_account, when=_team_mailbox,
    descriptors=lambda message: {"@kuvert/has_attachments": bool(message.has_attachments)},
    descriptor_keys=("@kuvert/has_attachments",),
    description="Mail arrived in a team mailbox.",
)
service.model_signal(
    models.Thread, "@kuvert/thread", kinds=("CREATED", "UPDATED"), organization=on_account, when=_team_mailbox,
    descriptors=lambda thread: {"@kuvert/message_count": thread.message_count},
    descriptor_keys=("@kuvert/message_count",),
    description="A conversation in a team mailbox started or grew.",
)
service.model_signal(
    models.OutgoingMessage, "@kuvert/outgoingmessage", kinds=("CREATED", "UPDATED"), organization=on_account, when=_team_mailbox,
    descriptors=lambda outgoing: {"@kuvert/status": str(outgoing.status)},
    descriptor_keys=("@kuvert/status",),
    description="Mail from a team mailbox was queued, sent or failed.",
)
