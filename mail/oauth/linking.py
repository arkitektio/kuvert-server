"""Linking (and re-linking) a Gmail or Microsoft mailbox through OAuth.

The service never handles the browser leg (as in bank): ``startOAuthLink`` returns an auth
session -- what the client opens, and the redirect URL the provider sends the browser back to
with ``?code&state``. The client catches that and calls ``completeAuth(state, code)``.

The login *is* the stored :class:`~mail.models.OAuthLink`: its ``state`` is the handle, and it
is only ever answered to the member who started it, in the organization it was started in
(:func:`find`). Completing it exchanges the code (PKCE) once, reads the approved address from the
id token and creates an ACTIVE XOAUTH2 mailbox -- or, for a re-link, stores the new tokens on the
existing one and makes it ACTIVE again. A settled login (COMPLETED, FAILED, CANCELLED) is
answered again as it is.
"""

import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from mail import crypto, models
from mail.errors import MailError
from mail.oauth import providers


#: How long one completion may hold the login while it exchanges the code.
EXCHANGE_LEASE = timedelta(seconds=60)


def _expires_in() -> timedelta:
    return timedelta(seconds=int((settings.KUVERT_OAUTH or {}).get("link_expires_seconds") or 900))


def start(organization, user, provider: str, protocol: str, redirect_url: str | None, name: str = "", account: models.MailAccount | None = None, login_hint: str | None = None) -> models.OAuthLink:  # noqa: ANN001
    """A PENDING link whose ``auth_url`` the client opens."""
    client = providers.client_for(provider)
    redirect = redirect_url or client.redirect_urls[0]
    if redirect not in client.redirect_urls:
        raise MailError("redirectUrl is not one of the registered redirect URLs.", models.MailErrorCode.INVALID_STATE)
    verifier, challenge = providers.pkce_pair()
    state = secrets.token_urlsafe(32)
    return models.OAuthLink.objects.create(
        organization=organization,
        creator=user,
        provider=provider,
        protocol=protocol,
        state=state,
        code_verifier=crypto.encrypt(verifier),
        redirect_url=redirect,
        auth_url=providers.authorize_url(client, state, challenge, redirect, login_hint or (account.email_address if account else None)),
        account=account,
        name=name,
        expires_at=timezone.now() + _expires_in(),
    )


def find(organization, user, state: str) -> models.OAuthLink:  # noqa: ANN001
    """The login with ``state`` that ``user`` started in ``organization``; anything else is the same as no login."""
    link = models.OAuthLink.objects.filter(state=state, organization=organization, creator=user).select_related("account").first()
    if link is None:
        raise MailError("No login with this state: it belongs to another login attempt, another member or another organization.", models.MailErrorCode.INVALID_STATE)
    return link


def is_expired(link: models.OAuthLink) -> bool:
    """PENDING past its time: nothing flips a login on a timer, it only reads as expired."""
    return link.status == models.OAuthLinkStatus.PENDING and link.expires_at < timezone.now()


def _reread(link: models.OAuthLink) -> models.OAuthLink:
    return models.OAuthLink.objects.select_related("account").get(pk=link.pk)


def _fail(link: models.OAuthLink, message: str, code: str) -> models.OAuthLink:
    """Settle a PENDING login as FAILED; its PKCE verifier is no longer needed."""
    models.OAuthLink.objects.filter(pk=link.pk, status=models.OAuthLinkStatus.PENDING).update(
        status=models.OAuthLinkStatus.FAILED, error_code=code, error_message=message[:2000], code_verifier="", claimed_until=None
    )
    return _reread(link)


def _claim(link: models.OAuthLink) -> tuple[models.OAuthLink, bool]:
    """The login as it is now, and whether this call may exchange its code (one winner).

    The winner holds ``claimed_until`` while it talks to the provider; a second completion
    arriving meanwhile (the callback page and a pasted redirect) sees the login still PENDING.
    """
    now = timezone.now()
    with transaction.atomic():
        current = models.OAuthLink.objects.select_for_update().get(pk=link.pk)
        if current.status != models.OAuthLinkStatus.PENDING:
            return _reread(link), False
        if current.expires_at < now:
            return _fail(link, "The login was not completed in time.", models.MailErrorCode.CODE_EXPIRED), False
        if current.claimed_until and current.claimed_until > now:
            return _reread(link), False
        current.claimed_until = now + EXCHANGE_LEASE
        current.save(update_fields=["claimed_until"])
    return _reread(link), True


