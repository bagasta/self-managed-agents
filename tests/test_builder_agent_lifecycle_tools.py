from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.tools.builder_management_tools import build_builder_management_tools


class _Result:
    def __init__(self, agent):
        self.agent = agent

    def scalar_one_or_none(self):
        return self.agent


class _Db:
    def __init__(self, agent):
        self.agent = agent
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, *_args, **_kwargs):
        return _Result(self.agent)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_renew_agent_reactivates_expired_owned_agent():
    previous = datetime.now(timezone.utc) - timedelta(days=1)
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Personal Assistant",
        is_deleted=False,
        capabilities=[],
        quota_period_days=30,
        active_until=previous,
        tokens_used=123,
        version=1,
    )
    db = _Db(agent)
    tools = build_builder_management_tools(lambda: db)

    payload = json.loads(await tools["renew_agent"].ainvoke({"agent_id": str(agent.id)}))

    assert db.committed is True
    assert payload["success"] is True
    assert payload["previous_active_until"] == previous.isoformat()
    assert datetime.fromisoformat(payload["active_until"]) > datetime.now(timezone.utc)
    assert agent.tokens_used == 0
    assert agent.version == 2
