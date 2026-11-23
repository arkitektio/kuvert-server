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
accounts are personal too and get no signals. Providers are the organization's own settings (no
credentials are described), so every change to one is announced.

A member shares mail with a guest of the organization through a collaboration (lok's, by key),
view only: a conversation, a message or sent mail, never a mailbox, a folder or a provider. A
mailbox holds its credentials, and everything in it is part of it (its required, cascading key),
so one in a collaboration would hand out the whole mailbox, live: nothing ever brings one in.
"""

from arkitekt_datalayer.models import BigFileStore
from arkitekt_service.service import Service, organization_of
from arkitekt_service.service.structures import recorded_under
from authentikate.models import User
from authentikate.vars import get_organization, get_user
from django.db.models import Q

from kuvert_server.contract import contract
from mail import models
from mail.scoping import visible_accounts_q

service = Service(
    "kuvert",
    hosts=contract.description.hosts,
    description="Mail: team mailboxes, their threads and outgoing mail.",
)


# --- Collaborations: what an object needs to be seen, put in along with it ------------------
# A message's attachments are part of it (their required, cascading key), so they are in a
# collaboration with it; their bytes (the datalayer's stores) are brought along, so a guest can
# open them. The raw message never is. A message is not part of its conversation (the key is
# nullable): a conversation brings the messages it has when it is put in, and mail threaded into
# it later stays out until it is shared again.


def _message_needs(message: models.Message) -> list[BigFileStore]:
    """The stored bytes of a message's attachments."""
    return [
        attachment.store
        for attachment in models.Attachment.objects.filter(
            message=message, store__isnull=False
        ).select_related("store")
        if attachment.store is not None
    ]


def _thread_needs(thread: models.Thread) -> list[models.Message]:
    """The conversation's messages, as they are now (deleted ones left out)."""
    return list(models.Message.objects.filter(thread=thread, deleted_at=None))


def _sent_mail_needs(outgoing: models.OutgoingMessage) -> list[BigFileStore]:
    """The files sent with it."""
    return list(outgoing.attachments.all())


# --- Structures: what kuvert hosts ----------------------------------------------------
# What a workflow (a root task) created, for ``shareWorkflow`` and the ``createdUnder`` filters:
# sent mail records the task that sent it (the CREATE row of its history). Messages and
# conversations are synced from the mail server and record no task: they are reported unknown.
# A provider is never put in a collaboration (``kuvert_server.schema.SHAREABLE``): it holds an
# OAuth client's secret.


def _never_shared(root_task_id: str) -> Q:
    """No object of the structure: a workflow shares none, and ``createdUnder`` lists none."""
    return Q(pk__in=[])


def _sent_by_workflow(root_task_id: str) -> Q:
    """Sent mail the workflow sent, through a mailbox the member asking sees.

    ``shareWorkflow`` asks no mailbox's visibility of its own: without this, anyone who knows a
    workflow's root task id would share mail sent from a colleague's private mailbox. Outside a
    request (no member) it is nothing.
    """
    user, organization = get_user(), get_organization()
    recorded = recorded_under(models.OutgoingMessage, root_task_id, organization)
    if not isinstance(user, User) or organization is None or recorded is None:
        return Q(pk__in=[])
    visible = models.MailAccount.objects.filter(
        visible_accounts_q(user), organization=organization
    ).values("id")
    return recorded & Q(account__in=visible)


mailprovider = service.structure(
    models.MailProvider,
    "@kuvert/mailprovider",
    describe=lambda provider: {
        "@kuvert/provider_kind": str(provider.kind),
        "@kuvert/enabled": provider.enabled,
        "@kuvert/capabilities": sorted(
            str(capability) for capability in provider.capabilities or []
        ),
    },
    created_under=_never_shared,
)
message = service.structure(
    models.Message,
    "@kuvert/message",
    describe=lambda message: {"@kuvert/has_attachments": bool(message.has_attachments)},
    depends=_message_needs,
)
thread = service.structure(
    models.Thread,
    "@kuvert/thread",
    describe=lambda thread: {"@kuvert/message_count": thread.message_count},
    depends=_thread_needs,
)
outgoingmessage = service.structure(
    models.OutgoingMessage,
    "@kuvert/outgoingmessage",
    describe=lambda outgoing: {"@kuvert/status": str(outgoing.status)},
    depends=_sent_mail_needs,
    created_under=_sent_by_workflow,
)


# --- Signals: what kuvert announces ----------------------------------------------------

on_account = organization_of("account.organization")

service.model_signal(mailprovider, organization=organization_of())


def _team_mailbox(
    obj: models.Message | models.Thread | models.OutgoingMessage, kind: str
) -> bool:
    """Only mail of a mailbox every member of the organization sees."""
    return obj.account.visibility == models.Visibility.ORGANIZATION


service.model_signal(message, organization=on_account, when=_team_mailbox)
service.model_signal(thread, organization=on_account, when=_team_mailbox)
service.model_signal(outgoingmessage, organization=on_account, when=_team_mailbox)
