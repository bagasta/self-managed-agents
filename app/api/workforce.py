"""Owner-scoped workforce control plane.

Routes accept either a platform ``X-API-Key`` (trusted admin, explicit
workspace scope) or an owner-bound ``X-User-Key``. User-key scope always comes
from the key record; client ``workspace_id`` values are ignored. Unbound legacy
user keys cannot access workforce data. Dispatch is a separate, opt-in action.
"""
from __future__ import annotations

import uuid
import json
import asyncio
from collections.abc import Iterable
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.domain.agent_ownership import owner_filter
from app.database import get_db, AsyncSessionLocal
from app.config import get_settings
from app.models.agent import Agent
from app.models.run import Run
from app.models.session import Session
from app.models.user_api_key import UserApiKey, hash_user_key
from app.models.workforce_task import WorkforceTask, WorkforceTaskEvent, WorkforceTaskStep
from app.schemas.workforce import (
    WorkforceRosterMember,
    WorkforceRosterResponse,
    WorkforceTaskCreate,
    WorkforceTaskDispatch,
    WorkforceTaskEventResponse,
    WorkforceTaskListResponse,
    WorkforceTaskResponse,
    WorkforceTaskStepCreate,
    WorkforceTaskStepResponse,
    WorkforceTaskStepUpdate,
    WorkforceTaskUpdate,
)

router = APIRouter(prefix="/v1/workforce", tags=["workforce"])


@dataclass(frozen=True)
class WorkforcePrincipal:
    owner_user_id: uuid.UUID | None
    owner_external_id: str | None = None
    is_platform_admin: bool = False


async def get_workforce_principal(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    x_user_key: str | None = Header(None, alias="X-User-Key"),
    db: AsyncSession = Depends(get_db),
) -> WorkforcePrincipal:
    """Accept global platform operators or owner-bound user keys only."""
    if x_api_key is not None:
        if x_api_key != get_settings().api_key:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return WorkforcePrincipal(owner_user_id=None, is_platform_admin=True)
    if not x_user_key:
        raise HTTPException(status_code=401, detail="A platform or owner-bound API key is required")
    key = (
        await db.execute(
            select(UserApiKey).where(UserApiKey.key_hash == hash_user_key(x_user_key))
        )
    ).scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if key.revoked:
        raise HTTPException(status_code=403, detail="API key has been revoked")
    expires_at = key.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at:
        raise HTTPException(status_code=403, detail="API key has expired")
    if not key.owner_user_id:
        raise HTTPException(status_code=403, detail="API key is not bound to a stable workforce owner")
    return WorkforcePrincipal(owner_user_id=key.owner_user_id, owner_external_id=key.owner_external_id)


def _authorized_workspace(principal: WorkforcePrincipal, requested: str | None) -> str:
    if principal.owner_user_id:
        # Owner-bound keys cannot select or override another workspace.
        return str(principal.owner_user_id)
    if not requested:
        raise HTTPException(status_code=422, detail="workspace_id is required for platform admin access")
    return requested

