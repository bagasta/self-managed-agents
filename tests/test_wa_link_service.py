"""Tests for wa_link_service — dashboard ↔ WhatsApp account linking."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.domain.wa_link_service import (
    _CODE_ALPHABET,
    _CODE_LENGTH,
    claim_wa_link_code,
    generate_wa_link_code,
    normalize_wa_link_code,
)


class _FakeResult:
    def __init__(self, *, scalar=None, scalars_list=None, first=None):
        self._scalar = scalar
        self._scalars_list = scalars_list or []
        self._first = first

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        items = self._scalars_list
        return SimpleNamespace(all=lambda: items, first=lambda: items[0] if items else None)

    def first(self):
        return self._first


class _FakeDb:
    def __init__(self, results):
        self._results = list(results)
        self.added = []

    async def execute(self, _query):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def _dashboard_user(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        email="bagas@clevio.co",
        full_name="Bagas",
        phone_number=None,
        wa_lid=None,
        external_id="dashboard-bagas",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _link_row(code="ABC234", *, minutes_left=10, used_at=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        code=code,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=minutes_left),
        used_at=used_at,
        claimed_identity=None,
        created_at=datetime.now(timezone.utc),
    )


def test_normalize_wa_link_code_strips_prefix_and_junk() -> None:
    assert normalize_wa_link_code(" link-abc 234 ") == "ABC234"
    assert normalize_wa_link_code("AbC234") == "ABC234"


@pytest.mark.asyncio
async def test_generate_wa_link_code_expires_pending_codes() -> None:
    user = _dashboard_user()
    pending = _link_row(minutes_left=10)
    db = _FakeDb([
        _FakeResult(scalar=user),
        _FakeResult(scalars_list=[pending]),
    ])

    link = await generate_wa_link_code(user.id, db)

    assert len(link.code) == _CODE_LENGTH
    assert all(ch in _CODE_ALPHABET for ch in link.code)
    assert pending.expires_at <= datetime.now(timezone.utc)
    assert link in db.added


@pytest.mark.asyncio
async def test_generate_wa_link_code_unknown_user_raises() -> None:
    db = _FakeDb([_FakeResult(scalar=None)])
    with pytest.raises(ValueError):
        await generate_wa_link_code(uuid.uuid4(), db)


@pytest.mark.asyncio
async def test_claim_links_sender_and_archives_placeholder_trial_user() -> None:
    link = _link_row("ABC234")
    dashboard = _dashboard_user(id=link.user_id)
    placeholder = SimpleNamespace(
        id=uuid.uuid4(),
        email="62895626765423@wa.placeholder",
        external_id="62895626765423",
        phone_number=None,
    )
    trial_sub = SimpleNamespace(status="trial")
    ent_plan = SimpleNamespace(code="enterprise", label="Enterprise")
    ent_sub = SimpleNamespace(status="active")

    db = _FakeDb([
        _FakeResult(scalars_list=[link]),          # link lookup
        _FakeResult(scalar=dashboard),             # dashboard user
        _FakeResult(scalars_list=[placeholder]),   # duplicates
        _FakeResult(scalars_list=[trial_sub]),     # placeholder subs
        _FakeResult(first=(ent_sub, ent_plan)),    # dashboard sub+plan
    ])

    result = await claim_wa_link_code(
        "link-abc234", db, sender_ids=["62895626765423@s.whatsapp.net", "62895626765423"]
    )

    assert result["success"] is True
    assert result["plan_code"] == "enterprise"
    assert dashboard.phone_number == "62895626765423"
    assert link.used_at is not None
    assert link.claimed_identity == "62895626765423"
    assert trial_sub.status == "merged"
    assert placeholder.external_id.startswith("merged:")
    assert placeholder.phone_number is None
    assert result["archived_duplicate_users"] == [str(placeholder.id)]


@pytest.mark.asyncio
async def test_claim_rejects_expired_code() -> None:
    link = _link_row("ABC234", minutes_left=-1)
    db = _FakeDb([_FakeResult(scalars_list=[link])])

    result = await claim_wa_link_code("ABC234", db, sender_ids=["628123456789"])

    assert "kedaluwarsa" in result["error"].lower()
    assert link.used_at is None


@pytest.mark.asyncio
async def test_claim_rejects_unknown_code() -> None:
    db = _FakeDb([_FakeResult(scalars_list=[])])
    result = await claim_wa_link_code("ZZZZZZ", db, sender_ids=["628123456789"])
    assert "tidak ditemukan" in result["error"].lower()


@pytest.mark.asyncio
async def test_claim_requires_sender_identity() -> None:
    db = _FakeDb([])
    result = await claim_wa_link_code("ABC234", db, sender_ids=[None, ""])
    assert "identitas" in result["error"].lower()


@pytest.mark.asyncio
async def test_claim_lid_only_sender_never_touches_phone_number() -> None:
    lid = "74350933852232"
    link = _link_row("ABC234")
    dashboard = _dashboard_user(id=link.user_id)
    db = _FakeDb([
        _FakeResult(scalars_list=[link]),
        _FakeResult(scalar=dashboard),
        _FakeResult(scalars_list=[]),   # no duplicates
        _FakeResult(first=None),        # no subscription row
    ])

    # Same digits arrive both with and without @lid suffix — must be LID.
    result = await claim_wa_link_code("ABC234", db, sender_ids=[f"+{lid}", f"{lid}@lid"])

    assert result["success"] is True
    assert dashboard.phone_number is None
    assert dashboard.wa_lid == lid
    assert result["linked_lid"] == lid
    assert result["linked_phone"] is None


@pytest.mark.asyncio
async def test_claim_links_both_real_phone_and_lid_to_their_own_fields() -> None:
    link = _link_row("ABC234")
    dashboard = _dashboard_user(id=link.user_id)
    db = _FakeDb([
        _FakeResult(scalars_list=[link]),
        _FakeResult(scalar=dashboard),
        _FakeResult(scalars_list=[]),
        _FakeResult(first=None),
    ])

    result = await claim_wa_link_code(
        "ABC234",
        db,
        sender_ids=["62895626765423@s.whatsapp.net", "74350933852232@lid"],
    )

    assert result["success"] is True
    assert dashboard.phone_number == "62895626765423"
    assert dashboard.wa_lid == "74350933852232"


@pytest.mark.asyncio
async def test_resolve_phone_for_wa_lid_returns_learned_phone() -> None:
    from app.core.domain.subscription_service import resolve_phone_for_wa_lid

    user = SimpleNamespace(phone_number="62895626765423", wa_lid="74350933852232")
    db = _FakeDb([_FakeResult(scalars_list=[user])])

    assert await resolve_phone_for_wa_lid("74350933852232", db) == "62895626765423"


@pytest.mark.asyncio
async def test_resolve_phone_for_wa_lid_unknown_returns_none() -> None:
    from app.core.domain.subscription_service import resolve_phone_for_wa_lid

    db = _FakeDb([_FakeResult(scalars_list=[])])
    assert await resolve_phone_for_wa_lid("74350933852232", db) is None
