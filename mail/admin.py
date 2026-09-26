from django.contrib import admin

from mail import models

for model in (
    models.MailAccount,
    models.MailFolder,
    models.Thread,
    models.Message,
    models.Attachment,
    models.OutgoingMessage,
    models.OAuthLink,
):
    admin.site.register(model)
