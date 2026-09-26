"""GraphQL enums for the model ``TextChoices``.

Plain ``(str, Enum)`` classes with the exact values the database stores, so an enum argument
can be written straight into a model field. ``tests/test_schema.py`` checks every one against
its ``TextChoices`` so the two cannot drift. Each value's meaning is listed in the description.
"""

from enum import Enum

import strawberry


@strawberry.enum(description='What went wrong, for a client to offer a fix. Also stored as ``last_error_code``. NOT_CONFIGURED: The service is not configured for this (an OAuth client, the datalayer); AUTH_FAILED: The server refused the username or password; CONSENT_EXPIRED: The OAuth grant was revoked or ran out; link the mailbox again; CONNECTION_FAILED: The server could not be reached, or the connection broke; TLS_FAILED: The TLS handshake failed (certificate or protocol); TLS_REQUIRED: The mailbox asks for a connection without TLS, which is not allowed; HOST_NOT_ALLOWED: The host resolves to a private or internal address; SYNC_IN_PROGRESS: Another sync holds this mailbox right now; RATE_LIMITED: Synced too recently; try again later; SERVER_ERROR: The server answered a command with an error; SEND_REJECTED: The SMTP server refused the message or a recipient; UNSUPPORTED_BY_PROTOCOL: POP3 cannot do this (folders, flags on the server); MAILBOX_INACTIVE: The mailbox is disabled or needs new credentials; INVALID_STATE: The link state is unknown, used or belongs to someone else; CODE_EXPIRED: The link was not completed in time; PROVIDER_ERROR: The OAuth provider answered with an error.')
class MailErrorCode(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH_FAILED = "AUTH_FAILED"
    CONSENT_EXPIRED = "CONSENT_EXPIRED"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    TLS_FAILED = "TLS_FAILED"
    TLS_REQUIRED = "TLS_REQUIRED"
    HOST_NOT_ALLOWED = "HOST_NOT_ALLOWED"
    SYNC_IN_PROGRESS = "SYNC_IN_PROGRESS"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    SEND_REJECTED = "SEND_REJECTED"
    UNSUPPORTED_BY_PROTOCOL = "UNSUPPORTED_BY_PROTOCOL"
    MAILBOX_INACTIVE = "MAILBOX_INACTIVE"
    INVALID_STATE = "INVALID_STATE"
    CODE_EXPIRED = "CODE_EXPIRED"
    PROVIDER_ERROR = "PROVIDER_ERROR"


@strawberry.enum(description='Who hosts a mailbox (decides OAuth, presets and Sent-copy behaviour). GENERIC: Any IMAP/POP3 server; GMAIL: Gmail / Google Workspace; MICROSOFT: Outlook.com / Microsoft 365.')
class Provider(str, Enum):
    GENERIC = "GENERIC"
    GMAIL = "GMAIL"
    MICROSOFT = "MICROSOFT"


@strawberry.enum(description='How incoming mail is read. IMAP: IMAP: folders, flags and moves live on the server; POP3: POP3: one inbox, downloaded; flags are local.')
class Protocol(str, Enum):
    IMAP = "IMAP"
    POP3 = "POP3"


@strawberry.enum(description='Transport security of a connection. TLS: Implicit TLS (IMAPS 993, POP3S 995, SMTPS 465); STARTTLS: Plain connection upgraded with STARTTLS (IMAP 143, POP3 110, SMTP 587); NONE: No encryption (only when the deployment allows it).')
class Security(str, Enum):
    TLS = "TLS"
    STARTTLS = "STARTTLS"
    NONE = "NONE"


@strawberry.enum(description='How the service logs in. PASSWORD: Username and (app) password; XOAUTH2: OAuth 2.0 access token (SASL XOAUTH2).')
class AuthMethod(str, Enum):
    PASSWORD = "PASSWORD"
    XOAUTH2 = "XOAUTH2"


@strawberry.enum(description='Lifecycle of a linked mailbox. ACTIVE: Linked; syncs and sends; NEEDS_REAUTH: The credentials stopped working; update the password or link again; DISABLED: Paused by a user; neither synced nor used to send.')
class MailAccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    NEEDS_REAUTH = "NEEDS_REAUTH"
    DISABLED = "DISABLED"


@strawberry.enum(description='Who in the organization sees a mailbox and its mail. PRIVATE: Only the member who linked it; SHARED: The member who linked it and the members it is shared with; ORGANIZATION: Every member of the organization (a team mailbox).')
class Visibility(str, Enum):
    PRIVATE = "PRIVATE"
    SHARED = "SHARED"
    ORGANIZATION = "ORGANIZATION"


@strawberry.enum(description="What a folder is for (IMAP SPECIAL-USE, else guessed from its name). INBOX: Incoming mail; SENT: Sent mail; DRAFTS: Drafts; TRASH: Deleted mail; ARCHIVE: Archived mail; JUNK: Spam; ALL: Every message (Gmail's All Mail); FLAGGED: A virtual folder of flagged mail; OTHER: A user folder.")
class FolderRole(str, Enum):
    INBOX = "INBOX"
    SENT = "SENT"
    DRAFTS = "DRAFTS"
    TRASH = "TRASH"
    ARCHIVE = "ARCHIVE"
    JUNK = "JUNK"
    ALL = "ALL"
    FLAGGED = "FLAGGED"
    OTHER = "OTHER"


@strawberry.enum(description='Where a sent message is. SENDING: Being handed to the SMTP server; SENT: Accepted by the SMTP server; FAILED: Refused or not delivered to the SMTP server.')
class OutgoingStatus(str, Enum):
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


@strawberry.enum(description='Lifecycle of an OAuth link attempt. PENDING: Waiting for the user to approve at the provider; COMPLETED: Approved; the mailbox is linked.')
class OAuthLinkStatus(str, Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
