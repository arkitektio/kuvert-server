"""Re-encrypt every stored mailbox credential with the first key of ``secrets.key_path``.

Put a new key on the first line of the key file, restart, run this, then drop the old key:

    python manage.py rotate_secrets
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from mail import crypto, models

FIELDS = {
    models.MailAccount: ("secret", "access_token", "smtp_secret"),
    models.OAuthLink: ("code_verifier",),
}


class Command(BaseCommand):
    help = "Re-encrypt stored mailbox credentials with the newest key."

    def handle(self, *args, **options) -> None:  # noqa: ANN002, ANN003
        rotated = 0
        with transaction.atomic():
            for model, fields in FIELDS.items():
                for row in model.objects.select_for_update().only("id", *fields):
                    for field in fields:
                        value = getattr(row, field)
                        if value:
                            setattr(row, field, crypto.rotate(value))
                    row.save(update_fields=list(fields))
                    rotated += 1
        self.stdout.write(self.style.SUCCESS(f"Re-encrypted {rotated} rows."))
