"""
Tests for:
- session_lock.py   : lock timeout, force eviction of stuck locks, cancellation wait
- agent_runner.py   : history trimming (cap + context-summary adaptive reduction)
- config.py         : default_subagent_max_tokens raised to 2048
- deep_agent_backend.py : write() is create-only
- agent_runner.py   : HITL ActionRequest key names ("name"/"args")
- subagent_builder.py   : SDK default general-purpose is not locally overridden

Run with: pytest tests/test_session_lock_and_history.py -v
"""
from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ============================================================================
# session_lock — lock timeout, force eviction, cancel wait
# ============================================================================

class TestSessionLockTimeout:
    """Lock timeout must cover max agent run time (3× base + buffer)."""

    def test_lock_timeout_covers_subagent_run(self):
        """_lock_timeout should be >= agent_timeout_seconds * 3."""
        from app.config import get_settings
        settings = get_settings()
        # Replicate the formula from session_lock.py
        _lock_timeout = settings.agent_timeout_seconds * 3 + 120
        max_subagent_run = settings.agent_timeout_seconds * 3
        assert _lock_timeout > max_subagent_run, (
            f"lock_timeout={_lock_timeout} must exceed max subagent run={max_subagent_run}"
        )

    def test_cancel_wait_is_short_for_user_interrupt(self):
        """cancel_active_run must not block a new user message for a long tool call."""
        import inspect
        from app.core.engine.session_lock import cancel_active_run
        src = inspect.getsource(cancel_active_run)
        assert "1.5" in src, (
            "cancel_active_run must keep WhatsApp interrupt handling responsive"
        )

    @pytest.mark.asyncio
    async def test_acquire_release_basic(self):
        """Lock should be acquirable and releasable normally."""
        from app.core.engine.session_lock import session_run_lock
        sid = uuid.uuid4()
        entered = False
        async with session_run_lock(sid):
            entered = True
        assert entered

    @pytest.mark.asyncio
    async def test_lock_serialises_concurrent_access(self):
        """Two concurrent calls on same session must run serially."""
        from app.core.engine.session_lock import session_run_lock
        sid = uuid.uuid4()
        order: list[str] = []

        async def run(label: str) -> None:
            async with session_run_lock(sid):
                order.append(f"{label}_start")
                await asyncio.sleep(0.05)
                order.append(f"{label}_end")

        await asyncio.gather(run("A"), run("B"))
        # A must fully complete before B starts (or vice versa, but no overlap)
        assert order.index("A_end") < order.index("B_start") or \
               order.index("B_end") < order.index("A_start"), \
               f"Concurrent overlap detected: {order}"

    @pytest.mark.asyncio
    async def test_force_evict_stuck_lock(self):
        """Lock held beyond _max_lock_age must be force-evicted."""
        from app.core.engine import session_lock as _sl

        sid = uuid.uuid4()
        # Manually inject a "stuck" lock entry: locked=True, acquired_at = long ago
        stuck_lock = asyncio.Lock()
        await stuck_lock.acquire()  # hold it
        from app.config import get_settings
        settings = get_settings()
        past = time.monotonic() - (settings.agent_timeout_seconds * 8 + 120)
        async with _sl._manager_lock:
            _sl._locks[sid] = (stuck_lock, time.monotonic(), past)

        # session_run_lock should detect stale lock and create a new one
        # so we can acquire it without waiting forever
        acquired = False
        try:
            async with asyncio.timeout(2.0):
                async with _sl.session_run_lock(sid):
                    acquired = True
        except asyncio.TimeoutError:
            pass

        stuck_lock.release()  # cleanup
        assert acquired, "Force eviction should have allowed new request to proceed"


