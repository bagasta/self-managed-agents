"""Owner-reviewed, deterministic Sheets recovery; never replay an entire run.

Plans/receipts live in existing Run.runtime_metadata, requiring no migration.
Each write compares live data to its prepared preimage. Retrying an interrupted
recovery first verifies the desired data, rather than repeating an append.
"""
from __future__ import annotations

import copy
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.models.message import Message
from app.models.run import Run
from app.models.session import Session
from .operations_common import (
    OperationRejected, canonical_values, configured_sheet, google_tools, owned_agent,
    range_shape, read_range, require_idle, write_allowed, write_range,
)


class RecoverySheetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    range_name: str
    values: list[list[str]] = Field(min_length=1, max_length=100)


def check_updates(updates):
    if not updates or len(updates) > 5:
        raise OperationRejected("Rencana pemulihan harus memuat 1–5 range eksplisit.")
    prepared = []
    occupied = set()
    for update in updates:
        update = update.model_dump() if isinstance(update, BaseModel) else update
        tab, row, col, h, w = range_shape(update["range_name"])
        values = update["values"]
        if len(values) != h or any(len(item) != w for item in values):
            raise OperationRejected("Setiap sel range harus memiliki nilai eksplisit; data tidak dipadatkan atau ditebak.")
        cells = {(tab, r, c) for r in range(row, row + h) for c in range(col, col + w)}
        if occupied & cells:
            raise OperationRejected("Range rencana pemulihan saling tumpang tindih.")
        occupied |= cells
        prepared.append({"range_name": update["range_name"], "values": canonical_values(values, h, w)})
    if len(json.dumps(prepared)) > 16000:
        raise OperationRejected("Rencana terlalu besar. Gunakan perbaikan yang lebih spesifik.")
    return prepared


def compare_recovery_values(current, before, desired):
    if current == desired:
        return "already_applied"
    if current != before:
        raise OperationRejected("Data berubah sejak rencana dibuat. Tidak ada penimpaan; siapkan rencana baru.")
    return "write"


async def recovery_source(db, run_id, owner_phone, self_agent_id, *, lock=False):
    try:
        parsed = uuid.UUID(str(run_id))
    except (TypeError, ValueError, AttributeError):
        raise OperationRejected("Run tidak ditemukan.")
    query = select(Run).where(Run.id == parsed)
    if lock:
        query = query.with_for_update()
    source = (await db.execute(query)).scalar_one_or_none()
    if source is None:
        raise OperationRejected("Run tidak ditemukan.")
    session = await db.get(Session, source.session_id)
    if session is None:
        raise OperationRejected("Sesi run tidak ditemukan.")
    agent = await owned_agent(db, session.agent_id, owner_phone, self_agent_id, lock=lock)
    reason = (source.runtime_metadata or {}).get("reply_guard_reason")
    if source.status not in {"failed", "timed_out", "abandoned", "cancelled"} and reason not in {"fallback_empty_reply", "unverified_google_deliverable"}:
        raise OperationRejected("Run ini tidak tercatat gagal atau menghasilkan fallback yang belum selesai.")
    latest = (await db.execute(select(Run.id).where(Run.session_id == session.id).order_by(Run.created_at.desc()).limit(1))).scalar_one_or_none()
    if latest != source.id:
        raise OperationRejected("Sudah ada run yang lebih baru pada sesi ini. Diagnosis run terbaru agar pekerjaan lama tidak terulang.")
    if session.ai_disabled or session.escalation_active:
        raise OperationRejected("Sesi sedang dijeda atau ditangani manusia.")
    if (agent.tools_config or {}).get("interrupt_on"):
        raise OperationRejected("Agent memakai persetujuan tindakan khusus; gunakan alur persetujuan normalnya.")
    return source, session, agent


async def message_marker(db, session_id):
    row = (await db.execute(select(Message.id).where(Message.session_id == session_id).order_by(Message.timestamp.desc(), Message.id.desc()).limit(1))).scalar_one_or_none()
    return str(row) if row else None


