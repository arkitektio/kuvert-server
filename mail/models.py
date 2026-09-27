"""The kuvert data model.

Everything belongs to an organization, and within it to a mailbox (:class:`MailAccount`):
folders, messages, threads, attachments and sent mail reach both through required FKs, which is
what :mod:`mail.scoping` follows. A mailbox is private to the member who linked it unless they
share it (``visibility``).

A message's identity on the server is (folder, UIDVALIDITY, UID) for IMAP and (UIDL) for POP3;
``message_id`` (the RFC 5322 header) is what threads and cross-folder copies are matched by.
"""

from authentikate.models import Client, Organization, User
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from koherent.fields import ProvenanceField

from datalayer.models import BigFileStore
from embeddings import engine
from embeddings.models import EmbeddedDescriptionMixin, embedding_indexes

#: How much of a message's text is embedded (with its subject and sender).
EMBEDDED_TEXT_CHARS = 1000


class MailErrorCode(models.TextChoices):
    """What went wrong, for a client to offer a fix. Also stored as ``last_error_code``."""

    NOT_CONFIGURED = "NOT_CONFIGURED", "The service is not configured for this (an OAuth client, the datalayer)"
    AUTH_FAILED = "AUTH_FAILED", "The server refused the username or password"
    CONSENT_EXPIRED = "CONSENT_EXPIRED", "The OAuth grant was revoked or ran out; link the mailbox again"
    CONNECTION_FAILED = "CONNECTION_FAILED", "The server could not be reached, or the connection broke"
    TLS_FAILED = "TLS_FAILED", "The TLS handshake failed (certificate or protocol)"
    TLS_REQUIRED = "TLS_REQUIRED", "The mailbox asks for a connection without TLS, which is not allowed"
    HOST_NOT_ALLOWED = "HOST_NOT_ALLOWED", "The host resolves to a private or internal address"
    SYNC_IN_PROGRESS = "SYNC_IN_PROGRESS", "Another sync holds this mailbox right now"
    RATE_LIMITED = "RATE_LIMITED", "Synced too recently; try again later"
    SERVER_ERROR = "SERVER_ERROR", "The server answered a command with an error"
    SEND_REJECTED = "SEND_REJECTED", "The SMTP server refused the message or a recipient"
    UNSUPPORTED_BY_PROTOCOL = "UNSUPPORTED_BY_PROTOCOL", "POP3 cannot do this (folders, flags on the server)"
    MAILBOX_INACTIVE = "MAILBOX_INACTIVE", "The mailbox is disabled or needs new credentials"
    INVALID_STATE = "INVALID_STATE", "The link state is unknown, used or belongs to someone else"
    CODE_EXPIRED = "CODE_EXPIRED", "The link was not completed in time"
    PROVIDER_ERROR = "PROVIDER_ERROR", "The OAuth provider answered with an error"


class Provider(models.TextChoices):
    """Who hosts a mailbox (decides OAuth, presets and Sent-copy behaviour)."""

    GENERIC = "GENERIC", "Any IMAP/POP3 server"
    GMAIL = "GMAIL", "Gmail / Google Workspace"
    MICROSOFT = "MICROSOFT", "Outlook.com / Microsoft 365"


class Protocol(models.TextChoices):
    """How incoming mail is read."""

    IMAP = "IMAP", "IMAP: folders, flags and moves live on the server"
    POP3 = "POP3", "POP3: one inbox, downloaded; flags are local"


class Security(models.TextChoices):
    """Transport security of a connection."""

    TLS = "TLS", "Implicit TLS (IMAPS 993, POP3S 995, SMTPS 465)"
    STARTTLS = "STARTTLS", "Plain connection upgraded with STARTTLS (IMAP 143, POP3 110, SMTP 587)"
    NONE = "NONE", "No encryption (only when the deployment allows it)"


class AuthMethod(models.TextChoices):
    """How the service logs in."""

    PASSWORD = "PASSWORD", "Username and (app) password"
    XOAUTH2 = "XOAUTH2", "OAuth 2.0 access token (SASL XOAUTH2)"


class MailAccountStatus(models.TextChoices):
    """Lifecycle of a linked mailbox."""

    ACTIVE = "ACTIVE", "Linked; syncs and sends"
    NEEDS_REAUTH = "NEEDS_REAUTH", "The credentials stopped working; update the password or link again"
    DISABLED = "DISABLED", "Paused by a user; neither synced nor used to send"