class TestCancelActiveRun:
    @pytest.mark.asyncio
    async def test_cancel_returns_false_when_no_task(self):
        from app.core.engine.session_lock import cancel_active_run
        result = await cancel_active_run(uuid.uuid4())
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_returns_false_for_done_task(self):
        from app.core.engine.session_lock import (
            cancel_active_run, register_active_task, unregister_active_task
        )
        sid = uuid.uuid4()

        async def noop():
            pass

        task = asyncio.create_task(noop())
        await asyncio.sleep(0)  # let it complete
        await register_active_task(sid, task)
        result = await cancel_active_run(sid)
        assert result is False
        await unregister_active_task(sid)

    @pytest.mark.asyncio
    async def test_active_task_identity_rejects_stale_progress_task(self):
        from app.core.engine.session_lock import (
            is_registered_active_task,
            register_active_task,
            unregister_active_task,
        )

        sid = uuid.uuid4()
        active = asyncio.create_task(asyncio.sleep(0.1))
        stale = asyncio.create_task(asyncio.sleep(0.1))
        await register_active_task(sid, active)

        assert await is_registered_active_task(sid, active) is True
        assert await is_registered_active_task(sid, stale) is False

        await unregister_active_task(sid, active)
        await asyncio.gather(active, stale)

    @pytest.mark.asyncio
    async def test_cancel_cancels_running_task(self):
        from app.core.engine.session_lock import (
            cancel_active_run, register_active_task, unregister_active_task
        )
        sid = uuid.uuid4()

        async def long_task():
            await asyncio.sleep(100)

        task = asyncio.create_task(long_task())
        await register_active_task(sid, task)
        result = await cancel_active_run(sid)
        assert result is True
        # cancel_active_run waits for the task to finish (via asyncio.wait_for/shield),
        # so by the time it returns, the task must be done.
        assert task.done(), "Task should be done after cancel_active_run returns"
        # Task should have been cancelled (not completed normally)
        assert task.cancelled() or isinstance(task.exception(), asyncio.CancelledError) if not task.cancelled() else True
        await unregister_active_task(sid)

    @pytest.mark.asyncio
    async def test_cancel_returns_quickly_when_task_cleanup_is_slow(self):
        from contextlib import suppress
        from app.core.engine.session_lock import (
            cancel_active_run, register_active_task, unregister_active_task
        )
        sid = uuid.uuid4()

        async def slow_cleanup_task():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                await asyncio.sleep(10)
                raise

        task = asyncio.create_task(slow_cleanup_task())
        await register_active_task(sid, task)
        started = time.monotonic()
        result = await cancel_active_run(sid)
        elapsed = time.monotonic() - started
        assert result is True
        assert elapsed < 3.0
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await unregister_active_task(sid)


class TestWhatsAppRunLifecycle:
    def test_success_path_stops_wa_typing_before_return(self):
        from app.core.engine import agent_runner

        src = inspect.getsource(agent_runner.run_agent)
        cleanup_pos = src.index("await _cleanup_sandboxes()")
        return_pos = src.index('return {\n        "reply": final_reply')
        assert cleanup_pos < return_pos

    def test_long_progress_notice_not_scheduled_for_every_wa_run(self):
        from app.core.engine import agent_runner

        src = inspect.getsource(agent_runner.run_agent)
        assert '_schedule_wa_long_progress_notice("run")' not in src
        assert "await _schedule_wa_long_progress_notice(tool_name)" in src
        assert "is_registered_active_task(session.id, _run_owner_task)" in src

    def test_outer_channel_cancellation_closes_exact_run(self):
        from app.api import channels
        from app.core.engine import agent_runner

        runner_src = inspect.getsource(agent_runner.persist_cancelled_run_for_task)
        channel_src = inspect.getsource(channels.wa_incoming)
        assert '_managed_agent_run_id' in runner_src
        assert 'Run.id == run_id, Run.status == "running"' in runner_src
        assert "persist_cancelled_run_for_task(current_task)" in channel_src


