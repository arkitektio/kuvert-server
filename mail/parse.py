"""RFC 5322 messages into the fields a :class:`~mail.models.Message` stores.

Parsing never fails a sync: a malformed header is read as raw text, an unknown charset is decoded
as UTF-8 with replacement characters, and a message that cannot be parsed at all keeps what could
be read.
"""

import email
import email.utils
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage, Message

from mail import sanitize

logger = logging.getLogger(__name__)

SNIPPET_CHARS = 200
_MSGID = re.compile(r"<([^<>\s]+)>")
_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|wg|sv|vs|antw|tr|rif|r)(\[\d+\])?\s*:\s*)+", re.IGNORECASE)


@dataclass
class ParsedAttachment:
    position: int
    filename: str
    content_type: str
    content_id: str | None
    inline: bool
    payload: bytes

    @property
    def size(self) -> int:
        return len(self.payload)


@dataclass
class ParsedMessage:
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    subject: str = ""
    sender_name: str = ""
    sender_address: str = ""
    reply_to: list[dict] = field(default_factory=list)
    to: list[dict] = field(default_factory=list)
    cc: list[dict] = field(default_factory=list)
    bcc: list[dict] = field(default_factory=list)
    date: datetime | None = None
    text_body: str = ""
    html_body: str = ""
    has_remote_images: bool = False
    attachments: list[ParsedAttachment] = field(default_factory=list)

    @property
    def snippet(self) -> str:
        return re.sub(r"\s+", " ", self.text_body).strip()[:SNIPPET_CHARS]

    @property
    def has_attachments(self) -> bool:
        return any(not a.inline for a in self.attachments)


def normalize_subject(subject: str) -> str:
    """The subject without Re:/Fwd:/AW:/WG: prefixes, whitespace collapsed, lower-cased."""
    return re.sub(r"\s+", " ", _PREFIX.sub("", subject or "")).strip().lower()


def message_ids(value: str | None) -> list[str]:
    """The ids in a Message-ID / In-Reply-To / References header, without angle brackets."""
    if not value:
        return []
    found = _MSGID.findall(value)
    if found:
        return found
    token = value.strip().strip("<>")
    return [token] if token and " " not in token else []


def _header(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:  # a header the policy cannot parse
        value = msg.get_all(name, failobj=[None])[0] if hasattr(msg, "get_all") else None
    return str(value).strip() if value is not None else ""


def _addresses(msg: Message, name: str) -> list[dict]:
    raw = msg.get_all(name, failobj=[])
    out = []
    for name_, address in email.utils.getaddresses([str(value) for value in raw]):
        if address or name_:
            out.append({"name": name_.strip(), "address": address.strip().lower()})
    return out


def _date(msg: Message) -> datetime | None:
    value = _header(msg, "Date")
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _text(part: Message) -> str:
    try:
        return part.get_content()  # type: ignore[attr-defined]
    except (LookupError, UnicodeError, AttributeError, KeyError, AssertionError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
    except Exception:
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)


def _body_parts(msg: EmailMessage) -> tuple[Message | None, Message | None]:
    try:
        plain = msg.get_body(preferencelist=("plain",))
        html = msg.get_body(preferencelist=("html",))
    except Exception:
        plain = html = None
    if plain is None and html is None and not msg.is_multipart():
        kind = msg.get_content_type()
        if kind == "text/html":
            html = msg
        elif kind.startswith("text/"):
            plain = msg
    return plain, html


def parse(raw: bytes) -> ParsedMessage:
    """Every stored field of the message ``raw``."""
    msg: EmailMessage = email.message_from_bytes(raw, policy=policy.default)  # type: ignore[assignment]
    out = ParsedMessage()
    ids = message_ids(_header(msg, "Message-ID"))
    out.message_id = ids[0][:998] if ids else None
    reply_ids = message_ids(_header(msg, "In-Reply-To"))
    out.in_reply_to = reply_ids[0][:998] if reply_ids else None
    out.references = [ref[:998] for ref in message_ids(_header(msg, "References"))]
    out.subject = _header(msg, "Subject")
    senders = _addresses(msg, "From")
    if senders:
        out.sender_name, out.sender_address = senders[0]["name"], senders[0]["address"]
    out.reply_to = _addresses(msg, "Reply-To")
    out.to = _addresses(msg, "To")
    out.cc = _addresses(msg, "Cc")
    out.bcc = _addresses(msg, "Bcc")
    out.date = _date(msg)

    plain, html = _body_parts(msg)
    if html is not None:
        out.html_body = sanitize.clean(_text(html))
        out.has_remote_images = sanitize.has_remote_images(out.html_body)
    if plain is not None:
        out.text_body = _text(plain).strip()
    elif out.html_body:
        out.text_body = sanitize.html_to_text(out.html_body)

    position = 0
    for part in msg.walk():
        if part.is_multipart() or part is plain or part is html:
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename() or ""
        kind = part.get_content_type()
        if not disposition and not filename and kind in ("text/plain", "text/html"):
            continue  # an alternative body we did not pick
        content_ids = message_ids(_header(part, "Content-ID"))
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            payload = (str(part.get_payload()) or "").encode()
        inline = disposition == "inline" or (not disposition and bool(content_ids))
        out.attachments.append(
            ParsedAttachment(
                position=position,
                filename=filename[:1000],
                content_type=kind[:255],
                content_id=content_ids[0][:998] if content_ids else None,
                inline=inline and kind.startswith("image/"),
                payload=payload,
            )
        )
        position += 1
    return out


def parse_headers(raw_headers: bytes) -> ParsedMessage:
    """What the headers alone give (a message too large to fetch whole)."""
    return parse(raw_headers.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n")
