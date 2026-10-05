from __future__ import annotations

import uuid

import pytest

from app.core.domain.tenant_identity import arthur_memory_scope
from app.core.domain.workforce_service import _next_task_step_sequence, execute_owner_workforce_task
from app.api.workforce import _agent_workspace_predicate, _require_task
from app.models.agent import Agent


def test_arthur_memory_scope_separates_owners_and_unresolved_sessions() -> None:
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()
    session_a, session_b = uuid.uuid4(), uuid.uuid4()

    assert arthur_memory_scope(owner_a, session_a) == f"user:{owner_a}"
    assert arthur_memory_scope(owner_b, session_a) == f"user:{owner_b}"
    assert arthur_memory_scope(None, session_a) == f"session:{session_a}"
    assert arthur_memory_scope("not-a-user-uuid", session_a) == f"session:{session_a}"
    assert arthur_memory_scope(None, session_a) != arthur_memory_scope(None, session_b)


def test_roster_filter_requires_exact_stable_owner_and_fails_closed_without_one() -> None:
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()
    predicate_a = _agent_workspace_predicate(
        str(owner_a), owner_only=True, owner_user_id=owner_a
    )
    predicate_b = _agent_workspace_predicate(
        str(owner_b), owner_only=True, owner_user_id=owner_b
    )
    assert predicate_a.left == Agent.owner_user_id
    assert predicate_a.right.value == owner_a
    assert predicate_b.right.value == owner_b
    assert predicate_a.right.value != predicate_b.right.value

    # An owner-path request without a resolved stable principal must not fall
    # back to mutable phone/workspace text.
    unresolved = _agent_workspace_predicate("628111", owner_only=True, owner_user_id=None)
    assert unresolved.left == Agent.id
    assert unresolved.operator.__name__ == "is_"


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Db:
    def __init__(self, task):
        self.task = task
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result(self.task)


@pytest.mark.asyncio
async def test_task_lookup_includes_stable_owner_predicate() -> None:
    owner_a, owner_b = uuid.uuid4(), uuid.uuid4()
    task_id = uuid.uuid4()
    task = object()
    db = _Db(task)

    assert await _require_task(db, str(owner_a), task_id, owner_user_id=owner_a) is task
    assert db.statement is not None
    params = list(db.statement.compile().params.values())
    assert owner_a in params
    assert owner_b not in params


@pytest.mark.asyncio
async def test_orchestrator_rejects_agent_outside_supplied_owner_roster_before_write() -> None:
    class NoWriteDb:
        def add(self, _value):
            raise AssertionError("invalid cross-owner assignment must be rejected before persistence")

        async def flush(self):
            raise AssertionError("invalid cross-owner assignment must be rejected before persistence")

    owner_a, agent_a, agent_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    result = await execute_owner_workforce_task(
        db=NoWriteDb(),
        owner_user_id=owner_a,
        title="Owner task",
        objective="Analyze a question",
        assignments=[{"agent_id": str(agent_b), "title": "Wrong owner", "instructions": "Work"}],
        worker_agents={str(agent_a): object()},
    )

    assert result["ok"] is False
    assert "owner's roster" in result["error"]


@pytest.mark.asyncio
async def test_direct_and_nested_steps_share_monotonic_sequence_allocator() -> None:
    class SequenceDb:
        def __init__(self):
            self.values = [None, 0, 1]

        async def execute(self, _statement):
            return _Result(self.values.pop(0))

    db = SequenceDb()
    task_id = uuid.uuid4()
    # First main step, then its nested peer, then the next main step.
    assert await _next_task_step_sequence(db, task_id) == 0
    assert await _next_task_step_sequence(db, task_id) == 1
    assert await _next_task_step_sequence(db, task_id) == 2
