"""kuvert as a service of the hub: what exists here (vendored ``rekuest_service``).

Two separate declarations, read by rekuest from the service's manifest (``*service.urls`` in
``urls.py``) and catalogued hub-wide:

* the **structures** kuvert hosts, and the descriptors of their objects. The GraphQL types answer
  ``descriptors`` from the same declarations (``mail.types``);
* the **signals** it emits: which saves and deletes are announced, with no emit in the mutations.
  Users' triggers are checked against the kinds and descriptor keys declared here.

Hosting announces nothing by itself: a structure with no signal below is hosted silently.

That is all a service is. What can be *done* in this process is not declared here: that is an
agent's to say (``kuvert_server.hook_agent``), a different thing with its own configuration.

Mail is personal by default. A signal reaches every member of the organization who can set a
trigger, so only mailboxes shared with the whole organization (``visibility = ORGANIZATION``,
a team mailbox) are announced; private and shared-with-some mail never is. Tasks and mail
accounts are personal too and get no signals.
"""

from mail import models
from rekuest_service import Descriptor, Service, organization_of

service = Service("kuvert", description="Mail: team mailboxes, their threads and outgoing mail.")


# --- Structures: what kuvert hosts ----------------------------------------------------

message = service.structure(
    models.Message,
    "@kuvert/message",
    descriptors=(Descriptor("@kuvert/has_attachments", "BOOL", "Whether it has attachments, not counting inline images"),),
    describe=lambda message: {"@kuvert/has_attachments": bool(message.has_attachments)},
    description="A mail message in one folder of a mailbox.",
)
thread = service.structure(
    models.Thread,
    "@kuvert/thread",
    descriptors=(Descriptor("@kuvert/message_count", "INT", "How many messages the conversation holds"),),
    describe=lambda thread: {"@kuvert/message_count": thread.message_count},
    description="A conversation: the messages of a mailbox that answer one another.",
)
outgoingmessage = service.structure(
    models.OutgoingMessage,
    "@kuvert/outgoingmessage",
    descriptors=(Descriptor("@kuvert/status", "STRING", "Where the message is: SENDING, SENT or FAILED"),),
    describe=lambda outgoing: {"@kuvert/status": str(outgoing.status)},
    description="A message sent through a mailbox's SMTP server.",
)


# --- Signals: what kuvert announces ----------------------------------------------------

on_account = organization_of("account.organization")


def _team_mailbox(obj, kind: str) -> bool:
    """Only mail of a mailbox every member of the organization sees."""
    return obj.account.visibility == models.Visibility.ORGANIZATION


service.model_signal(
    message,
    kinds=("CREATED",),
    organization=on_account,
    when=_team_mailbox,
    description="Mail arrived in a team mailbox.",
)
service.model_signal(
    thread,
    kinds=("CREATED", "UPDATED"),
    organization=on_account,
    when=_team_mailbox,
    description="A conversation in a team mailbox started or grew.",
)
service.model_signal(
    outgoingmessage,
    kinds=("CREATED", "UPDATED"),
    organization=on_account,
    when=_team_mailbox,
    description="Mail from a team mailbox was queued, sent or failed.",
)
