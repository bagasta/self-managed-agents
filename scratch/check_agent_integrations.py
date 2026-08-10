import sys
sys.path.insert(0, ".")
import asyncio
import json
import sqlalchemy as sa

import app.database as db
from app.models.agent import Agent
from app.models.session import Session
from app.models.conversation_summary import ConversationSummary

async def main():
    async with db.AsyncSessionLocal() as session:
        # 1. Check Agents
        res_agents = await session.execute(sa.select(Agent))
        agents = res_agents.scalars().all()
        print(f"=== AGENTS ({len(agents)}) ===")
        for a in agents:
            tc = a.tools_config or {}
            print(f"Agent ID   : {a.id}")
            print(f"Name       : {a.name}")
            print(f"Integrations: {json.dumps(tc.get('integration_status', {}), indent=2)}")
            print(f"MCP Servers: {json.dumps(tc.get('mcp', {}), indent=2)}")
            print("-" * 50)
            
        # 2. Check Recent Sessions & Summaries
        res_sessions = await session.execute(sa.select(Session).order_by(Session.updated_at.desc()).limit(5))
        sessions = res_sessions.scalars().all()
        print(f"\n=== RECENT SESSIONS ({len(sessions)}) ===")
        for s in sessions:
            print(f"Session ID   : {s.id}")
            print(f"Agent ID     : {s.agent_id}")
            print(f"Channel Type : {s.channel_type}")
            
            # Fetch active summary for this session
            res_sum = await session.execute(
                sa.select(ConversationSummary).where(
                    ConversationSummary.session_id == s.id,
                    ConversationSummary.is_active == True
                )
            )
            summary = res_sum.scalar_one_or_none()
            if summary:
                print(f"Active Summary ID: {summary.id}")
                print(f"Summary text (first 300 chars):\n{summary.summary_text[:300]}...")
            else:
                print("No active summary for this session.")
            print("-" * 50)

if __name__ == "__main__":
    asyncio.run(main())
