"""Opt-in, turn-bound approval receipts for administrative mutations."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.core.utils.phone_utils import normalize_phone
from app.models.message import Message
from app.models.session import Session

_KEY = "arthur_reviewed_change"
_ACTIONS = {
    "create_assistant", "update_assistant", "delete_assistant", "add_assistant_knowledge",
    "configure_assistant_runtime", "configure_assistant_access", "edit_assistant_memory",
    "reset_assistant_context", "rollback_assistant_revision", "set_assistant_paused",
    "configure_assistant_autonomous_run", "stop_assistant_automation",
    "disconnect_assistant_whatsapp", "transfer_assistant_whatsapp",
    "set_assistant_google_spreadsheet", "test_assistant_google_tools",
    "recover_assistant_run", "manage_assistant_google_connection",
    "send_to_number",
}
_APPROVALS = {"ya", "iya", "yes", "ok", "oke", "setuju", "ya, setuju", "ya setuju",
              "ya, terapkan", "terapkan", "ya, buat", "buat", "ya, impor", "ya, hapus",
              "ya, rollback", "ya, advance", "ya, normal", "konfirmasi"}


def _payload(kwargs, schema=None):
    if hasattr(schema, "model_validate"):
        kwargs = schema.model_validate(kwargs).model_dump()
    def encode(value):
        if hasattr(value, "model_dump"):
            return value.model_dump()
        raise TypeError("Unsupported approval payload")
    return json.loads(json.dumps({k: v for k, v in kwargs.items() if k != "confirmed"},
                                 default=encode, sort_keys=True, ensure_ascii=False))


def apply_confirmation_gate(tools, *, db_factory, session_id, owner_phone, self_agent_id, only=None):
    """Disabled by default; rollout changes no existing Owner's approvals."""
    settings = get_settings()
    reviewed_owners = {normalize_phone(p) for p in getattr(settings, "arthur_confirmation_owner_ids", [])}
    reviewed_owners.discard("")
    if normalize_phone(owner_phone or "") not in reviewed_owners:
        return tools

    def wrap(original, name, schema):
        async def invoke(**kwargs):
            if name != "send_to_number" and not kwargs.get("confirmed"):
                return await original(**kwargs)
            if name == "test_assistant_google_tools" and kwargs.get("mode", "read") == "read":
                return await original(**kwargs)
            if name == "manage_assistant_google_connection" and kwargs.get("action", "status") == "status":
                return await original(**kwargs)
            payload = _payload(kwargs, schema)
            digest = hashlib.sha256(json.dumps([name, payload], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            now = datetime.now(timezone.utc)
            try:
                session_uuid = uuid.UUID(str(session_id))
            except (ValueError, TypeError):
                return {"ok": False, "error": "Konteks persetujuan tidak tersedia."}
            async with db_factory() as db:
                session = (await db.execute(select(Session).where(Session.id == session_uuid).with_for_update())).scalar_one_or_none()
                if session is None or str(session.agent_id) != str(self_agent_id):
                    return {"ok": False, "error": "Konteks persetujuan tidak tersedia."}
                cfg = session.channel_config or {}
                identities = {normalize_phone(str(v or "")) for v in (
                    session.external_user_id, cfg.get("phone_number"), cfg.get("user_phone"))}
                if normalize_phone(owner_phone or "") not in identities:
                    return {"ok": False, "error": "Identitas persetujuan tidak cocok."}
                turns = (await db.execute(select(Message).where(
                    Message.session_id == session.id, Message.role == "user",
                ).order_by(Message.timestamp.desc(), Message.id.desc()).limit(2))).scalars().all()
                if not turns:
                    return {"ok": False, "error": "Pesan persetujuan belum tersedia."}
                latest = turns[0]
                state = (session.metadata_ or {}).get(_KEY) or {}
                same = state.get("digest") == digest
                if state.get("status") == "executing":
                    return {"ok": False, "outcome_uncertain": True,
                            "error": "Aksi sebelumnya sedang diproses atau hasilnya belum terverifikasi. Periksa status aktual sebelum membuat perubahan lain."}
                if same and state.get("approved_message_id") == str(latest.id):
                    if state.get("status") == "applied":
                        return {"ok": True, "already_applied": True, **state.get("receipt", {})}
                    return {"ok": False, "outcome_uncertain": True,
                            "error": "Aksi ini sudah dicoba. Periksa status aktual; jangan ulangi mutasi otomatis."}
                text = (latest.content or "").rsplit("Pesan:", 1)[-1].strip().casefold().rstrip(".!")
                fresh = bool(state.get("expires_at") and now < datetime.fromisoformat(state["expires_at"]))
                adjacent = len(turns) == 2 and state.get("prepared_message_id") == str(turns[1].id)
                if not (same and fresh and adjacent and text in _APPROVALS and state.get("status") == "prepared"):
                    state = {"digest": digest, "tool": name, "status": "prepared",
                             "prepared_message_id": str(latest.id),
                             "expires_at": (now + timedelta(minutes=10)).isoformat()}
                    session.metadata_ = {**(session.metadata_ or {}), _KEY: state}
                    await db.commit()
                    return {"ok": False, "needs_confirmation": True, "action": name,
                            "review": payload,
                            "message": "Tampilkan perubahan konkret ini kepada Owner dan tunggu persetujuan pada pesan berikutnya. Belum ada mutasi yang dijalankan."}
                state = {**state, "status": "executing", "approved_message_id": str(latest.id)}
                session.metadata_ = {**(session.metadata_ or {}), _KEY: state}
                await db.commit()
            try:
                result = await original(**kwargs)
            except BaseException:
                # The executing receipt is retained: a timeout/crash is never
                # interpreted as permission to repeat an external side effect.
                raise
            receipt = {key: result[key] for key in ("agent_id", "deleted_agent_id", "version") if isinstance(result, dict) and key in result}
            if isinstance(result, dict) and isinstance(result.get("assistant"), dict):
                receipt["assistant"] = {key: result["assistant"][key] for key in ("id", "name", "version") if key in result["assistant"]}
            async with db_factory() as db:
                session = (await db.execute(select(Session).where(Session.id == session_uuid).with_for_update())).scalar_one()
                current = (session.metadata_ or {}).get(_KEY) or {}
                if (current.get("digest"), current.get("approved_message_id")) != (digest, state["approved_message_id"]):
                    return result
                succeeded = (isinstance(result, dict) and result.get("ok")) or (
                    isinstance(result, str) and result.startswith("[SENT_TO_NUMBER:"))
                session.metadata_ = {**(session.metadata_ or {}), _KEY: {
                    **state, "status": "applied" if succeeded else "not_verified",
                    "receipt": receipt}}
                await db.commit()
            return result
        return invoke

    for tool in tools:
        if tool.name in (only if only is not None else _ACTIONS) and tool.coroutine:
            tool.coroutine = wrap(tool.coroutine, tool.name, tool.args_schema)
    return tools
