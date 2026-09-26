from django.apps import AppConfig


class MailConfig(AppConfig):
    """Linked mailboxes, their folders and messages, and the mail sent through them."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "mail"

    def ready(self) -> None:
        import embeddings.checks  # noqa: F401  registers the model/width system checks
        from mail import scheduled  # noqa: F401  registers the rekuest actions
