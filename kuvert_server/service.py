"""kuvert as the hub's rekuest sees it (vendored ``rekuest_service``): the service, and its HookAgent.

Two declarations, read by rekuest from one manifest and mounted by ``urls.py`` (``*service.urls``):

* the **service** says what exists: the structures kuvert hosts, the descriptors of their objects,
  and — every save and delete being announced, with no emit in the mutations — the signals it
  emits. Hub-wide; users' triggers are checked against the kinds and descriptor keys declared here,
  and the GraphQL types answer ``descriptors`` from the same declarations (``mail.types``);
* its **agent** says what can be done: the actions rekuest runs here (``mail/scheduled.py``). Every
  organization has the agent and its own schedules, so an action does one organization's share of
  the work.

Mail is personal by default. A signal reaches every member of the organization who can set a
trigger, so only mailboxes shared with the whole organization (``visibility = ORGANIZATION``,
a team mailbox) are announced; private and shared-with-some mail never is. Tasks and mail
accounts are personal too and get no signals.
"""

from mail import models
from rekuest_service import Descriptor, HookAgent, Service, organization_of

service = Service("kuvert", description="Mail: team mailboxes, their threads and outgoing mail.")


# --- Structures ---------------------------------------------------------------------------

on_account = organization_of("account.organization")


def _team_mailbox(obj, kind: str) -> bool:
    """Only mail of a mailbox every member of the organization sees."""
    return obj.account.visibility == models.Visibility.ORGANIZATION


service.structure(
    models.Message,
    "@kuvert/message",
    kinds=("CREATED",),
    organization=on_account,
    when=_team_mailbox,
    descriptors=(Descriptor("@kuvert/has_attachments", "BOOL", "Whether it has attachments, not counting inline images"),),
    describe=lambda message: {"@kuvert/has_attachments": bool(message.has_attachments)},
    description="A mail message in one folder of a mailbox.",
    signal_description="Mail arrived in a team mailbox.",
)
service.structure(
    models.Thread,
    "@kuvert/thread",
    kinds=("CREATED", "UPDATED"),
    organization=on_account,
    when=_team_mailbox,
    descriptors=(Descriptor("@kuvert/message_count", "INT", "How many messages the conversation holds"),),
    describe=lambda thread: {"@kuvert/message_count": thread.message_count},
    description="A conversation: the messages of a mailbox that answer one another.",
    signal_description="A conversation in a team mailbox started or grew.",
)
service.structure(
    models.OutgoingMessage,
    "@kuvert/outgoingmessage",
    kinds=("CREATED", "UPDATED"),
    organization=on_account,
    when=_team_mailbox,
    descriptors=(Descriptor("@kuvert/status", "STRING", "Where the message is: SENDING, SENT or FAILED"),),
    describe=lambda outgoing: {"@kuvert/status": str(outgoing.status)},
    description="A message sent through a mailbox's SMTP server.",
    signal_description="Mail from a team mailbox was queued, sent or failed.",
)


# --- The HookAgent ------------------------------------------------------------------------
# Its actions are declared in ``mail/scheduled.py`` (imported by ``mail.apps``).

agent = HookAgent(service)
