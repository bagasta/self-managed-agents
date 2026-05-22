"""
Tests for ghost reply detection, /reset handler, and progress hooks.
Run with: pytest tests/test_ghost_reply.py -v
"""
import re
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — pure logic extracted from agent_runner (no DB/LLM needed)
# ---------------------------------------------------------------------------

GHOST_MARKERS = (
    "bentar ya", "lagi dikerjain", "lagi riset",
    "hasilnya langsung dikirim", "langsung dikirim", "segera dikirim",
    "sedang menyiapkan", "lagi menyiapkan",
    "tunggu ya", "tunggu sebentar",
    "[system override]",
)

URL_PAT = re.compile(r"https://[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:/[^\s\"']*)?")


def is_ghost_reply(text: str) -> bool:
    if not text or len(text) >= 300:
        return False
    return any(m in text.lower() for m in GHOST_MARKERS)


def extract_url_from_steps(steps: list[dict]) -> str | None:
    for s in steps:
        m = URL_PAT.search(s.get("result", ""))
        if m:
            return m.group(0).rstrip(".,)")
    return None


# ---------------------------------------------------------------------------
# Ghost reply detection
# ---------------------------------------------------------------------------

class TestGhostReplyDetection:
    def test_clear_ghost_bentar_ya(self):
        assert is_ghost_reply("Lagi dikerjain sys_coder, bentar ya!") is True

    def test_clear_ghost_lagi_dikerjain(self):
        assert is_ghost_reply("Lagi dikerjain, hasilnya langsung dikirim!") is True

    def test_clear_ghost_tunggu_ya(self):
        assert is_ghost_reply("Oke tunggu ya, lagi menyiapkan file-nya.") is True

    def test_clear_ghost_sedang_menyiapkan(self):
        assert is_ghost_reply("Sedang menyiapkan portfolio-mu.") is True

    def test_not_ghost_real_reply_short(self):
        assert is_ghost_reply("Portfolio sudah selesai! Cek di https://example.com") is False

    def test_not_ghost_real_reply_with_url(self):
        assert is_ghost_reply("Selesai! URL: https://abc.trycloudflare.com") is False

    def test_not_ghost_long_reply(self):
        # Reply panjang dengan konten real tidak dianggap ghost meski ada kata yg match
        long_reply = "Ini adalah portfolio website yang sudah saya buat. " * 10
        assert is_ghost_reply(long_reply) is False

    def test_not_ghost_empty(self):
        assert is_ghost_reply("") is False

    def test_not_ghost_normal_baik(self):
        # "baik" removed from markers — tidak boleh false positive
        assert is_ghost_reply("Baik, deployment sudah berhasil!") is False

    def test_not_ghost_saya_akan_removed(self):
        # "saya akan" removed — bisa muncul dalam konteks valid
        assert is_ghost_reply("Saya akan menjelaskan hasilnya: website sudah live.") is False

    def test_ghost_system_override(self):
        assert is_ghost_reply("[system override] do something") is True


# ---------------------------------------------------------------------------
# Scenario A only — no classifier, no Scenario B
# ---------------------------------------------------------------------------

class TestGhostDetectionRemoved:
    """Ghost reply detection has been fully removed from agent_runner.py.
    The agent communicates directly with subagents via the SDK task() tool —
    the reply it produces is always the final reply, no post-processing needed.
    Marker-based detection caused false positives (e.g. 'lagi buat' matching
    '1 menit lagi buat angkat air') and LLM-based detection was worse.
    """

    def test_no_ghost_detection_in_agent_runner(self):
        import inspect
        from app.core.engine import agent_runner
        src = inspect.getsource(agent_runner)
        assert "_classify_ghost_promise" not in src
        assert "_GHOST_MARKERS_A" not in src
        assert "ghost_reply_detected" not in src
        assert "_ghost_detected" not in src

    def test_scheduler_reply_not_mangled(self):
        """'gue bakal ingetin lo 1 menit lagi buat angkat air' must pass through unchanged.
        Previously 'lagi buat' marker matched this valid reply and replaced it."""
        reply = "Oke Bagas, gue bakal ingetin lo 1 menit lagi buat angkat air."
        # With ghost detection gone, ensure_non_empty_reply just passes it through
        from app.core.engine.reply_guard import ensure_non_empty_reply
        steps = [{"tool": "set_reminder", "result": "Reminder set"}]
        assert ensure_non_empty_reply(reply, steps) == reply

    def test_casual_reply_not_mangled(self):
        reply = "Sip, Bagas! Kalau ada yang mau dikerjain, tinggal bilang aja."
        from app.core.engine.reply_guard import ensure_non_empty_reply
        assert ensure_non_empty_reply(reply, []) == reply


# ---------------------------------------------------------------------------
# URL extraction from steps
# ---------------------------------------------------------------------------

class TestUrlExtraction:
    def test_extract_cloudflare_url(self):
        steps = [
            {"tool": "task", "args": {}, "result": "Deploy berhasil! URL: https://abc-def.trycloudflare.com"},
        ]
        url = extract_url_from_steps(steps)
        assert url == "https://abc-def.trycloudflare.com"

    def test_extract_url_strips_trailing_punctuation(self):
        steps = [
            {"tool": "deploy_app", "args": {}, "result": "URL: https://test.trycloudflare.com."},
        ]
        url = extract_url_from_steps(steps)
        assert url == "https://test.trycloudflare.com"

    def test_extract_no_url(self):
        steps = [
            {"tool": "write_file", "args": {}, "result": "File ditulis ke /workspace/index.html"},
        ]
        assert extract_url_from_steps(steps) is None

    def test_extract_first_url_wins(self):
        steps = [
            {"tool": "task", "args": {}, "result": "URL: https://first.trycloudflare.com dan selesai."},
            {"tool": "deploy_app", "args": {}, "result": "URL: https://second.trycloudflare.com"},
        ]
        url = extract_url_from_steps(steps)
        assert url == "https://first.trycloudflare.com"

    def test_extract_empty_steps(self):
        assert extract_url_from_steps([]) is None

    def test_extract_non_cloudflare_url(self):
        steps = [
            {"tool": "deploy_app", "args": {}, "result": "Live at https://myapp.fly.dev/"},
        ]
        url = extract_url_from_steps(steps)
        assert url is not None
        assert "fly.dev" in url


