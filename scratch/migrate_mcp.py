import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import select, update
from app.database import AsyncSessionLocal
from app.models.agent import Agent

async def migrate():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent))
        agents = result.scalars().all()
        updated = 0
        for agent in agents:
            if isinstance(agent.tools_config, dict):
                mcp = agent.tools_config.get("mcp", {})
                if isinstance(mcp, dict) and "servers" in mcp:
                    servers = mcp.get("servers", {})
                    if "google_workspace" in servers:
                        # Migrate to new dynamic MCP format
                        print(f"Migrating agent: {agent.name}")
                        new_tools_config = dict(agent.tools_config)
                        new_tools_config["mcp"] = {"enabled": True}
                        agent.tools_config = new_tools_config
                        updated += 1
        if updated > 0:
            await db.commit()
            print(f"Migrated {updated} agents.")
        else:
            print("No agents needed migration.")

if __name__ == "__main__":
    asyncio.run(migrate())