_MAX_STEPS_PER_TASK = 8
_SENSITIVE_CONTEXT_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}
_TASK_TRANSITIONS = {
    "draft": {"open", "cancelled"},
    "open": {"in_progress", "blocked", "cancelled"},
    "in_progress": {"blocked", "completed", "cancelled"},
    "blocked": {"in_progress", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
_STEP_TRANSITIONS = {
    "queued": {"in_progress", "blocked", "cancelled"},
    "pending": {"in_progress", "blocked", "cancelled"},
    "in_progress": {"blocked", "completed", "cancelled"},
    "blocked": {"in_progress", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _reject_sensitive_context(value: Any, path: str = "context") -> None:
    """Reject obvious secret fields before they become timeline metadata."""
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_CONTEXT_KEYS:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{path}.{key} is not allowed in workforce context",
                )
            _reject_sensitive_context(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_sensitive_context(nested, f"{path}[{index}]")


def _agent_workspace_predicate(
    workspace_id: str,
    *,
    owner_only: bool,
    owner_user_id: uuid.UUID | None = None,
):
    if not owner_only:
        return owner_filter(workspace_id)
    return Agent.owner_user_id == owner_user_id if owner_user_id else Agent.id.is_(None)


async def _workspace_agents_scoped(
    db: AsyncSession, workspace_id: str, *, owner_only: bool, owner_user_id: uuid.UUID | None = None
) -> list[Agent]:
    return (
        await db.execute(
            select(Agent)
            .where(
                Agent.is_deleted.is_(False),
                _agent_workspace_predicate(workspace_id, owner_only=owner_only, owner_user_id=owner_user_id),
            )
            .order_by(Agent.created_at.asc())
        )
    ).scalars().all()


async def _require_workspace_agent(
    db: AsyncSession,
    workspace_id: str,
    agent_id: uuid.UUID | None,
    *,
    owner_only: bool = False,
    owner_user_id: uuid.UUID | None = None,
) -> Agent | None:
    if agent_id is None:
        return None
    agent = (
        await db.execute(
            select(Agent).where(
                Agent.id == agent_id,
                Agent.is_deleted.is_(False),
                _agent_workspace_predicate(workspace_id, owner_only=owner_only, owner_user_id=owner_user_id),
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent is not available in this workspace")
    return agent


async def _require_workspace_session(
    db: AsyncSession,
    workspace_id: str,
    session_id: uuid.UUID | None,
    *,
    owner_only: bool = False,
    owner_user_id: uuid.UUID | None = None,
) -> Session | None:
    if session_id is None:
        return None
    row = (
        await db.execute(
            select(Session)
            .join(Agent, Agent.id == Session.agent_id)
            .where(
                Session.id == session_id,
                Agent.is_deleted.is_(False),
                _agent_workspace_predicate(workspace_id, owner_only=owner_only, owner_user_id=owner_user_id),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Session is not available in this workspace")
    return row


async def _require_task(
    db: AsyncSession,
    workspace_id: str,
    task_id: uuid.UUID,
    *,
    owner_user_id: uuid.UUID | None = None,
) -> WorkforceTask:
    filters = [WorkforceTask.id == task_id, WorkforceTask.workspace_id == workspace_id]
    if owner_user_id is not None:
        filters.append(WorkforceTask.owner_user_id == owner_user_id)
    task = (
        await db.execute(
            select(WorkforceTask).where(*filters)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Workforce task not found")
    return task


async def _require_step(
    db: AsyncSession, task_id: uuid.UUID, step_id: uuid.UUID
) -> WorkforceTaskStep:
    step = (
        await db.execute(
            select(WorkforceTaskStep).where(
                WorkforceTaskStep.id == step_id,
                WorkforceTaskStep.task_id == task_id,
            )
        )
    ).scalar_one_or_none()
    if step is None:
        raise HTTPException(status_code=404, detail="Workforce task step not found")
    return step


async def _require_workspace_run(
    db: AsyncSession,
    workspace_id: str,
    run_id: uuid.UUID,
    assigned_agent_id: uuid.UUID | None,
    *,
    owner_only: bool = False,
    owner_user_id: uuid.UUID | None = None,
) -> Run:
    run = (
        await db.execute(
            select(Run)
            .join(Session, Session.id == Run.session_id)
            .join(Agent, Agent.id == Session.agent_id)
            .where(
                Run.id == run_id,
                Agent.is_deleted.is_(False),
                _agent_workspace_predicate(workspace_id, owner_only=owner_only, owner_user_id=owner_user_id),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Run is not available in this workspace")
    if assigned_agent_id is not None:
        session_agent_id = (
            await db.execute(select(Session.agent_id).where(Session.id == run.session_id))
        ).scalar_one()
        if session_agent_id != assigned_agent_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Run must belong to the assigned agent for this step",
            )
    return run


def _validate_transition(current: str, target: str, transitions: dict[str, set[str]]) -> None:
    if current == target:
        return
    if target not in transitions.get(current, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot transition from {current} to {target}",
        )


def _record_event(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
    event_type: str,
    step_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    actor_agent_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    _reject_sensitive_context(metadata or {}, "event_metadata")
    db.add(
        WorkforceTaskEvent(
            task_id=task_id,
            step_id=step_id,
            run_id=run_id,
            actor_agent_id=actor_agent_id,
            event_type=event_type,
            metadata_=metadata or {},
        )
    )


def _progress_callback(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
    actor_agent_id: uuid.UUID,
    step_id: uuid.UUID | None = None,
):
    async def persist(event_type: str, run_id: uuid.UUID, metadata: dict[str, Any]) -> None:
        _record_event(
            db,
            task_id=task_id,
            step_id=step_id,
            run_id=run_id,
            actor_agent_id=actor_agent_id,
            event_type=event_type,
            metadata=metadata,
        )
        await db.commit()

    return persist


async def _task_response(db: AsyncSession, task: WorkforceTask, *, include_events: bool = True) -> WorkforceTaskResponse:
    steps = (
        await db.execute(
            select(WorkforceTaskStep)
            .where(WorkforceTaskStep.task_id == task.id)
            .order_by(WorkforceTaskStep.sequence.asc())
        )
    ).scalars().all()
    events: Iterable[WorkforceTaskEvent] = []
    if include_events:
        events = (
            await db.execute(
                select(WorkforceTaskEvent)
                .where(WorkforceTaskEvent.task_id == task.id)
                .order_by(WorkforceTaskEvent.created_at.asc(), WorkforceTaskEvent.id.asc())
            )
        ).scalars().all()
    return WorkforceTaskResponse(
        **WorkforceTaskResponse.model_validate(task).model_dump(exclude={"steps", "events"}),
        steps=[WorkforceTaskStepResponse.model_validate(step) for step in steps],
        events=[WorkforceTaskEventResponse.model_validate(event) for event in events],
    )


@router.get("/roster", response_model=WorkforceRosterResponse)
async def get_roster(
    workspace_id: str | None = Query(None, min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
    principal: WorkforcePrincipal = Depends(get_workforce_principal),
) -> WorkforceRosterResponse:
    workspace_id = _authorized_workspace(principal, workspace_id)
    agents = await _workspace_agents_scoped(
        db,
        workspace_id,
        owner_only=principal.owner_user_id is not None,
        owner_user_id=principal.owner_user_id,
    )
    if not agents:
        raise HTTPException(status_code=404, detail="Workspace has no registered agents")
    agent_ids = [agent.id for agent in agents]
    counts = dict(
        (
            await db.execute(
                select(WorkforceTask.assigned_agent_id, func.count(WorkforceTask.id))
                .where(
                    WorkforceTask.workspace_id == workspace_id,
                    *([WorkforceTask.owner_user_id == principal.owner_user_id]
                      if principal.owner_user_id is not None else []),
                    WorkforceTask.assigned_agent_id.in_(agent_ids),
                    WorkforceTask.status.in_(("open", "in_progress", "blocked")),
                )
                .group_by(WorkforceTask.assigned_agent_id)
            )
        ).all()
    )
    return WorkforceRosterResponse(
        workspace_id=workspace_id,
        items=[
            WorkforceRosterMember(
                id=agent.id,
                name=agent.name,
                description=agent.description,
                capabilities=agent.capabilities or [],
                model=agent.model,
                active_task_count=int(counts.get(agent.id, 0)),
            )
            for agent in agents
        ],
    )


@router.post("/tasks", response_model=WorkforceTaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: WorkforceTaskCreate,
    db: AsyncSession = Depends(get_db),
    principal: WorkforcePrincipal = Depends(get_workforce_principal),
) -> WorkforceTaskResponse:
    workspace_id = _authorized_workspace(principal, payload.workspace_id)
    payload_data = payload.model_dump()
    payload_data["workspace_id"] = workspace_id
    _reject_sensitive_context(payload.context)
    owner_only = principal.owner_user_id is not None
    owner_user_id = principal.owner_user_id
    if owner_user_id is None:
        from app.core.domain.tenant_identity import resolve_unique_user_id

        owner_user_id = await resolve_unique_user_id(db, workspace_id)
    payload_data["owner_user_id"] = owner_user_id
    if not await _workspace_agents_scoped(
        db, workspace_id, owner_only=owner_only, owner_user_id=principal.owner_user_id
    ):
        raise HTTPException(status_code=404, detail="Workspace has no registered agents")
    if payload.idempotency_key:
        existing = (
            await db.execute(
                select(WorkforceTask).where(
                    WorkforceTask.workspace_id == workspace_id,
                    WorkforceTask.owner_user_id == owner_user_id,
                    WorkforceTask.idempotency_key == payload.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return await _task_response(db, existing)
    await _require_workspace_agent(
        db, workspace_id, payload.assigned_agent_id, owner_only=owner_only, owner_user_id=principal.owner_user_id
    )
    await _require_workspace_session(
        db, workspace_id, payload.customer_session_id, owner_only=owner_only, owner_user_id=principal.owner_user_id
    )
    if payload.parent_task_id is not None:
        parent = await _require_task(db, workspace_id, payload.parent_task_id, owner_user_id=owner_user_id)
        if parent.parent_task_id is not None:
            raise HTTPException(status_code=422, detail="Delegation depth is limited to one child task")
    task = WorkforceTask(**payload_data)
    db.add(task)
    await db.flush()
    _record_event(
        db,
        task_id=task.id,
        event_type="task_created",
        actor_agent_id=task.assigned_agent_id,
        metadata={"status": task.status, "priority": task.priority},
    )
    await db.flush()
    return await _task_response(db, task)


@router.get("/tasks", response_model=WorkforceTaskListResponse)
async def list_tasks(
    workspace_id: str | None = Query(None, min_length=1, max_length=64),
    task_status: str | None = Query(None, alias="status"),
    assigned_agent_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    principal: WorkforcePrincipal = Depends(get_workforce_principal),
) -> WorkforceTaskListResponse:
    workspace_id = _authorized_workspace(principal, workspace_id)
    filters = [WorkforceTask.workspace_id == workspace_id]
    if principal.owner_user_id is not None:
        filters.append(WorkforceTask.owner_user_id == principal.owner_user_id)
    if task_status:
        filters.append(WorkforceTask.status == task_status)
    if assigned_agent_id:
        await _require_workspace_agent(
            db,
            workspace_id,
            assigned_agent_id,
            owner_only=principal.owner_user_id is not None,
            owner_user_id=principal.owner_user_id,
        )
        filters.append(WorkforceTask.assigned_agent_id == assigned_agent_id)
    total = (await db.execute(select(func.count()).select_from(WorkforceTask).where(*filters))).scalar_one()
    rows = (
        await db.execute(
            select(WorkforceTask)
            .where(*filters)
            .order_by(WorkforceTask.updated_at.desc(), WorkforceTask.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return WorkforceTaskListResponse(
        items=[await _task_response(db, task, include_events=False) for task in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/tasks/{task_id}", response_model=WorkforceTaskResponse)
async def get_task(
    task_id: uuid.UUID,
    workspace_id: str | None = Query(None, min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
    principal: WorkforcePrincipal = Depends(get_workforce_principal),
) -> WorkforceTaskResponse:
    workspace_id = _authorized_workspace(principal, workspace_id)
    return await _task_response(
        db, await _require_task(db, workspace_id, task_id, owner_user_id=principal.owner_user_id)
    )


@router.get("/tasks/{task_id}/stream")
async def stream_task(
    task_id: uuid.UUID,
    request: Request,
    workspace_id: str | None = Query(None, min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
    principal: WorkforcePrincipal = Depends(get_workforce_principal),
):
    workspace = _authorized_workspace(principal, workspace_id)
    await _require_task(db, workspace, task_id, owner_user_id=principal.owner_user_id)
    await db.rollback()

    async def events():
        previous = None
        while not await request.is_disconnected():
            async with AsyncSessionLocal() as poll:
                task = await _require_task(poll, workspace, task_id, owner_user_id=principal.owner_user_id)
                snapshot = (await _task_response(poll, task)).model_dump_json()
                terminal = task.status in {"completed", "blocked", "cancelled"}
            if snapshot != previous:
                yield f"event: task\ndata: {snapshot}\n\n"
                previous = snapshot
            else:
                yield ": heartbeat\n\n"
            if terminal:
                return
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.patch("/tasks/{task_id}", response_model=WorkforceTaskResponse)
async def update_task(
    task_id: uuid.UUID,
    payload: WorkforceTaskUpdate,
    workspace_id: str | None = Query(None, min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
    principal: WorkforcePrincipal = Depends(get_workforce_principal),
) -> WorkforceTaskResponse:
    workspace_id = _authorized_workspace(principal, workspace_id)
    task = await _require_task(db, workspace_id, task_id, owner_user_id=principal.owner_user_id)
    update_data = payload.model_dump(exclude_unset=True)
    if "context" in update_data and update_data["context"] is not None:
        _reject_sensitive_context(update_data["context"])
    if "assigned_agent_id" in update_data:
        await _require_workspace_agent(
            db, workspace_id, update_data["assigned_agent_id"],
            owner_only=principal.owner_user_id is not None,
            owner_user_id=principal.owner_user_id,
        )
        if update_data["assigned_agent_id"] != task.assigned_agent_id:
            _record_event(
                db,
                task_id=task.id,
                event_type="task_reassigned",
                actor_agent_id=update_data["assigned_agent_id"],
                metadata={
                    "from": str(task.assigned_agent_id) if task.assigned_agent_id else None,
                    "to": str(update_data["assigned_agent_id"]) if update_data["assigned_agent_id"] else None,
                },
            )
    if "status" in update_data:
        _validate_transition(task.status, update_data["status"], _TASK_TRANSITIONS)
        previous_status = task.status
        if update_data["status"] == "in_progress" and task.started_at is None:
            task.started_at = _now()
        if update_data["status"] in {"completed", "cancelled"}:
            task.completed_at = _now()
        _record_event(
            db,
            task_id=task.id,
            event_type="task_status_changed",
            actor_agent_id=update_data.get("assigned_agent_id", task.assigned_agent_id),
            metadata={"from": previous_status, "to": update_data["status"]},
        )
    for field, value in update_data.items():
        setattr(task, field, value)
    if update_data.get("status") == "cancelled":
        from app.core.domain.workforce_jobs import cancel_task
        task.status = previous_status
        await cancel_task(db, task)
    await db.flush()
    return await _task_response(db, task)


@router.post("/tasks/{task_id}/steps", response_model=WorkforceTaskStepResponse, status_code=status.HTTP_201_CREATED)
async def create_task_step(
    task_id: uuid.UUID,
    payload: WorkforceTaskStepCreate,
    workspace_id: str | None = Query(None, min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
    principal: WorkforcePrincipal = Depends(get_workforce_principal),
) -> WorkforceTaskStepResponse:
    workspace_id = _authorized_workspace(principal, workspace_id)
    task = await _require_task(db, workspace_id, task_id, owner_user_id=principal.owner_user_id)
    if task.status in {"completed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Cannot delegate from a terminal task")
    _reject_sensitive_context(payload.input_context)
    owner_only = principal.owner_user_id is not None
    await _require_workspace_agent(
        db, workspace_id, payload.assigned_agent_id, owner_only=owner_only, owner_user_id=principal.owner_user_id
    )
    await _require_workspace_agent(
        db, workspace_id, payload.delegated_by_agent_id, owner_only=owner_only, owner_user_id=principal.owner_user_id
    )
    count = (
        await db.execute(
            select(func.count()).select_from(WorkforceTaskStep).where(WorkforceTaskStep.task_id == task.id)
        )
    ).scalar_one()
    if count >= _MAX_STEPS_PER_TASK:
        raise HTTPException(status_code=422, detail=f"A task may contain at most {_MAX_STEPS_PER_TASK} steps")
    next_sequence = (
        await db.execute(
            select(func.coalesce(func.max(WorkforceTaskStep.sequence), -1)).where(WorkforceTaskStep.task_id == task.id)
        )
    ).scalar_one() + 1
    step = WorkforceTaskStep(sequence=next_sequence, task_id=task.id, **payload.model_dump())
    db.add(step)
    await db.flush()
    _record_event(
        db,
        task_id=task.id,
        step_id=step.id,
        event_type="handoff_created",
        actor_agent_id=step.delegated_by_agent_id,
        metadata={"sequence": step.sequence, "assigned_agent_id": str(step.assigned_agent_id) if step.assigned_agent_id else None},
    )
    await db.flush()
    return WorkforceTaskStepResponse.model_validate(step)


@router.patch("/tasks/{task_id}/steps/{step_id}", response_model=WorkforceTaskStepResponse)
async def update_task_step(
    task_id: uuid.UUID,
    step_id: uuid.UUID,
    payload: WorkforceTaskStepUpdate,
    workspace_id: str | None = Query(None, min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
    principal: WorkforcePrincipal = Depends(get_workforce_principal),
) -> WorkforceTaskStepResponse:
    workspace_id = _authorized_workspace(principal, workspace_id)
    task = await _require_task(db, workspace_id, task_id, owner_user_id=principal.owner_user_id)
    step = await _require_step(db, task.id, step_id)
    update_data = payload.model_dump(exclude_unset=True)
    if update_data.get("run_id") is not None:
        await _require_workspace_run(
            db, workspace_id, update_data["run_id"], step.assigned_agent_id,
            owner_only=principal.owner_user_id is not None,
            owner_user_id=principal.owner_user_id,
        )
        _record_event(
            db,
            task_id=task.id,
            step_id=step.id,
            run_id=update_data["run_id"],
            event_type="run_linked",
            actor_agent_id=step.assigned_agent_id,
            metadata={},
        )
    if "status" in update_data:
        _validate_transition(step.status, update_data["status"], _STEP_TRANSITIONS)
        previous_status = step.status
        if update_data["status"] == "in_progress" and step.started_at is None:
            step.started_at = _now()
        if update_data["status"] in {"completed", "cancelled"}:
            step.completed_at = _now()
        _record_event(
            db,
            task_id=task.id,
            step_id=step.id,
            run_id=update_data.get("run_id", step.run_id),
            event_type="step_status_changed",
            actor_agent_id=step.assigned_agent_id,
            metadata={"from": previous_status, "to": update_data["status"]},
        )
    for field, value in update_data.items():
        setattr(step, field, value)
    await db.flush()
    return WorkforceTaskStepResponse.model_validate(step)


@router.post("/tasks/{task_id}/dispatch", response_model=WorkforceTaskResponse)
async def dispatch_task(
    task_id: uuid.UUID,
    payload: WorkforceTaskDispatch,
    workspace_id: str | None = Query(None, min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
    principal: WorkforcePrincipal = Depends(get_workforce_principal),
) -> WorkforceTaskResponse:
    """Let an owner Manager select and invoke one tool-free internal specialist."""
    workspace_id = _authorized_workspace(principal, workspace_id)
    task = (
        await db.execute(
            select(WorkforceTask)
            .where(
                WorkforceTask.id == task_id,
                WorkforceTask.workspace_id == workspace_id,
                *([WorkforceTask.owner_user_id == principal.owner_user_id]
                  if principal.owner_user_id is not None else []),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Workforce task not found")
    if isinstance(task.context, dict) and task.context.get("capability_preflight_blocker"):
        raise HTTPException(
            status_code=409,
            detail="This task is blocked by an unavailable runtime capability and cannot be redispatched. "
            "Resolve the capability before creating a new assignment.",
        )
    if task.status not in {"open", "blocked"}:
        raise HTTPException(status_code=409, detail="Only open or blocked tasks can be dispatched")
    if task.customer_session_id is not None:
        # Internal work receives task fields only; never inherit customer session
        # history, identity, or channel configuration.
        await _require_workspace_session(
            db, workspace_id, task.customer_session_id,
            owner_only=principal.owner_user_id is not None,
            owner_user_id=principal.owner_user_id,
        )

    owner_only = principal.owner_user_id is not None
    manager = await _require_workspace_agent(
        db, workspace_id, payload.manager_agent_id, owner_only=owner_only, owner_user_id=principal.owner_user_id
    )
    if manager is None:
        raise HTTPException(status_code=404, detail="Manager agent is not available")
    specialists = [
        agent for agent in await _workspace_agents_scoped(
            db, workspace_id, owner_only=owner_only, owner_user_id=principal.owner_user_id
        )
        if agent.id != manager.id
    ]
    if not specialists:
        raise HTTPException(status_code=409, detail="No internal specialist is available for this owner")
    existing_steps = (
        await db.execute(
            select(func.count()).select_from(WorkforceTaskStep).where(WorkforceTaskStep.task_id == task.id)
        )
    ).scalar_one()
    if existing_steps >= _MAX_STEPS_PER_TASK:
        raise HTTPException(status_code=422, detail=f"A task may contain at most {_MAX_STEPS_PER_TASK} steps")

    roster = [
        {
            "id": str(agent.id),
            "name": agent.name,
            "description": agent.description,
            "capabilities": agent.capabilities or [],
        }
        for agent in specialists
    ]
    manager_prompt = (
        "Choose exactly one specialist from the allowed roster for this internal task. "
        "Return one JSON object only with keys specialist_agent_id, step_title, instructions. "
        "specialist_agent_id must exactly match a roster id. instructions should be a concise, "
        "bounded internal assignment. Do not request customer or channel actions.\n\n"
        f"TASK: {task.title}\nDESCRIPTION: {task.description or ''}\n"
        f"CONTEXT: {json.dumps(task.context or {}, ensure_ascii=False, default=str)}\n"
        f"ALLOWED SPECIALISTS: {json.dumps(roster, ensure_ascii=False, default=str)}"
    )
    task.assigned_agent_id = manager.id
    task.status = "in_progress"
    task.started_at = task.started_at or _now()
    _record_event(db, task_id=task.id, event_type="manager_dispatch_started", actor_agent_id=manager.id)
    from app.core.engine.workforce_runner import run_workforce_deep_agent

    manager_run_id, manager_output, manager_ok = await run_workforce_deep_agent(
        agent=manager,
        workspace_id=workspace_id,
        prompt=manager_prompt,
        role="manager",
        task_id=task.id,
        db=db,
        progress_callback=_progress_callback(
            db, task_id=task.id, actor_agent_id=manager.id
        ),
    )
    if not manager_ok:
        task.status = "blocked"
        task.result_summary = "Manager planning failed; retry after reviewing platform logs."
        _record_event(
            db,
            task_id=task.id,
            event_type="manager_dispatch_failed",
            actor_agent_id=manager.id,
            run_id=manager_run_id,
        )
        await db.flush()
        return await _task_response(db, task)

    try:
        plan = json.loads(manager_output.strip().removeprefix("```json").removesuffix("```").strip())
        specialist_id = uuid.UUID(str(plan.get("specialist_agent_id", "")))
        step_title = str(plan.get("step_title", "")).strip()[:255]
        instructions = str(plan.get("instructions", "")).strip()[:20_000]
    except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
        plan = {}
        specialist_id = None
        step_title = ""
        instructions = ""
    specialist = next((agent for agent in specialists if agent.id == specialist_id), None)
    if specialist is None or not step_title or not instructions:
        task.status = "blocked"
        task.result_summary = "Manager did not return a valid internal specialist assignment."
        _record_event(
            db,
            task_id=task.id,
            event_type="manager_plan_rejected",
            actor_agent_id=manager.id,
            run_id=manager_run_id,
        )
        await db.flush()
        return await _task_response(db, task)

    step = WorkforceTaskStep(
        task_id=task.id,
        sequence=int(existing_steps),
        title=step_title,
        instructions=instructions,
        status="in_progress",
        input_context={"task_context": task.context or {}},
        delegated_by_agent_id=manager.id,
        assigned_agent_id=specialist.id,
        started_at=_now(),
    )
    db.add(step)
    await db.flush()
    _record_event(
        db,
        task_id=task.id,
        step_id=step.id,
        event_type="handoff_created",
        actor_agent_id=manager.id,
        run_id=manager_run_id,
        metadata={"assigned_agent_id": str(specialist.id), "sequence": step.sequence},
    )
    specialist_prompt = (
        f"Internal task: {task.title}\n"
        f"Description: {task.description or ''}\n"
        f"Manager assignment: {instructions}\n"
        f"Task context: {json.dumps(task.context or {}, ensure_ascii=False, default=str)}\n\n"
        "Return your internal work result with clear assumptions and unknowns. Do not contact anyone, "
        "send a channel reply, change customer systems, or claim external actions."
    )
    worker_run_id, result_summary, worker_ok = await run_workforce_deep_agent(
        agent=specialist,
        workspace_id=workspace_id,
        prompt=specialist_prompt,
        role="specialist",
        task_id=task.id,
        db=db,
        assignment_text=f"{task.title}\n{task.description or ''}\n{step_title}\n{instructions}",
        progress_callback=_progress_callback(
            db, task_id=task.id, actor_agent_id=specialist.id, step_id=step.id
        ),
    )
    step.run_id = worker_run_id
    step.output_summary = result_summary
    step.status = "completed" if worker_ok else "blocked"
    step.completed_at = _now() if worker_ok else None
    task.result_summary = result_summary
    task.status = "completed" if worker_ok else "blocked"
    task.completed_at = _now() if worker_ok else None
    if not worker_ok and str(result_summary).startswith("[BLOCKED:"):
        task.context = {
            **(task.context if isinstance(task.context, dict) else {}),
            "capability_preflight_blocker": str(result_summary)[:2_000],
        }
    _record_event(
        db,
        task_id=task.id,
        step_id=step.id,
        event_type="specialist_completed" if worker_ok else (
            "capability_preflight_blocked" if str(result_summary).startswith("[BLOCKED:") else "specialist_failed"
        ),
        actor_agent_id=specialist.id,
        run_id=worker_run_id,
        metadata={"manager_run_id": str(manager_run_id)},
    )
    await db.flush()
    return await _task_response(db, task)
