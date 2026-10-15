"""Additive P1 controls. Existing Arthur tools and API contracts stay intact."""
from __future__ import annotations

import functools
import asyncio
import uuid
from typing import Literal

from langchain_core.tools import tool
from sqlalchemy import select

from app.core.domain.whatsapp_binding import clear_cloud_api_binding
from app.core.domain.agent_service_controls import service_snapshot
from app.models.outbound_message import OutboundMessage
from app.models.run import Run
from app.models.scheduled_job import ScheduledJob
from app.models.session import Session
from .operations_common import (
    OperationRejected, configured_sheet, google_config, google_tools,
    owned_agent, range_shape, read_range, require_idle, write_allowed, write_range,
)

_BINDING_FIELDS = ("wa_device_id", "wa_phone_number_id", "wa_waba_id", "wa_access_token_encrypted",
                   "wa_display_phone", "wa_business_name", "wa_connection_type", "wa_cloud_api_mode")
_TRANSPORT_KEYS = {"device_id", "meta_phone_number_id", "meta_access_token"}


def safe_operation(fn):
    @functools.wraps(fn)
    async def invoke(*args, **kwargs):
        try:
            async with asyncio.timeout(90):
                return await fn(*args, **kwargs)
        except OperationRejected as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            # Provider/DB exception messages can contain tokens or customer data.
            return {"ok": False, "error": "Operasi belum berhasil diverifikasi. Konfigurasi tidak diklaim berubah.", "error_type": type(exc).__name__}
    return invoke


def binding_present(agent):
    return bool(agent.wa_device_id or agent.wa_phone_number_id or agent.wa_access_token_encrypted)


def move_binding(source, target):
    """Caller must lock both rows, clear+flush source before assigning target."""
    if binding_present(target):
        raise OperationRejected("Agent tujuan sudah memiliki koneksi. Lepaskan koneksinya secara terpisah terlebih dahulu.")
    if not binding_present(source):
        raise OperationRejected("Agent asal belum memiliki nomor terhubung.")
    if source.wa_device_id and source.wa_phone_number_id:
        raise OperationRejected("Agent asal memiliki dua transport; periksa koneksi sebelum dipindahkan.")
    return {key: getattr(source, key) for key in _BINDING_FIELDS}


async def transport_dependents(db, agent_ids, *, apply=False):
    """Lock queue/jobs before checking them; old payloads must not escape later."""
    outbounds = (await db.execute(select(OutboundMessage).where(
        OutboundMessage.agent_id.in_(agent_ids), OutboundMessage.status.in_(["queued", "sending"]),
    ).with_for_update())).scalars().all()
    jobs = (await db.execute(select(ScheduledJob).where(
        ScheduledJob.agent_id.in_(agent_ids), ScheduledJob.status.in_(["active", "running"]),
    ).with_for_update())).scalars().all()
    if any(item.status == "sending" for item in outbounds) or any(job.status == "running" for job in jobs):
        raise OperationRejected("Ada pengiriman atau jadwal yang sedang berjalan. Tunggu selesai sebelum memutus/memindahkan nomor.")
    if apply:
        for item in outbounds:
            item.status = "cancelled"
            item.last_error = "owner_approved_whatsapp_binding_change"
        for job in jobs:
            job.status = "paused"
    return {"queued_messages_cancelled": len(outbounds), "scheduled_jobs_paused": len(jobs)}


async def clear_session_transports(db, agent_ids):
    sessions = (await db.execute(select(Session).where(Session.agent_id.in_(agent_ids)).with_for_update())).scalars().all()
    count = 0
    for session in sessions:
        cfg = session.channel_config or {}
        if session.channel_type == "whatsapp" and _TRANSPORT_KEYS.intersection(cfg):
            session.channel_config = {k: v for k, v in cfg.items() if k not in _TRANSPORT_KEYS}
            session.channel_type = None
            count += 1
    return count