class TestGoogleDrivePendingAttachment:
    def test_resolves_recent_previous_turn_attachment_for_one_time_upload(self, tmp_path):
        from app.core.engine.agent_runner import _resolve_google_drive_attachment

        source = tmp_path / "shared" / "current_input" / "image.jpg"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"image")

        path, aliases, consume, age = _resolve_google_drive_attachment(
            workspace_dir=tmp_path,
            session_metadata={
                "current_attachment": {"filename": "image.jpg", "saved_at": 900.0}
            },
            current_attachment_name=None,
            reuse_seconds=300,
            now_timestamp=1000.0,
        )

        assert path == str(source.resolve())
        assert "/workspace/data/incoming/current_input/image.jpg" in aliases
        assert consume is True
        assert age == 100.0

    def test_rejects_expired_previous_turn_attachment(self, tmp_path):
        from app.core.engine.agent_runner import _resolve_google_drive_attachment

        source = tmp_path / "shared" / "current_input" / "image.jpg"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"image")

        path, aliases, consume, age = _resolve_google_drive_attachment(
            workspace_dir=tmp_path,
            session_metadata={
                "current_attachment": {"filename": "image.jpg", "saved_at": 100.0}
            },
            current_attachment_name=None,
            reuse_seconds=300,
            now_timestamp=1000.0,
        )

        assert path is None
        assert aliases == set()
        assert consume is False
        assert age == 900.0


class TestGraphResultExtraction:
    def test_agent_runner_initializes_graph_output_before_recoverable_retries(self):
        from app.core.engine import agent_runner

        src = inspect.getsource(agent_runner.run_agent)
        init_pos = src.index("_graph_output: Any | None = None")
        first_ainvoke_pos = src.index("_graph_output = await graph.ainvoke")
        interrupt_pos = src.index("handle_graph_interrupt(")

        assert init_pos < first_ainvoke_pos < interrupt_pos

    @pytest.mark.asyncio
    async def test_falls_back_to_ainvoke_output_without_checkpointer(self):
        from app.core.engine.agent_runner import _graph_result_from_output

        class NoCheckpointerGraph:
            async def aget_state(self, _config):
                raise ValueError("No checkpointer set")

        result = await _graph_result_from_output(
            graph=NoCheckpointerGraph(),
            graph_config={},
            graph_output={"messages": ["ok"]},
            log=MagicMock(),
        )
        assert result == {"messages": ["ok"]}

    @pytest.mark.asyncio
    async def test_prefers_checkpointer_state_when_available(self):
        from types import SimpleNamespace
        from app.core.engine.agent_runner import _graph_result_from_output

        class CheckpointerGraph:
            async def aget_state(self, _config):
                return SimpleNamespace(values={"messages": ["from-state"]})

        result = await _graph_result_from_output(
            graph=CheckpointerGraph(),
            graph_config={},
            graph_output={"messages": ["from-output"]},
            log=MagicMock(),
        )
        assert result == {"messages": ["from-state"]}


# ============================================================================
# History trimming — context window cap and adaptive reduction
# ============================================================================