def build_recovery_tools(*, db_factory, owner_phone, self_agent_id):
    from .operations import safe_operation

    @tool
    @safe_operation
    async def prepare_assistant_run_recovery(run_id: str, updates: list[RecoverySheetUpdate] | None = None) -> dict:
        """Prepare recovery for a failed run's configured spreadsheet. With no updates, inspect its conversation/tool trace. With explicit exact range/value updates, read live preimages and return a short-lived review plan. Show changes to owner and wait for approval before recover_assistant_run. No writes/messages."""
        async with db_factory() as db:
            source, session, agent = await recovery_source(db, run_id, owner_phone, self_agent_id, lock=bool(updates))
            await require_idle(db, [agent.id])
            spreadsheet = configured_sheet(agent)
            old_plan = (source.runtime_metadata or {}).get("arthur_recovery") or {}
            if old_plan.get("status") in {"executing", "verified", "outcome_uncertain"}:
                return {"ok": False, "error": "Rencana pemulihan sudah dijalankan atau perlu diverifikasi; gunakan token rencana yang sama.", "recovery_status": old_plan.get("status")}
            if updates is None:
                rows = (await db.execute(select(Message).where(Message.session_id == session.id,
                    Message.timestamp <= (source.completed_at or source.created_at)).order_by(Message.timestamp.desc()).limit(50))).scalars().all()
                return {"ok": True, "run_id": str(source.id), "agent_id": str(agent.id), "writes_executed": 0,
                        "conversation": [{"role": m.role, "text": (m.content or "")[:3000]} for m in reversed(rows) if m.role in {"user", "agent"}],
                        "source_tools": [{"tool": m.tool_name, "args": {k: v for k, v in (m.tool_args or {}).items() if k in {"spreadsheet_id", "range_name"}}, "result_present": bool(m.tool_result)} for m in rows if m.run_id == source.id and m.tool_name],
                        "next_step": "Baca header/data target memakai test_assistant_google_tools(include_values=true), lalu usulkan range dan nilai berdasarkan percakapan. Jangan menebak kolom/baris atau membuat data baru."}
            if not write_allowed(agent):
                raise OperationRejected("Policy agent hanya mengizinkan baca spreadsheet.")
            plan_updates = check_updates(updates)
            # Unknown side effects cannot be made safe by blindly rerunning a
            # model. This first recovery path handles bounded Sheets changes.
            tool_rows = (await db.execute(select(Message).where(Message.run_id == source.id, Message.role == "tool"))).scalars().all()
            supported = {"get_spreadsheet_info", "read_sheet_values", "modify_sheet_values"}
            if any(m.tool_name not in supported for m in tool_rows):
                raise OperationRejected("Run memiliki tindakan selain Sheets. Pemulihan otomatis dibatasi ke rencana Sheets yang dapat dibandingkan dan diverifikasi.")
            marker = await message_marker(db, session.id)
            async with google_tools(agent, session) as tools:
                for update in plan_updates:
                    update["before"] = await read_range(tools, spreadsheet, update["range_name"])
            token = secrets.token_urlsafe(24)
            plan = {"token_hash": hashlib.sha256(token.encode()).hexdigest(), "status": "prepared",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                    "agent_version": agent.version, "spreadsheet_id": spreadsheet, "message_marker": marker,
                    "updates": plan_updates}
            source.runtime_metadata = {**(source.runtime_metadata or {}), "arthur_recovery": copy.deepcopy(plan)}
            await db.commit()
            return {"ok": True, "needs_confirmation": True, "run_id": str(source.id), "plan_token": token,
                    "changes": [{"range_name": u["range_name"], "before": u["before"], "after": u["values"]} for u in plan_updates],
                    "writes_executed": 0, "messages_sent": 0, "expires_in_seconds": 600}

    @tool
    @safe_operation
    async def recover_assistant_run(run_id: str, plan_token: str, confirmed: bool = False) -> dict:
        """Execute the owner's reviewed Sheets recovery plan, comparing live data before every write and reading it back. Repeated calls verify/skip already applied values. Does not replay the model or send any notification/customer message."""
        if not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Tampilkan perubahan rencana dan tunggu persetujuan owner sebelum menjalankan pemulihan."}
        # Lock session through the whole recovery, using the same process lock
        # as normal inbound runs, without cancelling a customer's active turn.
        from app.core.engine.session_lock import is_session_busy, session_run_lock
        async with db_factory() as db:
            source, session, agent = await recovery_source(db, run_id, owner_phone, self_agent_id)
            if await is_session_busy(session.id):
                raise OperationRejected("Sesi sedang aktif; tunggu pekerjaan pengguna selesai.")
            sid = session.id
        async with session_run_lock(sid):
            async with db_factory() as db:
                source, session, agent = await recovery_source(db, run_id, owner_phone, self_agent_id, lock=True)
                await require_idle(db, [agent.id])
                plan = copy.deepcopy((source.runtime_metadata or {}).get("arthur_recovery") or {})
                if not secrets.compare_digest(plan.get("token_hash", ""), hashlib.sha256(plan_token.encode()).hexdigest()):
                    raise OperationRejected("Token rencana tidak valid. Siapkan dan tinjau rencana terlebih dahulu.")
                if plan.get("status") == "verified":
                    return {"ok": True, "already_recovered": True, "run_id": str(source.id), "writes_executed": 0}
                if plan.get("status") == "executing":
                    started = datetime.fromisoformat(plan.get("execution_started_at") or plan["expires_at"])
                    if datetime.now(timezone.utc) - started < timedelta(seconds=120):
                        raise OperationRejected("Pemulihan sedang berjalan; tunggu selesai sebelum mencoba kembali.")
                    # The executor is bounded to 90s. After a crash/restart,
                    # resume the same reviewed ranges by live comparison.
                if plan.get("status") == "prepared" and datetime.fromisoformat(plan["expires_at"]) < datetime.now(timezone.utc):
                    raise OperationRejected("Rencana kedaluwarsa; baca ulang data dan siapkan rencana baru.")
                if agent.version != plan["agent_version"] or configured_sheet(agent) != plan["spreadsheet_id"]:
                    raise OperationRejected("Konfigurasi agent berubah; siapkan rencana baru.")
                if await message_marker(db, session.id) != plan["message_marker"]:
                    raise OperationRejected("Percakapan berubah sejak rencana dibuat; tinjau ulang agar tidak menimpa pekerjaan baru.")
                plan["status"] = "executing"
                plan["execution_started_at"] = datetime.now(timezone.utc).isoformat()
                source.runtime_metadata = {**(source.runtime_metadata or {}), "arthur_recovery": copy.deepcopy(plan)}
                await db.commit()  # Durable claim before any external write.
                writes = 0
                write_attempted = False
                try:
                    async with google_tools(agent, session) as tools:
                        # Preflight all ranges before starting a partial write.
                        for update in plan["updates"]:
                            current = await read_range(tools, plan["spreadsheet_id"], update["range_name"])
                            compare_recovery_values(current, update["before"], update["values"])
                        for update in plan["updates"]:
                            current = await read_range(tools, plan["spreadsheet_id"], update["range_name"])
                            if compare_recovery_values(current, update["before"], update["values"]) == "write":
                                write_attempted = True
                                await write_range(tools, plan["spreadsheet_id"], update["range_name"], update["values"])
                                writes += 1
                            if await read_range(tools, plan["spreadsheet_id"], update["range_name"]) != update["values"]:
                                raise OperationRejected("Hasil baca ulang tidak cocok; pemulihan belum dinyatakan selesai.")
                    plan["status"] = "verified"
                    plan["verified_at"] = datetime.now(timezone.utc).isoformat()
                except BaseException:
                    # Keep the exact same plan/preimages for inspection. Do not
                    # generate a new append or claim an unverified write failed.
                    plan["status"] = "outcome_uncertain" if write_attempted or plan.get("writes_executed") else "preflight_failed"
                    raise
                finally:
                    plan["writes_executed"] = int(plan.get("writes_executed", 0)) + writes
                    source.runtime_metadata = {**(source.runtime_metadata or {}), "arthur_recovery": copy.deepcopy(plan)}
                    await db.commit()
                return {"ok": True, "run_id": str(source.id), "recovery_status": "verified", "verified_ranges": len(plan["updates"]),
                        "writes_executed": writes, "messages_sent": 0}

    return [prepare_assistant_run_recovery, recover_assistant_run]