class Visibility(models.TextChoices):
    """Who in the organization sees a mailbox and its mail."""

    PRIVATE = "PRIVATE", "Only the member who linked it"
    SHARED = "SHARED", "The member who linked it and the members it is shared with"
    ORGANIZATION = "ORGANIZATION", "Every member of the organization (a team mailbox)"


class FolderRole(models.TextChoices):
    """What a folder is for (IMAP SPECIAL-USE, else guessed from its name)."""

    INBOX = "INBOX", "Incoming mail"
    SENT = "SENT", "Sent mail"
    DRAFTS = "DRAFTS", "Drafts"
    TRASH = "TRASH", "Deleted mail"
    ARCHIVE = "ARCHIVE", "Archived mail"
    JUNK = "JUNK", "Spam"
    ALL = "ALL", "Every message (Gmail's All Mail)"
    FLAGGED = "FLAGGED", "A virtual folder of flagged mail"
    OTHER = "OTHER", "A user folder"


class OutgoingStatus(models.TextChoices):
    """Where a sent message is."""

    SENDING = "SENDING", "Being handed to the SMTP server"
    SENT = "SENT", "Accepted by the SMTP server"
    FAILED = "FAILED", "Refused or not delivered to the SMTP server"


class TaskStatus(models.TextChoices):
    """Where a task is."""

    OPEN = "OPEN", "To do"
    DONE = "DONE", "Done"
    DISMISSED = "DISMISSED", "Dropped without doing it"


class TaskLinkSource(models.TextChoices):
    """Who put a thread into a task."""

    USER = "USER", "A person, by hand"
    APP = "APP", "An app that sorts mail"


class OAuthLinkStatus(models.TextChoices):
    """Lifecycle of an OAuth link attempt."""

    PENDING = "PENDING", "Waiting for the user to approve at the provider"
    COMPLETED = "COMPLETED", "Approved; the mailbox is linked"


