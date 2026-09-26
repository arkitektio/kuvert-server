"""From a stored mailbox to what the protocol clients need: endpoints and decrypted credentials.

Blocking (a token refresh is an HTTP call, and takes the mailbox's row lock so that one replica
rotates a refresh token at a time); call it from the worker thread that also talks to the server.
"""

from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from mail import crypto, models
from mail.errors import MailError
from mail.protocols.clients import Credentials, Endpoint

#: An access token closer than this to its expiry is refreshed before use.
REFRESH_MARGIN = timedelta(seconds=120)


def incoming_endpoint(account: models.MailAccount) -> Endpoint:
    """Where incoming mail is read."""
    return Endpoint(account.incoming_host, account.incoming_port, account.incoming_security)


def smtp_endpoint(account: models.MailAccount) -> Endpoint:
    """Where mail is sent; SEND_REJECTED-style failure when the mailbox has no SMTP server."""
    if not account.smtp_host or not account.smtp_port:
        raise MailError("This mailbox has no SMTP server, so it cannot send.", models.MailErrorCode.NOT_CONFIGURED)
    return Endpoint(account.smtp_host, account.smtp_port, account.smtp_security)


def ensure_active(account: models.MailAccount) -> None:
    """Raise MAILBOX_INACTIVE unless the mailbox is ACTIVE."""
    if account.status != models.MailAccountStatus.ACTIVE:
        raise MailError(f"This mailbox is {account.status.lower().replace('_', ' ')}.", models.MailErrorCode.MAILBOX_INACTIVE)


def access_token(account: models.MailAccount) -> str:
    """A usable OAuth access token of the mailbox, refreshed first when it is (about to be) expired."""
    from mail.oauth import providers

    now = timezone.now()
    if account.access_token and account.token_expires_at and account.token_expires_at - REFRESH_MARGIN > now:
        return crypto.decrypt(account.access_token)
    try:
        return _refreshed(account, now)
    except providers.TokenRevoked as error:
        # Outside the refresh's transaction, which the error rolled back.
        models.MailAccount.objects.filter(pk=account.pk).update(status=models.MailAccountStatus.NEEDS_REAUTH, last_error=str(error), last_error_code=error.code)
        raise


def _refreshed(account: models.MailAccount, now: datetime) -> str:
    from mail.oauth import providers

    with transaction.atomic():
        locked = models.MailAccount.objects.select_for_update().get(pk=account.pk)
        if locked.access_token and locked.token_expires_at and locked.token_expires_at - REFRESH_MARGIN > now:
            token = crypto.decrypt(locked.access_token)  # another replica refreshed meanwhile
        else:
            if not locked.secret:
                raise providers.TokenRevoked("The mailbox has no refresh token; link it again.")
            client = providers.client_for(locked.provider)
            tokens = providers.refresh(client, crypto.decrypt(locked.secret))
            token = tokens.access_token
            locked.access_token = crypto.encrypt(token)
            locked.token_expires_at = now + timedelta(seconds=tokens.expires_in or 3600)
            fields = ["access_token", "token_expires_at"]
            if tokens.refresh_token:  # rotated (Microsoft)
                locked.secret = crypto.encrypt(tokens.refresh_token)
                fields.append("secret")
            locked.save(update_fields=fields)
        account.access_token, account.token_expires_at, account.secret = locked.access_token, locked.token_expires_at, locked.secret
    return token


def incoming_credentials(account: models.MailAccount) -> Credentials:
    """How to log in to the incoming server."""
    if account.auth_method == models.AuthMethod.XOAUTH2:
        return Credentials(account.username, access_token=access_token(account))
    return Credentials(account.username, password=crypto.decrypt_optional(account.secret) or "")


def smtp_credentials(account: models.MailAccount) -> Credentials:
    """How to log in to the SMTP server (its own login when one is set)."""
    if account.auth_method == models.AuthMethod.XOAUTH2:
        return Credentials(account.username, access_token=access_token(account))
    if account.smtp_username:
        return Credentials(account.smtp_username, password=crypto.decrypt_optional(account.smtp_secret) or "")
    return Credentials(account.username, password=crypto.decrypt_optional(account.secret) or "")
