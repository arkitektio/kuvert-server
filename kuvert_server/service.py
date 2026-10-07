"""kuvert as a service of the hub: the models and the code behind what its contract says it hosts.

What exists here (the structures, the descriptors of their objects, the signals and their kinds)
is declared once, as data, in ``kuvert_server.contract`` (``hosts``), so that a hub knows it from the
image. This module only binds it: each structure to its model and to what computes its
descriptors, each signal to the saves and deletes that send it. A structure the contract does not
declare cannot be bound, and one it declares that nothing binds here stops the service at its
start. The GraphQL types answer ``descriptors`` from the same binding (``mail.types``).

Hosting announces nothing by itself: a structure with no signal below is hosted silently.

That is all a service is. What can be *done* in this process is not declared here: that is an
agent's to say (``kuvert_server.hook_agent``), a different thing with its own configuration.

Mail is personal by default. A signal reaches every member of the organization who can set a
trigger, so only mailboxes shared with the whole organization (``visibility = ORGANIZATION``,
a team mailbox) are announced; private and shared-with-some mail never is. Tasks and mail
accounts are personal too and get no signals.
"""

from mail import models
from arkitekt_service.service import Service, organization_of

from kuvert_server.contract import contract

service = Service("kuvert", hosts=contract.description.hosts, description="Mail: team mailboxes, their threads and outgoing mail.")


# --- Structures: what kuvert hosts ----------------------------------------------------

message = service.structure(models.Message, "@kuvert/message", describe=lambda message: {"@kuvert/has_attachments": bool(message.has_attachments)})
thread = service.structure(models.Thread, "@kuvert/thread", describe=lambda thread: {"@kuvert/message_count": thread.message_count})
outgoingmessage = service.structure(
    models.OutgoingMessage,
    "@kuvert/outgoingmessage",
    describe=lambda outgoing: {"@kuvert/status": str(outgoing.status)},
)


# --- Signals: what kuvert announces ----------------------------------------------------

on_account = organization_of("account.organization")


def _team_mailbox(obj, kind: str) -> bool:
    """Only mail of a mailbox every member of the organization sees."""
    return obj.account.visibility == models.Visibility.ORGANIZATION


service.model_signal(message, organization=on_account, when=_team_mailbox)
service.model_signal(thread, organization=on_account, when=_team_mailbox)
service.model_signal(outgoingmessage, organization=on_account, when=_team_mailbox)
