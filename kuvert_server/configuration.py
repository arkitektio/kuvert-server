"""Typed, fully-documented configuration schema for the **kuvert** service.

Owned by this service. Values resolve (highest precedence first) from init
kwargs, environment variables (nested via ``__`` — e.g. ``POSTGRES__PASSWORD``),
then the YAML file (the mount's ``config.yaml`` by default; override with
``ARKITEKT_CONFIG_FILE``). Secret fields have **no default**: loading fails fast
with a ``ValidationError`` if they are not supplied via config or environment.
"""

import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ByteSize, ConfigDict, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from authentikate.base_models import AuthentikateSettings

_DEFAULT_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"
)


class AdminSettings(BaseModel):
    """Django superuser created on first boot."""

    username: str = Field(description="Superuser login name.")
    password: str = Field(description="Superuser password. Secret — must be set.")
    email: Optional[str] = Field(default=None, description="Superuser email address.")


class DjangoSettings(BaseModel):
    """Core Django framework settings."""

    secret_key: str = Field(description="Django SECRET_KEY for cryptographic signing. Secret — must be set.")
    debug: bool = Field(default=False, description="Enable Django debug mode (never in production).")
    log_level: str = Field(default="INFO", description="Root logger level (e.g. DEBUG, INFO, WARNING). The LOG_LEVEL env var overrides it.")
    enable_rich_logging: bool = Field(default=False, description="Render console logs with rich (colours, boxed tracebacks). A dev convenience; off by default, as plain one-line records suit container logs.")
    hosts: List[str] = Field(default_factory=lambda: ["*"], description="ALLOWED_HOSTS entries.")
    use_x_forwarded_host: bool = Field(default=True, description="Trust the X-Forwarded-Host header behind a reverse proxy.")
    admin: Optional[AdminSettings] = Field(default=None, description="Superuser provisioned on first boot.")
    csrf_trusted_origins: List[str] = Field(default_factory=lambda: ["http://localhost", "https://localhost"], description="CSRF_TRUSTED_ORIGINS for unsafe (POST) requests.")
    force_script_name: str = Field(default="", description="URL path prefix (FORCE_SCRIPT_NAME) this service is served under.")


class PostgresSettings(BaseModel):
    """PostgreSQL database connection (Django ``DATABASES['default']``)."""

    model_config = ConfigDict(extra="allow")

    engine: str = Field(default="django.db.backends.postgresql", description="Django database backend (PostgreSQL).")
    db_name: str = Field(description="Database name.")
    username: str = Field(description="Database user.")
    password: str = Field(description="Database password. Secret — must be set.")
    host: str = Field(description="Database host.")
    port: int = Field(default=5432, description="Database port.")


class RedisSettings(BaseModel):
    """Redis connection (channel layer / cache)."""

    model_config = ConfigDict(extra="allow")

    host: str = Field(description="Redis host.")
    port: int = Field(default=6379, description="Redis port.")

class DatalayerBucket(BaseModel):
    """A single S3 bucket binding within the datalayer."""

    model_config = ConfigDict(extra="allow")

    bucket: str = Field(description="S3 bucket name.")
    default_max_bytes: Optional[ByteSize] = Field(default=None, description="Per-upload byte budget advertised on this bucket's grants when no quota sets `max_upload_bytes`. Accepts `500GiB`-style strings. Unset: 100 MiB.")


class QuotaLimits(BaseModel):
    """Byte limits at one level of the quota tree. Unset inherits from the level above; null at every level is unlimited.

    Byte values accept ints or strings such as ``500GiB`` / ``2TB``.
    """

    model_config = ConfigDict(extra="forbid")

    max_upload_bytes: Optional[ByteSize] = Field(default=None, description="Largest single store (upload) a user may write. Advertised on the grant as `maxBytes`; a declared `fileSize` above it is refused.")
    max_user_bytes: Optional[ByteSize] = Field(default=None, description="Total bytes one user may hold in one organization. A new upload grant is refused once it would pass this.")


class OrganizationQuota(QuotaLimits):
    """Quota for one organization, plus per-user overrides inside it."""

    max_org_bytes: Optional[ByteSize] = Field(default=None, description="Total bytes the whole organization may hold.")
    users: Dict[str, QuotaLimits] = Field(default_factory=dict, description="Per-user overrides in this organization, keyed by the user's token `sub`.")


