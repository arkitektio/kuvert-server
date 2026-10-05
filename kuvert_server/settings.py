"""Django settings for the kuvert service.

Every value comes from the typed configuration (``configuration.py``); application code reads
the plain Django settings below, never ``conf``.
"""

from pathlib import Path
import os
from .configuration import Settings
from .logs import build_logging


# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
conf = Settings()  # Load the configuration from environment variables and config.yaml, #type: ignore

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/4.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = conf.django.secret_key

DEBUG = conf.django.debug

ALLOWED_HOSTS: list[str] = conf.django.hosts
USE_X_FORWARDED_HOST = conf.django.use_x_forwarded_host


# Application definition

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",  # ArrayField (flags, references)
    "channels_redis",
    "guardian",
    "simple_history",
    "authentikate",
    "koherent",
    "kante",
    "channels",
    "django_probes",
    "polymorphic",
    "datalayer",
    "mail",
    "health_check",  # required for health checks
    "health_check.db",  # stock Django health checkers
]


AUTH_USER_MODEL = "authentikate.User"


STRAWBERRY_DJANGO = {
    "FIELD_DESCRIPTION_FROM_HELP_TEXT": True,
    "TYPE_DESCRIPTION_FROM_MODEL_DOCSTRING": True,
    "USE_DEPRECATED_FILTERS": False,
    "DEFAULT_PK_FIELD_NAME": "id",
}


CHANNEL_LAYERS = {
    "default": {
        # Uses the Redis channel layer implementation channels_redis
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [(conf.redis.host, conf.redis.port)], "prefix": "kuvert"},
    },
}

CORS_ALLOW_ALL_ORIGINS = True


CSRF_TRUSTED_ORIGINS = conf.django.csrf_trusted_origins
MY_SCRIPT_NAME = conf.django.force_script_name

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "kuvert_server.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

AUTHENTICATION_BACKENDS = (
    "django.contrib.auth.backends.ModelBackend",  # this is default
    "guardian.backends.ObjectPermissionBackend",
)

WSGI_APPLICATION = "kuvert_server.wsgi.application"
ASGI_APPLICATION = "kuvert_server.asgi.application"


# Database
# https://docs.djangoproject.com/en/4.2/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": conf.postgres.engine,
        "NAME": conf.postgres.db_name,
        "USER": conf.postgres.username,
        "PASSWORD": conf.postgres.password,
        "HOST": conf.postgres.host,
        "PORT": conf.postgres.port,
    },
}


# Password validation
# https://docs.djangoproject.com/en/4.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",  # noqa: E501
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


AUTHENTIKATE = conf.authentikate.model_dump()


CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"redis://{conf.redis.host}:{conf.redis.port}/1",
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
        "KEY_PREFIX": "kuvert_server_cache",
    }
}

CACHE_TTL_DEFAULT = 60 * 15

# Mailbox credentials are Fernet-encrypted with the key(s) in this file (``mail.crypto``).
KUVERT_SECRETS = conf.secrets.model_dump()
KUVERT_SYNC = conf.sync.model_dump()
KUVERT_WRITEBACK = conf.writeback.model_dump()
KUVERT_MAIL = conf.mail.model_dump()
KUVERT_OAUTH = conf.oauth.model_dump()
# The vendored datalayer reads only this; empty without a `datalayer` block (raw messages and
# attachments are then not stored, and uploads answer NOT_CONFIGURED).
DATALAYER = conf.datalayer.model_dump(exclude_none=True) if conf.datalayer else {}

# Semantic search (the vendored ``embeddings`` package, as in rekuest, mikro, kabinet and bank).
# Messages embed subject + sender + the head of their text into a pgvector column, with a
# model2vec static model running in this process.
# DIMENSIONS is also the column width: the ``embeddings`` system checks refuse to start when
# the model, this setting and the column disagree. Stale rows are re-embedded by the
# ``reembed_stale`` rekuest action, never by a command.
EMBEDDINGS = {
    "ENABLED": conf.embeddings.enabled,
    "MODEL": conf.embeddings.model,
    "MODEL_PATH": conf.embeddings.model_path,
    "DIMENSIONS": conf.embeddings.dimensions,
    "DISTANCE_THRESHOLD": conf.embeddings.distance_threshold,
    "SWEEP_INTERVAL": conf.embeddings.sweep_interval,
    "SWEEP_BATCH_SIZE": conf.embeddings.sweep_batch_size,
}
# Two declarations reach the hub's rekuest from this process, each with its own setting: the
# service (what exists here: ``rekuest_service``) and the hook agent (what can be done here:
# ``rekuest_hook``). Both read where rekuest is from the same ``rekuest_hook`` config block.
REKUEST_SERVICE = (
    {"REKUEST_URL": conf.rekuest_hook.rekuest_url, "SERVICE": conf.rekuest_hook.service, "MAX_SKEW": conf.rekuest_hook.max_skew}
    if conf.rekuest_hook
    else None
)
REKUEST_HOOK = (
    {"REKUEST_URL": conf.rekuest_hook.rekuest_url, "AGENT": conf.rekuest_hook.service, "MAX_SKEW": conf.rekuest_hook.max_skew}
    if conf.rekuest_hook
    else None
)
# This instance's key and the hub trust bundle (``arkitekt_service.trust``): requests to and from
# rekuest are signed with instance keys the coord vouches for — no shared secrets.
INSTANCE = (
    {"PRIVATE_KEY": conf.instance.private_key, "TRUST_JWKS_URI": conf.instance.trust.jwks_uri, "TRUST_JWKS": conf.instance.trust.jwks}
    if conf.instance
    else None
)


# Internationalization
# https://docs.djangoproject.com/en/4.2/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/

STATIC_URL = "static/"

# WhiteNoise serves static directly from the staticfiles finders at request time
# (works under both runserver and daphne), so no collectstatic / STATIC_ROOT is needed.
WHITENOISE_USE_FINDERS = True

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Console logging: one plain line per record on root; see logs.py. ``LOG_LEVEL=DEBUG``
# (env) brings back per-event detail; ``django.enable_rich_logging`` renders with rich.
LOGGING = build_logging(
    level=os.environ.get("LOG_LEVEL", conf.django.log_level),
    rich=conf.django.enable_rich_logging,
)
