"""Regression: plan_agent must not push 'upgrade Tier 2' when the user is at
their agent-count limit but actually wants to MODIFY an existing agent.

Incident (2026-06-05): a Trial user (1/1 agents) asked Arthur to fix escalation
on their existing agent. Arthur called plan_agent, whose create-entitlement check
returned allowed=false (would exceed max_agents), so Arthur told the user to
upgrade to Tier 2 — even though updating an existing agent has no count limit
and escalation isn't tier-gated. plan_agent's next_action must steer Arthur to
update_agent on an agent-COUNT block instead of an upgrade pitch.
"""
import json

import pytest

from app.core.tools.builder_planning_tools import build_builder_planning_tools


async def _preview_count_block(**kwargs):
    return {
        "checked": True,
        "allowed": False,
        "reason": "Kamu sudah punya 1 agent aktif. Plan Trial maksimal 1 agent. Upgrade ke Tier 2 untuk bisa membuat lebih banyak agent.",
        "plan": "Trial",
        "agents_used": 1,
        "max_agents": 1,
    }


async def _preview_expired_block(**kwargs):
    return {"checked": True, "allowed": False, "reason": "Subscription kamu sudah expired.", "plan": "Trial"}


@pytest.mark.asyncio
async def test_agent_count_block_steers_to_update_agent():
    tools = build_builder_planning_tools(preview_agent_creation_entitlement=_preview_count_block)
    raw = await tools["plan_agent"].ainvoke(
        {
            "user_goal": "tambahkan notifikasi ke admin saat pelanggan minta jemput atau kirim bukti transfer",
            "agent_name": "Admin Laundry Kiloan",
            "channel": "whatsapp",
        }
    )
    data = json.loads(raw)
    assert data["plan_status"] == "blocked_by_subscription"
    assert "update_agent" in data["next_action"].lower()


@pytest.mark.asyncio
async def test_non_count_block_does_not_claim_update_fixes_it():
    tools = build_builder_planning_tools(preview_agent_creation_entitlement=_preview_expired_block)
    raw = await tools["plan_agent"].ainvoke({"user_goal": "buat agent CS toko", "channel": "whatsapp"})
    data = json.loads(raw)
    assert data["plan_status"] == "blocked_by_subscription"
    # An expired subscription is not solved by update_agent.
    assert "update_agent" not in data["next_action"].lower()