class QuotaSettings(BaseModel):
    """Upload quotas, set by the hub owner. Resolved most specific first: user in org, then org, then `default`."""

    model_config = ConfigDict(extra="forbid")

    default: OrganizationQuota = Field(default_factory=OrganizationQuota, description="Limits for every organization without its own entry (its `users` map is ignored).")
    organizations: Dict[str, OrganizationQuota] = Field(default_factory=dict, description="Per-organization quotas, keyed by the token `org` claim -- since authentikate 4.0 the lok organization *id* as a string (e.g. `3`), not a readable name.")


class DatalayerSettings(BaseModel):
    """S3 storage (the vendored ``datalayer`` app, as in mikro/elektro): raw messages, attachments, outgoing attachments.

    A sync writes each message's raw ``.eml`` and its attachments with the service's own
    credentials. A client downloads them with a scoped read grant, and uploads the attachments of
    a message it sends with a scoped upload grant (STS ``AssumeRole`` with a one-key session policy).
    """

    model_config = ConfigDict(extra="allow")

    access_key: str = Field(description="S3 access key. Secret — must be set.")
    secret_key: str = Field(description="S3 secret key. Secret — must be set.")
    host: Optional[str] = Field(default=None, description="S3 endpoint host.")
    port: Optional[int] = Field(default=None, description="S3 endpoint port.")
    protocol: str = Field(default="http", description="S3 endpoint protocol (http or https).")
    region: str = Field(default="us-east-1", description="S3 region name.")
    role_arn: Optional[str] = Field(default=None, description="The role upload grants assume. RustFS/MinIO ignore its value and scope the session by the inline policy alone, but STS needs one.")
    session_duration_seconds: int = Field(default=3600, description="How long an upload or read grant lasts (clamped to 900–43200).")
    bigfile: DatalayerBucket = Field(description="Bucket for raw messages and attachments.")
    upload_roles: List[str] = Field(default_factory=lambda: ["admin", "editor", "bot"], description="Organization roles allowed to request upload grants. Holding any one of them is enough.")
    quotas: QuotaSettings = Field(default_factory=QuotaSettings, description="Per-organization, per-user and per-upload byte quotas.")


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
    sweep_interval: int = Field(default=300, description="The default schedule (seconds) rekuest gives the `reembed_stale` action, which re-embeds rows whose `embedding_model` is not `model`.")
    sweep_batch_size: int = Field(default=200, description="Rows re-embedded per batch.")


class RekuestHookSettings(BaseModel):
    """This service as a HookAgent of the hub's rekuest (the vendored ``rekuest_service`` package)."""

    rekuest_url: str = Field(default="http://rekuest:80/rekuest", description="rekuest's base URL on the internal network; runs are reported to its `agi/http/<agent>` intake.")
    service: str = Field(default="kuvert", description="The name rekuest knows this service by (its `rekuest.service_agents[].service`); signals are sent as it.")
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
    scheduled_every_seconds: Optional[int] = Field(default=300, description="The default schedule rekuest gives the `sync_all_mailboxes` action. Null declares no default — the action then only runs when scheduled or triggered in rekuest.")
    connect_timeout_seconds: float = Field(default=30, description="Socket timeout for IMAP, POP3 and SMTP connections.")


class MailSettings(BaseModel):
    """What the service may connect to, and how it renders and sends mail."""

    allowed_private_hosts: List[str] = Field(default_factory=list, description="Mail hosts that may resolve to private, loopback or link-local addresses. Everything else must be public: a user-supplied host is never allowed to reach the deployment's own network (the database, redis, S3).")
    allow_insecure: bool = Field(default=False, description="Allow connections without TLS (security NONE). Off: a mailbox must use implicit TLS or STARTTLS.")
    tls_verify: bool = Field(default=True, description="Verify server certificates. Only switch off for a test server with a self-signed certificate.")
    block_remote_images: bool = Field(default=True, description="Strip remote images from sanitized HTML unless a client asks with `html(allowRemote: true)` (remote images tell the sender when and where mail was read).")
    max_send_bytes: ByteSize = Field(default=ByteSize(25 * 1024 * 1024), description="Largest message `sendMessage` builds, attachments included.")
    user_agent: str = Field(default="kuvert", description="The X-Mailer header of sent mail, and the IMAP ID the service announces.")


