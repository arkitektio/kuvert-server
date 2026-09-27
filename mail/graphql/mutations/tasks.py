"""Tasks over mail conversations, and the lists they are on.

Everything here is personal: the caller only ever sees and changes their own tasks and lists
(``mail.scoping``), and can only put conversations into a task that they can see. An app that sorts
mail calls ``upsertTask`` with its own ``externalKey``, so sorting again updates instead of
duplicating; its links are recorded as source APP with the app's confidence and reason.
"""

import datetime
from typing import List, Optional

import strawberry
from django.db import transaction
from django.db.models import Max, QuerySet
from django.utils import timezone
from kante.errors import ValidationError
from kante.types import Info

from mail import enums, models, types
from mail.graphql.errors import translated
from mail.graphql.utils import get_many, get_or_404
from mail.scoping import for_org

__all__ = [
    "CreateTaskListInput",
    "UpdateTaskListInput",
    "ThreadLinkInput",
    "CreateTaskInput",
    "UpsertTaskInput",
    "UpdateTaskInput",
    "SetTaskStatusInput",
    "SnoozeTasksInput",
    "LinkThreadsInput",
    "UnlinkThreadsInput",
    "create_task_list",
    "update_task_list",
    "delete_task_list",
    "create_task",
    "upsert_task",
    "update_task",
    "delete_task",
    "set_task_status",
    "snooze_tasks",
    "link_threads",
    "unlink_threads",
]


@strawberry.input(description="A new task list.")
class CreateTaskListInput:
    name: str
    color: str = ""
    position: Optional[float] = strawberry.field(default=None, description="Where it sorts; after the last list by default.")


@strawberry.input(description="Changes to a task list; omitted fields stay as they are.")
class UpdateTaskListInput:
    id: strawberry.ID
    name: Optional[str] = strawberry.UNSET
    color: Optional[str] = strawberry.UNSET
    position: Optional[float] = strawberry.UNSET


@strawberry.input(description="How a conversation is put into a task.")
class ThreadLinkInput:
    source: enums.TaskLinkSource = strawberry.field(default=enums.TaskLinkSource.USER, description="APP when an app sorted it; USER when a person did.")
    confidence: Optional[float] = strawberry.field(default=None, description="An app's confidence, 0–1.")
    reason: str = strawberry.field(default="", description="Why the conversation belongs here.")


@strawberry.input(description="A new task, optionally with its conversations.")
class CreateTaskInput:
    title: str
    notes: str = ""
    list: Optional[strawberry.ID] = None
    due_at: Optional[datetime.datetime] = None
    pinned: bool = False
    position: Optional[float] = strawberry.field(default=None, description="Where it sorts; after the last task by default.")
    external_key: Optional[str] = strawberry.field(default=None, description="An app's own key; must be unique among the caller's tasks.")
    threads: List[strawberry.ID] = strawberry.field(default_factory=lambda: [], description="Conversations to put into the task.")
    link: Optional[ThreadLinkInput] = strawberry.field(default=None, description="How those conversations are linked.")


@strawberry.input(description="Create or update the caller's task with this `externalKey` (an app's idempotent sort call). Omitted fields keep their value on update; `threads` are added (never removed).")
class UpsertTaskInput:
    external_key: str
    title: str
    notes: Optional[str] = strawberry.UNSET
    list: Optional[strawberry.ID] = strawberry.UNSET
    due_at: Optional[datetime.datetime] = strawberry.UNSET
    pinned: Optional[bool] = strawberry.UNSET
    threads: List[strawberry.ID] = strawberry.field(default_factory=lambda: [])
    link: Optional[ThreadLinkInput] = None


@strawberry.input(description="Changes to a task; omitted fields stay as they are.")
class UpdateTaskInput:
    id: strawberry.ID
    title: Optional[str] = strawberry.UNSET
    notes: Optional[str] = strawberry.UNSET
    list: Optional[strawberry.ID] = strawberry.field(default=strawberry.UNSET, description="Another list; null takes it off its list.")
    due_at: Optional[datetime.datetime] = strawberry.UNSET
    pinned: Optional[bool] = strawberry.UNSET
    position: Optional[float] = strawberry.UNSET