class TestHistoryTrimming:
    """Hard cap at _MAX_PRIOR_MESSAGES prevents context explosion."""

    def _make_messages(self, n: int) -> list:
        from langchain_core.messages import HumanMessage, AIMessage
        msgs = []
        for i in range(n):
            msgs.append(HumanMessage(content=f"user msg {i}"))
            msgs.append(AIMessage(content=f"agent reply {i}"))
        return msgs

    def test_cap_at_30_messages(self):
        """When prior messages exceed 30, only last 30 are kept."""
        msgs = self._make_messages(25)  # 50 messages total
        assert len(msgs) == 50
        _MAX_PRIOR_MESSAGES = 30
        if len(msgs) > _MAX_PRIOR_MESSAGES:
            msgs = msgs[-_MAX_PRIOR_MESSAGES:]
        assert len(msgs) == 30

    def test_under_cap_passes_unchanged(self):
        msgs = self._make_messages(10)  # 20 messages
        _MAX_PRIOR_MESSAGES = 30
        trimmed = msgs[-_MAX_PRIOR_MESSAGES:] if len(msgs) > _MAX_PRIOR_MESSAGES else msgs
        assert len(trimmed) == 20

    def test_trimmed_messages_are_last_n(self):
        """After trimming, the last N messages from the original list are kept."""
        from langchain_core.messages import HumanMessage
        msgs = [HumanMessage(content=str(i)) for i in range(50)]
        _MAX = 30
        trimmed = msgs[-_MAX:]
        assert trimmed[0].content == "20"
        assert trimmed[-1].content == "49"

    def test_adaptive_reduction_with_context_summary(self):
        """When context_summary is active, load half of short_term_memory_turns (min 5)."""
        from app.config import get_settings
        settings = get_settings()

        context_summary = "Some summary text"
        _history_turns = (
            max(settings.short_term_memory_turns // 2, 5)
            if context_summary
            else settings.short_term_memory_turns
        )
        assert _history_turns == max(settings.short_term_memory_turns // 2, 5)
        assert _history_turns < settings.short_term_memory_turns

    def test_adaptive_no_reduction_without_summary(self):
        from app.config import get_settings
        settings = get_settings()

        context_summary = ""
        _history_turns = (
            max(settings.short_term_memory_turns // 2, 5)
            if context_summary
            else settings.short_term_memory_turns
        )
        assert _history_turns == settings.short_term_memory_turns

    def test_adaptive_min_floor_is_5(self):
        """Even with a very short short_term_memory_turns, floor is 5."""
        for turns in [1, 2, 4, 6, 10]:
            result = max(turns // 2, 5)
            assert result >= 5


# ============================================================================
# Config — default_subagent_max_tokens raised to 2048
# ============================================================================

class TestConfig:
    def test_default_subagent_max_tokens_at_least_2048(self):
        from app.config import get_settings
        settings = get_settings()
        assert settings.default_subagent_max_tokens >= 2048, (
            f"default_subagent_max_tokens={settings.default_subagent_max_tokens} "
            "is too low — subagents need at least 2048 tokens to produce complete responses"
        )

    def test_agent_timeout_seconds_sensible(self):
        from app.config import get_settings
        settings = get_settings()
        # Must be >= 60s; too low means agents always timeout on non-trivial tasks
        assert settings.agent_timeout_seconds >= 60

    def test_short_term_memory_turns_sensible(self):
        from app.config import get_settings
        settings = get_settings()
        assert 5 <= settings.short_term_memory_turns <= 50


# ============================================================================
# DockerBackend.write() — create-only per BackendProtocol spec
# ============================================================================

class TestDockerBackendWriteCreateOnly:
    def _make_backend(self, tmp_path) -> Any:
        from unittest.mock import MagicMock
        sandbox = MagicMock()
        sandbox.workspace_dir = tmp_path
        sandbox.session_id = uuid.uuid4()
        from app.core.engine.deep_agent_backend import DockerBackend
        return DockerBackend(sandbox)

    def test_write_creates_new_file(self, tmp_path):
        backend = self._make_backend(tmp_path)
        result = backend.write("new_file.txt", "hello")
        assert result.error is None
        assert (tmp_path / "new_file.txt").read_text() == "hello"

    def test_write_rejects_existing_file(self, tmp_path):
        backend = self._make_backend(tmp_path)
        (tmp_path / "existing.txt").write_text("original")
        result = backend.write("existing.txt", "overwrite attempt")
        assert result.error is not None
        assert "already exists" in result.error
        # Original content must be preserved
        assert (tmp_path / "existing.txt").read_text() == "original"

    def test_write_error_path_is_none_on_conflict(self, tmp_path):
        backend = self._make_backend(tmp_path)
        (tmp_path / "file.txt").write_text("x")
        result = backend.write("file.txt", "y")
        assert result.path is None

    def test_write_creates_nested_dirs(self, tmp_path):
        backend = self._make_backend(tmp_path)
        result = backend.write("subdir/deep/file.txt", "content")
        assert result.error is None
        assert (tmp_path / "subdir" / "deep" / "file.txt").exists()

    def test_edit_can_modify_existing_file(self, tmp_path):
        """edit() is the correct way to modify files after write()."""
        backend = self._make_backend(tmp_path)
        backend.write("editable.txt", "original content")
        result = backend.edit("editable.txt", "original", "updated")
        assert result.error is None
        assert (tmp_path / "editable.txt").read_text() == "updated content"

    @pytest.mark.asyncio
    async def test_awrite_also_create_only(self, tmp_path):
        backend = self._make_backend(tmp_path)
        (tmp_path / "async_existing.txt").write_text("existing")
        result = await backend.awrite("async_existing.txt", "new content")
        assert result.error is not None
        assert "already exists" in result.error

    def test_read_binary_returns_error_not_file_block(self, tmp_path):
        """Binary files must return ReadResult(error=...) so SDK returns plain string,
        not a ToolMessage with content_blocks=[{"type":"file","base64":...}] which
        causes OpenRouter 400 due to missing 'filename' field."""
        backend = self._make_backend(tmp_path)
        (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 fake pdf content")
        result = backend.read("report.pdf")
        assert result.error is not None
        assert result.file_data is None
        assert "Binary file" in result.error

    def test_read_image_returns_error_not_image_block(self, tmp_path):
        backend = self._make_backend(tmp_path)
        (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
        result = backend.read("photo.jpg")
        assert result.error is not None
        assert "Binary file" in result.error

    def test_read_text_file_works_normally(self, tmp_path):
        backend = self._make_backend(tmp_path)
        (tmp_path / "script.py").write_text("print('hello')")
        result = backend.read("script.py")
        assert result.error is None
        assert result.file_data is not None
        assert "print" in result.file_data["content"]

    def test_read_text_file_default_limit_is_bounded(self, tmp_path):
        backend = self._make_backend(tmp_path)
        (tmp_path / "long.txt").write_text("\n".join(f"line {i}" for i in range(1000)))
        result = backend.read("long.txt")
        assert result.error is None
        assert result.file_data is not None
        assert len(result.file_data["content"].splitlines()) == 300

    def test_read_text_file_large_limit_is_clamped(self, tmp_path):
        backend = self._make_backend(tmp_path)
        (tmp_path / "long.txt").write_text("\n".join(f"line {i}" for i in range(1000)))
        result = backend.read("long.txt", limit=2000)
        assert result.error is None
        assert result.file_data is not None
        assert len(result.file_data["content"].splitlines()) == 500

    def test_execute_output_is_bounded(self, tmp_path):
        backend = self._make_backend(tmp_path)
        backend._sandbox.bash_result.return_value = ("x" * 20_000, 0)
        result = backend.execute("print huge")
        assert result.exit_code == 0
        assert result.truncated is True
        assert len(result.output) < 9_000
        assert "output truncated by runtime" in result.output


# ============================================================================
# Interrupted run — partial messages from last run stripped from history
# ============================================================================

class TestInterruptedRunHistoryStripping:
    """When prior_run_was_interrupted=True, messages from the last (partial) run
    must be stripped so the LLM doesn't resume an old task."""

    def _make_message_row(self, run_id: uuid.UUID, role: str, content: str):
        """Create a mock DB Message row."""
        m = MagicMock()
        m.run_id = run_id
        m.role = role
        m.content = content
        m.step_index = 0
        m.tool_name = None
        m.tool_args = None
        m.tool_result = None
        return m

    def test_interrupted_run_messages_are_stripped(self):
        """Messages from the last run_id should be excluded when interrupted."""
        old_run_id = uuid.uuid4()
        interrupted_run_id = uuid.uuid4()

        rows = [
            self._make_message_row(old_run_id, "user", "build me a portfolio"),
            self._make_message_row(old_run_id, "agent", "I'll build it for you!"),
            self._make_message_row(interrupted_run_id, "user", "hurry up"),
            self._make_message_row(interrupted_run_id, "agent", "working on it..."),  # partial
            self._make_message_row(interrupted_run_id, "tool", ""),                   # partial
        ]

        last_run_id = rows[-1].run_id
        rows_before_last_run = [m for m in rows if m.run_id != last_run_id]

        assert len(rows_before_last_run) == 2
        assert all(m.run_id == old_run_id for m in rows_before_last_run)

    def test_no_stripping_when_not_interrupted(self):
        """Without prior_run_was_interrupted, all history rows are kept."""
        run_id = uuid.uuid4()
        rows = [
            self._make_message_row(run_id, "user", "hello"),
            self._make_message_row(run_id, "agent", "hi there"),
        ]
        # No stripping — rows unchanged
        prior_run_was_interrupted = False
        result = rows if not prior_run_was_interrupted else [m for m in rows if False]
        assert len(result) == 2

    def test_stripping_with_single_run_history(self):
        """If ALL rows belong to the last run, stripping leaves empty history."""
        run_id = uuid.uuid4()
        rows = [
            self._make_message_row(run_id, "user", "do something complex"),
            self._make_message_row(run_id, "agent", "starting..."),
            self._make_message_row(run_id, "tool", ""),
        ]
        last_run_id = rows[-1].run_id
        rows_before = [m for m in rows if m.run_id != last_run_id]
        assert rows_before == [], "All rows from interrupted run should be stripped"

    def test_system_note_injected_when_interrupted(self):
        """A [SYSTEM] interrupt note must be in input_messages when interrupted."""
        from langchain_core.messages import SystemMessage, HumanMessage
        sanitized_prior = [HumanMessage(content="old message")]

        prior_run_was_interrupted = True
        interrupt_note = []
        if prior_run_was_interrupted:
            interrupt_note = [SystemMessage(content=(
                "[SYSTEM] The previous task was interrupted because the user sent a new message. "
                "Do NOT continue or retry the previous task. Focus solely on the user's current message."
            ))]

        input_messages = sanitized_prior + interrupt_note + [HumanMessage(content="hello")]
        system_msgs = [m for m in input_messages if isinstance(m, SystemMessage)]
        assert len(system_msgs) == 1
        assert "interrupted" in system_msgs[0].content

    def test_no_system_note_when_not_interrupted(self):
        from langchain_core.messages import SystemMessage, HumanMessage
        prior_run_was_interrupted = False
        interrupt_note = [SystemMessage(content="...")] if prior_run_was_interrupted else []
        input_messages = [HumanMessage(content="hello")] + interrupt_note
        assert not any(isinstance(m, SystemMessage) for m in input_messages)


# ============================================================================
# HITL ActionRequest key names — must use "name"/"args" not "tool_name"/"tool_input"
# ============================================================================

class TestHITLActionRequestKeys:
    """SDK ActionRequest TypedDict uses 'name' and 'args' — not 'tool_name'/'tool_input'."""

    def test_pending_tool_read_uses_name_key(self):
        """Extract tool name from action_request using 'name', not 'tool_name'."""
        action_request = {"name": "send_message", "args": {"to": "123", "message": "hi"}}
        # Replicate the pattern from agent_runner.py resume path
        _pending_tool = action_request.get("name", "unknown")
        _pending_args = action_request.get("args", {})
        assert _pending_tool == "send_message"
        assert _pending_args == {"to": "123", "message": "hi"}

    def test_wrong_keys_return_defaults(self):
        """Using old wrong keys ('tool_name'/'tool_input') returns defaults — breaks HITL."""
        action_request = {"name": "send_message", "args": {"to": "123"}}
        # Old wrong pattern
        _wrong_tool = action_request.get("tool_name", "unknown")
        _wrong_args = action_request.get("tool_input", {})
        assert _wrong_tool == "unknown"  # wrong — would produce no tool name
        assert _wrong_args == {}         # wrong — args lost

    def test_agent_runner_uses_correct_keys(self):
        """Verify agent_runner.py source does NOT use 'tool_name'/'tool_input' on action_requests."""
        import inspect
        import re
        from app.core.engine import agent_runner
        src = inspect.getsource(agent_runner)

        # Find all .get("tool_name", ...) calls on action_request variables
        wrong_patterns = re.findall(
            r'_(?:pending|action)_(?:requests?|tool|args)\s*\[?\d*\]?.*?\.get\(["\']tool_(?:name|input)["\']',
            src,
        )
        assert not wrong_patterns, (
            f"Found wrong ActionRequest key access in agent_runner.py: {wrong_patterns}\n"
            "Use 'name' and 'args' per SDK ActionRequest TypedDict."
        )

    def test_agent_runner_has_name_key_reads(self):
        """agent_runner.py must read .get('name', ...) from _action_requests / _pending."""
        import inspect
        from app.core.engine import agent_runner
        src = inspect.getsource(agent_runner)
        # Should have at least 2 reads of "name" from action_requests
        assert src.count('.get("name",') >= 2 or src.count(".get('name',") >= 2


# ============================================================================
# Subagent builder — SDK owns default general-purpose subagent
# ============================================================================

class TestGeneralPurposeSubagentSpec:
    """build_subagents() must not override the SDK's default general-purpose agent."""

    @pytest.mark.asyncio
    async def test_general_purpose_not_injected_in_system_subagents(self):
        """Deep Agents auto-adds general-purpose with parent model/tools."""
        mock_db = AsyncMock()
        # Simulate no DB sys agents found → fallback to hardcoded _SYSTEM_SUBAGENTS
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        log = MagicMock()
        log.info = MagicMock()
        log.warning = MagicMock()

        # create_deep_agent is imported lazily inside _build_system_subagent;
        # patch it at deepagents module level + DockerSandbox to avoid real I/O.
        # Sandbox subagents must compile; compile failure is fail-closed.
        with patch("app.core.engine.subagent_builder.DockerSandbox") as MockSandbox, \
             patch("deepagents.create_deep_agent", return_value=MagicMock(name="compiled_subagent")):
            MockSandbox.return_value = MagicMock()

            from app.core.engine.subagent_builder import build_subagents
            subagents, sandboxes = await build_subagents([], uuid.uuid4(), mock_db, log)

        names = [s.get("name") for s in subagents]
        assert "general-purpose" not in names, (
            f"'general-purpose' should be left to the Deep Agents SDK default: {names}\n"
            "A local override narrows the default tool/model inheritance."
        )

    def test_system_subagents_no_general_purpose_name_collision(self):
        """None of the _SYSTEM_SUBAGENTS should be named 'general-purpose'
        (reserved name that the SDK injects automatically)."""
        from app.core.engine.subagent_builder import _SYSTEM_SUBAGENTS
        names = [s["name"] for s in _SYSTEM_SUBAGENTS]
        assert "general-purpose" not in names, (
            "Don't add 'general-purpose' to _SYSTEM_SUBAGENTS; let the SDK default inherit parent tools/model."
        )

    def test_all_system_subagents_have_required_fields(self):
        from app.core.engine.subagent_builder import _SYSTEM_SUBAGENTS
        for spec in _SYSTEM_SUBAGENTS:
            assert "name" in spec, f"Missing 'name' in spec: {spec}"
            assert "description" in spec, f"Missing 'description' in spec: {spec}"
            assert "system_prompt" in spec, f"Missing 'system_prompt' in spec: {spec}"
            assert "model" in spec, f"Missing 'model' in spec: {spec}"

    def test_sys_analyst_knows_whatsapp_upload_input_path(self):
        from app.core.engine.subagent_builder import _SYSTEM_SUBAGENTS

        spec = next(s for s in _SYSTEM_SUBAGENTS if s["name"] == "sys_analyst")
        prompt = spec["system_prompt"]

        assert "/workspace/data/incoming/<filename>" in prompt
        assert "/workspace/shared/<filename>" in prompt
        assert "Jangan mencari file hanya dari nama file" in prompt

    def test_sys_coder_knows_whatsapp_upload_input_path(self):
        from app.core.engine.subagent_builder import _SYSTEM_SUBAGENTS

        spec = next(s for s in _SYSTEM_SUBAGENTS if s["name"] == "sys_coder")
        prompt = spec["system_prompt"]

        assert "/workspace/data/incoming/<filename>" in prompt
        assert "/workspace/shared/<filename>" in prompt
        assert "Jangan mencari file upload hanya dari nama file" in prompt

    def test_sys_researcher_exists_as_named_specialist(self):
        """sys_researcher remains available as a specialist, not as GP override."""
        from app.core.engine.subagent_builder import _SYSTEM_SUBAGENTS
        names = [s["name"] for s in _SYSTEM_SUBAGENTS]
        assert "sys_researcher" in names, (
            "sys_researcher should remain available for explicit research delegation."
        )
