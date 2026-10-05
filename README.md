# Kuvert-Server

> [!WARNING]
> **Experimental, for personal use.** Kuvert is one of the most experimental Arkitekt services,
> and much of it was written quickly with an AI assistant ("vibecoded"). It works for its author's
> own setup, but it has not been reviewed or hardened the way the core services have. Expect
> breaking changes, and think twice before trusting it with data or credentials that matter.

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
  the hub's rekuest runs the `sync_all_mailboxes` action (on whatever schedule the organization
  set up there; none by default). Nothing loops here, and IMAP IDLE is not used.
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
  - The server's flags (`serverFlags`) are re-read via `CHANGEDSINCE` when the server has
    CONDSTORE, otherwise for the newest `sync.flag_window` messages. Changes made here stay on
    top of them (see [Changing mail](#changing-mail)).
- **Push, then pull.** A sync first pushes the changes made here that are due, then reads the
  server, then pushes what the read made findable again.
- **POP3** has one INBOX, identified by UIDL.
  - Flags are kept locally, and moves answer `UNSUPPORTED_BY_PROTOCOL`. Deletes are pushed
    (`DELE`).
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

- `messages(filters: {account, folder, folderRole, thread, unread, flagged, hasFlag, category,
  syncState, sender, recipient, dateFrom, dateTo, hasAttachments, search, similarTo}, ordering,
  pagination)`.
- `threads(filters: {account, folder, folderRole, unread, flagged, hasAttachments, search, ids})`
  lists conversations directly; a conversation matches when any of its messages does. A row
  shows `latestMessage(folder, folderRole)`, `participants` (distinct senders, oldest first),
  `unreadCount(folder, folderRole)`, `flagged` and `hasAttachments`. `threadsCount` and
  `messagesCount` take the same filters. `mailAccounts(filters: {search})` matches name and address.
- `mailboxSyncs` events carry `folders`: the folders whose messages changed, so a client
  refetches only those lists.
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

Every change is **local first**. `markMessagesRead`, `setMessageFlags`, `categorizeMessages`,
`moveMessages` and `deleteMessages` change the database at once and answer with the messages
as they are now. The server learns of the change afterwards, from a queue of changes
(`mailChanges`).

**How a change reaches the server**

- **Right after the request.** Pushed at once when the change is due
  (`writeback.push_inline`).
- **By rekuest.** Otherwise pushed by the `flush_mail_changes` action, whenever the
  organization's automation runs it.
- **Next sync.** Or pushed by the next sync.
- **Undo windows.** A change is not pushed before its undo window ends (`writeback.undo_seconds_*`;
  10 s for moves, 30 s for deletes, none for flags). Until then, `undoMailChanges` takes it back.
- **Where a message stands.** `Message.syncState` is one of:
  - `SYNCED`: as the server has it;
  - `PENDING`: a change is on its way;
  - `LOCAL`: changed here only;
  - `FAILED`: the server refused a change.

  `Message.changes` lists the change.
- **Retries.** Refusals are retried with backoff. `retryMailChanges` queues FAILED changes again,
  and `pushMailChanges(account)` pushes now.

**Per-mailbox opt-outs** (`updateMailAccount`, owner only)

| Setting | On (default) | Off |
|---|---|---|
| `pushSeen` | read/unread goes to the server | kept here |
| `pushFlagged` | flagging goes to the server | kept here |
| `pushKeywords` | keywords (KEYWORD categories, `$Label…`) go to the server | kept here |
| `pushMoves` | moves and archiving go to the server | moves are refused: a folder only exists on the server |
| `pushDeletes` | deletes go to the server (Trash, then expunge) | deleted mail is only hidden here |

- **A value kept here pins the message.** Server changes to that flag no longer show on it. Other
  flags, and messages not touched here, still follow the server.
- **Undoing a pin.** `revertMessagesToServer` drops the pin.
- **Turning a setting on** pushes what was kept.

**How changes behave**

- **Moves keep ids.** The message is in its new folder here at once. The push uses the
  server's COPYUID to learn the new UID; without COPYUID, the next sync of the folder adopts the row
  instead of adding a copy.
- **Deletes.** A delete moves messages to Trash. It expunges them when they are already in Trash,
  or when `permanent` is set.
- **Conflicts.** For a flag changed here, the local value wins over a change another client made
  before the push. Pushes are `+FLAGS`/`-FLAGS` deltas, so other flags are never overwritten.

## Categories

Categories belong to a mailbox and are shared by everyone who sees it
(`createCategory`, `updateCategory`, `deleteCategory`, `categorizeMessages`,
`messages(filters: {category})`).

- **LOCAL** categories live only here, kept under the message key: the Message-ID, else a hash of
  the headers. Moves and copies anywhere keep them.
- **KEYWORD** categories *are* an IMAP keyword on the server (`$Work`), so other mail clients see
  them.
  - A keyword another client sets puts the message into the category.
  - A new KEYWORD category starts out holding the messages that already carry its keyword.
- **Switching** between LOCAL and KEYWORD keeps the members. `removeKeywords` also takes the
  keyword off the server.

## Limitations

- **Latency.** Other mail clients see a change only after its undo window and the next push (at
  once, or at the next `flush_mail_changes` run or sync).
- **Local wins, per flag.** If another client changes the same flag between the change here and
  its push, the change here wins. Other flags are unaffected.
- **Pins diverge from the server** until `revertMessagesToServer`, or until the push setting is
  turned on again.
  - A pin or a LOCAL category applies to every copy of a message with the same message key.
  - Messages that share a Message-ID (sent to yourself, list duplicates) therefore share them.
- **Keyword support varies by server.**
  - A folder without `\*` in its PERMANENTFLAGS keeps no keywords. Its KEYWORD categories stay
    local and the change is FAILED (`KEYWORDS_NOT_PERMITTED`). `MailFolder.keywordsAllowed` shows
    this, and is learned on the first push.
  - Dovecot with Maildir allows at most 26 keywords per mailbox.
- **Gmail.**
  - Whether Gmail keeps custom keywords is unverified, and keywords are not Gmail labels. Real label
    sync would need `X-GM-LABELS`.
  - Gmail already shows a message once per label folder.
- **Outlook / Exchange.** Whether IMAP keywords map to Outlook categories is unverified. Native
  categories need Microsoft Graph.
- **Servers without UIDPLUS.**
  - A queued expunge is refused (`UNSAFE_EXPUNGE`) while other messages of the folder are marked
    `\Deleted`: a plain EXPUNGE would remove those too.
  - Without COPYUID, a moved message without a Message-ID cannot be adopted and comes back as a
    new row.
- **Servers without CONDSTORE.** Server flag changes are only read for the newest
  `sync.flag_window` messages per folder.
- **Undo** only works before the push. An expunged message is gone for good.
- **Team mailboxes.** Read state and categories are shared by everyone who sees the mailbox; there
  is no per-member read state.
- **Paused and removed mailboxes.** A DISABLED or NEEDS_REAUTH mailbox keeps its queue until it is
  active again. Deleting a mailbox drops its queue.
- **POP3.** Messages deleted here are deleted from the row before QUIT commits the `DELE`. If QUIT
  fails, the next sync reads the message in again.

## Tasks

Mail treated as tasks, in the style of Google Inbox. A **task** holds conversations
(threads) from any mailbox its owner can see. A conversation can be in several tasks. Tasks and
**task lists** are personal: only their owner sees them.

- An app that sorts mail calls `upsertTask(input: {externalKey, title, threads, link: {source:
  APP, confidence, reason}})`. The `externalKey` is the app's own key, so sorting again updates the
  same task and adds conversations instead of duplicating. Every link records who made it (APP or
  USER, plus the app's client id), how sure the app was, and why.
- `tasks(filters: {active: true})` is the Inbox view: OPEN tasks that aren't snoozed. You can also
  filter by `status`, `pinned`, `snoozed`, `list`/`noList`, `dueBefore`, `thread`, `externalKey`
  and `search`. `setTaskStatus`, `snoozeTasks`, `updateTask` (pin, due date, list, position),
  `linkThreads` and `unlinkThreads` change tasks.
- `Thread.tasks` lists the caller's tasks for a conversation. `threads(filters: {hasTask: false})`
  hides mail that's already in an open task, and `threads(filters: {task})` lists a task's
  conversations.
- A task's status is independent of its mail. Finishing a task doesn't archive or mark anything
  read; an app can do that with the mail mutations.
- Conversations keep their identity. A thread remembers every Message-ID it has held, so messages
  that are moved, or read again after a UIDVALIDITY change, return to the same thread. A thread a
  task links is never deleted, even when it's empty. A link to a mailbox the owner can no longer
  see drops out of every read, and comes back if the mailbox is shared again.

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

## Hub integration

Declared in [`kuvert_server/contract.py`](kuvert_server/contract.py):

- **Scopes**: `kuvert_read`, `kuvert_write`.
- **Needs**: rekuest 6 or newer, an instance key, `bigfile` storage, tokens issued by lok, and
  a mounted Fernet key file. Without the key the contract refuses to render a config.

kuvert is known to the hub's rekuest in two separate ways. As a **service**
(`_rekuest/service`) it hosts the structures `@kuvert/message`, `@kuvert/thread` and
`@kuvert/outgoingmessage` ([`kuvert_server/service.py`](kuvert_server/service.py)). As a
**hook agent** (`_rekuest/hook`) it offers the actions below.

## Unattended work (rekuest)

With `rekuest_hook` configured, kuvert offers these actions to the hub's rekuest
([`mail/scheduled.py`](mail/scheduled.py)). Every organization has the agent, so a run does one
organization's share of the work:

| Action | What it does |
|---|---|
| `sync_all_mailboxes` | one sync pass of every ACTIVE mailbox of the organization (mailboxes being synced are skipped) |
| `flush_mail_changes` | pushes the organization's due changes made here; only mailboxes with some are connected to |
| `reembed_stale` | embeds the organization's messages whose vector is missing or from another model |
| `purge_orphaned_stores` | deletes the organization's stored raw messages and attachments nothing references any more |

The actions are only offered. Nothing schedules them by default: whether and how often one runs
is the organization's own automation in rekuest (a schedule, a trigger, or by hand). Without a
schedule, mail arrives only when a client calls `syncMailAccount`.

## Running

The image is `jhnnsrs/kuvert`. It has no default command, and starting it takes two steps:

```bash
python -m arkitekt_service migrate   # wait for the database, migrate, ensureadmin
bash run.sh                          # serve on :80 (daphne), and nothing else
```

`run-debug.sh` does both in one go with Django's autoreloading server, for development.

It needs Postgres with pgvector ([`jhnnsrs/daten`](https://github.com/arkitektio/daten-server))
and Redis, plus S3 (RustFS) for raw messages and attachments. GraphQL is served at `/graphql`,
with the SDL at `/schema`.

## Development

```bash
uv sync
uv run --no-sync pytest       # brings up the stack below via dokker; needs Docker
uv run python manage.py validate_settings
```

The suite runs against a real stack, from `tests/integration/docker-compose.yaml`:

- Postgres with pgvector.
- [GreenMail](https://greenmail-mail-test.github.io/greenmail/): real SMTP, IMAP and POP3 over
  TLS with authentication. Tests seed mail over SMTP and change mailboxes over IMAP behind the
  service's back.
- Dovecot, a second IMAP server, for what GreenMail lacks: CONDSTORE/QRESYNC, UIDPLUS and MOVE.
- RustFS for S3.
- `tests/integration/fakeoauth`: a strict stand-in for Google's and Microsoft's token endpoints
  (code + PKCE, refresh, rotation, revocation).

Nothing in the service is mocked.

Configuration is documented in [CONFIG.md](CONFIG.md). The Fernet key is mounted, never committed
(`*.fernet` is git-ignored). Rotate it with `manage.py rotate_secrets`.

## Releases

Releases are tags: a push to `main` cuts a stable version, a push to `next` a release
candidate. Each one publishes `jhnnsrs/kuvert` under its version (`X.Y.Z`, `X.Y`, `X`), plus
`latest` from `main` and `next` from `next`. The `version` in `pyproject.toml` is a
placeholder. Release notes are on
[GitHub Releases](https://github.com/arkitektio/kuvert-server/releases).
