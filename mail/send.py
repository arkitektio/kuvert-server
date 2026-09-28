"""Sending mail through a mailbox's own SMTP server.

Synchronous, inside the request: the message is built, handed to the SMTP server, and the
outcome is recorded on an :class:`~mail.models.OutgoingMessage` (the audit trail, and what
``outbox`` lists). Then, best effort:

* a copy is appended to the mailbox's Sent folder (``save_sent_copy``; Gmail and Microsoft keep
  one themselves), and read in, so it shows up in the thread at once;
* the message it answers is flagged ``\\Answered`` (a queued change, pushed right away).

A failure after the SMTP server accepted the message never turns the send into a failure: the
mail is out, and saying otherwise would make a user send it twice.
"""

import logging
import mimetypes
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formataddr, format_datetime, make_msgid

from django.conf import settings
from django.utils import timezone

from mail import accounts, changes, models, storage, sync
from mail.errors import MailError, code_for, not_configured
from mail.protocols.clients import open_smtp
from mail.sync import imap as imap_sync

logger = logging.getLogger(__name__)


def _header_list(recipients: list[dict]) -> str:
    return ", ".join(formataddr((r.get("name") or "", r["address"])) for r in recipients)


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1] if "@" in address else "localhost"


def build(outgoing: models.OutgoingMessage) -> EmailMessage:
    """The RFC 5322 message of ``outgoing`` (Bcc is not a header; it only goes into the envelope)."""
    account = outgoing.account
    msg = EmailMessage()
    msg["From"] = formataddr((account.display_name, account.email_address))
    if outgoing.to:
        msg["To"] = _header_list(outgoing.to)
    if outgoing.cc:
        msg["Cc"] = _header_list(outgoing.cc)
    msg["Subject"] = outgoing.subject
    msg["Date"] = format_datetime(timezone.now())
    msg["Message-ID"] = f"<{outgoing.message_id}>"
    msg["X-Mailer"] = settings.KUVERT_MAIL.get("user_agent") or "kuvert"
    parent = outgoing.in_reply_to
    if parent is not None and parent.message_id:
        msg["In-Reply-To"] = f"<{parent.message_id}>"
        references = [*parent.references, parent.message_id][-20:]
        msg["References"] = " ".join(f"<{ref}>" for ref in references)
    msg.set_content(outgoing.text_body or "")
    if outgoing.html_body:
        msg.add_alternative(outgoing.html_body, subtype="html")
    for store in outgoing.attachments.all():
        payload = storage.read_bytes(store)
        kind = store.content_type or mimetypes.guess_type(store.original_file_name or "")[0] or "application/octet-stream"
        maintype, _, subtype = kind.partition("/")
        msg.add_attachment(payload, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=store.get_upload_file_name())
    return msg


def new_message_id(account: models.MailAccount) -> str:
    """A fresh Message-ID in the mailbox's domain, without angle brackets."""
    return make_msgid(domain=_domain(account.email_address)).strip("<>")


def _save_copy(account: models.MailAccount, raw: bytes) -> bool:
    """Append ``raw`` to the Sent folder and read it in; False when there is none (or it failed)."""
    sent = models.MailFolder.objects.filter(account=account, role=models.FolderRole.SENT, exists_on_server=True).first()
    if sent is None:
        return False
    with sync.incoming_session(account) as client:
        client.append(sent.path, raw, flags=["\\Seen"])
        if sync.claim(account.id):
            try:
                imap_sync.sync_folder(client, account, sent, imap_sync.ImapResult(), condstore=False)
            finally:
                sync.release(account.id)
    return True


def send(outgoing: models.OutgoingMessage) -> models.OutgoingMessage:
    """Send a SENDING ``outgoing`` and record what happened (blocking; run it in a worker)."""
    account = outgoing.account
    try:
        accounts.ensure_active(account)
        msg = build(outgoing)
        raw = msg.as_bytes()
        if len(raw) > int(settings.KUVERT_MAIL["max_send_bytes"]):
            raise MailError(f"The message is {len(raw)} bytes, more than the {settings.KUVERT_MAIL['max_send_bytes']} allowed.", models.MailErrorCode.SEND_REJECTED)
        envelope_to = [r["address"] for r in [*outgoing.to, *outgoing.cc, *outgoing.bcc]]
        client = open_smtp(accounts.smtp_endpoint(account), accounts.smtp_credentials(account), local_hostname=_domain(account.email_address))
        try:
            refused = client.send_message(msg, from_addr=account.email_address, to_addrs=envelope_to)
        finally:
            try:
                client.quit()
            except Exception:
                client.close()
    except Exception as error:
        code = code_for(error)
        if code is None:
            raise
        outgoing.status, outgoing.error, outgoing.error_code = models.OutgoingStatus.FAILED, str(error)[:2000], code
        outgoing.save(update_fields=["status", "error", "error_code"])
        if code in (models.MailErrorCode.AUTH_FAILED, models.MailErrorCode.CONSENT_EXPIRED):
            models.MailAccount.objects.filter(pk=account.pk).update(status=models.MailAccountStatus.NEEDS_REAUTH, last_error=str(error)[:2000], last_error_code=code)
        return outgoing

    outgoing.status, outgoing.sent_at = models.OutgoingStatus.SENT, timezone.now()
    outgoing.refused = {address: [status, text.decode(errors="replace") if isinstance(text, bytes) else str(text)] for address, (status, text) in (refused or {}).items()}
    outgoing.save(update_fields=["status", "sent_at", "refused"])

    if account.protocol == models.Protocol.IMAP and account.save_sent_copy:
        try:
            outgoing.saved_to_sent = _save_copy(account, raw)
            outgoing.save(update_fields=["saved_to_sent"])
        except Exception:
            logger.warning("Sent message %s, but could not save a copy to Sent.", outgoing.pk, exc_info=True)
    parent = outgoing.in_reply_to
    if parent is not None and "\\Answered" not in parent.flags:
        try:
            changes.set_flags(account, [parent], ["\\Answered"], [], outgoing.creator)
            if settings.KUVERT_WRITEBACK.get("push_inline", True):
                sync.push_now(account.id)
        except Exception:
            logger.warning("Sent reply %s, but could not flag %s as answered.", outgoing.pk, parent.pk, exc_info=True)
    return outgoing


def prepare(account: models.MailAccount, creator, *, to: list[dict], cc: list[dict], bcc: list[dict], subject: str, text: str, html: str, in_reply_to: models.Message | None, attachments: list) -> models.OutgoingMessage:  # noqa: ANN001
    """Validate and record a message to send (status SENDING)."""
    recipients = [*to, *cc, *bcc]
    if not recipients:
        raise MailError("A message needs at least one recipient.", models.MailErrorCode.SEND_REJECTED)
    for recipient in recipients:
        try:
            Address(addr_spec=recipient["address"])
        except Exception as error:
            raise MailError(f"{recipient['address']!r} is not an email address.", models.MailErrorCode.SEND_REJECTED) from error
    if attachments and not storage.enabled():
        raise not_configured("File storage (the datalayer)")
    if in_reply_to is not None and not subject:
        subject = in_reply_to.subject if in_reply_to.subject.lower().startswith("re:") else f"Re: {in_reply_to.subject}"
    outgoing = models.OutgoingMessage.objects.create(
        account=account,
        creator=creator,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        text_body=text,
        html_body=html,
        in_reply_to=in_reply_to,
        message_id=new_message_id(account),
    )
    if attachments:
        outgoing.attachments.set(attachments)
    return outgoing
