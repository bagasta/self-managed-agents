"""Spam-window detector + reset behaviour (in-memory path, redis disabled).

Regression: after an operator re-enables AI, the next customer message was
immediately flagged as spam again because the sliding window was never cleared.
"""
from __future__ import annotations

import asyncio

import pytest

from app.api import wa_helpers


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    async def _none():
        return None

    monkeypatch.setattr("app.core.infra.redis_client.get_redis", _none)
    wa_helpers._mem_spam_windows.clear()
    yield
    wa_helpers._mem_spam_windows.clear()


def _check(agent_id="a1", session_id="s1", sender_id="628111"):
    return _run(
        wa_helpers.check_wa_spam_window(
            agent_id=agent_id,
            session_id=session_id,
            sender_id=sender_id,
            limit=5,
            window_seconds=60,
        )
    )


def test_window_flags_spam_after_limit():
    results = [_check() for _ in range(6)]
    # First 5 allowed, 6th (count 6 > limit 5) is spam.
    assert results[4][0] is False
    assert results[5][0] is True


def test_reset_clears_window_so_next_message_not_spam():
    for _ in range(6):
        _check()
    assert _check()[0] is True  # still spamming before reset

    _run(
        wa_helpers.reset_wa_spam_window(
            agent_id="a1", session_id="s1", sender_id="628111"
        )
    )

    is_spam, count = _check()
    assert is_spam is False
    assert count == 1
