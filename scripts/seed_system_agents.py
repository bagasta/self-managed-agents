"""
seed_system_agents.py — Seed system sub-agents into the DB.

Reads from _SYSTEM_SUBAGENTS (the canonical definitions) and upserts
Agent rows with is_system=True. This allows admins to edit system agents
via API without needing code deploys.

Usage:
    python -m scripts.seed_system_agents
    # or via make:
    make seed-agents
"""
from __future__ import annotations

import asyncio
import sys
import os

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.agent import Agent
from app.core.engine.subagent_builder import _SYSTEM_SUBAGENTS


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        seeded = 0
        for spec in _SYSTEM_SUBAGENTS:
            name = spec["name"]

            # Check if already exists
            existing = (
                await db.execute(
                    select(Agent).where(
                        Agent.name == name,
                        Agent.capabilities.contains(["system"]),
                    )
                )
            ).scalar_one_or_none()

            if existing:
                # Update in place
                existing.instructions = spec["system_prompt"]
                existing.model = spec["model"]
                existing.tools_config = spec.get("tools_config", {})
                existing.description = spec.get("description", "")
                if existing.capabilities is None:
                    existing.capabilities = ["system", "subagent"]
                elif "subagent" not in existing.capabilities:
                    existing.capabilities = list(existing.capabilities) + ["subagent"]
                if not existing.created_by_type:
                    existing.created_by_type = "system"
                    existing.created_by_agent_name = "System"
                print(f"  ✅ Updated: {name}")
            else:
                agent = Agent(
                    name=name,
                    instructions=spec["system_prompt"],
                    model=spec["model"],
                    tools_config=spec.get("tools_config", {}),
                    description=spec.get("description", ""),
                    capabilities=["system", "subagent"],
                    created_by_type="system",
                    created_by_agent_name="System",
                )
                db.add(agent)
                print(f"  ✅ Created: {name}")
            seeded += 1

        await db.commit()
        print(f"\n🎯 Seeded {seeded} system agents.")


if __name__ == "__main__":
    asyncio.run(seed())
