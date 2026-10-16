"""Tokyo8 incident: reading memory must never become a WhatsApp media action."""
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.engine.agent_followups import (
    _extract_shared_workspace_file_from_steps,
    _needs_whatsapp_file_delivery_followup,
)
from app.core.engine.agent_step_utils import _has_whatsapp_media_send_step
from test_arthur_p1_operations import database, agent_row, conversation, OWNER
from app.models.session import Session
from app.models.memory import Memory
from sqlalchemy import select


@pytest.mark.parametrize("result", [
    "[MEDIA_SOURCE_UNAVAILABLE] Tidak ada gambar tervalidasi untuk dikirim pada percakapan ini.",
    "Tidak terkirim karena sumber tidak tersedia.",
    "[error] [IMAGE_SENT] status lama, kirim baru gagal",
    "File akan dikirim setelah konfirmasi.",
])
def test_only_verified_media_receipt_proves_delivery(result):
    assert not _has_whatsapp_media_send_step([{"tool": "send_whatsapp_image", "result": result}])


@pytest.mark.parametrize("tool", ["inspect_assistant_memory", "read_assistant_knowledge", "recall"])
def test_reading_stored_file_reference_never_creates_or_delivers_artifact(tool):
    steps = [{"tool": tool, "result": json.dumps({"ok": True, "total": 7, "memories": [
        {"key": "active_context", "value_preview": "Lampiran CV: /workspace/shared/current_input/image.jpg"},
    ]})}]
    reply = 'Ada 7 memori pada nomor ini. Balas "ya" untuk reset.'
    assert _extract_shared_workspace_file_from_steps(steps, reply) is None
    assert _needs_whatsapp_file_delivery_followup("hapus memori sesi saya di bot Tokyo8", {}, steps, reply) == (False, None)


def test_failed_drive_upload_is_not_a_generated_whatsapp_deliverable():
    steps = [{"tool": "create_drive_file", "args": {"fileUrl": "file:///workspace/shared/image.jpg"},
              "result": "Error calling tool: Path does not exist: /workspace/shared/image.jpg"}]
    assert _needs_whatsapp_file_delivery_followup("[Gambar] CV saya", {}, steps, "Upload belum berhasil.") == (False, None)


def test_drive_upload_receipt_counts_as_recovered_artifact_but_error_does_not():
    from app.core.engine.google_mcp_support import _has_google_workspace_artifact_step
    receipt = "Successfully created file 'CV.jpg' (ID: verified-id). Link: https://drive.google.com/file/d/verified-id/view"
    assert _has_google_workspace_artifact_step([{"tool": "create_drive_file", "result": receipt}])
    assert not _has_google_workspace_artifact_step([{"tool": "create_drive_file", "result": "Error calling tool: " + receipt}])


@pytest.mark.asyncio
async def test_missing_drive_resource_does_not_invent_sheet_requirement_or_recovery_attempt():
    from app.core.engine.agent_google_routing import _route_google_workspace_blocker_to_owner_if_customer
    agent = SimpleNamespace(name="Tokyo8", owner_external_id=OWNER, operator_ids=[OWNER], tools_config={}, capabilities=[])
    session = SimpleNamespace(channel_type="whatsapp", external_user_id=OWNER, channel_config={"phone_number": OWNER})
    reply = await _route_google_workspace_blocker_to_owner_if_customer(
        reply="Belum selesai", session=session, agent_model=agent, user_message="CV saya",
        error_text="File not found: invalid-folder", auth_url=None, log=MagicMock(),
    )
    assert "belum selesai" in reply
    assert "Sheet" not in reply and "pengganti otomatis" not in reply


@pytest.mark.asyncio
async def test_rejected_delivery_cannot_borrow_previous_success_receipt():
    from app.core.engine.agent_runner import _deliver_shared_whatsapp_file_via_tool

    tool = SimpleNamespace(name="send_whatsapp_image", ainvoke=AsyncMock(return_value="[MEDIA_SOURCE_UNAVAILABLE] Tidak ada gambar untuk dikirim pada percakapan ini."))
    parsed = {"steps": [{"tool": "send_whatsapp_image", "result": "[IMAGE_SENT] Gambar lama dikirim"}], "db_messages": []}
    sent, reply = await _deliver_shared_whatsapp_file_via_tool(
        tools=[tool], shared_path="/workspace/shared/current_input/image.jpg", parsed=parsed,
        session_id=uuid.uuid4(), run_id=uuid.uuid4(), step_index=1, log=MagicMock(),
    )
    assert sent is False
    assert "sudah saya kirim" not in reply


@pytest.mark.asyncio
async def test_customer_reset_clears_attachment_context_and_preserves_other_agents(database, monkeypatch):
    from arthur_v2.plugin import build_arthur_v2_tools
    from app.core.domain.agent_revision_service import ADMIN_SCOPE

    monkeypatch.setenv("CHANNEL_SECRET_KEY", "isolated-reset-archive-key")
    target = await agent_row(database)
    arthur = await agent_row(database, tools_config={"builder": True})
    session, _ = await conversation(database, target)
    other_session, _ = await conversation(database, arthur)
    stale = {"current_attachment": {"filename": "cv.jpg"}, "last_incoming_media": {},
             "latest_shared_artifact": {"path": "/workspace/shared/current_input/cv.jpg"},
             "shared_artifacts": [], "context_summary": "Old CV", "keep": "unchanged"}
    async with database() as db:
        for sid in (session.id, other_session.id):
            s = await db.get(Session, sid)
            s.external_user_id = OWNER
            s.metadata_ = stale.copy()
        db.add_all([Memory(agent_id=target.id, scope=OWNER, key="longterm", value_data="Customer CV"),
                    Memory(agent_id=arthur.id, scope=OWNER, key="longterm", value_data="Arthur setup"),
                    Memory(agent_id=target.id, scope="6287777777777", key="longterm", value_data="Other applicant")])
        await db.commit()
    tools = {t.name: t for t in build_arthur_v2_tools(db_factory=database, owner_phone=OWNER, self_agent_id=str(arthur.id))}
    result = await tools["reset_assistant_context"].ainvoke({"agent_id": str(target.id), "scope": OWNER, "expected_count": 1, "confirmed": True})
    assert result["ok"] and result["sessions_reset"] == 1
    async with database() as db:
        reset = await db.get(Session, session.id)
        assert set(reset.metadata_) == {"keep", "context_reset_at"}
        assert (await db.get(Session, other_session.id)).metadata_ == stale
        rows = (await db.execute(select(Memory).where(Memory.scope != ADMIN_SCOPE))).scalars().all()
        assert {(m.agent_id, m.value_data) for m in rows} == {(arthur.id, "Arthur setup"), (target.id, "Other applicant")}
