"""Where the service may connect, and how: the SSRF guard and TLS policy shared by IMAP, POP3 and SMTP.

A mailbox's host and port are chosen by a user, and the sync runs inside the deployment's network
-- next to the database, redis and S3. So every connection:

* resolves the host once and refuses private, loopback, link-local, multicast and reserved
  addresses (unless the host is listed in ``mail.allowed_private_hosts``);
* connects to exactly the address it checked (no second lookup a DNS rebind could change),
  while TLS still verifies the certificate against the host *name*;
* requires TLS (implicit or STARTTLS) unless ``mail.allow_insecure`` is set.
"""

import ipaddress
import socket
import ssl

from django.conf import settings


class HostNotAllowed(Exception):
    """The host resolves to an address the service must not connect to."""


class TLSRequired(Exception):
    """The mailbox asks for a connection without TLS, and the deployment does not allow that."""


def mail_settings() -> dict:
    """The ``mail`` config block."""
    return settings.KUVERT_MAIL


def connect_timeout() -> float:
    """Socket timeout of every mail connection."""
    return float(settings.KUVERT_SYNC["connect_timeout_seconds"])


def _forbidden(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or (isinstance(address, ipaddress.IPv4Address) and address in ipaddress.IPv4Network("100.64.0.0/10"))  # carrier-grade NAT, tailnets
    )


def is_allowed_private(host: str) -> bool:
    """Whether ``host`` is explicitly allowed to resolve to an internal address."""
    return host.lower().rstrip(".") in {h.lower().rstrip(".") for h in mail_settings().get("allowed_private_hosts", [])}


def resolve(host: str, port: int) -> list[tuple]:
    """The addresses to try for ``host``, all checked; raises :class:`HostNotAllowed` if any is internal.

    Every address must pass, not just the first: a host with one public and one internal
    record must not reach the internal one on a retry.
    """
    if not host or len(host) > 255:
        raise HostNotAllowed(f"Invalid host {host!r}.")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ConnectionError(f"Cannot resolve {host}: {error}") from error
    if not is_allowed_private(host):
        for info in infos:
            address = ipaddress.ip_address(info[4][0].split("%")[0])
            if _forbidden(address):
                raise HostNotAllowed(f"{host} resolves to {address}, an internal address.")
    return infos


def check_host(host: str, port: int) -> None:
    """Raise unless ``host:port`` may be connected to (for validating input before storing it)."""
    resolve(host, port)


def open_socket(host: str, port: int, timeout: float | None = None) -> socket.socket:
    """A TCP connection to a checked address of ``host``."""
    timeout = connect_timeout() if timeout is None else timeout
    last_error: Exception | None = None
    for family, kind, proto, _, address in resolve(host, port):
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(address)
            return sock
        except OSError as error:
            sock.close()
            last_error = error
    raise ConnectionError(f"Cannot connect to {host}:{port}: {last_error}")


def tls_context() -> ssl.SSLContext:
    """The client TLS context: verified by default, TLS 1.2 or newer."""
    context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if not mail_settings().get("tls_verify", True):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def require_security(security: str) -> None:
    """Raise :class:`TLSRequired` for a plain connection the deployment does not allow."""
    from mail.models import Security

    if security == Security.NONE and not mail_settings().get("allow_insecure", False):
        raise TLSRequired("Connections without TLS are not allowed; use TLS or STARTTLS.")
