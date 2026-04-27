import asyncio
from app.database import get_db
from app.models.agent import Agent
from app.models.session import Session
from sqlalchemy import select

async def main():
    async for db in get_db():
        res = await db.execute(select(Agent).limit(1))
        agent = res.scalar_one_or_none()
        print("Agent ID:", agent.id)
        if agent.escalation_config:
            print("Escalation config:", agent.escalation_config)
        else:
            print("No escalation config")
        break

asyncio.run(main())