@strawberry.input(description="Set the status of tasks (DONE records when; OPEN clears it).")
class SetTaskStatusInput:
    tasks: List[strawberry.ID]
    status: enums.TaskStatus


@strawberry.input(description="Hide tasks from the active view until a time; null wakes them now.")
class SnoozeTasksInput:
    tasks: List[strawberry.ID]
    until: Optional[datetime.datetime]


@strawberry.input(description="Put conversations into a task. A conversation already in it keeps its place; its link details are updated.")
class LinkThreadsInput:
    task: strawberry.ID
    threads: List[strawberry.ID]
    link: Optional[ThreadLinkInput] = None


@strawberry.input(description="Take conversations out of a task.")
class UnlinkThreadsInput:
    task: strawberry.ID
    threads: List[strawberry.ID]


def _next_position(rows: QuerySet) -> float:
    last = rows.aggregate(last=Max("position"))["last"]
    return (last or 0) + 1


def _list(info: Info, value: Optional[strawberry.ID]) -> models.TaskList | None:
    return get_or_404(models.TaskList, info, value) if value is not None else None


def _link(info: Info, task: models.Task, thread_ids: List[strawberry.ID], link: Optional[ThreadLinkInput]) -> List[models.TaskThread]:
    """Link visible conversations to ``task``; NOT_FOUND for any the caller cannot see."""
    link = link or ThreadLinkInput()
    if link.confidence is not None and not 0 <= link.confidence <= 1:
        raise ValidationError("confidence must be between 0 and 1.")
    threads = get_many(models.Thread, info, thread_ids)
    client = getattr(info.context.request, "client", None)
    out = []
    position = _next_position(task.links.all())
    for thread in threads:
        row, created = models.TaskThread.objects.get_or_create(
            task=task,
            thread=thread,
            defaults={"source": link.source.value, "client": client, "confidence": link.confidence, "reason": link.reason, "position": position},
        )
        if created:
            position += 1
        else:
            row.source, row.client, row.confidence, row.reason = link.source.value, client, link.confidence, link.reason
            row.save(update_fields=["source", "client", "confidence", "reason"])
        out.append(row)
    return out


def _check_title(title: str) -> str:
    title = title.strip()
    if not title:
        raise ValidationError("A task needs a title.")
    return title[:500]


@translated
def create_task_list(info: Info, input: CreateTaskListInput) -> types.TaskList:
    """A new list of the caller's."""
    request = info.context.request
    if not input.name.strip():
        raise ValidationError("A list needs a name.")
    position = input.position if input.position is not None else _next_position(for_org(models.TaskList, info))
    return models.TaskList.objects.create(organization=request.organization, owner=request.user, name=input.name.strip()[:200], color=input.color[:20], position=position)  # type: ignore[return-value]


@translated
def update_task_list(info: Info, input: UpdateTaskListInput) -> types.TaskList:
    """Rename, recolor or move a list."""
    row = get_or_404(models.TaskList, info, input.id)
    if input.name is not strawberry.UNSET and input.name:
        row.name = input.name.strip()[:200]
    if input.color is not strawberry.UNSET:
        row.color = (input.color or "")[:20]
    if input.position is not strawberry.UNSET and input.position is not None:
        row.position = input.position
    row.save()
    return row  # type: ignore[return-value]


@translated
def delete_task_list(info: Info, id: strawberry.ID) -> strawberry.ID:
    """Delete a list; its tasks stay, on no list."""
    get_or_404(models.TaskList, info, id).delete()
    return id


@translated
def create_task(info: Info, input: CreateTaskInput) -> types.Task:
    """A new task of the caller's, optionally with conversations."""
    request = info.context.request
    with transaction.atomic():
        task_list = _list(info, input.list)
        if input.external_key and for_org(models.Task, info).filter(external_key=input.external_key).exists():
            raise ValidationError("You already have a task with this externalKey; use upsertTask.")
        task = models.Task.objects.create(
            organization=request.organization,
            owner=request.user,
            list=task_list,
            title=_check_title(input.title),
            notes=input.notes,
            due_at=input.due_at,
            pinned=input.pinned,
            position=input.position if input.position is not None else _next_position(for_org(models.Task, info).filter(list=task_list)),
            external_key=input.external_key or None,
            client=getattr(request, "client", None),
        )
        _link(info, task, input.threads, input.link)
    return task  # type: ignore[return-value]


