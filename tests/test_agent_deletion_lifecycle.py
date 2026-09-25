import uuid
from types import SimpleNamespace

import pytest

from app.api.agents import delete_agent


@pytest.mark.asyncio
async def test_delete_agent_cancels_background_jobs(monkeypatch) -> None:
    """Soft delete must cancel jobs because FK cascades only cover hard deletes."""
    agent = SimpleNamespace(id=uuid.uuid4(), is_deleted=False, wa_device_id=None)

    class FakeResult:
        def scalar_one_or_none(self):
            return agent

    class FakeDb:
        def __init__(self):
            self.statements = []
            self.flushed = False

        async def execute(self, statement):
            self.statements.append(statement)
            return FakeResult()

        async def flush(self):
            self.flushed = True

    db = FakeDb()

    async def fake_get_active_agent(agent_id, session):
        assert agent_id == agent.id
        assert session is db
        return agent

    monkeypatch.setattr("app.api.agents._get_active_agent", fake_get_active_agent)

    await delete_agent(agent.id, db, None)

    rendered = "\n".join(str(statement) for statement in db.statements)
    assert agent.is_deleted is True
    assert db.flushed is True
    assert "scheduled_jobs" in rendered
    assert "cancelled" in rendered