class OAuthProviderSettings(BaseModel):
    """One OAuth 2.0 client (authorization code flow with PKCE) for XOAUTH2 mailbox access."""

    client_id: str = Field(description="The OAuth client id.")
    client_secret: Optional[str] = Field(default=None, description="The OAuth client secret (a public client with PKCE has none). Secret.")
    redirect_urls: List[str] = Field(description="Redirect URLs registered for the client. A client may pick one per link; the first is the default. Anything else is refused.")
    authorize_url: Optional[str] = Field(default=None, description="Override the provider's authorization endpoint (tests, sovereign clouds).")
    token_url: Optional[str] = Field(default=None, description="Override the provider's token endpoint.")
    userinfo_url: Optional[str] = Field(default=None, description="Override the endpoint the mailbox address is read from.")
    scopes: Optional[List[str]] = Field(default=None, description="Override the scopes asked for. The defaults give IMAP, POP3 and SMTP access plus a refresh token.")
    imap_host: Optional[str] = Field(default=None, description="Override the IMAP host of linked mailboxes.")
    imap_port: Optional[int] = Field(default=None, description="Override the IMAP port.")
    smtp_host: Optional[str] = Field(default=None, description="Override the SMTP host.")
    smtp_port: Optional[int] = Field(default=None, description="Override the SMTP port.")
    timeout_seconds: float = Field(default=20, description="Timeout for one token request.")


class OAuthSettings(BaseModel):
    """OAuth clients per provider. A provider without one answers NOT_CONFIGURED; mailboxes with an app password still work."""

    google: Optional[OAuthProviderSettings] = Field(default=None, description="A Google Cloud OAuth client (Gmail).")
    microsoft: Optional[OAuthProviderSettings] = Field(default=None, description="A Microsoft Entra app registration (Outlook, Microsoft 365).")
    link_expires_seconds: int = Field(default=900, description="How long a started OAuth link may be completed.")


class InstanceTrustSettings(BaseModel):
    """Where the hub's instance public keys come from: the coord's bundle, or inline."""

    jwks_uri: Optional[str] = Field(default=None, description="The coord's hub-keys URL (the fakts `self.hub_keys_url`).")
    jwks: Optional[Dict[str, Any]] = Field(default=None, description="The bundle inline (a JWKS whose keys carry `service`), for a hub not enrolled yet.")


class InstanceSettings(BaseModel):
    """This instance's key — its only secret towards the hub's other services — and whom it trusts."""

    private_key: str = Field(description="Ed25519 private key (PKCS#8 PEM). Signs this service's requests to rekuest. Secret — must be set.")
    trust: InstanceTrustSettings = Field(default_factory=InstanceTrustSettings, description="The hub's trust bundle.")


class Settings(BaseSettings):
    """Top-level, validated configuration for the kuvert service."""

    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    django: DjangoSettings = Field(description="Core Django settings.")
    postgres: PostgresSettings = Field(description="PostgreSQL connection.")
    redis: RedisSettings = Field(description="Redis connection.")
    authentikate: AuthentikateSettings = Field(description="Token-verification config (authentikate).")
    secrets: SecretsSettings = Field(description="Encryption at rest for mailbox credentials.")
    sync: SyncSettings = Field(default_factory=SyncSettings, description="How syncs run.")
    mail: MailSettings = Field(default_factory=MailSettings, description="Connection policy, rendering and sending.")
    oauth: OAuthSettings = Field(default_factory=OAuthSettings, description="OAuth clients for Gmail and Microsoft mailboxes.")
    embeddings: EmbeddingsSettings = Field(default_factory=EmbeddingsSettings, description="Semantic search model and thresholds.")
    datalayer: Optional[DatalayerSettings] = Field(default=None, description="S3 storage for raw messages and attachments. Without it messages keep their text and attachment metadata only, and nothing can be attached to sent mail.")
    rekuest_hook: Optional[RekuestHookSettings] = Field(default=None, description="Expose `sync_all_mailboxes` to the hub's rekuest, which schedules it. Without it nothing syncs unless a client asks.")
    instance: Optional[InstanceSettings] = Field(default=None, description="This instance's key and the hub trust bundle (signed requests to and from rekuest, no shared secrets).")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence: explicit init kwargs > environment variables > YAML file.
        path = os.environ.get("ARKITEKT_CONFIG_FILE", _DEFAULT_CONFIG)
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls, yaml_file=path),
            file_secret_settings,
        )