@translated
def upsert_task(info: Info, input: UpsertTaskInput) -> types.Task:
    """Create or update the caller's task with this `externalKey`, then add the conversations."""
    request = info.context.request
    key = input.external_key.strip()
    if not key:
        raise ValidationError("externalKey must not be empty.")
    with transaction.atomic():
        task = for_org(models.Task, info).select_for_update().filter(external_key=key).first()
        if task is None:
            task_list = _list(info, input.list) if input.list is not strawberry.UNSET else None
            task = models.Task(
                organization=request.organization,
                owner=request.user,
                external_key=key,
                list=task_list,
                position=_next_position(for_org(models.Task, info).filter(list=task_list)),
                client=getattr(request, "client", None),
            )
        task.title = _check_title(input.title)
        if input.notes is not strawberry.UNSET:
            task.notes = input.notes or ""
        if input.list is not strawberry.UNSET:
            task.list = _list(info, input.list)
        if input.due_at is not strawberry.UNSET:
            task.due_at = input.due_at
        if input.pinned is not strawberry.UNSET and input.pinned is not None:
            task.pinned = input.pinned
        task.save()
        _link(info, task, input.threads, input.link)
    return task  # type: ignore[return-value]


@translated
def update_task(info: Info, input: UpdateTaskInput) -> types.Task:
    """Change a task."""
    task = get_or_404(models.Task, info, input.id)
    if input.title is not strawberry.UNSET and input.title is not None:
        task.title = _check_title(input.title)
    if input.notes is not strawberry.UNSET:
        task.notes = input.notes or ""
    if input.list is not strawberry.UNSET:
        task.list = _list(info, input.list)
    if input.due_at is not strawberry.UNSET:
        task.due_at = input.due_at
    if input.pinned is not strawberry.UNSET and input.pinned is not None:
        task.pinned = input.pinned
    if input.position is not strawberry.UNSET and input.position is not None:
        task.position = input.position
    task.save()
    return task  # type: ignore[return-value]


@translated
def delete_task(info: Info, id: strawberry.ID) -> strawberry.ID:
    """Delete a task (its conversations stay where they are)."""
    get_or_404(models.Task, info, id).delete()
    return id


@translated
def set_task_status(info: Info, input: SetTaskStatusInput) -> List[types.Task]:
    """Mark tasks OPEN, DONE or DISMISSED. No mail changes."""
    tasks = get_many(models.Task, info, input.tasks)
    now = timezone.now()
    for task in tasks:
        task.status = input.status.value
        task.completed_at = (task.completed_at or now) if input.status == enums.TaskStatus.DONE else None
        task.save(update_fields=["status", "completed_at", "updated_at"])
    return tasks  # type: ignore[return-value]


@translated
def snooze_tasks(info: Info, input: SnoozeTasksInput) -> List[types.Task]:
    """Hide tasks from the active view until `until` (null: wake them now)."""
    tasks = get_many(models.Task, info, input.tasks)
    for task in tasks:
        task.snoozed_until = input.until
        task.save(update_fields=["snoozed_until", "updated_at"])
    return tasks  # type: ignore[return-value]


@translated
def link_threads(info: Info, input: LinkThreadsInput) -> List[types.TaskThread]:
    """Put conversations the caller can see into their task."""
    task = get_or_404(models.Task, info, input.task)
    with transaction.atomic():
        return _link(info, task, input.threads, input.link)  # type: ignore[return-value]


@translated
def unlink_threads(info: Info, input: UnlinkThreadsInput) -> types.Task:
    """Take conversations out of a task."""
    from mail import threads as thread_module

    task = get_or_404(models.Task, info, input.task)
    removed = set(models.TaskThread.objects.filter(task=task, thread_id__in=input.threads).values_list("thread_id", flat=True))
    models.TaskThread.objects.filter(task=task, thread_id__in=removed).delete()
    # A conversation kept only for its task (its messages gone) can go now.
    thread_module.refresh_many(removed)
    return task  # type: ignore[return-value]
