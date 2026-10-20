"""What this image answers a hub's installer: ``arkitekt-service <verb>`` (see ``arkitekt_service.contract``).

The installer knows the hub; how this release spells its config is written here, with the
settings it is read by. A key renamed in ``configuration.py`` is renamed in :func:`render` in
the same commit, and no installer has to learn of it.
"""

from __future__ import annotations

from arkitekt_service.contract import JSON, Contract, Description, Descriptor, Facts, Hosts, Job, Needs, Offers, Refused, Scope, Signal, Start, Structure, blocks

from kuvert_server.configuration import Settings

#: What a token may be allowed to do here: defined at the coordination server when the hub enrols.
SCOPES = [
    Scope(key="kuvert_read", description="Read synced mail"),
    Scope(key="kuvert_write", description="Link mailboxes, organise and send mail"),
]

#: What exists on a hub because this service is there: said here, as data, so the hub knows it from
#: the image. ``service.py`` binds each of these to its model and refuses anything not said here.
PROVIDER_DESCRIPTORS = [
    Descriptor(key="@kuvert/provider_kind", type="STRING", description="The kind of provider: GENERIC (an IMAP or POP3 mail server), GMAIL (Gmail, Google Workspace) or MICROSOFT (Outlook.com, Microsoft 365)"),
    Descriptor(key="@kuvert/enabled", type="BOOL", description="Whether it is switched on: it links mailboxes, and they sync and send mail"),
    Descriptor(key="@kuvert/capabilities", type="LIST", description="What is switched on beyond reading mail: SEND (sending email) and SCHEDULED_SYNC"),
]

HOSTS = Hosts(
    structures=[
        Structure(
            identifier="@kuvert/mailprovider",
            label="Mail Provider",
            description="An email provider an organization set up (an IMAP mail server, a Gmail or Microsoft OAuth client), through which mailboxes (email accounts) are linked.",
            descriptors=PROVIDER_DESCRIPTORS,
        ),
        Structure(
            identifier="@kuvert/message",
            label="Email Message",
            description="An email (e-mail message) in one folder of a mailbox, such as the inbox, with its sender, recipients, subject, body and attachments.",
            descriptors=[
                Descriptor(key="@kuvert/has_attachments", type="BOOL", description="Whether the email has attachments (attached files), not counting inline images"),
            ],
        ),
        Structure(
            identifier="@kuvert/thread",
            label="Email Thread",
            description="A conversation: the emails of a mailbox that answer one another (replies and forwards), across its folders.",
            descriptors=[
                Descriptor(key="@kuvert/message_count", type="INT", description="How many emails the conversation holds"),
            ],
        ),
        Structure(
            identifier="@kuvert/outgoingmessage",
            label="Outgoing Email",
            description="An email sent from a mailbox through its SMTP server: what was asked to be sent, and what happened.",
            descriptors=[
                Descriptor(key="@kuvert/status", type="STRING", description="Where the outgoing email is: SENDING, SENT or FAILED"),
            ],
        ),
    ],
    signals=[
        Signal(
            identifier="@kuvert/mailprovider",
            kinds=["CREATED", "UPDATED", "DELETED"],
            descriptors=[descriptor.key for descriptor in PROVIDER_DESCRIPTORS],
            description="A mail provider was set up, changed (enabled, its capabilities, its servers or client) or removed.",
        ),
        Signal(
            identifier="@kuvert/message",
            kinds=["CREATED"],
            descriptors=["@kuvert/has_attachments"],
            description="New email arrived in a team mailbox (one every member of the organization sees).",
        ),
        Signal(
            identifier="@kuvert/thread",
            kinds=["CREATED", "UPDATED"],
            descriptors=["@kuvert/message_count"],
            description="An email conversation (thread) in a team mailbox started or grew.",
        ),
        Signal(
            identifier="@kuvert/outgoingmessage",
            kinds=["CREATED", "UPDATED"],
            descriptors=["@kuvert/status"],
            description="An email from a team mailbox was queued for sending, sent or failed.",
        ),
    ],
)


def render(facts: Facts) -> dict[str, JSON]:
    """This release's config for the hub ``facts`` describes."""
    document: dict[str, JSON] = blocks.server(facts)
    key = facts.secrets.get("fernet")
    if key is None:
        raise Refused("it needs a Fernet key file mounted: stored mailbox credentials are encrypted with it")
    document["secrets"] = {"key_path": key}
    if facts.storage is not None:
        document["datalayer"] = blocks.datalayer(facts)
    document["instance"] = blocks.instance(facts)
    hook = blocks.rekuest_hook(facts)
    if hook is not None:
        document["rekuest_hook"] = hook
    return document


contract = Contract(
    description=Description(
        name="kuvert",
        identifier="live.arkitekt.kuvert",
        summary="Mail, synced and organised.",
        needs=Needs(scopes=SCOPES, storage=["bigfile"], instance_key=True, peers=["rekuest"], secrets=["fernet"]),
        offers=Offers(endpoints={"rekuest_service": "_rekuest/service", "rekuest_hook": "_rekuest/hook"}),
        requires={"rekuest": ">=6"},
        hosts=HOSTS,
    ),
    settings=Settings,
    render=render,
    # How this service is started: there is no script beside it. `arkitekt-service serve`
    # (and `debug`) become these, so they get the container's signals themselves.
    serve=Start(("daphne", "-b", "0.0.0.0", "-p", "80", "--websocket_timeout", "-1", "kuvert_server.asgi:application")),
    debug=Start(("python", "manage.py", "runserver", "0.0.0.0:80")),
    jobs={
        "ensureadmin": Job(("ensureadmin",), "Create the operator account the config names"),
        "rotate_secrets": Job(("rotate_secrets",), "Re-encrypt every stored mailbox and provider credential with the first key of the key file"),
        "restore_descriptors": Job(("restore_descriptors",), "Store the descriptors of every provider, message, conversation and sent mail that misses one (--all: of every row)"),
    },
    setup=("ensureadmin", "restore_descriptors"),
)
