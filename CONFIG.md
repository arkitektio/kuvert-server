# Kuvert — Configuration Reference

This document explains how the **kuvert** service is configured, then lists every
configuration value, its environment-variable name, its default, and what it does.

The single source of truth for the schema is
[`kuvert_server/configuration.py`](kuvert_server/configuration.py); the reference tables below
are generated from it. If the two ever disagree, the code wins — and you can always print the
live, resolved configuration with `python manage.py validate_settings`.

---

## How configuration works

Configuration is a typed [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
schema. Values are resolved from several sources, **highest precedence first**:

1. **Init kwargs** — values passed directly in code (rarely used; tests).
2. **Environment variables** — override anything in the YAML file.
3. **The YAML file** — [`config.yaml`](config.yaml) by default.
4. **File secrets** — Docker/systemd secret files, if used.

So an environment variable always beats the YAML file, which makes containerized
overrides easy without editing the mounted config.

### The YAML file

By default the service reads `config.yaml` next to the project. Point it elsewhere with
the `ARKITEKT_CONFIG_FILE` environment variable:

```bash
ARKITEKT_CONFIG_FILE=/etc/kuvert/config.yaml python manage.py runserver
```

The file is a nested mapping, one top-level key per configuration *block*:

```yaml
django:
  secret_key: "change-me"
  debug: false
postgres:
  db_name: kuvert
  username: kuvert
  password: "change-me"
  host: db
  port: 5432
redis:
  host: redis
  port: 6379
```

### Environment variables (the `__` rule)

Every value is also settable from the environment. The nesting is expressed with a
**double-underscore** (`__`) delimiter, and names are case-insensitive:

| YAML path | Environment variable |
|---|---|
| `postgres.password` | `POSTGRES__PASSWORD` |
| `postgres.port` | `POSTGRES__PORT` |
| `django.debug` | `DJANGO__DEBUG` |
| `redis.host` | `REDIS__HOST` |

Lists and nested objects (e.g. `authentikate.issuers`) are awkward
to express as environment variables — prefer the YAML file for those and use env vars
for the flat scalars (hosts, ports, passwords, toggles).

### Secrets fail fast

Fields marked **secret / required** below have **no default**. If they are missing from
both the YAML file and the environment, the service refuses to start and raises a
`pydantic.ValidationError` naming the missing field. The same error blocks
`manage.py` entirely, so a broken config cannot be deployed silently.

### Validating a configuration

Run the bundled command to load the config exactly as the app would, validate it, and
print the fully-resolved result as a tree with **secrets redacted**:

```bash
python manage.py validate_settings
```

- Valid config → prints a green `Configuration valid` tree and exits `0`.
- Invalid config → prints each offending field and its error, and exits `1`.

It honors `ARKITEKT_CONFIG_FILE`, so you can validate an alternate file the same way.
(Note: because Django loads settings on startup, a fundamentally invalid config also
surfaces the same validation errors when running *any* `manage.py` command.)

---

---

## Configuration reference

Secret fields are flagged with 🔒. "Required" means there is no default.
### `django` (required) — Core Django framework settings.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `secret_key` 🔒 | `DJANGO__SECRET_KEY` | str | *required* | Django SECRET_KEY for cryptographic signing. Secret — must be set. |
| `debug` | `DJANGO__DEBUG` | bool | `False` | Enable Django debug mode (never in production). |
| `log_level` | `DJANGO__LOG_LEVEL` | str | `"INFO"` | Root logger level (e.g. DEBUG, INFO, WARNING). The LOG_LEVEL env var overrides it. |
| `enable_rich_logging` | `DJANGO__ENABLE_RICH_LOGGING` | bool | `False` | Render console logs with rich (colours, boxed tracebacks). A dev convenience; off by default, as plain one-line records suit container logs. |
| `hosts` | `DJANGO__HOSTS` | List[str] | `['*']` | ALLOWED_HOSTS entries. |
| `use_x_forwarded_host` | `DJANGO__USE_X_FORWARDED_HOST` | bool | `True` | Trust the X-Forwarded-Host header behind a reverse proxy. |
| `csrf_trusted_origins` | `DJANGO__CSRF_TRUSTED_ORIGINS` | List[str] | `['http://localhost', 'https://localhost']` | CSRF_TRUSTED_ORIGINS for unsafe (POST) requests. |
| `force_script_name` | `DJANGO__FORCE_SCRIPT_NAME` | str | `""` | URL path prefix (FORCE_SCRIPT_NAME) this service is served under. |

### `django.admin` — Django superuser created on first boot.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `username` | `DJANGO__ADMIN__USERNAME` | str | *required* | Superuser login name. |
| `password` 🔒 | `DJANGO__ADMIN__PASSWORD` | str | *required* | Superuser password. Secret — must be set. |
| `email` | `DJANGO__ADMIN__EMAIL` | Optional[str] | `None` | Superuser email address. |

### `postgres` (required) — PostgreSQL database connection (Django ``DATABASES['default']``).

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `engine` | `POSTGRES__ENGINE` | str | `"django.db.backends.postgresql"` | Django database backend (PostgreSQL). |
| `db_name` | `POSTGRES__DB_NAME` | str | *required* | Database name. |
| `username` | `POSTGRES__USERNAME` | str | *required* | Database user. |
| `password` 🔒 | `POSTGRES__PASSWORD` | str | *required* | Database password. Secret — must be set. |
| `host` | `POSTGRES__HOST` | str | *required* | Database host. |
| `port` | `POSTGRES__PORT` | int | `5432` | Database port. |

### `redis` (required) — Redis connection (channel layer / cache).

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `host` | `REDIS__HOST` | str | *required* | Redis host. |
| `port` | `REDIS__PORT` | int | `6379` | Redis port. |

### `authentikate` — token verification

The authentikate library's own schema: `issuers` (the lok JWKS/RSA keys tokens are checked against), `audience`, `static_tokens` (tests only). See the authentikate docs; the shipped `config.yaml` shows a working block.

### `secrets` (required) — Encryption at rest for mailbox credentials (passwords, OAuth refresh and access tokens).

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `key_path` 🔒 | `SECRETS__KEY_PATH` | str | *required* | Path to a file holding one or more Fernet keys (``Fernet.generate_key()``), one per line. The first encrypts; every one decrypts, so a new key goes on top and an old one is dropped once `rotate_secrets` ran. Secret — mount it, never commit it. Losing it means every mailbox must be linked again. |

### `sync` (optional; defaults apply) — How a mailbox sync runs — requested by a client, or scheduled by the hub's rekuest (``rekuest_service``).

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `lease_seconds` | `SYNC__LEASE_SECONDS` | int | `600` | How long a sync may hold a mailbox before another request may take it over (a crashed sync frees it after this). |
| `batch_size` | `SYNC__BATCH_SIZE` | int | `200` | Messages fetched per folder and run, new mail first. A large mailbox is backfilled over several runs. |
| `backfill_days` | `SYNC__BACKFILL_DAYS` | Optional[int] | `365` | How far back the first sync reaches (by message date). Null backfills everything. |
| `flag_window` | `SYNC__FLAG_WINDOW` | int | `500` | Without CONDSTORE, a sync re-reads the flags of this many newest messages per folder. |
| `max_message_bytes` | `SYNC__MAX_MESSAGE_BYTES` | ByteSize | `52428800` | A message larger than this is stored with its headers only (`truncated`). Accepts `25MiB`-style strings. |
| `folders_excluded` | `SYNC__FOLDERS_EXCLUDED` | List[str] | `['JUNK', 'ALL', 'FLAGGED']` | Folder roles (INBOX, SENT, DRAFTS, TRASH, ARCHIVE, JUNK, ALL, FLAGGED, OTHER) a newly discovered folder is not synced for (a user can still turn one on with `updateMailFolder`). ALL (Gmail's All Mail) and FLAGGED (Starred, Important) are virtual views that repeat messages of other folders, so syncing them duplicates mail. |
| `max_messages_per_run` | `SYNC__MAX_MESSAGES_PER_RUN` | int | `1000` | Messages one sync run fetches over all folders together; the rest follows on the next run. Keeps a first sync of a mailbox with many folders inside one request and one lease. |
| `min_interval_seconds` | `SYNC__MIN_INTERVAL_SECONDS` | int | `0` | A sync of the same mailbox within this many seconds of the last one answers RATE_LIMITED. 0 disables it. |
| `scheduled_every_seconds` | `SYNC__SCHEDULED_EVERY_SECONDS` | Optional[int] | `300` | The default schedule rekuest gives the `sync_all_mailboxes` action. Null declares no default — the action then only runs when scheduled or triggered in rekuest. |
| `connect_timeout_seconds` | `SYNC__CONNECT_TIMEOUT_SECONDS` | float | `30` | Socket timeout for IMAP, POP3 and SMTP connections. |

### `mail` (optional; defaults apply) — What the service may connect to, and how it renders and sends mail.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `allowed_private_hosts` | `MAIL__ALLOWED_PRIVATE_HOSTS` | List[str] | `[]` | Mail hosts that may resolve to private, loopback or link-local addresses. Everything else must be public: a user-supplied host is never allowed to reach the deployment's own network (the database, redis, S3). |
| `allow_insecure` | `MAIL__ALLOW_INSECURE` | bool | `False` | Allow connections without TLS (security NONE). Off: a mailbox must use implicit TLS or STARTTLS. |
| `tls_verify` | `MAIL__TLS_VERIFY` | bool | `True` | Verify server certificates. Only switch off for a test server with a self-signed certificate. |
| `block_remote_images` | `MAIL__BLOCK_REMOTE_IMAGES` | bool | `True` | Strip remote images from sanitized HTML unless a client asks with `html(allowRemote: true)` (remote images tell the sender when and where mail was read). |
| `max_send_bytes` | `MAIL__MAX_SEND_BYTES` | ByteSize | `26214400` | Largest message `sendMessage` builds, attachments included. |
| `user_agent` | `MAIL__USER_AGENT` | str | `"kuvert"` | The X-Mailer header of sent mail, and the IMAP ID the service announces. |

### `oauth` (optional; defaults apply) — OAuth clients per provider. A provider without one answers NOT_CONFIGURED; mailboxes with an app password still work.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `link_expires_seconds` | `OAUTH__LINK_EXPIRES_SECONDS` | int | `900` | How long a started OAuth link may be completed. |

### `oauth.google` — One OAuth 2.0 client (authorization code flow with PKCE) for XOAUTH2 mailbox access.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `client_id` | `OAUTH__GOOGLE__CLIENT_ID` | str | *required* | The OAuth client id. |
| `client_secret` 🔒 | `OAUTH__GOOGLE__CLIENT_SECRET` | Optional[str] | `None` | The OAuth client secret (a public client with PKCE has none). Secret. |
| `redirect_urls` | `OAUTH__GOOGLE__REDIRECT_URLS` | List[str] | *required* | Redirect URLs registered for the client. A client may pick one per link; the first is the default. Anything else is refused. |
| `authorize_url` | `OAUTH__GOOGLE__AUTHORIZE_URL` | Optional[str] | `None` | Override the provider's authorization endpoint (tests, sovereign clouds). |
| `token_url` | `OAUTH__GOOGLE__TOKEN_URL` | Optional[str] | `None` | Override the provider's token endpoint. |
| `userinfo_url` | `OAUTH__GOOGLE__USERINFO_URL` | Optional[str] | `None` | Override the endpoint the mailbox address is read from. |
| `scopes` | `OAUTH__GOOGLE__SCOPES` | Optional[List[str]] | `None` | Override the scopes asked for. The defaults give IMAP, POP3 and SMTP access plus a refresh token. |
| `imap_host` | `OAUTH__GOOGLE__IMAP_HOST` | Optional[str] | `None` | Override the IMAP host of linked mailboxes. |
| `imap_port` | `OAUTH__GOOGLE__IMAP_PORT` | Optional[int] | `None` | Override the IMAP port. |
| `smtp_host` | `OAUTH__GOOGLE__SMTP_HOST` | Optional[str] | `None` | Override the SMTP host. |
| `smtp_port` | `OAUTH__GOOGLE__SMTP_PORT` | Optional[int] | `None` | Override the SMTP port. |
| `timeout_seconds` | `OAUTH__GOOGLE__TIMEOUT_SECONDS` | float | `20` | Timeout for one token request. |

### `oauth.microsoft` — One OAuth 2.0 client (authorization code flow with PKCE) for XOAUTH2 mailbox access.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `client_id` | `OAUTH__MICROSOFT__CLIENT_ID` | str | *required* | The OAuth client id. |
| `client_secret` 🔒 | `OAUTH__MICROSOFT__CLIENT_SECRET` | Optional[str] | `None` | The OAuth client secret (a public client with PKCE has none). Secret. |
| `redirect_urls` | `OAUTH__MICROSOFT__REDIRECT_URLS` | List[str] | *required* | Redirect URLs registered for the client. A client may pick one per link; the first is the default. Anything else is refused. |
| `authorize_url` | `OAUTH__MICROSOFT__AUTHORIZE_URL` | Optional[str] | `None` | Override the provider's authorization endpoint (tests, sovereign clouds). |
| `token_url` | `OAUTH__MICROSOFT__TOKEN_URL` | Optional[str] | `None` | Override the provider's token endpoint. |
| `userinfo_url` | `OAUTH__MICROSOFT__USERINFO_URL` | Optional[str] | `None` | Override the endpoint the mailbox address is read from. |
| `scopes` | `OAUTH__MICROSOFT__SCOPES` | Optional[List[str]] | `None` | Override the scopes asked for. The defaults give IMAP, POP3 and SMTP access plus a refresh token. |
| `imap_host` | `OAUTH__MICROSOFT__IMAP_HOST` | Optional[str] | `None` | Override the IMAP host of linked mailboxes. |
| `imap_port` | `OAUTH__MICROSOFT__IMAP_PORT` | Optional[int] | `None` | Override the IMAP port. |
| `smtp_host` | `OAUTH__MICROSOFT__SMTP_HOST` | Optional[str] | `None` | Override the SMTP host. |
| `smtp_port` | `OAUTH__MICROSOFT__SMTP_PORT` | Optional[int] | `None` | Override the SMTP port. |
| `timeout_seconds` | `OAUTH__MICROSOFT__TIMEOUT_SECONDS` | float | `20` | Timeout for one token request. |

### `embeddings` (optional; defaults apply) — Semantic search over mail: a model2vec static model embeds text into pgvector columns.

Same block as rekuest/mikro/kabinet (the vendored ``embeddings`` package). Every value has a default, so the block may be omitted. The vector width is fixed by the model *and* by the database column; see CONFIG.md before changing ``model``.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `enabled` | `EMBEDDINGS__ENABLED` | bool | `True` | Embed messages and give `search` a semantic leg. Off: `search` is substring-only, `similarTo` finds nothing, and the embedding columns stay NULL. |
| `model` | `EMBEDDINGS__MODEL` | str | `"minishlab/potion-base-8M"` | model2vec model id. Recorded on every row; rows embedded by another model are re-embedded by the `reembed_stale` action and skipped by vector search until then. |
| `model_path` | `EMBEDDINGS__MODEL_PATH` | Optional[str] | `None` | Directory holding the weights of `model` (save_pretrained layout). The Docker image bakes them under /opt/models and sets EMBEDDINGS__MODEL_PATH; unset, model2vec downloads from Hugging Face on first use. |
| `dimensions` | `EMBEDDINGS__DIMENSIONS` | int | `256` | Vector width of `model`. Also the width of the database column, so changing it is a migration. Checked against both at startup. |
| `distance_threshold` | `EMBEDDINGS__DISTANCE_THRESHOLD` | float | `0.55` | Cosine distance (0 identical, 1 unrelated) above which a row no longer counts as a semantic `search` hit. |
| `sweep_interval` | `EMBEDDINGS__SWEEP_INTERVAL` | int | `300` | The default schedule (seconds) rekuest gives the `reembed_stale` action, which re-embeds rows whose `embedding_model` is not `model`. |
| `sweep_batch_size` | `EMBEDDINGS__SWEEP_BATCH_SIZE` | int | `200` | Rows re-embedded per batch. |

### `datalayer` (optional block — absent turns the feature off) — S3 storage (the vendored ``datalayer`` app, as in mikro/elektro): raw messages, attachments, outgoing attachments.

A sync writes each message's raw ``.eml`` and its attachments with the service's own credentials. A client downloads them with a scoped read grant, and uploads the attachments of a message it sends with a scoped upload grant (STS ``AssumeRole`` with a one-key session policy).

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `access_key` 🔒 | `DATALAYER__ACCESS_KEY` | str | *required* | S3 access key. Secret — must be set. |
| `secret_key` 🔒 | `DATALAYER__SECRET_KEY` | str | *required* | S3 secret key. Secret — must be set. |
| `host` | `DATALAYER__HOST` | Optional[str] | `None` | S3 endpoint host. |
| `port` | `DATALAYER__PORT` | Optional[int] | `None` | S3 endpoint port. |
| `protocol` | `DATALAYER__PROTOCOL` | str | `"http"` | S3 endpoint protocol (http or https). |
| `region` | `DATALAYER__REGION` | str | `"us-east-1"` | S3 region name. |
| `role_arn` | `DATALAYER__ROLE_ARN` | Optional[str] | `None` | The role upload grants assume. RustFS/MinIO ignore its value and scope the session by the inline policy alone, but STS needs one. |
| `session_duration_seconds` | `DATALAYER__SESSION_DURATION_SECONDS` | int | `3600` | How long an upload or read grant lasts (clamped to 900–43200). |
| `upload_roles` | `DATALAYER__UPLOAD_ROLES` | List[str] | `['admin', 'editor', 'bot']` | Organization roles allowed to request upload grants. Holding any one of them is enough. |

### `datalayer.bigfile` — A single S3 bucket binding within the datalayer.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `bucket` | `DATALAYER__BIGFILE__BUCKET` | str | *required* | S3 bucket name. |
| `default_max_bytes` | `DATALAYER__BIGFILE__DEFAULT_MAX_BYTES` | Optional[ByteSize] | `None` | Per-upload byte budget advertised on this bucket's grants when no quota sets `max_upload_bytes`. Accepts `500GiB`-style strings. Unset: 100 MiB. |

### `datalayer.quotas.default` — Quota for one organization, plus per-user overrides inside it.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `max_upload_bytes` | `DATALAYER__QUOTAS__DEFAULT__MAX_UPLOAD_BYTES` | Optional[ByteSize] | `None` | Largest single store (upload) a user may write. Advertised on the grant as `maxBytes`; a declared `fileSize` above it is refused. |
| `max_user_bytes` | `DATALAYER__QUOTAS__DEFAULT__MAX_USER_BYTES` | Optional[ByteSize] | `None` | Total bytes one user may hold in one organization. A new upload grant is refused once it would pass this. |
| `max_org_bytes` | `DATALAYER__QUOTAS__DEFAULT__MAX_ORG_BYTES` | Optional[ByteSize] | `None` | Total bytes the whole organization may hold. |

### `datalayer.quotas.default.users` — Byte limits at one level of the quota tree. Unset inherits from the level above; null at every level is unlimited.

Byte values accept ints or strings such as ``500GiB`` / ``2TB``.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `max_upload_bytes` | `DATALAYER__QUOTAS__DEFAULT__USERS__MAX_UPLOAD_BYTES` | Optional[ByteSize] | `None` | Largest single store (upload) a user may write. Advertised on the grant as `maxBytes`; a declared `fileSize` above it is refused. |
| `max_user_bytes` | `DATALAYER__QUOTAS__DEFAULT__USERS__MAX_USER_BYTES` | Optional[ByteSize] | `None` | Total bytes one user may hold in one organization. A new upload grant is refused once it would pass this. |

### `datalayer.quotas.organizations` — Quota for one organization, plus per-user overrides inside it.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `max_upload_bytes` | `DATALAYER__QUOTAS__ORGANIZATIONS__MAX_UPLOAD_BYTES` | Optional[ByteSize] | `None` | Largest single store (upload) a user may write. Advertised on the grant as `maxBytes`; a declared `fileSize` above it is refused. |
| `max_user_bytes` | `DATALAYER__QUOTAS__ORGANIZATIONS__MAX_USER_BYTES` | Optional[ByteSize] | `None` | Total bytes one user may hold in one organization. A new upload grant is refused once it would pass this. |
| `max_org_bytes` | `DATALAYER__QUOTAS__ORGANIZATIONS__MAX_ORG_BYTES` | Optional[ByteSize] | `None` | Total bytes the whole organization may hold. |

### `datalayer.quotas.organizations.users` — Byte limits at one level of the quota tree. Unset inherits from the level above; null at every level is unlimited.

Byte values accept ints or strings such as ``500GiB`` / ``2TB``.

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `max_upload_bytes` | `DATALAYER__QUOTAS__ORGANIZATIONS__USERS__MAX_UPLOAD_BYTES` | Optional[ByteSize] | `None` | Largest single store (upload) a user may write. Advertised on the grant as `maxBytes`; a declared `fileSize` above it is refused. |
| `max_user_bytes` | `DATALAYER__QUOTAS__ORGANIZATIONS__USERS__MAX_USER_BYTES` | Optional[ByteSize] | `None` | Total bytes one user may hold in one organization. A new upload grant is refused once it would pass this. |

### `rekuest_hook` (optional block — absent turns the feature off) — This service as a HookAgent of the hub's rekuest (the vendored ``rekuest_service`` package).

| Key | Env var | Type | Default | Description |
|---|---|---|---|---|
| `secret` 🔒 | `REKUEST_HOOK__SECRET` | str | *required* | The HMAC secret shared with rekuest (rekuest's `service_agents[].secret` for this service). Secret — must be set. |
| `rekuest_url` | `REKUEST_HOOK__REKUEST_URL` | str | `"http://rekuest:80/rekuest"` | rekuest's base URL on the internal network; runs are reported to its `agi/http/<agent>` intake. |
| `service` | `REKUEST_HOOK__SERVICE` | str | `"kuvert"` | The name rekuest knows this service by (its `rekuest.service_agents[].service`); signals are sent as it. |
| `max_skew` | `REKUEST_HOOK__MAX_SKEW` | int | `300` | How old (seconds) a signed request from rekuest may be. |

---

## Minimal example

```yaml
django:
  secret_key: "change-me"
postgres:
  db_name: kuvert
  username: kuvert
  password: "change-me"
  host: db
redis:
  host: redis
authentikate:
  audience: "*"
  issuers:
  - kind: jwks_uri
    issuer: https://example.org
    jwks_uri: https://example.org/lok/o/jwks/
secrets:
  key_path: /secrets/kuvert.fernet
```

Generate the key file once and mount it read-only:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > kuvert.fernet
```

To rotate it, put a new key on the **first** line (keep the old one below), restart, run
`python manage.py rotate_secrets`, then remove the old line.