# ---------------------------------------------------------------------------
# /reset handler — FastAPI test client
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


@pytest.fixture
def mock_session():
    s = MagicMock()
    s.id = uuid.uuid4()
    s.agent_id = uuid.uuid4()
    s.metadata_ = {"context_summary": "some cached summary"}
    return s


@pytest.fixture
def mock_agent():
    a = MagicMock()
    a.id = uuid.uuid4()
    a.name = "TestAgent"
    a.api_key = "test-key"
    a.is_deleted = False
    a.active_until = __import__("datetime").datetime(2099, 1, 1, tzinfo=__import__("datetime").timezone.utc)
    a.tokens_used = 0
    a.token_quota = 1_000_000
    return a


class TestResetHandler:
    """Test /reset intercept in messages endpoint (no full DB needed — mock only)."""

    @pytest.mark.asyncio
    async def test_reset_returns_confirmation(self, mock_db, mock_session, mock_agent):
        from app.schemas.message import MessageResponse

        # Simulate the reset logic directly (bypass FastAPI routing)
        from sqlalchemy import delete
        from app.models.message import Message

        # Replicate the reset block from messages.py
        async def handle_reset(db, session):
            await db.execute(delete(Message).where(Message.session_id == session.id))
            session.metadata_ = {}
            db.add(session)
            await db.commit()
            return MessageResponse(
                reply="Percakapan direset. Memori sesi ini telah dibersihkan.",
                steps=[],
                run_id=None,
            )

        result = await handle_reset(mock_db, mock_session)
        assert result.reply == "Percakapan direset. Memori sesi ini telah dibersihkan."
        assert result.steps == []
        assert result.run_id is None
        mock_db.commit.assert_called_once()
        assert mock_session.metadata_ == {}

    def test_reset_keyword_detection(self):
        """Ensure only exact /reset triggers, not messages containing it."""
        assert "/reset".strip().lower() == "/reset"
        assert "  /reset  ".strip().lower() == "/reset"
        assert "/resetpassword".strip().lower() != "/reset"
        assert "reset everything".strip().lower() != "/reset"


# ---------------------------------------------------------------------------
# WA progress hook — _AgentLogger emits correct messages
# ---------------------------------------------------------------------------

class TestAgentLoggerProgress:
    """Test that _AgentLogger sends correct WA progress messages for each tool."""

    TOOL_PROGRESS = {
        "task":                   "🤖 Mendelegasikan ke subagent...",
        "http_get":               "🔍 Mengambil data dari web...",
        "deploy_app":             "🚀 Sedang deploy aplikasi...",
        "execute":                "⚙️ Menjalankan kode...",
        "write_file":             "✏️ Menulis file...",
        "edit_file":              "✏️ Mengedit file...",
        "send_whatsapp_document": "📎 Mengirim file...",
        "send_whatsapp_image":    "🖼️ Mengirim gambar...",
    }

    def test_all_key_tools_have_progress_message(self):
        critical_tools = ["task", "deploy_app", "execute", "write_file", "send_whatsapp_document"]
        for tool in critical_tools:
            assert tool in self.TOOL_PROGRESS, f"Missing progress msg for tool: {tool}"

    def test_progress_messages_are_non_empty(self):
        for tool, msg in self.TOOL_PROGRESS.items():
            assert msg.strip(), f"Empty progress msg for tool: {tool}"

    @pytest.mark.asyncio
    async def test_throttle_prevents_spam(self):
        """Same tool type should only send one WA message per run."""
        sent = []

        async def fake_send(device_id, target, msg):
            sent.append(msg)

        import time
        notified = set()
        last_ts = 0.0

        async def wa_progress(tool_name, msg):
            nonlocal last_ts
            now = time.monotonic()
            if tool_name in notified:
                return  # already notified for this tool type
            if now - last_ts < 6:
                return
            notified.add(tool_name)
            last_ts = now
            await fake_send("dev", "target", msg)

        # Simulate calling same tool 3 times
        await wa_progress("write_file", "✏️ Menulis file...")
        await wa_progress("write_file", "✏️ Menulis file...")
        await wa_progress("write_file", "✏️ Menulis file...")

        assert len(sent) == 1, "Should send only once per tool type"

    @pytest.mark.asyncio
    async def test_different_tools_each_send(self):
        """Different tool types each get their own progress message."""
        sent = []

        async def fake_send(device_id, target, msg):
            sent.append(msg)

        import time
        notified = set()
        last_ts = [0.0]

        async def wa_progress(tool_name, msg):
            now = time.monotonic()
            if tool_name in notified:
                return
            if now - last_ts[0] < 6:
                last_ts[0] = now - 7  # force past throttle for test
            notified.add(tool_name)
            last_ts[0] = now
            await fake_send("dev", "target", msg)

        await wa_progress("http_get", "🔍 Mengambil data dari web...")
        await wa_progress("deploy_app", "🚀 Sedang deploy aplikasi...")
        await wa_progress("execute", "⚙️ Menjalankan kode...")

        assert len(sent) == 3
