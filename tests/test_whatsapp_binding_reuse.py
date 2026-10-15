from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.api.meta_signup import _commit_phone_binding, _release_deleted_phone_binding


@pytest.mark.asyncio
async def test_deleted_binding_is_released_before_reassignment():
    previous = SimpleNamespace(is_deleted=True, version=2, wa_device_id=None,
                               wa_phone_number_id="phone", wa_access_token_encrypted="enc:old")
    db = SimpleNamespace(execute=AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: previous)),
                         flush=AsyncMock())
    await _release_deleted_phone_binding(db, SimpleNamespace(id="new"), "phone")
    assert previous.wa_phone_number_id is None
    assert previous.wa_access_token_encrypted is None
    assert previous.version == 3
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_active_binding_is_preserved():
    previous = SimpleNamespace(is_deleted=False, wa_phone_number_id="phone")
    db = SimpleNamespace(execute=AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: previous)), flush=AsyncMock())
    with pytest.raises(HTTPException) as caught:
        await _release_deleted_phone_binding(db, SimpleNamespace(id="new"), "phone")
    assert caught.value.status_code == 409
    assert previous.wa_phone_number_id == "phone"
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_phone_claim_returns_conflict():
    db = SimpleNamespace(commit=AsyncMock(side_effect=IntegrityError("update", {}, Exception("ix_agents_wa_phone_number_id"))), rollback=AsyncMock())
    with pytest.raises(HTTPException) as caught:
        await _commit_phone_binding(db)
    assert caught.value.status_code == 409
    db.rollback.assert_awaited_once()
