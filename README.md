# Kuvert-Server

A mail backend following the design principles of the [Arkitekt](https://arkitekt.live)
framework. Users link the mailboxes they already have (Gmail, Outlook, Fastmail, a company
IMAP or POP3 server). Kuvert syncs their mail into Postgres and serves it over GraphQL: folders,
conversations, flags, search by text and by meaning. It sends mail through each mailbox's own
SMTP server.

Kuvert is a mail **client** service, not a mail server. It hosts no mailboxes, receives no mail,
and opens no IMAP/SMTP ports of its own.

## Who sees what

Everything belongs to an **organization**, and within it to a **mailbox**. A mailbox is private
to the member who linked it until they share it:

| `visibility` | Who sees the mailbox and its mail |
|---|---|
| `PRIVATE` (default) | only the member who linked it |
| `SHARED` | that member and the members in `sharedWith` (`shareMailAccount`) |
| `ORGANIZATION` | every member (a team mailbox like info@) |

Anyone who can see a mailbox can read, flag, move and delete its mail and send from it. Only the
member who linked it can change its credentials or sharing, or unlink it. A row the caller cannot
see answers `NOT_FOUND`, exactly like a missing one.

## Linking a mailbox

**With a password** (or an app password):
`createMailAccount(input: {emailAddress, password, protocol: IMAP|POP3, incoming?, smtp?})`.
Servers default to the preset for the address's provider (`mailPresets(address)`). The login is
tested before anything is stored. A wrong password fails with `AUTH_FAILED` and nothing is saved.

**With OAuth** (Gmail, Microsoft; Microsoft no longer accepts passwords). The flow matches
bank's link flow:

1. `startOAuthLink(input: {provider: GMAIL, redirectUrl?})` returns an `AuthSession`. Open its
   `openUrl`.
2. The provider redirects to `redirectUrl?code=…&state=…`.
3. Call `completeOAuthLink(input: {code, state})`. A state is accepted only from the member who
   started it, in the same organization, once, and before it expires.

The refresh token is stored encrypted, and access tokens are refreshed before use. If the user
revokes access, the mailbox turns `NEEDS_REAUTH` (`CONSENT_EXPIRED`). To fix it, call
`startOAuthLink(input: {provider, account})`, which re-links the same mailbox.

Passwords and tokens are Fernet-encrypted at rest (`secrets.key_path`). They are never part of
the schema and never written to history rows.

## Syncing

- **Request/response only.** A mailbox syncs when a client calls `syncMailAccount(id)` or when
  the hub's rekuest runs `sync_all_mailboxes` (every 5 min by default). Nothing loops here, and
  IMAP IDLE is not used.
- **A lease per mailbox.** Two syncs never run at once on any number of replicas; the second
  answers `SYNC_IN_PROGRESS`. A crashed sync frees the mailbox when its lease runs out.
- **Chunked.** Each run takes at most `sync.batch_size` messages per folder and `sync.max_messages_per_run` in total, renewing its lease as it goes:
  - New mail comes first.
  - The backfill then continues newest first, reaching back `sync.backfill_days`.

  A large mailbox fills in over several runs. `more` in the result says there is more to fetch,
  and `backfillDone` says the backfill is complete. Messages are fetched with `BODY.PEEK`, so
  syncing never marks mail as read.
- **IMAP identity** is (folder, UIDVALIDITY, UID).
  - If a folder's UIDVALIDITY changes, that folder is read again from scratch.
  - Messages expunged on the server are deleted here.
  - Flags follow the server: via `CHANGEDSINCE` when the server has CONDSTORE, otherwise by
    re-reading the flags of the newest `sync.flag_window` messages.
- **POP3** has one INBOX, identified by UIDL.
  - Flags are kept locally, and moves answer `UNSUPPORTED_BY_PROTOCOL`.
  - With `popLeaveOnServer` (the default), the local copy mirrors the server.
  - Without it, mail is deleted on the server once stored, and the local copy is the only one.
- **Folders** are discovered with their roles: SPECIAL-USE flags first, then well-known names in
  several languages. Junk and the virtual views (Gmail's All Mail, Starred, Important) are not synced by default (`sync.folders_excluded`), so mail is not duplicated. Toggle any folder
  with `updateMailFolder`.
- **Threads** are built from In-Reply-To/References across all folders of the mailbox, with a
  subject fallback for replies that lost their headers.
- **Oversized messages** above `sync.max_message_bytes` are stored with their headers only
  (`truncated`).

## Reading

- `messages(filters: {account, folder, folderRole, thread, unread, flagged, hasFlag, sender,
  recipient, dateFrom, dateTo, hasAttachments, search, similarTo}, ordering, pagination)`.
- `search` combines a substring match on subject, sender and text with semantic similarity:
  "airline" finds the flight confirmation. `similarTo` orders messages by likeness to another
  message.
- `Message.html` is sanitized on the server: no scripts, styles, event handlers, forms, frames
  or CSS `url()`. Remote images are removed unless the client asks with `html(allowRemote: true)`;
  `hasRemoteImages` tells it when to offer that. Inline images keep their `cid:` references, which
  map to `attachments { contentId }`.
- With a datalayer configured, `Message.raw` (the `.eml`) and `Attachment.store` hold the bytes in
  S3. Read them through the store's `accessGrant`, which is only reachable through a message the
  caller can see.

## Changing mail

`setMessageFlags`, `markMessagesRead`, `moveMessages` and `deleteMessages` change the server
**first** and the database after. If the server refuses, nothing local changes.

- A move reads the destination at once, so the moved messages come back with their new ids.
- A delete moves messages to Trash. It expunges them when they are already in Trash, or when
  `permanent` is set.

## Sending

`sendMessage(input: {account, to, cc, bcc, subject, text, html, inReplyTo, attachments})` sends
the message through the mailbox's SMTP server, inside the request.

- Replies get `In-Reply-To`/`References`, and the original is flagged `\Answered`.
- A copy goes to the Sent folder (`saveSentCopy`; off for Gmail and Microsoft, which file sent
  mail themselves). It is read in at once, so it appears in the conversation.
- To attach files, upload them first with `requestBigfileUpload` / `finishBigfileUpload`. Only
  files the caller uploaded can be attached.
- A refused send is not a GraphQL error. It comes back `FAILED` with `error`/`errorCode`, and
  every send is listed in `outbox`.

## Safety

- User-supplied hosts are resolved once, and private, loopback, link-local, CGNAT and reserved
  addresses are refused (`HOST_NOT_ALLOWED`). The connection then goes to exactly the checked
  address, so a DNS rebind cannot reach the deployment's database, redis or S3. Exceptions go in
  `mail.allowed_private_hosts`.
- TLS (implicit or STARTTLS) is required and certificates are verified. Plaintext needs
  `mail.allow_insecure`.
- Every error carries `extensions.code`, and mailboxes carry `lastErrorCode`. Both use the
  `MailErrorCode` enum: `AUTH_FAILED`, `CONSENT_EXPIRED`, `CONNECTION_FAILED`, `TLS_FAILED`,
  `HOST_NOT_ALLOWED`, `SYNC_IN_PROGRESS`, `SEND_REJECTED`, `UNSUPPORTED_BY_PROTOCOL`, …

## Scheduled work (rekuest)

With `rekuest_hook` configured, the hub's rekuest runs these actions:

| Action | Default schedule | What it does |
|---|---|---|
| `sync_all_mailboxes` | every 300 s | one sync pass of every ACTIVE mailbox (mailboxes being synced are skipped) |
| `reembed_stale` | every `embeddings.sweep_interval` | embeds messages whose vector is missing or from another model |
| `purge_orphaned_stores` | every 6 h (with a datalayer) | deletes stored raw messages and attachments whose messages have been gone for a day |

## Development

```bash
uv sync
uv run --no-sync pytest       # brings up postgres, GreenMail, RustFS and a fake OAuth server via dokker
uv run python manage.py validate_settings
```

The suite runs against a real stack:

- Postgres with pgvector.
- [GreenMail](https://greenmail-mail-test.github.io/greenmail/): real SMTP, IMAP and POP3 over
  TLS with authentication. Tests seed mail over SMTP and change mailboxes over IMAP behind the
  service's back.
- RustFS for S3.
- `tests/integration/fakeoauth`: a strict stand-in for Google's and Microsoft's token endpoints
  (code + PKCE, refresh, rotation, revocation).

Nothing in the service is mocked.

Configuration is documented in [CONFIG.md](CONFIG.md). The Fernet key is mounted, never committed
(`*.fernet` is git-ignored). Rotate it with `manage.py rotate_secrets`.

### Connecting from an app

```bash
arkitekt-server service connect --url http://localhost:8000 --identifier live.arkitekt.kuvert
```
