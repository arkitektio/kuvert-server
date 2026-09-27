"""Tasks over mail threads, and threads that remember their Message-IDs.

``Thread.message_ids`` is backfilled from the messages each thread holds, so a message moved or
re-read after this release rejoins its thread (and the tasks linking it keep it).
"""

import django.contrib.postgres.fields
import django.contrib.postgres.indexes
import django.db.models.deletion
import simple_history.models
from django.conf import settings
from django.db import migrations, models


def backfill_message_ids(apps, schema_editor):  # noqa: ANN001, ANN201
    Message = apps.get_model("mail", "Message")
    Thread = apps.get_model("mail", "Thread")
    ids: dict[int, set[str]] = {}
    for thread_id, message_id in Message.objects.exclude(thread=None).exclude(message_id=None).values_list("thread_id", "message_id").iterator():
        ids.setdefault(thread_id, set()).add(message_id)
    for thread_id, message_ids in ids.items():
        Thread.objects.filter(id=thread_id).update(message_ids=sorted(message_ids))


class Migration(migrations.Migration):

    dependencies = [
        ('authentikate', '0007_user_profile'),
        ('koherent', '0003_rename_assignation_to_task'),
        ('mail', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='HistoricalTask',
            fields=[
                ('id', models.BigIntegerField(auto_created=True, blank=True, db_index=True, verbose_name='ID')),
                ('title', models.CharField(help_text='What to do.', max_length=500)),
                ('notes', models.TextField(blank=True, default='', help_text='Free notes.')),
                ('status', models.CharField(choices=[('OPEN', 'To do'), ('DONE', 'Done'), ('DISMISSED', 'Dropped without doing it')], default='OPEN', help_text='Where the task is.', max_length=10)),
                ('pinned', models.BooleanField(default=False, help_text='Pinned to the top.')),
                ('due_at', models.DateTimeField(blank=True, help_text='When it is due.', null=True)),
                ('snoozed_until', models.DateTimeField(blank=True, help_text='Hidden from the active view until then.', null=True)),
                ('position', models.FloatField(default=0, help_text='Where the task sorts in its list.')),
                ('external_key', models.CharField(blank=True, help_text="An app's own key for the task: `upsertTask` finds the task by it, so sorting again updates instead of duplicating.", max_length=500, null=True)),
                ('completed_at', models.DateTimeField(blank=True, help_text='When it was marked DONE.', null=True)),
                ('created_at', models.DateTimeField(blank=True, editable=False, help_text='When the task was created.')),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField(db_index=True)),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
            ],
            options={
                'verbose_name': 'historical task',
                'verbose_name_plural': 'historical tasks',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': ('history_date', 'history_id'),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
        migrations.CreateModel(
            name='HistoricalTaskList',
            fields=[
                ('id', models.BigIntegerField(auto_created=True, blank=True, db_index=True, verbose_name='ID')),
                ('name', models.CharField(help_text="The list's name.", max_length=200)),
                ('color', models.CharField(blank=True, default='', help_text='A display color (e.g. #4f86f7).', max_length=20)),
                ('position', models.FloatField(default=0, help_text="Where the list sorts among the owner's lists.")),
                ('created_at', models.DateTimeField(blank=True, editable=False, help_text='When the list was created.')),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField(db_index=True)),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
            ],
            options={
                'verbose_name': 'historical task list',
                'verbose_name_plural': 'historical task lists',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': ('history_date', 'history_id'),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
        migrations.CreateModel(
            name='Task',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(help_text='What to do.', max_length=500)),
                ('notes', models.TextField(blank=True, default='', help_text='Free notes.')),
                ('status', models.CharField(choices=[('OPEN', 'To do'), ('DONE', 'Done'), ('DISMISSED', 'Dropped without doing it')], default='OPEN', help_text='Where the task is.', max_length=10)),
                ('pinned', models.BooleanField(default=False, help_text='Pinned to the top.')),
                ('due_at', models.DateTimeField(blank=True, help_text='When it is due.', null=True)),
                ('snoozed_until', models.DateTimeField(blank=True, help_text='Hidden from the active view until then.', null=True)),
                ('position', models.FloatField(default=0, help_text='Where the task sorts in its list.')),
                ('external_key', models.CharField(blank=True, help_text="An app's own key for the task: `upsertTask` finds the task by it, so sorting again updates instead of duplicating.", max_length=500, null=True)),
                ('completed_at', models.DateTimeField(blank=True, help_text='When it was marked DONE.', null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, help_text='When the task was created.')),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='When the task last changed.')),
            ],
            options={
                'ordering': ['-pinned', 'position', 'id'],
            },
        ),
        migrations.CreateModel(
            name='TaskList',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text="The list's name.", max_length=200)),
                ('color', models.CharField(blank=True, default='', help_text='A display color (e.g. #4f86f7).', max_length=20)),
                ('position', models.FloatField(default=0, help_text="Where the list sorts among the owner's lists.")),
                ('created_at', models.DateTimeField(auto_now_add=True, help_text='When the list was created.')),
            ],
            options={
                'ordering': ['position', 'id'],
            },
        ),
        migrations.CreateModel(
            name='TaskThread',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(choices=[('USER', 'A person, by hand'), ('APP', 'An app that sorts mail')], default='USER', help_text='Who put the thread into the task.', max_length=10)),
                ('confidence', models.FloatField(blank=True, help_text="An app's confidence (0–1) that the thread belongs here.", null=True)),
                ('reason', models.TextField(blank=True, default='', help_text='Why the thread belongs here, in words.')),
                ('position', models.FloatField(default=0, help_text='Where the thread sorts within the task.')),
                ('created_at', models.DateTimeField(auto_now_add=True, help_text='When the thread was put into the task.')),
            ],
            options={
                'ordering': ['position', 'id'],
            },
        ),
        migrations.AddField(
            model_name='thread',
            name='message_ids',
            field=django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=998), blank=True, default=list, help_text='Every Message-ID the conversation has held. A message that comes back (moved, re-read after a UIDVALIDITY change) rejoins the thread by it, so tasks keep their threads.'),
        ),
        migrations.AddIndex(
            model_name='thread',
            index=django.contrib.postgres.indexes.GinIndex(fields=['message_ids'], name='mail_thread_mids'),
        ),
        migrations.AddField(
            model_name='historicaltask',
            name='client',
            field=models.ForeignKey(blank=True, db_constraint=False, help_text='The app that created the task, if one did.', null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='authentikate.client'),
        ),
        migrations.AddField(
            model_name='historicaltask',
            name='history_user',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='historicaltask',
            name='organization',
            field=models.ForeignKey(blank=True, db_constraint=False, help_text='The organization it belongs to.', null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='authentikate.organization'),
        ),
        migrations.AddField(
            model_name='historicaltask',
            name='owner',
            field=models.ForeignKey(blank=True, db_constraint=False, help_text='The member whose task it is.', null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='historicaltask',
            name='task',
            field=models.ForeignKey(blank=True, help_text='The task during which the change occurred, if any', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='koherent.task'),
        ),
        migrations.AddField(
            model_name='historicaltasklist',
            name='client',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='authentikate.client'),
        ),
        migrations.AddField(
            model_name='historicaltasklist',
            name='history_user',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='historicaltasklist',
            name='organization',
            field=models.ForeignKey(blank=True, db_constraint=False, help_text='The organization it belongs to.', null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='authentikate.organization'),
        ),
        migrations.AddField(
            model_name='historicaltasklist',
            name='owner',
            field=models.ForeignKey(blank=True, db_constraint=False, help_text='The member whose list it is.', null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='historicaltasklist',
            name='task',
            field=models.ForeignKey(blank=True, help_text='The task during which the change occurred, if any', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='koherent.task'),
        ),
        migrations.AddField(
            model_name='task',
            name='client',
            field=models.ForeignKey(blank=True, help_text='The app that created the task, if one did.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='authentikate.client'),
        ),
        migrations.AddField(
            model_name='task',
            name='organization',
            field=models.ForeignKey(help_text='The organization it belongs to.', on_delete=django.db.models.deletion.CASCADE, related_name='mail_tasks', to='authentikate.organization'),
        ),
        migrations.AddField(
            model_name='task',
            name='owner',
            field=models.ForeignKey(help_text='The member whose task it is.', on_delete=django.db.models.deletion.CASCADE, related_name='mail_tasks', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='historicaltask',
            name='history_relation',
            field=models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.DO_NOTHING, related_name='provenance_entries', to='mail.task'),
        ),
        migrations.AddField(
            model_name='tasklist',
            name='organization',
            field=models.ForeignKey(help_text='The organization it belongs to.', on_delete=django.db.models.deletion.CASCADE, related_name='mail_task_lists', to='authentikate.organization'),
        ),
        migrations.AddField(
            model_name='tasklist',
            name='owner',
            field=models.ForeignKey(help_text='The member whose list it is.', on_delete=django.db.models.deletion.CASCADE, related_name='mail_task_lists', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='task',
            name='list',
            field=models.ForeignKey(blank=True, help_text='The list it is on, if any.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='tasks', to='mail.tasklist'),
        ),
        migrations.AddField(
            model_name='historicaltasklist',
            name='history_relation',
            field=models.ForeignKey(db_constraint=False, on_delete=django.db.models.deletion.DO_NOTHING, related_name='provenance_entries', to='mail.tasklist'),
        ),
        migrations.AddField(
            model_name='historicaltask',
            name='list',
            field=models.ForeignKey(blank=True, db_constraint=False, help_text='The list it is on, if any.', null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='mail.tasklist'),
        ),
        migrations.AddField(
            model_name='taskthread',
            name='client',
            field=models.ForeignKey(blank=True, help_text='The app the request came from.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='authentikate.client'),
        ),
        migrations.AddField(
            model_name='taskthread',
            name='task',
            field=models.ForeignKey(help_text='The task.', on_delete=django.db.models.deletion.CASCADE, related_name='links', to='mail.task'),
        ),
        migrations.AddField(
            model_name='taskthread',
            name='thread',
            field=models.ForeignKey(help_text='The conversation.', on_delete=django.db.models.deletion.CASCADE, related_name='task_links', to='mail.thread'),
        ),
        migrations.AddField(
            model_name='task',
            name='threads',
            field=models.ManyToManyField(help_text='The conversations the task is about.', related_name='tasks', through='mail.TaskThread', to='mail.thread'),
        ),
        migrations.AddConstraint(
            model_name='taskthread',
            constraint=models.UniqueConstraint(fields=('task', 'thread'), name='mail_taskthread_unique'),
        ),
        migrations.AddIndex(
            model_name='task',
            index=models.Index(fields=['owner', 'status'], name='mail_task_owner_status'),
        ),
        migrations.AddConstraint(
            model_name='task',
            constraint=models.UniqueConstraint(condition=models.Q(('external_key__isnull', False)), fields=('organization', 'owner', 'external_key'), name='mail_task_external_key'),
        ),
        migrations.RunPython(backfill_message_ids, migrations.RunPython.noop),
    ]