class MailAccount(models.Model):
    """One linked mailbox: where it lives, how to log in, and how far it is synced.

    ``secret`` holds the password (PASSWORD) or the refresh token (XOAUTH2), and
    ``access_token`` the current OAuth access token -- both Fernet-encrypted
    (:mod:`mail.crypto`), never exposed and never written to history rows.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="mail_accounts", help_text="The organization this mailbox belongs to.")
    creator = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="mail_accounts", help_text="The member who linked the mailbox; only they change its credentials or sharing.")
    name = models.CharField(max_length=200, help_text="A display name for the mailbox (e.g. 'Work').")
    email_address = models.CharField(max_length=320, help_text="The mailbox's address; the From of sent mail.")
    display_name = models.CharField(max_length=200, blank=True, default="", help_text="The sender name of sent mail.")
    provider = models.CharField(max_length=20, choices=Provider.choices, default=Provider.GENERIC, help_text="Who hosts the mailbox.")
    status = models.CharField(max_length=20, choices=MailAccountStatus.choices, default=MailAccountStatus.ACTIVE, help_text="Where the mailbox is in its lifecycle.")
    visibility = models.CharField(max_length=20, choices=Visibility.choices, default=Visibility.PRIVATE, help_text="Who in the organization sees the mailbox and its mail.")
    shared_with = models.ManyToManyField(User, blank=True, related_name="shared_mail_accounts", help_text="Members who see the mailbox when it is SHARED.")

    protocol = models.CharField(max_length=10, choices=Protocol.choices, default=Protocol.IMAP, help_text="How incoming mail is read.")
    incoming_host = models.CharField(max_length=255, help_text="The IMAP or POP3 server.")
    incoming_port = models.PositiveIntegerField(help_text="The IMAP or POP3 port.")
    incoming_security = models.CharField(max_length=10, choices=Security.choices, default=Security.TLS, help_text="Transport security of the incoming connection.")
    smtp_host = models.CharField(max_length=255, null=True, blank=True, help_text="The SMTP server; without one the mailbox cannot send.")
    smtp_port = models.PositiveIntegerField(null=True, blank=True, help_text="The SMTP port.")
    smtp_security = models.CharField(max_length=10, choices=Security.choices, default=Security.TLS, help_text="Transport security of the SMTP connection.")

    username = models.CharField(max_length=320, help_text="The login name (usually the address).")
    auth_method = models.CharField(max_length=10, choices=AuthMethod.choices, default=AuthMethod.PASSWORD, help_text="How the service logs in.")
    secret = models.TextField(null=True, blank=True, help_text="Encrypted password (PASSWORD) or refresh token (XOAUTH2). Never exposed.")
    access_token = models.TextField(null=True, blank=True, help_text="Encrypted OAuth access token. Never exposed.")
    token_expires_at = models.DateTimeField(null=True, blank=True, help_text="When the stored access token runs out.")
    smtp_username = models.CharField(max_length=320, null=True, blank=True, help_text="A separate SMTP login, when the server wants one; else `username`.")
    smtp_secret = models.TextField(null=True, blank=True, help_text="Encrypted separate SMTP password. Never exposed.")

    save_sent_copy = models.BooleanField(default=True, help_text="Append sent mail to the Sent folder (off for Gmail and Microsoft, which keep a copy themselves).")
    pop_leave_on_server = models.BooleanField(default=True, help_text="POP3: keep downloaded mail on the server. Off deletes it there once stored.")
    capabilities = ArrayField(models.CharField(max_length=100), default=list, blank=True, help_text="What the incoming server announced (IMAP CAPABILITY, POP3 CAPA).")

    sync_lease_until = models.DateTimeField(null=True, blank=True, help_text="Held by a running sync until then.")
    last_synced_at = models.DateTimeField(null=True, blank=True, help_text="When the last successful sync finished.")
    last_error = models.TextField(null=True, blank=True, help_text="Why the last sync or connection failed, if it did.")
    last_error_code = models.CharField(max_length=30, choices=MailErrorCode.choices, null=True, blank=True, help_text="The machine-readable kind of `last_error`.")
    backfill_done = models.BooleanField(default=False, help_text="Every synced folder has reached the backfill window.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the mailbox was linked.")
    updated_at = models.DateTimeField(auto_now=True, help_text="When the mailbox settings last changed.")
    # Credentials and sync state rotate constantly and must not pile up in history rows.
    provenance = ProvenanceField(excluded_fields=["secret", "access_token", "token_expires_at", "smtp_secret", "sync_lease_until", "last_synced_at", "last_error", "last_error_code", "backfill_done", "capabilities", "updated_at"])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "creator", "email_address", "protocol"], name="mail_account_unique_per_creator"),
        ]

    def __str__(self) -> str:
        return f"{self.name} <{self.email_address}> [{self.protocol}]"


class MailFolder(models.Model):
    """A folder (IMAP mailbox) of a linked mailbox; a POP3 mailbox has exactly one, its INBOX."""

    account = models.ForeignKey(MailAccount, on_delete=models.CASCADE, related_name="folders", help_text="The mailbox this folder is in.")
    path = models.CharField(max_length=1000, help_text="The folder's full name on the server, decoded (e.g. 'INBOX/Receipts').")
    name = models.CharField(max_length=1000, help_text="The last segment of the path.")
    delimiter = models.CharField(max_length=5, null=True, blank=True, help_text="The server's hierarchy delimiter.")
    role = models.CharField(max_length=10, choices=FolderRole.choices, default=FolderRole.OTHER, help_text="What the folder is for.")
    selectable = models.BooleanField(default=True, help_text="Whether the folder can hold messages (\\Noselect folders only hold folders).")
    sync_enabled = models.BooleanField(default=True, help_text="Whether syncs read this folder.")
    exists_on_server = models.BooleanField(default=True, help_text="False once the server stopped listing the folder.")
    uidvalidity = models.BigIntegerField(null=True, blank=True, help_text="The folder's UIDVALIDITY when last synced; a change invalidates every stored UID.")
    highest_modseq = models.BigIntegerField(null=True, blank=True, help_text="CONDSTORE: the HIGHESTMODSEQ when flags were last read.")
    last_uid = models.BigIntegerField(default=0, help_text="The highest UID stored; new mail is everything above it.")
    oldest_uid = models.BigIntegerField(null=True, blank=True, help_text="The lowest UID stored by the backfill.")
    backfill_done = models.BooleanField(default=False, help_text="The backfill reached the window (or the first message).")
    total_count = models.IntegerField(default=0, help_text="Messages in the folder, as the server counts them.")
    unread_count = models.IntegerField(default=0, help_text="Unread messages in the folder, as the server counts them.")
    last_synced_at = models.DateTimeField(null=True, blank=True, help_text="When the folder was last synced.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["account", "path"], name="mail_folder_account_path")]
        ordering = ["account", "path"]

    def __str__(self) -> str:
        return self.path


class Thread(models.Model):
    """A conversation: messages linked by In-Reply-To/References, across the mailbox's folders."""

    account = models.ForeignKey(MailAccount, on_delete=models.CASCADE, related_name="threads", help_text="The mailbox the conversation is in.")
    subject = models.TextField(blank=True, default="", help_text="The subject without Re:/Fwd: prefixes.")
    last_message_at = models.DateTimeField(null=True, blank=True, help_text="The date of the newest message.")
    message_count = models.IntegerField(default=0, help_text="How many messages are in the conversation.")
    message_ids = ArrayField(models.CharField(max_length=998), default=list, blank=True, help_text="Every Message-ID the conversation has held. A message that comes back (moved, re-read after a UIDVALIDITY change) rejoins the thread by it, so tasks keep their threads.")

    class Meta:
        indexes = [models.Index(fields=["account", "-last_message_at"], name="mail_thread_recent"), GinIndex(fields=["message_ids"], name="mail_thread_mids")]


