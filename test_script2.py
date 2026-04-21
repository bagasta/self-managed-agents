import asyncio
from app.database import get_db
from app.models.agent import Agent
from sqlalchemy import select

async def main():
    async for db in get_db():
        res = await db.execute(select(Agent))
        agents = res.scalars().all()
        for a in agents:
            print("Agent:", a.name, "WaDeviceID:", a.wa_device_id, "OpPhone:", a.escalation_config.get("operator_phone") if a.escalation_config else "None")

asyncio.run(main())
