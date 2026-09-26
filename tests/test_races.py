"""Two syncs of one mailbox at once: one runs, the other is told SYNC_IN_PROGRESS."""

import asyncio

import pytest

from mail import models

pytestmark = pytest.mark.django_db(transaction=True)


async def test_concurrent_syncs(mailbox, greenmail, aexecute):
    box = await mailbox()
    for n in range(20):
        greenmail.deliver(box["address"], f"m{n}")
    greenmail.wait_for(box["address"], 20)
    document = 'mutation($id: ID!) { syncMailAccount(id: $id) { created } }'
    results = await asyncio.gather(*[aexecute(document, {"id": box["id"]}, allow_errors=True) for _ in range(3)])
    ok = [r for r in results if not r.errors]
    busy = [r for r in results if r.errors]
    assert sum(r.data["syncMailAccount"]["created"] for r in ok) == 20
    assert all(r.errors[0].extensions["code"] == "SYNC_IN_PROGRESS" for r in busy)
    assert await models.Message.objects.filter(account_id=box["id"]).acount() == 20
    account = await models.MailAccount.objects.aget(id=box["id"])
    assert account.sync_lease_until is None and account.last_error is None
