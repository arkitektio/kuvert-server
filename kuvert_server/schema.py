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
    threads_count: int = field(resolver=queries.threads_count, description="How many conversations match the filters (for a list header).")
    messages_count: int = field(resolver=queries.messages_count, description="How many messages match the filters.")
    task_lists: list[types.TaskList] = field(description="The caller's task lists.")
    task_list: types.TaskList = field(resolver=queries.task_list, description="A task list by id.")
    tasks: list[types.Task] = field(description="The caller's tasks (paginated, filterable — `active` is the Inbox view — and orderable).")
    task: types.Task = field(resolver=queries.task, description="A task by id.")
    tasks_count: int = field(resolver=queries.tasks_count, description="How many of the caller's tasks match the filters.")
    categories: list[types.Category] = field(description="Categories of the visible mailboxes (filter by `account`).")
    category: types.Category = field(resolver=queries.category, description="A category by id.")
    mail_changes: list[types.MailChange] = field(description="Changes made here that have not reached the server yet (pending or failed), oldest first.")
    outbox: list[types.OutgoingMessage] = field(description="Mail sent through the visible mailboxes, newest first.")
    outgoing_message: types.OutgoingMessage = field(resolver=queries.outgoing_message, description="A sent message by id.")
    mail_presets: list[types.MailPreset] = field(resolver=queries.mail_presets, description="Server settings of well-known providers (the one for `address`, when given).")
    oauth_providers: list[enums.Provider] = field(resolver=queries.oauth_providers, name="oauthProviders", description="Providers this deployment can link through OAuth.")
    auth_session: types.AuthSession = field(resolver=mutations.auth_session, description="Where a login is. No side effect.")


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
    complete_auth = mutation(resolver=mutations.complete_auth, description="REDIRECT: finish with the code. POLL: advance one step; call until not PENDING.")
    resume_auth = mutation(resolver=mutations.resume_auth, description="The same login again (a fresh openUrl if the old one cannot be reused).")
    cancel_auth = mutation(resolver=mutations.cancel_auth, description="Drop a login that will not be finished. Idempotent.")
    # Messages
    set_message_flags = mutation(resolver=mutations.set_message_flags, description="Add and remove flags of messages.")
    mark_messages_read = mutation(resolver=mutations.mark_messages_read, description="Mark messages read or unread.")
    move_messages = mutation(resolver=mutations.move_messages, description="Move messages to another folder of their mailbox.")
    delete_messages = mutation(resolver=mutations.delete_messages, description="Delete messages (into Trash, or for good).")
    categorize_messages = mutation(resolver=mutations.categorize_messages, description="Put messages into categories and take them out.")
    undo_mail_changes = mutation(resolver=mutations.undo_mail_changes, description="Take back changes that have not reached the server.")
    revert_messages_to_server = mutation(resolver=mutations.revert_messages_to_server, description="Drop local-only and queued flag changes of messages: back to what the server has.")
    retry_mail_changes = mutation(resolver=mutations.retry_mail_changes, description="Queue failed changes again.")
    push_mail_changes = mutation(resolver=mutations.push_mail_changes, description="Push a mailbox's due changes now.")
    # Categories (per mailbox, shared)
    create_category = mutation(resolver=mutations.create_category, description="Create a category of a mailbox.")
    update_category = mutation(resolver=mutations.update_category, description="Change a category.")
    delete_category = mutation(resolver=mutations.delete_category, description="Delete a category.")
    # Tasks (personal; an app sorts conversations into them with upsertTask)
    create_task_list = mutation(resolver=mutations.create_task_list, description="Create a task list.")
    update_task_list = mutation(resolver=mutations.update_task_list, description="Rename, recolor or move a task list.")
    delete_task_list = mutation(resolver=mutations.delete_task_list, description="Delete a task list; its tasks stay, on no list.")
    create_task = mutation(resolver=mutations.create_task, description="Create a task, optionally with conversations.")
    upsert_task = mutation(resolver=mutations.upsert_task, description="Create or update the caller's task with this externalKey, and add conversations to it.")
    update_task = mutation(resolver=mutations.update_task, description="Change a task.")
    delete_task = mutation(resolver=mutations.delete_task, description="Delete a task.")
    set_task_status = mutation(resolver=mutations.set_task_status, description="Mark tasks OPEN, DONE or DISMISSED.")
    snooze_tasks = mutation(resolver=mutations.snooze_tasks, description="Snooze tasks until a time (null wakes them).")
    link_threads = mutation(resolver=mutations.link_threads, description="Put conversations into a task.")
    unlink_threads = mutation(resolver=mutations.unlink_threads, description="Take conversations out of a task.")
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
