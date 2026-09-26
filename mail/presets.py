"""Server settings of well-known mail providers, so a client can fill in hosts and ports.

Only a convenience for the ``mailPresets`` query and for mailboxes linked through OAuth; a
mailbox can use any server.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    domains: tuple[str, ...]
    provider: str
    imap: tuple[str, int, str] | None
    pop3: tuple[str, int, str] | None
    smtp: tuple[str, int, str] | None
    save_sent_copy: bool = True
    oauth: bool = False
    note: str = ""


PRESETS: tuple[Preset, ...] = (
    Preset("gmail", "Gmail", ("gmail.com", "googlemail.com"), "GMAIL", ("imap.gmail.com", 993, "TLS"), ("pop.gmail.com", 995, "TLS"), ("smtp.gmail.com", 465, "TLS"), save_sent_copy=False, oauth=True, note="Link with OAuth, or use an app password (needs 2-step verification)."),
    Preset("microsoft", "Outlook.com / Microsoft 365", ("outlook.com", "hotmail.com", "live.com", "msn.com"), "MICROSOFT", ("outlook.office365.com", 993, "TLS"), ("outlook.office365.com", 995, "TLS"), ("smtp.office365.com", 587, "STARTTLS"), save_sent_copy=False, oauth=True, note="Microsoft turned off password logins; link with OAuth."),
    Preset("icloud", "iCloud Mail", ("icloud.com", "me.com", "mac.com"), "GENERIC", ("imap.mail.me.com", 993, "TLS"), None, ("smtp.mail.me.com", 587, "STARTTLS"), note="Use an app-specific password; the username is the part before @."),
    Preset("fastmail", "Fastmail", ("fastmail.com", "fastmail.fm"), "GENERIC", ("imap.fastmail.com", 993, "TLS"), ("pop.fastmail.com", 995, "TLS"), ("smtp.fastmail.com", 465, "TLS"), note="Use an app password."),
    Preset("yahoo", "Yahoo Mail", ("yahoo.com", "yahoo.de", "ymail.com"), "GENERIC", ("imap.mail.yahoo.com", 993, "TLS"), ("pop.mail.yahoo.com", 995, "TLS"), ("smtp.mail.yahoo.com", 465, "TLS"), note="Use an app password."),
    Preset("gmx", "GMX", ("gmx.net", "gmx.de", "gmx.at", "gmx.com"), "GENERIC", ("imap.gmx.net", 993, "TLS"), ("pop.gmx.net", 995, "TLS"), ("mail.gmx.net", 587, "STARTTLS"), note="Enable IMAP/POP3 access in the GMX settings first."),
    Preset("webde", "WEB.DE", ("web.de",), "GENERIC", ("imap.web.de", 993, "TLS"), ("pop3.web.de", 995, "TLS"), ("smtp.web.de", 587, "STARTTLS"), note="Enable IMAP/POP3 access in the WEB.DE settings first."),
    Preset("posteo", "Posteo", ("posteo.de", "posteo.net"), "GENERIC", ("posteo.de", 993, "TLS"), ("posteo.de", 995, "TLS"), ("posteo.de", 465, "TLS")),
    Preset("mailbox", "mailbox.org", ("mailbox.org",), "GENERIC", ("imap.mailbox.org", 993, "TLS"), ("pop3.mailbox.org", 995, "TLS"), ("smtp.mailbox.org", 465, "TLS")),
)

BY_KEY = {preset.key: preset for preset in PRESETS}


def for_address(address: str) -> Preset | None:
    """The preset whose domains include ``address``'s domain."""
    domain = address.rsplit("@", 1)[-1].strip().lower()
    return next((preset for preset in PRESETS if domain in preset.domains), None)


def for_provider(provider: str) -> Preset | None:
    """The preset of an OAuth provider (GMAIL, MICROSOFT)."""
    return next((preset for preset in PRESETS if preset.provider == provider and preset.oauth), None)
