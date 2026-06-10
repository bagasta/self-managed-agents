"""
Tests for deterministic context/attachment binding across turns.

Covers the two production symptoms audited in
docs/audit-context-binding-2026-06-10.md:

- Symptom 1: stale agent error replies replayed as history.
- Symptom 2: old uploaded file used instead of the newest attachment.

Run with: pytest tests/test_context_binding.py -v
"""
from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


class _Log:
    """Minimal structlog-compatible logger stub."""

    def warning(self, *a, **k): ...
    def debug(self, *a, **k): ...
    def info(self, *a, **k): ...


def _row(role, content, *, tool_name=None, run_id=None):
    return SimpleNamespace(role=role, content=content, tool_name=tool_name, run_id=run_id)


# ===========================================================================
# FIX A — db_messages_to_lc elides stale attachment bodies from history
# ===========================================================================

class TestElideStaleAttachmentBody:
    def test_old_document_body_is_stripped_but_header_kept(self):
        from app.core.engine.context_service import db_messages_to_lc

        content = (
            "Tolong buat visualisasi\n"
            "[Dokumen diterima: titanic.txt, tersimpan di /workspace/titanic.txt]\n"
            "Isi dokumen:\n```\nname,age\nJack,20\nRose,17\n```"
        )
        out = db_messages_to_lc([_row("user", content)])

        assert len(out) == 1
        body = out[0].content
        # The heavy inline body must be gone (prevents stale-file context bleed)
        assert "Jack,20" not in body
        assert "name,age" not in body
        # The header (that a file was shared) is preserved
        assert "titanic.txt" in body
        # An explicit placeholder marks it as not the active attachment
        assert "disembunyikan" in body.lower()

    def test_message_without_document_body_is_unchanged(self):
        from app.core.engine.context_service import db_messages_to_lc

        out = db_messages_to_lc([_row("user", "halo bro apa kabar")])
        assert out[0].content == "halo bro apa kabar"


# ===========================================================================
# FIX B — build_input_messages binds the current attachment as sole source
# ===========================================================================

class TestCurrentAttachmentBinding:
    def test_attachment_name_injects_authority_system_message(self):
        from app.core.engine.agent_input import build_input_messages

        out = build_input_messages(
            prior_messages=[],
            history_rows=[],
            human_content="buatkan grafiknya",
            log=_Log(),
            current_attachment_name="dummy test.pdf",
        )
        sys_msgs = [m for m in out if isinstance(m, SystemMessage)]
        assert any(
            "dummy test.pdf" in m.content and "satu-satunya" in m.content.lower()
            for m in sys_msgs
        ), "must inject a binding directive naming the current attachment as sole source"
        # Current human message is still last
        assert isinstance(out[-1], HumanMessage)
        assert out[-1].content == "buatkan grafiknya"

    def test_no_attachment_adds_no_binding_directive(self):
        from app.core.engine.agent_input import build_input_messages

        out = build_input_messages(
            prior_messages=[],
            history_rows=[],
            human_content="halo",
            log=_Log(),
        )
        assert not any(
            isinstance(m, SystemMessage) and "satu-satunya" in m.content.lower()
            for m in out
        )


# ===========================================================================
# FIX C — delivery-status agent rows are not replayed as conversation history
# ===========================================================================

class TestDeliveryStatusQuarantine:
    def test_tagged_delivery_status_agent_row_is_excluded(self):
        from app.core.engine.context_service import (
            DELIVERY_STATUS_TAG,
            db_messages_to_lc,
        )

        rows = [
            _row("user", "kirim laporan ke 628xxx"),
            _row("agent", "Gagal mengirim laporan.xlsx lewat WhatsApp",
                 tool_name=DELIVERY_STATUS_TAG),
            _row("user", "hah? laporan apa"),
            _row("agent", "Maaf, bisa diperjelas?"),
        ]
        out = db_messages_to_lc(rows)
        joined = "\n".join(m.content for m in out)
        assert "Gagal mengirim laporan.xlsx" not in joined
        # Normal agent reply survives
        assert "bisa diperjelas" in joined

    def test_untagged_agent_row_is_kept(self):
        from app.core.engine.context_service import db_messages_to_lc

        out = db_messages_to_lc([_row("agent", "Halo, ada yang bisa dibantu?")])
        assert len(out) == 1
        assert isinstance(out[0], AIMessage)


# ===========================================================================
# FIX D — messages from dead (failed/abandoned/...) runs are quarantined,
#         for BOTH user and agent roles
# ===========================================================================

class TestDeadRunQuarantine:
    def test_agent_rows_from_dead_run_are_dropped(self):
        from app.core.engine.context_service import filter_dead_run_messages

        run_status = {"r1": "failed", "r2": "completed"}
        rows = [
            _row("user", "u-dead", run_id="r1"),
            _row("agent", "a-dead", run_id="r1"),
            _row("user", "u-live", run_id="r2"),
            _row("agent", "a-live", run_id="r2"),
        ]
        kept = filter_dead_run_messages(rows, run_status)
        contents = {r.content for r in kept}
        assert contents == {"u-live", "a-live"}

    def test_rows_without_run_id_are_kept(self):
        from app.core.engine.context_service import filter_dead_run_messages

        rows = [_row("agent", "no-run", run_id=None)]
        assert filter_dead_run_messages(rows, {}) == rows