def build_operations_tools(*, db_factory, owner_phone, self_agent_id):
    @tool
    @safe_operation
    async def disconnect_assistant_whatsapp(agent_id: str, confirmed: bool = False) -> dict:
        """Disconnect an owned agent without deleting it. Confirmation covers cancelling queued sends and pausing schedules; chat history remains. Cloud disconnect is local, not Meta deregistration."""
        if not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Konfirmasi pelepasan nomor, pembatalan pesan antre, dan jeda jadwal agent terlebih dahulu."}
        async with db_factory() as db:
            agent = await owned_agent(db, agent_id, owner_phone, self_agent_id, lock=True)
            await require_idle(db, [agent.id])
            if not binding_present(agent):
                return {"ok": True, "already_disconnected": True, "agent_id": str(agent.id)}
            effects = await transport_dependents(db, [agent.id])
            if agent.wa_device_id and agent.wa_phone_number_id:
                raise OperationRejected("Agent memiliki dua transport; periksa koneksinya terlebih dahulu.")
            if agent.wa_device_id:
                from app.core.infra.wa_client import delete_wa_device
                import httpx
                try:
                    await delete_wa_device(agent.wa_device_id)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:
                        raise
            agent.wa_device_id = None
            clear_cloud_api_binding(agent)
            agent.version += 1
            await transport_dependents(db, [agent.id], apply=True)
            count = await clear_session_transports(db, [agent.id])
            await db.commit()
            return {"ok": True, "agent_id": str(agent.id), "disconnected": True, "sessions_detached": count, **effects}

    @tool
    @safe_operation
    async def transfer_assistant_whatsapp(source_agent_id: str, target_agent_id: str, confirmed: bool = False) -> dict:
        """Atomically move a WhatsApp binding between two owned agents, preserving both agents/history/config. Target must be unbound. Confirmation covers pausing schedules and cancelling queued sends on both agents. No QR/logout/Meta registration."""
        if not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Konfirmasi agent asal/tujuan, perpindahan nomor, pembatalan pesan antre, dan jeda jadwal kedua agent."}
        source_agent_id = str(uuid.UUID(source_agent_id))
        target_agent_id = str(uuid.UUID(target_agent_id))
        if source_agent_id == target_agent_id:
            raise OperationRejected("Agent asal dan tujuan harus berbeda.")
        async with db_factory() as db:
            # Deterministic lock order prevents opposing transfers deadlocking.
            agents = {aid: await owned_agent(db, aid, owner_phone, self_agent_id, lock=True)
                      for aid in sorted({source_agent_id, target_agent_id})}
            source, target = agents[source_agent_id], agents[target_agent_id]
            if source.id == target.id:
                raise OperationRejected("Agent asal dan tujuan harus berbeda.")
            await require_idle(db, [source.id, target.id])
            if source.wa_inbound_route != "ai_staff" or target.wa_inbound_route != "ai_staff":
                raise OperationRejected("Perpindahan untuk nomor yang dirutekan ke n8n memerlukan konfigurasi terpisah.")
            binding = move_binding(source, target)
            effects = await transport_dependents(db, [source.id, target.id])
            source.wa_device_id = None
            clear_cloud_api_binding(source)
            source.version += 1
            await db.flush()  # Release exclusive phone/device key in this transaction.
            for key, value in binding.items():
                setattr(target, key, value)
            target.channel_type = "whatsapp"
            target.version += 1
            await transport_dependents(db, [source.id, target.id], apply=True)
            count = await clear_session_transports(db, [source.id, target.id])
            await db.commit()
            return {"ok": True, "source_agent_id": str(source.id), "target_agent_id": str(target.id),
                    "transferred": True, "sessions_detached": count, "history_moved": False, **effects}

    @tool
    @safe_operation
    async def diagnose_assistant(agent_id: str) -> dict:
        """Read-only health diagnosis: configured capabilities, WhatsApp/Google connectivity, last runs, queue/jobs. Does not generate OAuth links or mutate the agent; no customer content or credentials returned."""
        from .google_oauth import get_google_oauth_status
        async with db_factory() as db:
            agent = await owned_agent(db, agent_id, owner_phone, self_agent_id)
            runs = (await db.execute(select(Run).join(Session, Session.id == Run.session_id).where(
                Session.agent_id == agent.id).order_by(Run.created_at.desc()).limit(5))).scalars().all()
            jobs = (await db.execute(select(ScheduledJob.status).where(ScheduledJob.agent_id == agent.id))).scalars().all()
            queue = (await db.execute(select(OutboundMessage.status).where(
                OutboundMessage.agent_id == agent.id, OutboundMessage.status.in_(["queued", "sending"]),
            ))).scalars().all()
            google = {"configured": bool(google_config(agent)), "connected": None}
            if google["configured"]:
                try:
                    status = await get_google_oauth_status(external_user_id=agent.owner_external_id or owner_phone, agent_id=str(agent.id))
                    google["connected"] = bool(status.get("connected"))
                except Exception:
                    google["status"] = "unverified_service_unavailable"
            whatsapp = {"configured": binding_present(agent), "transport": agent.wa_connection_type, "status": "not_connected"}
            if agent.wa_phone_number_id:
                # Local credentials are not proof of live Meta connectivity.
                whatsapp["status"] = "configured_live_status_unverified"
            elif agent.wa_device_id:
                from app.core.infra.wa_client import get_wa_status
                try:
                    whatsapp["status"] = str((await get_wa_status(agent.wa_device_id)).get("status", "unknown"))
                except Exception:
                    whatsapp["status"] = "unverified_service_unavailable"
            return {"ok": True, "agent_id": str(agent.id), "version": agent.version,
                    **service_snapshot(agent),
                    "google": {**google, "allowed_services": google_config(agent).get("allowed_services", []),
                               "allowed_operations": google_config(agent).get("allowed_operations", {}),
                               "spreadsheet_configured": bool((agent.tools_config or {}).get("google_workspace_resources", {}).get("default_spreadsheet_id"))},
                    "whatsapp": whatsapp, "jobs": {s: jobs.count(s) for s in set(jobs)},
                    "outbound": {s: queue.count(s) for s in set(queue)},
                    "recent_runs": [{"run_id": str(r.id), "session_id": str(r.session_id), "status": r.status,
                                     "reply_guard_reason": (r.runtime_metadata or {}).get("reply_guard_reason"),
                                     "recovery_status": (r.runtime_metadata or {}).get("arthur_recovery", {}).get("status"),
                                     "completion_tokens": r.completion_tokens} for r in runs]}

    @tool
    @safe_operation
    async def test_assistant_google_tools(agent_id: str, range_name: str, mode: Literal["read", "write_test"] = "read",
                                         confirmed: bool = False, include_values: bool = False) -> dict:
        """Probe only the owned agent's configured spreadsheet. Read by default. Write-test requires explicit approval and one blank cell on an Arthur_Test* tab; writes a random marker, verifies and clears only its own marker. Never sends messages."""
        shape = range_shape(range_name)
        if mode == "write_test" and (not confirmed or shape[3:] != (1, 1) or not shape[0].startswith("Arthur_Test")):
            raise OperationRejected("Uji tulis memerlukan konfirmasi eksplisit dan satu sel kosong di tab khusus Arthur_Test*.")
        async with db_factory() as db:
            agent = await owned_agent(db, agent_id, owner_phone, self_agent_id, lock=mode == "write_test")
            spreadsheet = configured_sheet(agent)
            if mode == "write_test":
                await require_idle(db, [agent.id])
                if not write_allowed(agent):
                    raise OperationRejected("Policy agent hanya mengizinkan baca spreadsheet.")
            async with google_tools(agent) as tools:
                values = await read_range(tools, spreadsheet, range_name)
                if mode == "read":
                    return {"ok": True, "read_verified": True, "write_verified": False,
                            "write_tool_available": write_allowed(agent) and "modify_sheet_values" in tools,
                            **({"values": values} if include_values else {}), "writes_executed": 0}
                if values != [[""]]:
                    raise OperationRejected("Sel uji tidak kosong; tidak ada data yang ditimpa.")
                marker = [["arthur-probe-" + uuid.uuid4().hex]]
                verified = False
                cleaned = False
                try:
                    await write_range(tools, spreadsheet, range_name, marker)
                    verified = await read_range(tools, spreadsheet, range_name) == marker
                finally:
                    # Even a timeout can mean the marker was written. Inspect
                    # first, and never clear content belonging to someone else.
                    current = await read_range(tools, spreadsheet, range_name)
                    if current == marker:
                        await write_range(tools, spreadsheet, range_name, [[""]])
                        cleaned = await read_range(tools, spreadsheet, range_name) == [[""]]
                return {"ok": verified and cleaned, "write_verified": verified, "cleanup_verified": cleaned,
                        "range_name": range_name, "messages_sent": 0}

    from .run_recovery import build_recovery_tools
    return [disconnect_assistant_whatsapp, transfer_assistant_whatsapp, diagnose_assistant,
            test_assistant_google_tools, *build_recovery_tools(db_factory=db_factory, owner_phone=owner_phone,
                                                             self_agent_id=self_agent_id)]
