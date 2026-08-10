import sys
sys.path.insert(0, ".")
import asyncio
import uuid
import app.database as db
import app.models.agent as ma
import sqlalchemy as sa

import app.models.message as mm
async def main():
    async with db.AsyncSessionLocal() as s:
        res = await s.execute(sa.select(mm.Message).where(mm.Message.session_id == uuid.UUID('90242acd-4d77-4aab-8577-b6c4a236a428')).order_by(mm.Message.step_index.desc()))
        rows = res.scalars().all()
        print('TOTAL MESSAGES:', len(rows))
        for r in rows[:10]:
            print(f"STEP {r.step_index} [{r.role}]: {r.content[:200] if r.content else ''}")

if __name__ == '__main__':
    asyncio.run(main())
