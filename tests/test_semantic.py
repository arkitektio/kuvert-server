"""Search by text and by meaning; similar messages."""

import pytest

from mail import models

pytestmark = pytest.mark.django_db(transaction=True)


async def test_search_and_similar(mailbox, greenmail, sync, aexecute):
    box = await mailbox()
    greenmail.deliver(box["address"], "Your flight booking confirmation", "Flight LH123 from Vienna to Berlin departs 08:15. Boarding pass attached.")
    greenmail.deliver(box["address"], "Boarding pass for your trip", "Your airline check-in is open: flight to Paris tomorrow.")
    greenmail.deliver(box["address"], "Weekly vegetable box", "Carrots, potatoes and leeks are in this week's delivery.")
    greenmail.wait_for(box["address"], 3)
    await sync(box["id"])
    assert await models.Message.objects.filter(account_id=box["id"], embedding__isnull=False).acount() == 3

    lexical = (await aexecute('{ messages(filters: {search: "carrots"}) { subject } }')).data["messages"]
    assert [m["subject"] for m in lexical] == ["Weekly vegetable box"]

    semantic = (await aexecute('{ messages(filters: {search: "airline"}) { subject } }')).data["messages"]
    assert semantic and semantic[0]["subject"] in {"Your flight booking confirmation", "Boarding pass for your trip"}

    flight = await models.Message.objects.aget(account_id=box["id"], subject__startswith="Your flight")
    similar = (await aexecute('query($id: ID!) { messages(filters: {similarTo: $id}, pagination: {limit: 1}) { subject } }', {"id": str(flight.id)})).data["messages"]
    assert similar == [{"subject": "Boarding pass for your trip"}]
