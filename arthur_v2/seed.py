"""Explicit seed command for the independent Arthur V2 system agent."""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.agent import Agent

from .plugin import (
    ARTHUR_V2_ASSISTANT_MODEL,
    ARTHUR_V2_PLUGIN,
    ARTHUR_V2_TOOL_AGENT_MAX_TOKENS,
    build_arthur_v2_system_prompt,
)

ARTHUR_V2_MODEL = ARTHUR_V2_ASSISTANT_MODEL


async def seed() -> Agent:
    """Create or update the Arthur V2 record without touching legacy Arthur."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Agent).where(Agent.tools_config["system_plugin"].astext == ARTHUR_V2_PLUGIN)
        )
        agent = result.scalar_one_or_none()
        if agent is None:
            agent = Agent(
                name="Arthur",
                description="Partner untuk membentuk dan mengelola AI assistant personal maupun bisnis.",
                instructions=build_arthur_v2_system_prompt(),
                capabilities=["builder"],
                tools_config={"system_plugin": ARTHUR_V2_PLUGIN, "builder": True},
                model=ARTHUR_V2_MODEL,
                # Arthur's control-plane turns can need a tool call followed by
                # a user-facing answer.  2,048 tokens was repeatedly exhausted
                # mid-turn, leaving no final message to deliver.
                max_tokens=ARTHUR_V2_TOOL_AGENT_MAX_TOKENS,
                created_by_type="system",
                is_deleted=False,
            )
            db.add(agent)
        else:
            agent.description = "Partner untuk membentuk dan mengelola AI assistant personal maupun bisnis."
            agent.instructions = build_arthur_v2_system_prompt()
            agent.model = ARTHUR_V2_MODEL
            # Apply the increased budget to installations that already have
            # Arthur; seed is deliberately idempotent and is run on release.
            agent.max_tokens = ARTHUR_V2_TOOL_AGENT_MAX_TOKENS
            agent.is_deleted = False
            agent.capabilities = sorted(set(agent.capabilities or []) | {"builder"})
            agent.tools_config = {**dict(agent.tools_config or {}), "system_plugin": ARTHUR_V2_PLUGIN, "builder": True}
        await db.commit()
        await db.refresh(agent)
        return agent


if __name__ == "__main__":
    created = asyncio.run(seed())
    print(f"Arthur V2 ready: {created.id}")