class Message(EmbeddedDescriptionMixin, models.Model):
    """One message in one folder. A copy in another folder is another row with the same ``message_id``."""

    account = models.ForeignKey(MailAccount, on_delete=models.CASCADE, related_name="messages", help_text="The mailbox.")
    folder = models.ForeignKey(MailFolder, on_delete=models.CASCADE, related_name="messages", help_text="The folder the message is in.")
    thread = models.ForeignKey(Thread, on_delete=models.SET_NULL, null=True, blank=True, related_name="messages", help_text="The conversation.")
    uid = models.BigIntegerField(null=True, blank=True, help_text="IMAP: the UID within the folder's UIDVALIDITY.")
    uidvalidity = models.BigIntegerField(null=True, blank=True, help_text="IMAP: the UIDVALIDITY the UID belongs to.")
    uidl = models.CharField(max_length=100, null=True, blank=True, help_text="POP3: the server's unique id.")
    message_id = models.CharField(max_length=998, null=True, blank=True, help_text="The Message-ID header, without angle brackets.")
    in_reply_to = models.CharField(max_length=998, null=True, blank=True, help_text="The In-Reply-To header, without angle brackets.")
    references = ArrayField(models.CharField(max_length=998), default=list, blank=True, help_text="The References header, oldest first.")
    subject = models.TextField(blank=True, default="", help_text="The decoded subject.")
    sender_name = models.CharField(max_length=500, blank=True, default="", help_text="The From display name.")
    sender_address = models.CharField(max_length=320, blank=True, default="", help_text="The From address, lower-cased.")
    reply_to = models.JSONField(default=list, blank=True, help_text="Reply-To addresses: [{name, address}].")
    to = models.JSONField(default=list, blank=True, help_text="To addresses: [{name, address}].")
    cc = models.JSONField(default=list, blank=True, help_text="Cc addresses: [{name, address}].")
    bcc = models.JSONField(default=list, blank=True, help_text="Bcc addresses (only on sent mail): [{name, address}].")
    date = models.DateTimeField(null=True, blank=True, help_text="The Date header (else when the server received it).")
    received_at = models.DateTimeField(null=True, blank=True, help_text="When the server received the message (IMAP INTERNALDATE).")
    snippet = models.CharField(max_length=300, blank=True, default="", help_text="The start of the text, for list views.")
    text_body = models.TextField(blank=True, default="", help_text="The plain-text body (converted from HTML when there is none).")
    html_body = models.TextField(blank=True, default="", help_text="The HTML body, sanitized (no scripts, handlers, forms); remote images are kept here and stripped on read unless asked for.")
    has_remote_images = models.BooleanField(default=False, help_text="The HTML loads images from the internet.")
    size = models.IntegerField(default=0, help_text="The message size in bytes.")
    flags = ArrayField(models.CharField(max_length=200), default=list, blank=True, help_text="IMAP flags and keywords (\\Seen, \\Flagged, \\Answered, \\Draft, $Label…).")
    has_attachments = models.BooleanField(default=False, help_text="The message has attachments (not counting inline images).")
    truncated = models.BooleanField(default=False, help_text="The message was larger than the sync limit; only its headers are stored.")
    raw = models.ForeignKey(BigFileStore, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", help_text="The raw RFC 5322 message in the datalayer (null without one).")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the message was first stored.")

    embedding_source_fields = ("subject", "sender_name", "sender_address", "text_body")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["folder", "uidvalidity", "uid"], condition=models.Q(uid__isnull=False), name="mail_msg_folder_uid"),
            models.UniqueConstraint(fields=["folder", "uidl"], condition=models.Q(uidl__isnull=False), name="mail_msg_folder_uidl"),
        ]
        indexes = [
            models.Index(fields=["folder", "-date"], name="mail_msg_folder_date"),
            models.Index(fields=["account", "-date"], name="mail_msg_account_date"),
            models.Index(fields=["account", "message_id"], name="mail_msg_account_mid"),
            *embedding_indexes("mail_msg"),
        ]

    def embedding_source_text(self) -> str | None:
        """Subject, sender and the head of the text: what a search for a message means."""
        return engine.source_text(self.subject, self.sender_name, self.sender_address, (self.text_body or "")[:EMBEDDED_TEXT_CHARS])

    @property
    def seen(self) -> bool:
        return "\\Seen" in self.flags

    def __str__(self) -> str:
        return f"{self.subject!r} from {self.sender_address}"


