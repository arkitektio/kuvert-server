"""What went wrong, as a :class:`~mail.models.MailErrorCode`.

One classification serves both places a client sees an error: the ``extensions.code`` of a
GraphQL error (:func:`mail.graphql.errors.translate`) and the ``lastErrorCode`` stored on a
mailbox. A client maps each code to a fix (update the password, link again, try later, …).
"""

import imaplib
import poplib
import smtplib
import ssl
from datetime import datetime

from mail.models import MailErrorCode


class MailError(Exception):
    """A failure with a known code, raised by the service itself."""

    def __init__(self, message: str, code: MailErrorCode) -> None:
        super().__init__(message)
        self.code = code


class AlreadySyncing(MailError):
    """Another sync holds this mailbox right now."""

    def __init__(self) -> None:
        super().__init__("This mailbox is being synced right now.", MailErrorCode.SYNC_IN_PROGRESS)


class SyncTooSoon(MailError):
    """The mailbox was synced less than ``sync.min_interval_seconds`` ago."""

    def __init__(self, allowed_at: datetime) -> None:
        super().__init__(f"No sync is allowed before {allowed_at.isoformat()}.", MailErrorCode.RATE_LIMITED)
        self.allowed_at = allowed_at


def unsupported(what: str) -> MailError:
    """A POP3 mailbox asked to do what only IMAP can."""
    return MailError(f"POP3 mailboxes cannot {what}.", MailErrorCode.UNSUPPORTED_BY_PROTOCOL)


def not_configured(what: str) -> MailError:
    """The deployment lacks the configuration ``what`` needs."""
    return MailError(f"{what} is not configured on this server.", MailErrorCode.NOT_CONFIGURED)


def code_for(error: BaseException) -> MailErrorCode | None:
    """The code of a known failure; None for anything else (a bug, not a server answer)."""
    from mail.protocols.clients import AuthFailed
    from mail.protocols.net import HostNotAllowed, TLSRequired

    if isinstance(error, MailError):
        return error.code
    if isinstance(error, AuthFailed):
        return MailErrorCode.AUTH_FAILED
    if isinstance(error, HostNotAllowed):
        return MailErrorCode.HOST_NOT_ALLOWED
    if isinstance(error, TLSRequired):
        return MailErrorCode.TLS_REQUIRED
    if isinstance(error, (ssl.SSLError, ssl.CertificateError)):
        return MailErrorCode.TLS_FAILED
    if isinstance(error, imaplib.IMAP4.abort):
        return MailErrorCode.CONNECTION_FAILED
    if isinstance(error, (imaplib.IMAP4.error, poplib.error_proto)):
        return MailErrorCode.SERVER_ERROR
    if isinstance(error, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError)):
        return MailErrorCode.SEND_REJECTED
    if isinstance(error, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return MailErrorCode.CONNECTION_FAILED
    if isinstance(error, smtplib.SMTPException):
        return MailErrorCode.SERVER_ERROR
    if isinstance(error, (OSError, EOFError)):  # refused, reset, timed out, unresolvable
        return MailErrorCode.CONNECTION_FAILED
    return None
