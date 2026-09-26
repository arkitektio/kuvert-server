"""One POP3 sync pass: the server's single mailbox into the account's INBOX folder.

Identity is the UIDL. POP3 has no folders, flags or dates to search by, so:

* new messages are downloaded newest first (highest message number), ``sync.batch_size`` per run;
* flags are local only (``\\Seen`` is set by a client, never read from the server);
* with ``pop_leave_on_server`` the local copy mirrors the server: a message deleted there (by
  another client) is deleted here. Without it, a downloaded message is deleted on the server
  once its row is committed, and the local copy is the only one.
"""

import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from mail import models
from mail.protocols.clients import GuardedPOP3, pop3_capabilities
from mail.sync.store import Fetched, delete, max_message_bytes, prepare, write

logger = logging.getLogger(__name__)


@dataclass
class Pop3Result:
    created: int = 0
    updated: int = 0
    deleted: int = 0
    folders: int = 1
    more: bool = False
    new_messages: list[int] = field(default_factory=list)


def inbox(account: models.MailAccount) -> models.MailFolder:
    """The one folder of a POP3 mailbox."""
    folder, _ = models.MailFolder.objects.get_or_create(account=account, path="INBOX", defaults={"name": "INBOX", "role": models.FolderRole.INBOX})
    return folder


def _listing(client: GuardedPOP3) -> tuple[dict[int, str], dict[int, int]]:
    _, uidl_lines, _ = client.uidl()
    _, list_lines, _ = client.list()
    uidls = {}
    for line in uidl_lines:
        number, uid = line.decode(errors="replace").split(" ", 1)
        uidls[int(number)] = uid.strip()
    sizes = {}
    for line in list_lines:
        number, size = line.decode(errors="replace").split(" ", 1)
        sizes[int(number)] = int(size.split()[0])
    return uidls, sizes


def sync(client: GuardedPOP3, account: models.MailAccount) -> Pop3Result:
    """Download what is new; mirror or delete on the server (see the module docstring)."""
    capabilities = pop3_capabilities(client)
    if account.capabilities != capabilities:
        account.capabilities = capabilities
        models.MailAccount.objects.filter(pk=account.pk).update(capabilities=capabilities)

    folder = inbox(account)
    result = Pop3Result()
    uidls, sizes = _listing(client)
    on_server = set(uidls.values())
    stored = set(folder.messages.exclude(uidl=None).values_list("uidl", flat=True))
    if account.pop_leave_on_server:
        gone = stored - on_server
        if gone:
            result.deleted += delete(folder.messages.filter(uidl__in=list(gone)))

    budget = min(int(settings.KUVERT_SYNC["batch_size"]), int(settings.KUVERT_SYNC["max_messages_per_run"]))
    new = [number for number in sorted(uidls, reverse=True) if uidls[number] not in stored]
    take, result.more = new[:budget], len(new) > budget
    limit = max_message_bytes()
    downloaded: list[int] = []
    from mail.sync import renew

    for start in range(0, len(take), 25):
        renew(account.id)
        chunk = take[start : start + 25]
        fetched = []
        for number in chunk:
            size = sizes.get(number, 0)
            if size <= limit:
                _, lines, _ = client.retr(number)
                truncated = False
            else:
                _, lines, _ = client.top(number, 0)
                truncated = True
            fetched.append(Fetched(raw=b"\r\n".join(lines) + b"\r\n", size=size, flags=[], uidl=uidls[number], truncated=truncated))
        created = write(account, folder, [prepare(account, item) for item in fetched])
        result.created += len(created)
        result.new_messages.extend(message.id for message in created)
        downloaded.extend(chunk)

    if not account.pop_leave_on_server:
        # Only now that the rows are committed; the deletions take effect at QUIT.
        for number in downloaded:
            client.dele(number)

    folder.total_count = folder.messages.count()
    folder.unread_count = folder.messages.exclude(flags__contains=["\\Seen"]).count()
    folder.backfill_done = not result.more
    folder.last_synced_at = timezone.now()
    folder.save(update_fields=["total_count", "unread_count", "backfill_done", "last_synced_at"])
    return result
