import asyncio
import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.core.domain.skill_service import publish_system_skill


def test_reactivating_immutable_skill_refreshes_live_bundle_metadata():
    content = "Stable immutable skill body."
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        checksum=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        enabled=False,
        bundle_version="old-bundle",
        publisher="old-publisher",
        published_at=None,
    )
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = existing
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[query_result, MagicMock()])
    db.flush = AsyncMock()

    result = asyncio.run(
        publish_system_skill(
            agent_id=uuid.uuid4(),
            name="arthur-discovery",
            description="Discovery",
            content_md=content,
            version="1.3.0",
            triggers=["create"],
            supported_states=["discovery"],
            allowed_tool_groups=["discovery"],
            bundle_version="arthur-skills-2026-07-28-v17",
            publisher="scripts/seed_arthur.py",
            db=db,
        )
    )

    assert result is existing
    assert existing.enabled is True
    assert existing.bundle_version == "arthur-skills-2026-07-28-v17"
    assert existing.publisher == "scripts/seed_arthur.py"
    assert existing.published_at is not None
    db.flush.assert_awaited_once()
