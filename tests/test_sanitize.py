"""HTML bodies are served safe; remote images only on request."""

import pytest

from mail import models, sanitize

pytestmark = pytest.mark.django_db(transaction=True)

EVIL = """
<html><head><style>body{background:url(https://track.example/css)}</style><script>alert(1)</script></head>
<body onload="steal()">
<p style="color:red">Hello <a href="javascript:alert(1)">click</a> <a href="https://ok.example">ok</a></p>
<img src="https://track.example/pixel.gif" width="1"><img src="cid:logo">
<form action="https://phish.example"><input name="pw"></form>
<iframe src="https://evil.example"></iframe>
<div style="background-image: url('https://track.example/bg')">bg</div>
<div style="background: \\75 rl(https://track.example/esc)">esc</div>
<div style="background-image: image-set('https://track.example/set' 1x)">set</div>
</body></html>
"""


def test_clean_strips_active_content():
    html = sanitize.clean(EVIL)
    for bad in ("<script", "onload", "javascript:", "<form", "<input", "<iframe", "<style", "url(", "image-set", "track.example/esc", "track.example/set", "track.example/bg"):
        assert bad not in html, bad
    assert 'href="https://ok.example"' in html and 'rel="noopener noreferrer"' in html
    assert 'src="cid:logo"' in html and 'src="https://track.example/pixel.gif"' in html
    assert sanitize.has_remote_images(html)
    blocked = sanitize.block_remote(html)
    assert "track.example" not in blocked and 'src="cid:logo"' in blocked


async def test_html_field_blocks_remote_images(mailbox, greenmail, sync, aexecute):
    box = await mailbox()
    greenmail.deliver(box["address"], "Newsletter", "Plain part", html=EVIL)
    greenmail.wait_for(box["address"], 1)
    await sync(box["id"])
    message = await models.Message.objects.aget(account_id=box["id"])
    data = (await aexecute('query($id: ID!) { message(id: $id) { hasRemoteImages safe: html remote: html(allowRemote: true) textBody } }', {"id": str(message.id)})).data["message"]
    assert data["hasRemoteImages"] is True
    assert "track.example" not in data["safe"] and "<script" not in data["safe"]
    assert "track.example/pixel.gif" in data["remote"] and "<script" not in data["remote"]
    assert data["textBody"] == "Plain part"
