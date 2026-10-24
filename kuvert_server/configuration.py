"""Typed, fully-documented configuration schema for the **kuvert** service.

Owned by this service. Values resolve (highest precedence first) from init
kwargs, environment variables (nested via ``__`` — e.g. ``POSTGRES__PASSWORD``),
then the YAML file (``config.yaml`` where the service runs by default; override with
``ARKITEKT_CONFIG_FILE``). Secret fields have **no default**: loading fails fast
with a ``ValidationError`` if they are not supplied via config or environment.
"""

from typing import List, Optional

from pydantic import BaseModel, ByteSize, ConfigDict, Field

from arkitekt_datalayer.configuration import DatalayerBucket, DatalayerSettings

from arkitekt_service.server.settings import DjangoSettings, InstanceSettings, PostgresSettings, RedisSettings, ServiceSettings
from authentikate.base_models import AuthentikateSettings

class KuvertDatalayerSettings(DatalayerSettings):
    """S3 storage for raw messages and attachments."""

    session_name: str = Field(default="kuvert", description="Prefix of every STS session kuvert assumes, so an object store's audit log names it.")
    bigfile: DatalayerBucket = Field(description="Bucket for raw messages and attachments.")


class EmbeddingsSettings(BaseModel):
    """Semantic search over mail: a model2vec static model embeds text into pgvector columns.

    Same block as rekuest/mikro/kabinet (the vendored ``embeddings`` package). Every value has a
    default, so the block may be omitted. The vector width is fixed by the model *and* by the
    database column; see CONFIG.md before changing ``model``.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    enabled: bool = Field(default=True, description="Embed messages and give `search` a semantic leg. Off: `search` is substring-only, `similarTo` finds nothing, and the embedding columns stay NULL.")
    model: str = Field(default="minishlab/potion-base-8M", description="model2vec model id. Recorded on every row; rows embedded by another model are re-embedded by the `reembed_stale` action and skipped by vector search until then.")
    model_path: Optional[str] = Field(default=None, description="Directory holding the weights of `model` (save_pretrained layout). The Docker image bakes them under /opt/models and sets EMBEDDINGS__MODEL_PATH; unset, model2vec downloads from Hugging Face on first use.")
    dimensions: int = Field(default=256, description="Vector width of `model`. Also the width of the database column, so changing it is a migration. Checked against both at startup.")
    distance_threshold: float = Field(default=0.55, description="Cosine distance (0 identical, 1 unrelated) above which a row no longer counts as a semantic `search` hit.")
    sweep_interval: int = Field(default=300, description="No longer used: `reembed_stale` (which re-embeds rows whose `embedding_model` is not `model`) is only offered as an action, and scheduling it is the organization's own automation. Kept so existing configs load.")
    sweep_batch_size: int = Field(default=200, description="Rows re-embedded per batch.")


class RekuestHookSettings(BaseModel):
    """How this process reaches the hub's rekuest: as a service (``rekuest_service``) and as a hook agent (``rekuest_hook``)."""

    rekuest_url: str = Field(default="http://rekuest:80/rekuest", description="rekuest's base URL on the internal network; runs are reported to its `agi/http/<agent>` intake.")
    service: str = Field(default="kuvert", description="The name rekuest knows this process by: its `rekuest.services[].name` (signals are sent as it) and its `rekuest.hook_agents[].name`.")
    max_skew: int = Field(default=30, description="Clock skew (seconds) tolerated on a signed request; tokens themselves live 60 s.")


class SecretsSettings(BaseModel):
    """Encryption at rest for mailbox credentials (passwords, OAuth refresh and access tokens)."""

    key_path: str = Field(description="Path to a file holding one or more Fernet keys (``Fernet.generate_key()``), one per line. The first encrypts; every one decrypts, so a new key goes on top and an old one is dropped once `rotate_secrets` ran. Secret — mount it, never commit it. Losing it means every mailbox must be linked again.")


class SyncSettings(BaseModel):
    """How a mailbox sync runs — requested by a client, or scheduled by the hub's rekuest (``rekuest_service``)."""

    lease_seconds: int = Field(default=600, description="How long a sync may hold a mailbox before another request may take it over (a crashed sync frees it after this).")
    batch_size: int = Field(default=200, description="Messages fetched per folder and run, new mail first. A large mailbox is backfilled over several runs.")
    backfill_days: Optional[int] = Field(default=365, description="How far back the first sync reaches (by message date). Null backfills everything.")
    flag_window: int = Field(default=500, description="Without CONDSTORE, a sync re-reads the flags of this many newest messages per folder.")
    max_message_bytes: ByteSize = Field(default=ByteSize(50 * 1024 * 1024), description="A message larger than this is stored with its headers only (`truncated`). Accepts `25MiB`-style strings.")
    folders_excluded: List[str] = Field(default_factory=lambda: ["JUNK", "ALL", "FLAGGED"], description="Folder roles (INBOX, SENT, DRAFTS, TRASH, ARCHIVE, JUNK, ALL, FLAGGED, OTHER) a newly discovered folder is not synced for (a user can still turn one on with `updateMailFolder`). ALL (Gmail's All Mail) and FLAGGED (Starred, Important) are virtual views that repeat messages of other folders, so syncing them duplicates mail.")
    max_messages_per_run: int = Field(default=1000, description="Messages one sync run fetches over all folders together; the rest follows on the next run. Keeps a first sync of a mailbox with many folders inside one request and one lease.")
    min_interval_seconds: int = Field(default=0, description="A sync of the same mailbox within this many seconds of the last one answers RATE_LIMITED. 0 disables it.")
    scheduled_every_seconds: Optional[int] = Field(default=300, description="No longer used: `sync_all_mailboxes` is only offered as an action, and scheduling it is the organization's own automation. Kept so existing configs load.")
    connect_timeout_seconds: float = Field(default=30, description="Socket timeout for IMAP, POP3 and SMTP connections.")


