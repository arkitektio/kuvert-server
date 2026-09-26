"""IMAP, POP3 and SMTP connections through the guard in :mod:`mail.protocols.net`.

Everything here is blocking (stdlib ``poplib``/``smtplib`` and ``imapclient`` over ``imaplib``) and
knows nothing about the ORM: callers pass an :class:`Endpoint` and :class:`Credentials` and run it
in a worker thread. Each ``open_*`` returns a logged-in client or raises one of
:class:`AuthFailed`, :class:`~mail.protocols.net.HostNotAllowed`,
:class:`~mail.protocols.net.TLSRequired`, ``ssl.SSLError`` or ``OSError``.
"""

import base64
import imaplib
import poplib
import smtplib
import socket
import ssl
from dataclasses import dataclass

from imapclient import IMAPClient
from imapclient import exceptions as imap_exceptions

from mail.protocols import net


class AuthFailed(Exception):
    """The server refused the credentials."""


@dataclass(frozen=True)
class Endpoint:
    """Where to connect: a host, a port and the transport security (``TLS``, ``STARTTLS`` or ``NONE``)."""

    host: str
    port: int
    security: str


@dataclass(frozen=True)
class Credentials:
    """How to log in: a password, or an OAuth access token (SASL XOAUTH2)."""

    username: str
    password: str | None = None
    access_token: str | None = None

    @property
    def xoauth2(self) -> bool:
        return self.access_token is not None


def xoauth2_string(username: str, access_token: str) -> str:
    """The SASL XOAUTH2 initial response (before base64): ``user=…^Aauth=Bearer …^A^A``."""
    return f"user={username}\x01auth=Bearer {access_token}\x01\x01"


# --- IMAP -------------------------------------------------------------------------------------


class _PinnedIMAP4(imaplib.IMAP4):
    def __init__(self, host: str, port: int, timeout: float, tls: ssl.SSLContext | None) -> None:
        self._tls = tls
        self._timeout = timeout
        super().__init__(host, port, timeout=timeout)

    def _create_socket(self, timeout: float | None = None) -> socket.socket:
        sock = net.open_socket(self.host, self.port, timeout if timeout is not None else self._timeout)
        return self._tls.wrap_socket(sock, server_hostname=self.host) if self._tls else sock


class GuardedIMAPClient(IMAPClient):
    """An ``IMAPClient`` whose socket goes through the SSRF guard."""

    def _create_IMAP4(self) -> imaplib.IMAP4:  # noqa: N802 - imapclient's name
        return _PinnedIMAP4(self.host, self.port, net.connect_timeout(), self.ssl_context if self.ssl else None)


def open_imap(endpoint: Endpoint, credentials: Credentials) -> GuardedIMAPClient:
    """A logged-in IMAP client."""
    net.require_security(endpoint.security)
    context = net.tls_context()
    client = GuardedIMAPClient(endpoint.host, port=endpoint.port, ssl=endpoint.security == "TLS", ssl_context=context, timeout=net.connect_timeout())
    client.normalise_times = False  # INTERNALDATE as aware datetimes, not the host's local time
    try:
        if endpoint.security == "STARTTLS":
            client.starttls(context)
        try:
            if credentials.xoauth2:
                client.oauth2_login(credentials.username, credentials.access_token)
            else:
                client.login(credentials.username, credentials.password or "")
        except imap_exceptions.LoginError as error:
            raise AuthFailed(str(error)) from error
    except BaseException:
        try:
            client.shutdown()
        except Exception:
            pass
        raise
    return client


# --- POP3 -------------------------------------------------------------------------------------


class GuardedPOP3(poplib.POP3):
    """A ``poplib.POP3`` whose socket goes through the SSRF guard (implicit TLS when ``tls`` is given)."""

    def __init__(self, host: str, port: int, timeout: float, tls: ssl.SSLContext | None) -> None:
        self._tls = tls
        super().__init__(host, port, timeout=timeout)

    def _create_socket(self, timeout: float) -> socket.socket:
        sock = net.open_socket(self.host, self.port, timeout)
        return self._tls.wrap_socket(sock, server_hostname=self.host) if self._tls else sock

    def auth_xoauth2(self, username: str, access_token: str) -> bytes:
        """``AUTH XOAUTH2`` with the initial response inline (poplib has no SASL support)."""
        payload = base64.b64encode(xoauth2_string(username, access_token).encode()).decode()
        try:
            return self._shortcmd(f"AUTH XOAUTH2 {payload}")
        except poplib.error_proto as error:
            # On failure some servers send a base64 JSON challenge ("+ eyJ…") and wait for an
            # empty line before the final -ERR.
            if str(error).startswith("b'+"):
                try:
                    self._shortcmd("")
                except poplib.error_proto as final:
                    raise AuthFailed(str(final)) from final
            raise AuthFailed(str(error)) from error


def open_pop3(endpoint: Endpoint, credentials: Credentials) -> GuardedPOP3:
    """A logged-in POP3 client."""
    net.require_security(endpoint.security)
    context = net.tls_context()
    client = GuardedPOP3(endpoint.host, endpoint.port, net.connect_timeout(), context if endpoint.security == "TLS" else None)
    try:
        if endpoint.security == "STARTTLS":
            client.stls(context)
        if credentials.xoauth2:
            client.auth_xoauth2(credentials.username, credentials.access_token or "")
        else:
            try:
                client.user(credentials.username)
                client.pass_(credentials.password or "")
            except poplib.error_proto as error:
                raise AuthFailed(str(error)) from error
    except BaseException:
        try:
            client.close()
        except Exception:
            pass
        raise
    return client


def pop3_capabilities(client: poplib.POP3) -> list[str]:
    """The server's CAPA answer (empty when it does not support CAPA)."""
    try:
        return sorted(client.capa().keys())
    except poplib.error_proto:
        return []


# --- SMTP -------------------------------------------------------------------------------------


class GuardedSMTP(smtplib.SMTP):
    """An ``smtplib.SMTP`` whose socket goes through the SSRF guard (implicit TLS when ``tls`` is given)."""

    def __init__(self, tls: ssl.SSLContext | None, **kwargs) -> None:
        self._tls = tls
        super().__init__(**kwargs)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        sock = net.open_socket(host, port, timeout)
        return self._tls.wrap_socket(sock, server_hostname=host) if self._tls else sock


def open_smtp(endpoint: Endpoint, credentials: Credentials, local_hostname: str | None = None) -> GuardedSMTP:
    """A logged-in SMTP client."""
    net.require_security(endpoint.security)
    context = net.tls_context()
    client = GuardedSMTP(context if endpoint.security == "TLS" else None, local_hostname=local_hostname, timeout=net.connect_timeout())
    try:
        client.connect(endpoint.host, endpoint.port)  # also the name STARTTLS verifies the certificate against
        if endpoint.security == "STARTTLS":
            client.starttls(context=context)
        client.ehlo_or_helo_if_needed()
        try:
            if credentials.xoauth2:
                token = xoauth2_string(credentials.username, credentials.access_token or "")
                # A failed XOAUTH2 answers with a 334 challenge (an error JSON) that wants an empty reply.
                client.auth("XOAUTH2", lambda challenge=None: token if challenge is None else "", initial_response_ok=True)
            elif credentials.password is not None:
                client.login(credentials.username, credentials.password)
        except smtplib.SMTPAuthenticationError as error:
            raise AuthFailed(str(error)) from error
    except BaseException:
        try:
            client.close()
        except Exception:
            pass
        raise
    return client
