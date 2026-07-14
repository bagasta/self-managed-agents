from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.model_defaults import CREATED_AGENT_DEFAULT_MODEL


def _arthur():
    return SimpleNamespace(capabilities=["builder", "system"], tools_config={"builder": True})


@pytest.mark.asyncio
async def test_arthur_admission_bounds_thirty_concurrent_users(monkeypatch) -> None:
    from app.config import get_settings
    from app.core.engine.arthur_admission import arthur_run_slot, reset_arthur_admission_for_tests

    settings = get_settings()
    monkeypatch.setattr(settings, "arthur_max_concurrent_runs", 4)
    monkeypatch.setattr(settings, "arthur_max_queued_runs", 64)
    await reset_arthur_admission_for_tests()

    active = 0
    max_active = 0
    completed = 0

    async def run_one() -> None:
        nonlocal active, max_active, completed
        async with arthur_run_slot(_arthur()):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            completed += 1

    await asyncio.gather(*(run_one() for _ in range(30)))

    assert max_active == 4
    assert completed == 30
    await reset_arthur_admission_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "setting_name", "limit"),
    [
        ("buat agent baru untuk customer service", "arthur_max_concurrent_builder_runs", 6),
        ("tolong tampilkan daftar agent saya", "arthur_max_concurrent_fast_runs", 12),
    ],
)
async def test_arthur_admission_separates_fast_and_builder_lanes(
    monkeypatch,
    message: str,
    setting_name: str,
    limit: int,
) -> None:
    from app.config import get_settings
    from app.core.engine.arthur_admission import arthur_run_slot, reset_arthur_admission_for_tests

    settings = get_settings()
    monkeypatch.setattr(settings, setting_name, limit)
    monkeypatch.setattr(settings, "arthur_max_queued_runs", 64)
    await reset_arthur_admission_for_tests()

    active = 0
    max_active = 0

    async def run_one() -> None:
        nonlocal active, max_active
        async with arthur_run_slot(_arthur(), message):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(run_one() for _ in range(30)))

    assert max_active == limit
    await reset_arthur_admission_for_tests()


def test_created_agent_default_model_is_consistent() -> None:
    import inspect

    from app.core.engine import agent_runner
    from app.core.tools.builder_catalog import AGENT_PRESETS, _DEFAULT_MODEL
    from app.core.domain.subscription_service import DEFAULT_SUBSCRIPTION_PLANS

    assert CREATED_AGENT_DEFAULT_MODEL == "deepseek/deepseek-v4-flash"
    assert _DEFAULT_MODEL == CREATED_AGENT_DEFAULT_MODEL
    assert all(
        preset["default_model"] == CREATED_AGENT_DEFAULT_MODEL
        for preset in AGENT_PRESETS.values()
    )
    trial_and_starter = [
        plan for plan in DEFAULT_SUBSCRIPTION_PLANS if plan["code"] in {"trial", "tier_1"}
    ]
    assert all(
        CREATED_AGENT_DEFAULT_MODEL in plan["allowed_models"]
        for plan in trial_and_starter
    )
    assert "Authoritative Platform Model Default" in inspect.getsource(agent_runner.run_agent)
