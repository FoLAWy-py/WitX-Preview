"""Long-term memory : a person's own entries for their future
tasks. The task's agent keeps what it learns on its own (after Claude Code's auto
memory); the person sees each entry, can undo or delete it, and can turn this off. Runs
inside the caller's transaction."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from witx.domain import rules
from witx.domain.model import Memory, Run, Task
from witx.domain.rules import Rejected
from witx.store import devices, execution, identity, memories, tasks


def _count(entries: list[Memory], kind: str, app: str | None) -> int:
    if kind == "app":
        return sum(1 for e in entries if e.app == app)
    return sum(1 for e in entries if e.kind != "app")


async def save(
    conn: AsyncConnection,
    task: Task,
    run: Run,
    *,
    kind: str,
    app: str | None,
    text: str,
    replaces: UUID | None = None,
) -> Memory:
    """What the task's agent keeps for the person who started the task, without asking. With
        ``replaces``, one of the entries an agent kept is brought up to date.

    What it may keep is limited to what it can know for that person: their preferences
    and corrections only from the main agent (sub-agents never hear the person, D57) and
    only while nobody else wrote in the task; notes about an app only for an app this
    task's screenshots showed, and never a sensitive one."""
    owner = await tasks.creator(conn, task.id)
    if not await identity.auto_memory(conn, task.tenant_id, owner):
        raise Rejected("memory_off", "the user turned off remembering on your own")
    app = rules.check_memory_kind(kind, app)
    if kind != "app":
        if run.parent_run_id is not None:
            raise Rejected("not_allowed", "a sub-agent keeps only notes about an app")
        others = [
            i
            for i in await tasks.inputs(conn, task.id)
            if i["kind"] == "note" and i["created_by"] not in (None, owner)
        ]
        if others:
            raise Rejected(
                "not_allowed", "someone else wrote in this task: keep no preferences from it"
            )
    else:
        shown = {
            o.screen.foreground_app
            for o in await execution.task_observations(conn, task.id)
            if o.screen is not None
        }
        sensitive = {
            name
            for d in await devices.list_all(conn)
            if d.id in task.device_ids
            for name in d.sensitive_apps
        }
        if app not in shown:
            raise Rejected("not_allowed", "only an app this task's screenshots showed")
        if app in sensitive:
            raise Rejected("not_allowed", "nothing is kept about a sensitive app")
    entries = await memories.of_user(conn, owner)
    if replaces is not None:
        old = await memories.get(conn, replaces, lock=True)
        if (
            old is None
            or old.user_id != owner
            or old.status != "kept"
            or not old.by_agent  # the person's own words are theirs to change
            or (old.kind, old.app) != (kind, app)
        ):
            raise Rejected("not_found", "no such entry kept by an agent, of this kind")
        cleaned = rules.check_memory(text)
        await memories.replace_text(conn, old.id, cleaned, task.id)
        saved = await memories.get(conn, old.id)
        assert saved is not None
    else:
        if kind == "app" and sum(1 for e in entries if e.kind == "app") >= rules.MEMORY_APPS_MAX:
            raise Rejected("memory_full", "too many notes about apps; bring one up to date")
        cleaned = rules.check_memory(text, _count(entries, kind, app), app=app)
        saved = await memories.add(
            conn,
            tenant_id=task.tenant_id,
            user_id=owner,
            text=cleaned,
            kind=kind,
            app=app,
            source_task_id=task.id,
        )
    await tasks.emit(conn, task, "memory.saved", memory_id=saved.id, kind=saved.kind, app=saved.app)
    return saved


async def add(conn: AsyncConnection, *, tenant_id: UUID, user_id: UUID, text: str) -> Memory:
    """An entry the person writes themselves: always in their tasks, also with auto
    memory off."""
    entries = await memories.of_user(conn, user_id)
    cleaned = rules.check_memory(text, _count(entries, "user", None))
    return await memories.add(conn, tenant_id=tenant_id, user_id=user_id, text=cleaned)


async def delete(conn: AsyncConnection, *, memory_id: UUID, user_id: UUID) -> None:
    """The person deletes (or undoes) one of their entries."""
    entry = await memories.get(conn, memory_id, lock=True)
    if entry is None or entry.user_id != user_id:
        raise Rejected("not_found")  # someone else's memory is not even shown to exist
    await memories.delete(conn, memory_id)
    if entry.source_task_id is not None:
        await tasks.emit(conn, entry.source_task_id, "memory.deleted", memory_id=memory_id)
