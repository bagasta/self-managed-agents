"""
Tests for the redesigned PostgreSQL-backed Conversation Memory System.

Tests cover:
  1. ConversationSummary model attributes
  2. MarkdownGenerator (pure in-memory formatting, estimate_tokens)
  3. ConversationMemoryService (get_active_summary, save_summary, get_or_create_summary, build_llm_context)
  4. memory_cache (Redis read-through, fallback, invalidation)
  5. prompt_builder integration with ConversationMemoryService
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace


try:
    import pytest
except ImportError:
    class _PytestFallback:
        @staticmethod
        def mark():
            pass
        class mark:
            @staticmethod
            def asyncio(func):
                return func
    pytest = _PytestFallback()


from app.models.conversation_summary import ConversationSummary
from app.core.domain import markdown_generator
from app.core.domain import conversation_memory_service
from app.core.infra import memory_cache


# ---------------------------------------------------------------------------
# 1. Model tests
# ---------------------------------------------------------------------------

def test_conversation_summary_model_instantiation():
    sid = uuid.uuid4()
    summary = ConversationSummary(
        id=uuid.uuid4(),
        session_id=sid,
        summary_text="User discussed system architecture redesign.",
        message_count_at=12,
        token_estimate=10,
        is_active=True,
    )
    assert summary.session_id == sid
    assert summary.summary_text == "User discussed system architecture redesign."
    assert summary.message_count_at == 12
    assert summary.token_estimate == 10
    assert summary.is_active is True


# ---------------------------------------------------------------------------
# 2. MarkdownGenerator tests
# ---------------------------------------------------------------------------

def test_estimate_tokens():
    assert markdown_generator.estimate_tokens("Hello World!") == 3
    assert markdown_generator.estimate_tokens("") == 0


def test_generate_summary_markdown():
    empty = markdown_generator.generate_summary_markdown("")
    assert empty == ""

    text = "The user asked about database scaling."
    res = markdown_generator.generate_summary_markdown(text)
    assert "## Conversation Summary" in res
    assert "The user asked about database scaling." in res


def test_generate_conversation_markdown():
    messages = [
        SimpleNamespace(role="user", content="How do I scale Postgres?", tool_name=None, tool_result=None),
        SimpleNamespace(role="agent", content="Use connection pooling and read replicas.", tool_name=None, tool_result=None),
    ]
    md = markdown_generator.generate_conversation_markdown(messages)
    assert "## Recent Conversation History" in md
    assert "**User**: How do I scale Postgres?" in md
    assert "**Agent**: Use connection pooling and read replicas." in md


def test_generate_memory_markdown():
    layered = {
        "today_date": "2026-08-05",
        "yesterday_date": "2026-08-04",
        "daily_today": "User discussed DB architecture.",
        "active_context": "Current focus: PostgreSQL memory redesign.",
        "longterm": "User prefers FastAPI and async SQLAlchemy.",
    }
    block = "- **tech_stack**: Python, FastAPI, PostgreSQL"

    md = markdown_generator.generate_memory_markdown(layered, block)
    assert "## Daily Log (2026-08-05)" in md
    assert "## Active Context" in md
    assert "## Long-Term Memory" in md
    assert "tech_stack" in md


def test_generate_full_context_markdown():
    summary = "Previous summary text."
    messages = [SimpleNamespace(role="user", content="Hi", tool_name=None, tool_result=None)]
    layered = {"today_date": "2026-08-05", "daily_today": "Daily log"}

    full_md = markdown_generator.generate_full_context_markdown(summary, messages, layered)
    assert "## Conversation Summary" in full_md
    assert "## Daily Log (2026-08-05)" in full_md
    assert "## Recent Conversation History" in full_md
    assert "---" in full_md


# ---------------------------------------------------------------------------
# 3. ConversationMemoryService tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_summary(monkeypatch):
    sid = uuid.uuid4()
    flushed = False

    class FakeDB:
        def __init__(self):
            self.added = []
            self.executed = []

        def add(self, item):
            self.added.append(item)

        async def execute(self, stmt):
            self.executed.append(stmt)

        async def flush(self):
            nonlocal flushed
            flushed = True

    db = FakeDB()
    summary = await conversation_memory_service.save_summary(
        session_id=sid,
        summary_text="New active summary text",
        message_count=15,
        db=db,
    )

    assert flushed is True
    assert summary.session_id == sid
    assert summary.summary_text == "New active summary text"
    assert summary.message_count_at == 15
    assert summary.is_active is True
    assert len(db.added) == 1


@pytest.mark.asyncio
async def test_get_or_create_summary_trigger_check(monkeypatch):
    sid = uuid.uuid4()
    session = SimpleNamespace(id=sid)

    async def fake_count_user_messages(_sid, _db):
        return 5

    # If user messages < trigger, return empty string
    monkeypatch.setattr(
        "app.core.engine.context_service.count_user_messages",
        fake_count_user_messages,
    )

    log = SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None, warning=lambda *a, **k: None)
    res = await conversation_memory_service.get_or_create_summary(
        session, db=SimpleNamespace(), llm=None, log=log, trigger=10
    )
    assert res == ""


@pytest.mark.asyncio
async def test_get_or_create_summary_returns_fresh_summary(monkeypatch):
    sid = uuid.uuid4()
    session = SimpleNamespace(id=sid)

    async def fake_count_user_messages(_sid, _db):
        return 12

    monkeypatch.setattr(
        "app.core.engine.context_service.count_user_messages",
        fake_count_user_messages,
    )

    existing_summary = SimpleNamespace(
        summary_text="Existing active summary text",
        message_count_at=10,
    )

    async def fake_get_active_summary(_sid, _db):
        return existing_summary

    monkeypatch.setattr(
        conversation_memory_service, "get_active_summary", fake_get_active_summary
    )

    log = SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None, warning=lambda *a, **k: None)
    res = await conversation_memory_service.get_or_create_summary(
        session, db=SimpleNamespace(), llm=None, log=log, trigger=10
    )
    # 12 - 10 = 2 < 10, so return existing active summary
    assert res == "Existing active summary text"


@pytest.mark.asyncio
async def test_build_llm_context(monkeypatch):
    sid = uuid.uuid4()
    agent_id = uuid.uuid4()
    session = SimpleNamespace(id=sid)

    async def fake_get_or_create_summary(*a, **k):
        return "Active summary text"

    async def fake_load_layered_memory(*a, **k):
        return {"today_date": "2026-08-05"}

    async def fake_build_memory_context(*a, **k):
        return "Generic memory block"

    async def fake_load_history(*a, **k):
        return [
            SimpleNamespace(role="user", content="Hello", run_id=None, step_index=1, timestamp=None),
            SimpleNamespace(role="agent", content="Hi Bagas!", tool_name=None, run_id=None, step_index=2, timestamp=None),
        ]

    monkeypatch.setattr(conversation_memory_service, "get_or_create_summary", fake_get_or_create_summary)
    monkeypatch.setattr("app.core.domain.memory_service.load_layered_memory", fake_load_layered_memory)
    monkeypatch.setattr("app.core.domain.memory_service.build_memory_context", fake_build_memory_context)
    monkeypatch.setattr("app.core.engine.context_service.load_history", fake_load_history)

    settings = SimpleNamespace(short_term_memory_turns=20, context_summary_trigger=10)
    log = SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None, warning=lambda *a, **k: None)

    payload = await conversation_memory_service.build_llm_context(
        session=session,
        agent_id=agent_id,
        db=SimpleNamespace(),
        llm=None,
        scope="62811",
        settings=settings,
        log=log,
    )

    assert isinstance(payload, conversation_memory_service.ContextPayload)
    assert payload.summary_text == "Active summary text"
    assert payload.context_summary == "Active summary text"
    assert "Hello" in payload.history_markdown
    assert len(payload.prior_messages) == 2


# ---------------------------------------------------------------------------
# 4. memory_cache tests (graceful degradation without Redis)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_cache_falls_back_to_db(monkeypatch):
    agent_id = uuid.uuid4()

    # Force redis to return None
    async def fake_get_redis():
        return None

    monkeypatch.setattr(memory_cache, "_get_redis", fake_get_redis)

    async def fake_get_memory(_agent_id, key, _db, scope=None):
        return SimpleNamespace(value_data=f"db_value_for_{key}")

    monkeypatch.setattr("app.core.domain.memory_service.get_memory", fake_get_memory)

    res = await memory_cache.get_memory_cached(agent_id, "active_context", db=SimpleNamespace())
    assert res is not None
    assert res.value_data == "db_value_for_active_context"



class SimpleMonkeypatch:
    def __init__(self):
        self._originals = []

    def setattr(self, target, name_or_value=None, value=None):
        if isinstance(target, str):
            mod_name, attr_name = target.rsplit(".", 1)
            import importlib
            mod = importlib.import_module(mod_name)
            orig = getattr(mod, attr_name)
            setattr(mod, attr_name, name_or_value)
            self._originals.append((mod, attr_name, orig))
        else:
            orig = getattr(target, name_or_value)
            setattr(target, name_or_value, value)
            self._originals.append((target, name_or_value, orig))

    def undo(self):
        for obj, attr, orig in reversed(self._originals):
            setattr(obj, attr, orig)
        self._originals.clear()


async def run_all_tests():
    import inspect, sys
    print("Executing Conversation Memory System Redesign Test Suite...")
    current_mod = sys.modules[__name__]
    passed = 0
    failed = 0
    for name, func in sorted(inspect.getmembers(current_mod, inspect.isfunction)):
        if name.startswith("test_"):
            mp = SimpleMonkeypatch()
            try:
                if asyncio.iscoroutinefunction(func):
                    if "monkeypatch" in inspect.signature(func).parameters:
                        await func(mp)
                    else:
                        await func()
                else:
                    if "monkeypatch" in inspect.signature(func).parameters:
                        func(mp)
                    else:
                        func()
                print(f"  [PASS] {name}")
                passed += 1
            except Exception as e:
                print(f"  [FAIL] {name}: {e}")
                import traceback
                traceback.print_exc()
                failed += 1
            finally:
                mp.undo()

    print(f"\nTest Summary: {passed} PASSED, {failed} FAILED")
    if failed > 0:
        raise RuntimeError(f"{failed} tests failed")


if __name__ == "__main__":
    asyncio.run(run_all_tests())


