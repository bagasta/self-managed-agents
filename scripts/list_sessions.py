import asyncio
from sqlalchemy import select
from app.database import async_session_maker
from app.models.session import Session

async def main():
    async with async_session_maker() as db:
        result = await db.execute(select(Session))
        sessions = result.scalars().all()
        for s in sessions:
            print(f"ID: {s.id}, Agent: {s.agent_id}, ExtUser: {s.external_user_id}, EscActive: {s.escalation_active}, ChnConfig: {s.channel_config}")

if __name__ == "__main__":
    asyncio.run(main())
