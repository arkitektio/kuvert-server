"""Raw messages and attachments in S3, readable through scoped grants; outgoing attachments."""

import email
from email import policy

import pytest
from arkitekt_datalayer.models import BigFileStore
from botocore.client import BaseClient
from kante.context import HttpContext

from mail import models, storage
from tests.conftest import GreenMail, Stack
from tests.support import AExecute, Mailbox, Sync, error_code

pytestmark = pytest.mark.django_db(transaction=True)


async def test_raw_and_attachments_are_stored(
    mailbox: Mailbox,
    greenmail: GreenMail,
    sync: Sync,
    aexecute: AExecute,
    datalayer: BaseClient,
) -> None:
    box = await mailbox()
    greenmail.deliver(
        box["address"],
        "With file",
        "see attached",
        attachments=[("report.csv", "text/csv", b"a,b\n1,2\n")],
    )
    greenmail.wait_for(box["address"], 1)
    await sync(box["id"])
    data = (
        await aexecute(
            "query($a: ID!) { messages(filters: {account: $a}) { raw { id sizeBytes accessGrant { key bucket } } attachments { filename store { id sizeBytes originalFileName } } } }",
            {"a": box["id"]},
        )
    ).data["messages"][0]
    assert (
        data["raw"]["sizeBytes"] > 0
        and data["raw"]["accessGrant"]["bucket"] == "kuvert-mail"
    )
    assert (
        data["attachments"][0]["store"]["sizeBytes"] == 8
        and data["attachments"][0]["store"]["originalFileName"] == "report.csv"
    )
    store = await BigFileStore.objects.aget(id=data["attachments"][0]["store"]["id"])
    assert storage.read_bytes(store) == b"a,b\n1,2\n"
    raw = await BigFileStore.objects.aget(id=data["raw"]["id"])
    assert (
        email.message_from_bytes(storage.read_bytes(raw), policy=policy.default)[
            "Subject"
        ]
        == "With file"
    )


async def test_deleted_mail_orphans_its_files(
    mailbox: Mailbox, greenmail: GreenMail, sync: Sync, datalayer: BaseClient
) -> None:
    box = await mailbox()
    greenmail.deliver(
        box["address"],
        "Gone soon",
        attachments=[("x.bin", "application/octet-stream", b"\x00\x01")],
    )
    greenmail.wait_for(box["address"], 1)
    await sync(box["id"])
    message = await models.Message.objects.aget(account_id=box["id"])
    with greenmail.imap(box["address"]) as imap:
        imap.select_folder("INBOX")
        imap.delete_messages(imap.search("ALL"))
        imap.expunge()
    await sync(box["id"])
    raw = await BigFileStore.objects.aget(id=message.raw_id)
    assert raw.orphaned_at is not None
    from datetime import timedelta

    from asgiref.sync import sync_to_async

    assert await sync_to_async(storage.purge_orphans)(grace=timedelta(0)) == 2
    assert not await BigFileStore.objects.filter(id=message.raw_id).aexists()


UPLOAD = 'mutation($name: String!, $size: ByteCount!) { requestBigfileUpload(input: {originalFileName: $name, fileSize: $size, contentType: "text/plain"}) { accessKey secretKey sessionToken region bucket key store } }'
FINISH = "mutation($id: String!) { finishBigfileUpload(input: {storeId: $id}) { id sizeBytes } }"


async def test_send_with_an_uploaded_attachment(
    mailbox: Mailbox,
    greenmail: GreenMail,
    aexecute: AExecute,
    datalayer: BaseClient,
    backend_stack: Stack,
    colleague_context: HttpContext,
) -> None:
    import boto3
    from botocore.config import Config

    box = await mailbox()
    content = b"attached text\n"
    grant = (await aexecute(UPLOAD, {"name": "note.txt", "size": len(content)})).data[
        "requestBigfileUpload"
    ]
    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://localhost:{backend_stack.rustfs_port}",
        aws_access_key_id=grant["accessKey"],
        aws_secret_access_key=grant["secretKey"],
        aws_session_token=grant["sessionToken"],
        region_name=grant["region"],
        config=Config(signature_version="s3v4"),
    )
    s3.put_object(Bucket=grant["bucket"], Key=grant["key"], Body=content)
    await aexecute(FINISH, {"id": grant["store"]})

    friend = greenmail.user()
    send = 'mutation($a: ID!, $to: String!, $files: [String!]!) { sendMessage(input: {account: $a, to: [{address: $to}], text: "file", attachments: $files}) { status attachments { originalFileName } } }'
    sent = (
        await aexecute(send, {"a": box["id"], "to": friend, "files": [grant["store"]]})
    ).data["sendMessage"]
    assert sent == {"status": "SENT", "attachments": [{"originalFileName": "note.txt"}]}
    greenmail.wait_for(friend, 1)
    with greenmail.imap(friend) as imap:
        imap.select_folder("INBOX", readonly=True)
        raw = next(iter(imap.fetch(imap.search("ALL"), ["BODY.PEEK[]"]).values()))[
            b"BODY[]"
        ]
    [part] = list(
        email.message_from_bytes(raw, policy=policy.default).iter_attachments()
    )
    assert part.get_filename() == "note.txt" and part.get_content().encode() == content

    # Somebody else's upload cannot be attached.
    await aexecute(
        "mutation($id: ID!, $v: Visibility!) { shareMailAccount(input: {id: $id, visibility: $v}) { id } }",
        {"id": box["id"], "v": "ORGANIZATION"},
    )
    refused = await aexecute(
        send,
        {"a": box["id"], "to": friend, "files": [grant["store"]]},
        context=colleague_context,
        allow_errors=True,
    )
    assert error_code(refused) == "VALIDATION_ERROR"


async def test_finish_upload_cannot_reach_stored_mail(
    mailbox: Mailbox,
    greenmail: GreenMail,
    sync: Sync,
    aexecute: AExecute,
    datalayer: BaseClient,
    colleague_context: HttpContext,
) -> None:
    """A store id is sequential: finishing someone else's (or a synced message's) store must not hand out its read grant."""
    box = await mailbox()
    greenmail.deliver(
        box["address"], "Private", attachments=[("p.txt", "text/plain", b"private")]
    )
    greenmail.wait_for(box["address"], 1)
    await sync(box["id"])
    message = await models.Message.objects.aget(account_id=box["id"])
    attachment = await models.Attachment.objects.aget(message=message)
    finish = "mutation($id: String!, $valid: Boolean!) { finishBigfileUpload(input: {storeId: $id, valid: $valid}) { id accessGrant { key } } }"
    for context in (
        colleague_context,
        None,
    ):  # a colleague, and even the owner (it is not an upload)
        for store_id in (message.raw_id, attachment.store_id):
            for valid in (True, False):
                result = await aexecute(
                    finish,
                    {"id": str(store_id), "valid": valid},
                    context=context,
                    allow_errors=True,
                )
                assert result.errors and error_code(result) == "NOT_FOUND", result
    raw = await BigFileStore.objects.aget(id=message.raw_id)
    assert raw.populated is True

    # A colleague's own upload finishes normally.
    grant = (
        await aexecute(
            UPLOAD, {"name": "mine.txt", "size": 1}, context=colleague_context
        )
    ).data["requestBigfileUpload"]
    other = await aexecute(
        finish, {"id": grant["store"], "valid": True}, allow_errors=True
    )
    assert error_code(other) == "NOT_FOUND"  # not the uploader
