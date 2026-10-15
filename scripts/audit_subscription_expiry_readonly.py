"""Read-only counts for reviewing expiry enforcement before enabling a cohort.

Run with the application's DATABASE_URL. No subscription is updated. User IDs
are printed only when --include-user-ids is explicitly supplied.
"""
import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.database import AsyncSessionLocal
from app.models.subscription import UserSubscription


async def audit(include_user_ids: bool = False):
    now = datetime.now(timezone.utc)
    counts = Counter()
    affected = []
    async with AsyncSessionLocal() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        result = await db.execute(select(
            UserSubscription.user_id, UserSubscription.status,
            UserSubscription.expires_at, UserSubscription.grace_until,
        ))
        for user_id, status, expires_at, grace_until in result:
            counts["total"] += 1
            if status not in ("trial", "active", "grace_period"):
                continue
            counts["legacy_usable"] += 1
            deadline = (grace_until or expires_at) if status == "grace_period" else expires_at
            if deadline is None:
                counts["no_deadline"] += 1
                continue
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if now >= deadline:
                counts["past_deadline_but_legacy_usable"] += 1
                counts[f"past_deadline_{status}"] += 1
                if include_user_ids:
                    affected.append(str(user_id))
        await db.rollback()
    report = {"observed_at": now.isoformat(), "read_only": True, "counts": dict(counts)}
    if include_user_ids:
        report["affected_user_ids"] = affected
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-user-ids", action="store_true")
    asyncio.run(audit(parser.parse_args().include_user_ids))
