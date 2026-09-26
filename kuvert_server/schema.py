"""The GraphQL schema for the kuvert service.

Every field requires an authenticated caller (``AuthExtension``). Every read sees only the
caller's active organization and, within it, only the mailboxes the caller may see (their own,
ones shared with them, and organization-wide ones) -- ``OrgScoped`` types and the
``mail.scoping`` lookups. Only the member who linked a mailbox changes its credentials or
sharing, or unlinks it.

* ``AuthentikateExtension`` — authenticates the request from its bearer token
  and exposes the user/organization/client on ``info.context.request``.
* ``KoherentExtension`` — attributes every model write in a request to that
  identity (provenance).
* ``DjangoOptimizerExtension`` — batches/prefetches ORM access to avoid N+1s.
"""

import strawberry
import strawberry_django
from authentikate.strawberry.directives import AuthExtension, AuthSubscribeExtension
from authentikate.strawberry.extension import AuthentikateExtension
from django.conf import settings
from koherent.strawberry.extension import KoherentExtension
from strawberry.schema.config import StrawberryConfig
from strawberry_django.optimizer import DjangoOptimizerExtension

from datalayer import mutations as datalayer_mutations
from datalayer import scalars as datalayer_scalars
from datalayer import types as datalayer_types
from datalayer.datalayer import DatalayerConfig
from kuvert_server.logs import QuietErrorsSchema
from mail import enums, types
from mail.graphql import mutations, queries, subscriptions


def field(**kwargs):  # noqa: ANN201
    """A query field that requires authentication."""
    return strawberry_django.field(extensions=[AuthExtension()], **kwargs)


def mutation(**kwargs):  # noqa: ANN201
    """A mutation that requires authentication."""
    return strawberry_django.mutation(extensions=[AuthExtension()], **kwargs)


def upload_mutation(**kwargs):  # noqa: ANN201
    """A mutation that hands out (or finishes) upload credentials: gated by ``datalayer.upload_roles``."""
    roles = DatalayerConfig(**getattr(settings, "DATALAYER", {})).upload_roles
    return strawberry_django.mutation(extensions=[AuthExtension(any_role_of=roles)], **kwargs)


def subscription(**kwargs):  # noqa: ANN201
    """A subscription that requires authentication."""
    return strawberry.subscription(extensions=[AuthSubscribeExtension()], **kwargs)


@strawberry.type
class Query:
    """The root query type."""

    mail_accounts: list[types.MailAccount] = field(description="The mailboxes the caller sees: their own, shared with them, and the organization's.")
    mail_account: types.MailAccount = field(resolver=queries.mail_account, description="A mailbox by id.")
    mail_folders: list[types.MailFolder] = field(description="Folders of the visible mailboxes (filter by `account`).")
    mail_folder: types.MailFolder = field(resolver=queries.mail_folder, description="A folder by id.")
    messages: list[types.Message] = field(description="Messages of the visible mailboxes (paginated, filterable — `search` also matches by meaning — and orderable).")
    message: types.Message = field(resolver=queries.message, description="A message by id.")
    threads: list[types.Thread] = field(description="Conversations of the visible mailboxes (paginated, filterable, orderable).")
    thread: types.Thread = field(resolver=queries.thread, description="A conversation by id.")
    outbox: list[types.OutgoingMessage] = field(description="Mail sent through the visible mailboxes, newest first.")
    outgoing_message: types.OutgoingMessage = field(resolver=queries.outgoing_message, description="A sent message by id.")
    mail_presets: list[types.MailPreset] = field(resolver=queries.mail_presets, description="Server settings of well-known providers (the one for `address`, when given).")
    oauth_providers: list[enums.Provider] = field(resolver=queries.oauth_providers, name="oauthProviders", description="Providers this deployment can link through OAuth.")


@strawberry.type
class Mutation:
    """The root mutation type."""

    # Mailboxes
    create_mail_account = mutation(resolver=mutations.create_mail_account, description="Link a mailbox with a username and (app) password; the login is tested first.")
    update_mail_account = mutation(resolver=mutations.update_mail_account, description="Change a mailbox (owner only); new servers or credentials are tested first.")
    test_mail_account = mutation(resolver=mutations.test_mail_account, description="Log in to a mailbox's servers now.")
    delete_mail_account = mutation(resolver=mutations.delete_mail_account, description="Unlink a mailbox (owner only). Mail on the server is untouched.")
    share_mail_account = mutation(resolver=mutations.share_mail_account, description="Set who sees a mailbox (owner only).")
    update_mail_folder = mutation(resolver=mutations.update_mail_folder, description="Turn syncing a folder on or off.")
    sync_mail_account = mutation(resolver=mutations.sync_mail_account, description="Sync a mailbox now.")
    # OAuth (Gmail, Microsoft)
    start_oauth_link = mutation(resolver=mutations.start_oauth_link, name="startOAuthLink", description="Start linking (or re-linking) a mailbox through OAuth.")
    complete_oauth_link = mutation(resolver=mutations.complete_oauth_link, name="completeOAuthLink", description="Finish an OAuth login with the redirect's code and state.")
    resume_oauth_link = mutation(resolver=mutations.resume_oauth_link, name="resumeOAuthLink", description="The caller's pending OAuth login again.")
    cancel_oauth_link = mutation(resolver=mutations.cancel_oauth_link, name="cancelOAuthLink", description="Drop the caller's pending OAuth login.")
    # Messages
    set_message_flags = mutation(resolver=mutations.set_message_flags, description="Add and remove flags of messages.")
    mark_messages_read = mutation(resolver=mutations.mark_messages_read, description="Mark messages read or unread.")
    move_messages = mutation(resolver=mutations.move_messages, description="Move messages to another folder of their mailbox.")
    delete_messages = mutation(resolver=mutations.delete_messages, description="Delete messages (into Trash, or for good).")
    # Sending
    send_message = mutation(resolver=mutations.send_message, description="Send a message through a mailbox's SMTP server.")
    # Uploads for attachments (the vendored datalayer): request a grant, write the file to S3, finish, then attach it.
    request_bigfile_upload = upload_mutation(resolver=datalayer_mutations.request_bigfile_upload, description="Request temporary S3 credentials to upload one file (an attachment to send).")
    finish_bigfile_upload = upload_mutation(resolver=mutations.finish_bigfile_upload, description="Finalize the caller's file upload after the client has written the object.")


@strawberry.type
class Subscription:
    """The root subscription type."""

    mailbox_syncs = subscription(resolver=subscriptions.mailbox_syncs, description="Events whenever a visible mailbox finished syncing.")


# A federation schema is required because the authentikate types (User,
# Organization, …) are federated entities carrying ``@key`` directives.
class Schema(QuietErrorsSchema, strawberry.federation.Schema):
    """strawberry.federation.Schema, logging expected resolver errors as one line and bugs with a traceback (see logs.py)."""


schema = Schema(
    query=Query,
    mutation=Mutation,
    subscription=Subscription,
    types=[datalayer_types.BigFileStore],
    config=StrawberryConfig(scalar_map={**datalayer_scalars.SCALAR_MAP}),
    extensions=[
        DjangoOptimizerExtension,
        AuthentikateExtension,
        KoherentExtension,
    ],
)
