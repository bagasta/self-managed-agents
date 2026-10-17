"""Preview or enqueue unanswered credit-blocked requests for one agent."""
import argparse
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

from app.core.domain.ai_credit_recovery import backfill_credit_recovery
from app.database import AsyncSessionLocal


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", type=uuid.UUID, required=True)
    parser.add_argument("--since-hours", type=float, default=24)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    async with AsyncSessionLocal() as db:
        result = await backfill_credit_recovery(
            db, agent_id=args.agent_id,
            since=datetime.now(timezone.utc) - timedelta(hours=args.since_hours), apply=args.apply,
        )
    print(json.dumps({"dry_run": not args.apply, **result}))


if __name__ == "__main__":
    asyncio.run(main())
