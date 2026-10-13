"""Reconcile persisted Google Workspace policies for existing managed agents.

Dry-run is the default. Pass ``--apply`` to persist canonical operations,
OAuth scopes, and the minimum output budget for Google-enabled agents.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.config import get_settings
from app.core.engine.google_mcp_support import reconcile_google_mcp_tools_config
from app.database import AsyncSessionLocal
from app.models.agent import Agent


def _google_server(config: dict[str, Any]) -> dict[str, Any] | None:
    mcp = config.get("mcp")
    if not isinstance(mcp, dict):
        return None
    servers = mcp.get("servers", mcp)
    if not isinstance(servers, dict):
        return None
    google = servers.get("google_workspace")
    return google if isinstance(google, dict) else None


async def run(*, apply: bool) -> dict[str, Any]:
    minimum_tokens = int(get_settings().google_agent_max_tokens)
    changed: list[dict[str, Any]] = []
    async with AsyncSessionLocal() as db:
        agents = (await db.execute(select(Agent))).scalars().all()
        for agent in agents:
            current = agent.tools_config if isinstance(agent.tools_config, dict) else {}
            current_google = _google_server(current)
            if not current_google or not current_google.get("allowed_services"):
                continue
            reconciled = reconcile_google_mcp_tools_config(
                current,
                requirement_text="\n".join(
                    (str(agent.description or ""), str(agent.instructions or ""))
                ),
            )
            next_google = _google_server(reconciled) or {}
            old_scopes = list(current_google.get("oauth_scopes") or [])
            new_scopes = list(next_google.get("oauth_scopes") or [])
            config_changed = reconciled != current
            token_changed = int(agent.max_tokens or 0) < minimum_tokens
            if not config_changed and not token_changed:
                continue
            if old_scopes != new_scopes:
                statuses = dict(reconciled.get("integration_status") or {})
                statuses["google_workspace"] = "auth_required"
                reconciled["integration_status"] = statuses
            changed.append(
                {
                    "agent_id": str(agent.id),
                    "name": agent.name,
                    "operations_before": current_google.get("allowed_operations") or {},
                    "operations_after": next_google.get("allowed_operations") or {},
                    "scopes_changed": old_scopes != new_scopes,
                    "max_tokens_before": agent.max_tokens,
                    "max_tokens_after": max(int(agent.max_tokens or 0), minimum_tokens),
                }
            )
            if apply:
                agent.tools_config = reconciled
                agent.max_tokens = max(int(agent.max_tokens or 0), minimum_tokens)
                agent.version += 1
                flag_modified(agent, "tools_config")
        if apply:
            await db.commit()
    return {"mode": "apply" if apply else "dry_run", "changed_count": len(changed), "agents": changed}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(apply=args.apply)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