class WritebackSettings(BaseModel):
    """How changes made here reach the server (``mail.push``): every change is local first and queued."""

    push_inline: bool = Field(default=True, description="Try to push a change right after the request that made it (when it is due and no sync holds the mailbox). Off leaves everything to `flush_mail_changes` and syncs.")
    flush_every_seconds: Optional[int] = Field(default=30, description="No longer used: `flush_mail_changes` is only offered as an action, and scheduling it is the organization's own automation. Kept so existing configs load.")
    undo_seconds_flags: int = Field(default=0, description="How long a flag change waits before it may be pushed (and can be undone).")
    undo_seconds_moves: int = Field(default=10, description="How long a move waits before it may be pushed (and can be undone).")
    undo_seconds_deletes: int = Field(default=30, description="How long a delete waits before it may be pushed (and can be undone).")
    max_attempts: int = Field(default=8, description="A change the server keeps refusing is marked FAILED after this many attempts; the local state stays.")
    backoff_base_seconds: int = Field(default=30, description="The wait after the first failed attempt; it doubles with every further one.")
    backoff_max_seconds: int = Field(default=3600, description="The longest wait between two attempts.")


class MailSettings(BaseModel):
    """What the service may connect to, and how it renders and sends mail."""

    allowed_private_hosts: List[str] = Field(default_factory=list, description="Mail hosts that may resolve to private, loopback or link-local addresses. Everything else must be public: a user-supplied host is never allowed to reach the deployment's own network (the database, redis, S3).")
    allow_insecure: bool = Field(default=False, description="Allow connections without TLS (security NONE). Off: a mailbox must use implicit TLS or STARTTLS.")
    tls_verify: bool = Field(default=True, description="Verify server certificates. Only switch off for a test server with a self-signed certificate.")
    block_remote_images: bool = Field(default=True, description="Strip remote images from sanitized HTML unless a client asks with `html(allowRemote: true)` (remote images tell the sender when and where mail was read).")
    max_send_bytes: ByteSize = Field(default=ByteSize(25 * 1024 * 1024), description="Largest message `sendMessage` builds, attachments included.")
    user_agent: str = Field(default="kuvert", description="The X-Mailer header of sent mail, and the IMAP ID the service announces.")


class OAuthEndpointSettings(BaseModel):
    """Where the service talks to for one kind of OAuth provider. The clients themselves (id, secret, redirect URLs) are the organizations' providers, set up by their admins; only these addresses stay with the deployment, since the service itself posts to them."""

    authorize_url: Optional[str] = Field(default=None, description="Override the provider's authorization endpoint (tests, sovereign clouds). Microsoft's default carries the provider's tenant.")
    token_url: Optional[str] = Field(default=None, description="Override the provider's token endpoint.")
    userinfo_url: Optional[str] = Field(default=None, description="Override the endpoint the mailbox address is read from.")
    scopes: Optional[List[str]] = Field(default=None, description="Override the scopes asked for. The defaults give IMAP, POP3 and SMTP access plus a refresh token.")
    imap_host: Optional[str] = Field(default=None, description="Override the IMAP host of linked mailboxes.")
    imap_port: Optional[int] = Field(default=None, description="Override the IMAP port.")
    smtp_host: Optional[str] = Field(default=None, description="Override the SMTP host.")
    smtp_port: Optional[int] = Field(default=None, description="Override the SMTP port.")
    timeout_seconds: float = Field(default=20, description="Timeout for one token request.")


class OAuthSettings(BaseModel):
    """The OAuth endpoints per kind of provider. Every value has the provider's public default, so the block is only needed to override one."""

    google: Optional[OAuthEndpointSettings] = Field(default=None, description="Gmail's endpoints.")
    microsoft: Optional[OAuthEndpointSettings] = Field(default=None, description="Microsoft's endpoints.")
    link_expires_seconds: int = Field(default=900, description="How long a started OAuth link may be completed.")


class Settings(ServiceSettings):
    """Top-level, validated configuration for the kuvert service."""

    django: DjangoSettings = Field(description="Core Django settings.")
    postgres: PostgresSettings = Field(description="PostgreSQL connection.")
    redis: RedisSettings = Field(description="Redis connection.")
    authentikate: AuthentikateSettings = Field(description="Token-verification config (authentikate).")
    secrets: SecretsSettings = Field(description="Encryption at rest for mailbox credentials.")
    sync: SyncSettings = Field(default_factory=SyncSettings, description="How syncs run.")
    writeback: WritebackSettings = Field(default_factory=WritebackSettings, description="How local changes (flags, categories, moves, deletes) reach the server.")
    mail: MailSettings = Field(default_factory=MailSettings, description="Connection policy, rendering and sending.")
    oauth: OAuthSettings = Field(default_factory=OAuthSettings, description="Overrides of the Gmail and Microsoft OAuth endpoints (the clients are the organizations' providers).")
    embeddings: EmbeddingsSettings = Field(default_factory=EmbeddingsSettings, description="Semantic search model and thresholds.")
    datalayer: Optional[KuvertDatalayerSettings] = Field(default=None, description="S3 storage for raw messages and attachments. Without it messages keep their text and attachment metadata only, and nothing can be attached to sent mail.")
    rekuest_hook: Optional[RekuestHookSettings] = Field(default=None, description="Expose `sync_all_mailboxes` to the hub's rekuest, which schedules it. Without it nothing syncs unless a client asks.")
    instance: Optional[InstanceSettings] = Field(default=None, description="This instance's key and the hub trust bundle (signed requests to and from rekuest, no shared secrets).")
