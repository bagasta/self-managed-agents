import asyncio
from sqlalchemy import select
from app.database import async_session_maker
from app.models.message import Message

async def main():
    async with async_session_maker() as db:
        result = await db.execute(select(Message).order_by(Message.timestamp.desc()).limit(10))
        msgs = result.scalars().all()
        for m in reversed(msgs):
            content = (m.content or "")[:200].replace('\n', ' ')
            print(f"[{m.role}] {m.session_id}: {content}")

asyncio.run(main())