def _release(link: models.OAuthLink) -> None:
    models.OAuthLink.objects.filter(pk=link.pk).update(claimed_until=None)


def complete(link: models.OAuthLink, code: str | None = None, error: str | None = None, error_description: str | None = None) -> models.OAuthLink:
    """Finish a login (blocking: HTTP to the provider); returns it as it is afterwards.

    ``error`` is the provider's own refusal, as its redirect carried it: the login ends FAILED
    with the provider's words. A code the provider refuses, a missing refresh token or the wrong
    mailbox end it FAILED too. Only not reaching the provider leaves it PENDING and raises.
    """
    if link.status != models.OAuthLinkStatus.PENDING:
        return link
    if error or error_description:
        return _fail(link, error_description or error or "", models.MailErrorCode.PROVIDER_ERROR)
    if not code:
        raise MailError("Finishing this login needs the `code` the provider redirected back with.", models.MailErrorCode.INVALID_STATE)
    link, claimed = _claim(link)
    if not claimed:
        return link
    try:
        _link_mailbox(link, code)
    except providers.ProviderUnreachable:
        _release(link)
        raise
    except MailError as failure:
        return _fail(link, str(failure), failure.code)
    except Exception:
        _release(link)
        raise
    return _reread(link)


def cancel(link: models.OAuthLink) -> models.OAuthLink:
    """Drop a login that will not be finished; the row stays (CANCELLED) so it can still be read. A settled login is returned as it is."""
    models.OAuthLink.objects.filter(pk=link.pk, status=models.OAuthLinkStatus.PENDING).update(status=models.OAuthLinkStatus.CANCELLED, code_verifier="", claimed_until=None)
    return _reread(link)


def _link_mailbox(link: models.OAuthLink, code: str) -> models.MailAccount:
    """Exchange the code and link (or re-link) the mailbox; marks the login COMPLETED."""
    organization, user = link.organization, link.creator
    client = providers.client_for(link.provider)
    tokens = providers.exchange_code(client, code, crypto.decrypt(link.code_verifier), link.redirect_url)
    if not tokens.refresh_token:
        raise MailError("The provider issued no refresh token; approve again with consent.", models.MailErrorCode.PROVIDER_ERROR)
    address, display_name = providers.mailbox_address(client, tokens)
    host, port, security = client.pop3 if link.protocol == models.Protocol.POP3 else client.imap
    smtp_host, smtp_port, smtp_security = client.smtp
    now = timezone.now()
    token_fields = {
        "secret": crypto.encrypt(tokens.refresh_token),
        "access_token": crypto.encrypt(tokens.access_token),
        "token_expires_at": now + timedelta(seconds=tokens.expires_in or 3600),
    }
    with transaction.atomic():
        # Used once: only a login that is still PENDING (not cancelled meanwhile) links a mailbox.
        if not models.OAuthLink.objects.filter(pk=link.pk, status=models.OAuthLinkStatus.PENDING).update(status=models.OAuthLinkStatus.COMPLETED, code_verifier="", claimed_until=None):
            raise MailError("This login is no longer pending.", models.MailErrorCode.INVALID_STATE)
        if link.account is not None:
            account = link.account
            if account.email_address.lower() != address:
                raise MailError(f"This mailbox is {account.email_address}, but {address} was approved.", models.MailErrorCode.INVALID_STATE)
            for key, value in token_fields.items():
                setattr(account, key, value)
            account.status, account.last_error, account.last_error_code = models.MailAccountStatus.ACTIVE, None, None
            account.save()
        else:
            account = models.MailAccount.objects.filter(organization=organization, creator=user, email_address=address, protocol=link.protocol).first()
            if account is None:
                account = models.MailAccount(organization=organization, creator=user, email_address=address, protocol=link.protocol)
            account.name = link.name or account.name or address
            account.display_name = account.display_name or display_name
            account.provider = link.provider
            account.status = models.MailAccountStatus.ACTIVE
            account.incoming_host, account.incoming_port, account.incoming_security = host, port, security
            account.smtp_host, account.smtp_port, account.smtp_security = smtp_host, smtp_port, smtp_security
            account.username = address
            account.auth_method = models.AuthMethod.XOAUTH2
            account.save_sent_copy = False  # Gmail and Microsoft file sent mail themselves
            account.last_error = account.last_error_code = None
            for key, value in token_fields.items():
                setattr(account, key, value)
            account.save()
        models.OAuthLink.objects.filter(pk=link.pk).update(account=account)
    return account
