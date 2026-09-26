import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kuvert_server.settings")
from django.core.asgi import get_asgi_application
# Initialize Django ASGI application early to ensure the AppRegistry
# is populated before importing code that may import ORM models.
django_asgi_app = get_asgi_application()


from .schema import schema  # noqa: E402
from kante.router import router  # noqa: E402

# Request/response only: nothing loops in here. Mailboxes sync when a client asks
# (``syncMailAccount``), or when the hub's rekuest runs the scheduled
# ``sync_all_mailboxes`` (``mail.scheduled``) — also just a request, to ``_rekuest/hook``.
application = router(
    schema=schema,
    django_asgi_app=django_asgi_app,
    schema_path="schema",
)