class Attachment(models.Model):
    """A file part of a message. Its bytes are in the datalayer (``store``) once stored."""

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="attachments", help_text="The message the file is attached to.")
    position = models.IntegerField(help_text="The part's position among the message's attachments.")
    filename = models.CharField(max_length=1000, blank=True, default="", help_text="The file name, as the sender gave it.")
    content_type = models.CharField(max_length=255, help_text="The MIME type.")
    size = models.IntegerField(default=0, help_text="The decoded size in bytes.")
    content_id = models.CharField(max_length=998, null=True, blank=True, help_text="The Content-ID an HTML body references (cid:), without angle brackets.")
    inline = models.BooleanField(default=False, help_text="Shown inside the HTML body (an inline image), not as a download.")
    store = models.ForeignKey(BigFileStore, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", help_text="The bytes in the datalayer (null without one).")

    class Meta:
        ordering = ["message", "position"]
        constraints = [models.UniqueConstraint(fields=["message", "position"], name="mail_att_message_position")]


class OutgoingMessage(models.Model):
    """A message sent through a mailbox's SMTP server: what was asked, and what happened."""

    account = models.ForeignKey(MailAccount, on_delete=models.CASCADE, related_name="outgoing", help_text="The mailbox it is sent from.")
    creator = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="sent_mail", help_text="The member who sent it.")
    status = models.CharField(max_length=10, choices=OutgoingStatus.choices, default=OutgoingStatus.SENDING, help_text="Where the message is.")
    to = models.JSONField(default=list, help_text="To addresses: [{name, address}].")
    cc = models.JSONField(default=list, blank=True, help_text="Cc addresses: [{name, address}].")
    bcc = models.JSONField(default=list, blank=True, help_text="Bcc addresses: [{name, address}].")
    subject = models.TextField(blank=True, default="", help_text="The subject.")
    text_body = models.TextField(blank=True, default="", help_text="The plain-text body.")
    html_body = models.TextField(blank=True, default="", help_text="The HTML body, as given.")
    in_reply_to = models.ForeignKey(Message, on_delete=models.SET_NULL, null=True, blank=True, related_name="replies", help_text="The message this answers (sets In-Reply-To/References).")
    attachments = models.ManyToManyField(BigFileStore, blank=True, related_name="+", help_text="Uploaded files attached to the message.")
    message_id = models.CharField(max_length=998, help_text="The Message-ID given to the message.")
    refused = models.JSONField(default=dict, blank=True, help_text="Recipients the SMTP server refused, with its answer.")
    saved_to_sent = models.BooleanField(default=False, help_text="A copy was appended to the Sent folder.")
    error = models.TextField(null=True, blank=True, help_text="Why sending failed.")
    error_code = models.CharField(max_length=30, choices=MailErrorCode.choices, null=True, blank=True, help_text="The machine-readable kind of `error`.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When sending was asked for.")
    sent_at = models.DateTimeField(null=True, blank=True, help_text="When the SMTP server accepted it.")
    provenance = ProvenanceField()

    class Meta:
        ordering = ["-created_at"]


