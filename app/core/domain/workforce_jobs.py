"""PostgreSQL-backed workforce dispatch and task-scoped team conversation."""
from __future__ import annotations

import asyncio
import uuid
import logging
from datetime import datetime, timezone

from sqlalchemy import select, text, update

from app.models.workforce_task import WorkforceTask, WorkforceTaskStep, WorkforceTaskEvent

log = logging.getLogger(__name__)


async def cancel_task(db, task):
    task = (await db.execute(select(WorkforceTask).where(WorkforceTask.id == task.id)
                            .with_for_update().execution_options(populate_existing=True))).scalar_one()
    if task.status in {"completed", "cancelled"}:
        return
    task.status = "cancelled"
    task.completed_at = datetime.now(timezone.utc)
    await db.execute(update(WorkforceTaskStep).where(
        WorkforceTaskStep.task_id == task.id,
        WorkforceTaskStep.status.in_(["pending", "queued", "in_progress", "blocked"]),
    ).values(status="cancelled"))
    db.add(WorkforceTaskEvent(task_id=task.id, event_type="task_cancelled", metadata_={}))
    await db.commit()


async def retry_task(db, task):
    task = (await db.execute(select(WorkforceTask).where(WorkforceTask.id == task.id)
                            .with_for_update().execution_options(populate_existing=True))).scalar_one()
    if task.status != "blocked":
        return {"ok": False, "error": "Only blocked tasks can be retried."}
    if not (task.context or {}).get("durable_dispatch"):
        return {"ok": False, "error": "This task does not use the durable worker queue."}
    task.status, task.completed_at = "in_progress", None
    await db.execute(update(WorkforceTaskStep).where(
        WorkforceTaskStep.task_id == task.id, WorkforceTaskStep.parent_step_id.is_(None),
        WorkforceTaskStep.status == "blocked",
    ).values(status="queued", completed_at=None))
    db.add(WorkforceTaskEvent(task_id=task.id, event_type="task_retry_requested", metadata_={}))
    await db.commit()
    return {"ok": True, "status": task.status}


def team_tools(db, task_id, agent_id, *, step_id=None, db_factory=None):
    from langchain_core.tools import tool

    async def _read(read_db) -> list[dict]:
        rows = list((await read_db.execute(select(WorkforceTaskEvent).where(
            WorkforceTaskEvent.task_id == task_id,
            WorkforceTaskEvent.event_type == "team_message",
        ).order_by(WorkforceTaskEvent.created_at.desc(), WorkforceTaskEvent.id.desc()).limit(30))).scalars().all())
        steps = list((await read_db.execute(select(WorkforceTaskStep).where(
            WorkforceTaskStep.task_id == task_id,
            WorkforceTaskStep.status == "completed",
        ))).scalars().all())
        return ([{"agent_id": str(row.actor_agent_id) if row.actor_agent_id else "owner",
                  "message": row.metadata_.get("message", "")} for row in reversed(rows)]
                + [{"agent_id": str(row.assigned_agent_id), "result": (row.output_summary or "")[:4000]}
                   for row in steps])

    @tool
    async def read_team_messages() -> list[dict]:
        """Read the latest shared messages and specialist results in this task."""
        if db_factory is None:
            return await _read(db)
        async with db_factory() as read_db:
            return await _read(read_db)

    async def _post(post_db, message: str) -> dict:
        content = message.strip()
        if not content or len(content) > 4000:
            return {"ok": False, "error": "Message must contain 1-4000 characters."}
        task = await post_db.get(WorkforceTask, task_id, populate_existing=True)
        if task is None or task.status in {"completed", "cancelled"}:
            return {"ok": False, "error": "Task is no longer active."}
        post_db.add(WorkforceTaskEvent(task_id=task_id, step_id=step_id, actor_agent_id=agent_id,
                                       event_type="team_message", metadata_={"message": content}))
        await post_db.commit()
        return {"ok": True}

    @tool
    async def post_team_message(message: str) -> dict:
        """Share a finding or question with the other specialists working on this task."""
        if db_factory is None:
            return await _post(db, message)
        async with db_factory() as post_db:
            return await _post(post_db, message)

    return [read_team_messages, post_team_message]


async def run_persisted_task(factory, task_id):
    from app.core.domain.workforce_service import execute_owner_workforce_task, list_owner_workforce_agents

    # A transaction-scoped advisory lock disappears even after a hard process crash.
    # It uses a separate connection so progress commits cannot release ownership.
    async with factory() as lock_db:
        lock_key = task_id.int % (2**63 - 1)
        acquired = (await lock_db.execute(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                         {"key": lock_key})).scalar()
        if not acquired:
            return
        async with factory() as db:
            task = await db.get(WorkforceTask, task_id)
            if task is None or task.status != "in_progress":
                return
            steps = list((await db.execute(select(WorkforceTaskStep).where(
                WorkforceTaskStep.task_id == task.id, WorkforceTaskStep.parent_step_id.is_(None)
            ).order_by(WorkforceTaskStep.sequence))).scalars().all())
            roster = await list_owner_workforce_agents(db, task.owner_user_id)
            assignments = [{"agent_id": str(step.assigned_agent_id), "title": step.title,
                            "instructions": step.instructions,
                            "requires_peer_help": (step.input_context or {}).get("requires_peer_help", False),
                            "requires_deployment": (step.input_context or {}).get("requires_deployment", False),
                            "depends_on_previous": (step.input_context or {}).get("depends_on_previous", False)}
                           for step in steps]
            runner = asyncio.create_task(execute_owner_workforce_task(
                db=db, owner_user_id=task.owner_user_id, title=task.title,
                objective=task.description or "", assignments=assignments,
                worker_agents={str(agent.id): agent for agent in roster},
                db_factory=factory, existing_task=task,
            ))
            try:
                while not runner.done():
                    await asyncio.wait({runner}, timeout=1)
                    await lock_db.execute(text("SELECT 1"))
                    async with factory() as poll:
                        current = await poll.get(WorkforceTask, task_id)
                        if current is None or current.status == "cancelled":
                            runner.cancel()
                            await asyncio.gather(runner, return_exceptions=True)
                            # Reapply cancellation after in-flight step commits settle.
                            if current is not None:
                                await poll.execute(update(WorkforceTaskStep).where(
                                    WorkforceTaskStep.task_id == task_id,
                                    WorkforceTaskStep.status != "completed",
                                ).values(status="cancelled"))
                                await poll.commit()
                            return
                result = await runner
                if result.get("error"):
                    task.status = "blocked"
                    task.result_summary = result["error"]
                    await db.commit()
            finally:
                if not runner.done():
                    runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)


async def workforce_queue_loop(factory):
    active: dict[uuid.UUID, asyncio.Task] = {}
    try:
        while True:
            for key, future in list(active.items()):
                if future.done():
                    try:
                        future.result()
                    except Exception:
                        log.exception("Workforce job interrupted; persisted state retained")
                    del active[key]
            try:
                async with factory() as db:
                    ids = list((await db.execute(select(WorkforceTask.id).where(
                        WorkforceTask.status == "in_progress",
                        WorkforceTask.context["durable_dispatch"].as_boolean().is_(True),
                    ).order_by(WorkforceTask.created_at).limit(100))).scalars().all())
                for key in ids:
                    if key not in active and len(active) < 2:
                        active[key] = asyncio.create_task(run_persisted_task(factory, key))
            except Exception:
                log.exception("Workforce queue poll failed")
            await asyncio.sleep(2)
    finally:
        for future in active.values():
            future.cancel()
        await asyncio.gather(*active.values(), return_exceptions=True)