class OAuthLink(models.Model):
    """A started OAuth login (authorization code + PKCE); completing it links, or re-links, a mailbox."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="mail_oauth_links", help_text="The organization the link was started in.")
    creator = models.ForeignKey(User, on_delete=models.CASCADE, related_name="mail_oauth_links", help_text="The member who started it; only they complete, resume or cancel it.")
    provider = models.CharField(max_length=20, choices=Provider.choices, help_text="The OAuth provider.")
    protocol = models.CharField(max_length=10, choices=Protocol.choices, default=Protocol.IMAP, help_text="How the linked mailbox is read.")
    state = models.CharField(max_length=100, unique=True, help_text="Opaque value tying the provider's redirect back to this attempt.")
    code_verifier = models.TextField(help_text="Encrypted PKCE verifier. Never exposed.")
    redirect_url = models.CharField(max_length=2000, help_text="Where the provider redirects after approval.")
    auth_url = models.TextField(help_text="What the client opens.")
    status = models.CharField(max_length=10, choices=OAuthLinkStatus.choices, default=OAuthLinkStatus.PENDING, help_text="Where the link is.")
    account = models.ForeignKey(MailAccount, on_delete=models.CASCADE, null=True, blank=True, related_name="oauth_links", help_text="The mailbox it re-links, or the one it linked.")
    name = models.CharField(max_length=200, blank=True, default="", help_text="The display name the new mailbox gets.")
    expires_at = models.DateTimeField(help_text="A link not completed by then is refused.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the link was started.")



class TaskList(models.Model):
    """A member's list of tasks (an Inbox "bundle" or project). Personal: only its owner sees it."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="mail_task_lists", help_text="The organization it belongs to.")
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="mail_task_lists", help_text="The member whose list it is.")
    name = models.CharField(max_length=200, help_text="The list's name.")
    color = models.CharField(max_length=20, blank=True, default="", help_text="A display color (e.g. #4f86f7).")
    position = models.FloatField(default=0, help_text="Where the list sorts among the owner's lists.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the list was created.")
    provenance = ProvenanceField()

    class Meta:
        ordering = ["position", "id"]


class Task(models.Model):
    """Something to do, made of mail threads (from any mailbox its owner can see). Personal: only its owner sees it."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="mail_tasks", help_text="The organization it belongs to.")
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="mail_tasks", help_text="The member whose task it is.")
    list = models.ForeignKey(TaskList, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks", help_text="The list it is on, if any.")
    title = models.CharField(max_length=500, help_text="What to do.")
    notes = models.TextField(blank=True, default="", help_text="Free notes.")
    status = models.CharField(max_length=10, choices=TaskStatus.choices, default=TaskStatus.OPEN, help_text="Where the task is.")
    pinned = models.BooleanField(default=False, help_text="Pinned to the top.")
    due_at = models.DateTimeField(null=True, blank=True, help_text="When it is due.")
    snoozed_until = models.DateTimeField(null=True, blank=True, help_text="Hidden from the active view until then.")
    position = models.FloatField(default=0, help_text="Where the task sorts in its list.")
    external_key = models.CharField(max_length=500, null=True, blank=True, help_text="An app's own key for the task: `upsertTask` finds the task by it, so sorting again updates instead of duplicating.")
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", help_text="The app that created the task, if one did.")
    threads = models.ManyToManyField(Thread, through="TaskThread", related_name="tasks", help_text="The conversations the task is about.")
    completed_at = models.DateTimeField(null=True, blank=True, help_text="When it was marked DONE.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the task was created.")
    updated_at = models.DateTimeField(auto_now=True, help_text="When the task last changed.")
    provenance = ProvenanceField(excluded_fields=["updated_at"])

    class Meta:
        ordering = ["-pinned", "position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "owner", "external_key"], condition=models.Q(external_key__isnull=False), name="mail_task_external_key"),
        ]
        indexes = [models.Index(fields=["owner", "status"], name="mail_task_owner_status")]


class TaskThread(models.Model):
    """A thread in a task: who put it there, and (for an app) how sure it was and why."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="links", help_text="The task.")
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="task_links", help_text="The conversation.")
    source = models.CharField(max_length=10, choices=TaskLinkSource.choices, default=TaskLinkSource.USER, help_text="Who put the thread into the task.")
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", help_text="The app the request came from.")
    confidence = models.FloatField(null=True, blank=True, help_text="An app's confidence (0–1) that the thread belongs here.")
    reason = models.TextField(blank=True, default="", help_text="Why the thread belongs here, in words.")
    position = models.FloatField(default=0, help_text="Where the thread sorts within the task.")
    created_at = models.DateTimeField(auto_now_add=True, help_text="When the thread was put into the task.")

    class Meta:
        ordering = ["position", "id"]
        constraints = [models.UniqueConstraint(fields=["task", "thread"], name="mail_taskthread_unique")]
