"""P2 service, Google resource, and customer access administration."""
from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from langchain_core.tools import tool
from sqlalchemy import select

from app.core.domain.agent_service_controls import CONTROL_KEY, controls, is_agent_paused, service_snapshot
from app.core.domain.agent_revision_service import ADMIN_SCOPE, encode_archive, decode_archive
from app.models.memory import Memory
from app.models.outbound_message import OutboundMessage
from app.models.scheduled_job import ScheduledJob
from .operations import safe_operation, transport_dependents
from .operations_common import OperationRejected, owned_agent, require_idle, google_config, google_tools, result_text


def phone_number(value):
    value = re.sub(r"[\s+()-]", "", value)
    if value.startswith("0"):
        value = "62" + value[1:]
    if not re.fullmatch(r"[1-9]\d{7,14}", value):
        raise OperationRejected("Gunakan nomor telepon lengkap, misalnya +6281234567890; bukan nama, LID, atau JID.")
    return value


def set_controls(agent, **updates):
    config = copy.deepcopy(agent.tools_config or {})
    config[CONTROL_KEY] = {**controls(agent), **updates}
    agent.tools_config = config


async def archive_state(db, agent_id, key):
    return (await db.execute(select(Memory).where(Memory.agent_id == agent_id, Memory.scope == ADMIN_SCOPE, Memory.key == key).with_for_update())).scalar_one_or_none()


def build_service_management_tools(*, db_factory, owner_phone, self_agent_id):
    @tool
    @safe_operation
    async def set_assistant_paused(agent_id: str, paused: bool, confirmed: bool = False, resume_schedules: bool = False) -> dict:
        """Pause/resume an owned agent without deleting/disconnecting it. Pause cancels queued replies and pauses active schedules. Resume service only by default. With explicit owner approval, resume_schedules=true reactivates only schedules paused by this service pause, using future run times; no backlog replay or manually paused jobs."""
        if not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Konfirmasi jeda/aktivasi layanan. Jeda membatalkan pesan antre dan menjeda jadwal; aktivasi tidak mengirim ulang pesan atau menyalakan jadwal otomatis."}
        if paused and resume_schedules:
            raise OperationRejected("resume_schedules hanya berlaku ketika mengaktifkan kembali layanan.")
        async with db_factory() as db:
            agent = await owned_agent(db, agent_id, owner_phone, self_agent_id, lock=True)
            await require_idle(db, [agent.id])
            if is_agent_paused(agent) == paused and not resume_schedules:
                return {"ok": True, "paused": paused, "already_in_requested_state": True}
            owned_jobs = list(controls(agent).get("service_paused_job_ids") or [])
            if paused:
                owned_jobs = [str(jid) for jid in (await db.execute(select(ScheduledJob.id).where(ScheduledJob.agent_id == agent.id, ScheduledJob.status == "active").with_for_update())).scalars().all()]
            effects = await transport_dependents(db, [agent.id], apply=paused)
            resumed = 0
            if not paused and resume_schedules and owned_jobs:
                from app.core.tools.scheduler_tool import _compute_next_run
                jobs = (await db.execute(select(ScheduledJob).where(ScheduledJob.agent_id == agent.id, ScheduledJob.id.in_([uuid.UUID(j) for j in owned_jobs]), ScheduledJob.status == "paused").with_for_update())).scalars().all()
                now = datetime.now(timezone.utc)
                for job in jobs:
                    next_run = _compute_next_run(job.cron_expr) if job.cron_expr else job.run_once_at
                    if next_run and (next_run if next_run.tzinfo else next_run.replace(tzinfo=timezone.utc)) > now:
                        job.status = "active"
                        job.next_run_at = next_run
                        resumed += 1
                owned_jobs = []
            set_controls(agent, paused=paused, service_paused_job_ids=owned_jobs)
            agent.version += 1
            await db.commit()
            return {"ok": True, "paused": paused, "version": agent.version,
                    **(effects if paused else {"scheduled_jobs_resumed": resumed, "messages_replayed": 0})}

    @tool
    @safe_operation
    async def get_assistant_access(agent_id: str) -> dict:
        """Read the owned agent's operator and WhatsApp customer admission settings. Customer tool usage uses configured workflow permissions, independent of operator membership."""
        async with db_factory() as db:
            agent = await owned_agent(db, agent_id, owner_phone, self_agent_id)
            return {"ok": True, "owner": agent.owner_external_id, "operators": agent.operator_ids or [],
                    **service_snapshot(agent),
                    "escalation_operator": (agent.escalation_config or {}).get("operator_phone"),
                    "customer_access": "all" if agent.allowed_senders is None else "allowlist" if agent.allowed_senders else "operators_only",
                    "allowed_customers": agent.allowed_senders, "version": agent.version,
                    "customer_tools": "Configured tools remain usable by admitted non-owner customers."}

    @tool
    @safe_operation
    async def configure_assistant_access(agent_id: str, customer_access: Literal["all", "allowlist", "operators_only"] | None = None,
                                         allowed_customers: list[str] | None = None, add_operators: list[str] | None = None,
                                         remove_operators: list[str] | None = None, expected_version: int | None = None,
                                         confirmed: bool = False) -> dict:
        """Explicitly change owned agent operators/customer admission. Requires current version from get_assistant_access and owner confirmation. All/allowlist/operators_only is an admission rule, never an owner-only restriction on Google tools. Owner access cannot be removed; omitted settings stay unchanged."""
        if not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Tinjau nomor operator/pelanggan dan konfirmasi perubahan akses terlebih dahulu."}
        if expected_version is None:
            raise OperationRejected("Baca get_assistant_access terlebih dahulu dan sertakan expected_version.")
        if not any([customer_access is not None, add_operators, remove_operators]):
            raise OperationRejected("Tidak ada perubahan akses yang diminta.")
        if allowed_customers is not None and customer_access != "allowlist":
            raise OperationRejected("Daftar pelanggan hanya dipakai bersama customer_access=allowlist.")
        async with db_factory() as db:
            agent = await owned_agent(db, agent_id, owner_phone, self_agent_id, lock=True)
            await require_idle(db, [agent.id])
            if agent.version != expected_version:
                raise OperationRejected("Versi konfigurasi berubah; baca ulang akses agent.")
            owner = phone_number(agent.owner_external_id or owner_phone)
            add = {phone_number(p) for p in add_operators or []}
            remove = {phone_number(p) for p in remove_operators or []}
            if owner in remove or add & remove:
                raise OperationRejected("Owner tidak boleh dihapus, dan satu nomor tidak boleh ditambah sekaligus dihapus.")
            operators = {phone_number(p) for p in agent.operator_ids or []}
            # Legacy escalation operator is a real operator too; removal must
            # update both sources or it would silently keep its privileges.
            escalation = copy.deepcopy(agent.escalation_config or {})
            if escalation.get("operator_phone") and phone_number(escalation["operator_phone"]) in remove:
                escalation.pop("operator_phone")
                agent.escalation_config = escalation
            agent.operator_ids = sorted((operators | add | {owner}) - remove)
            if customer_access == "all":
                agent.allowed_senders = None
            elif customer_access == "operators_only":
                agent.allowed_senders = []
            elif customer_access == "allowlist":
                if not allowed_customers:
                    raise OperationRejected("Mode allowlist memerlukan daftar pelanggan eksplisit yang tidak kosong.")
                agent.allowed_senders = sorted({phone_number(p) for p in allowed_customers})
            set_controls(agent, managed_access=True)
            agent.version += 1
            await db.commit()
            return {"ok": True, "version": agent.version, "operators": agent.operator_ids,
                    "allowed_customers": agent.allowed_senders, "customer_tools_policy_changed": False}

    @tool
    @safe_operation
    async def manage_assistant_google_connection(agent_id: str, action: Literal["status", "disconnect", "reconnect"] = "status",
                                                 confirmed: bool = False) -> dict:
        """Inspect or disconnect/reconnect one owned agent's Google integration, never other agents/global owner access. Disconnect deactivates its integration and removes local connector only; it does not revoke Google's entire account grant. Reconnect restores its saved connector policy and returns owner OAuth; it cannot select an account silently."""
        from .google_oauth import get_google_oauth_status, start_google_oauth
        from app.config import get_settings
        import httpx
        if action != "status" and not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Konfirmasi perubahan koneksi Google agent ini. Koneksi agent lain tidak ikut dilepas."}
        async with db_factory() as db:
            agent = await owned_agent(db, agent_id, owner_phone, self_agent_id, lock=action != "status")
            subject = agent.owner_external_id or owner_phone
            if action == "status":
                data = await get_google_oauth_status(external_user_id=subject, agent_id=str(agent.id))
                return {"ok": True, "connector_configured": bool(google_config(agent)), "connected": bool(data.get("connected")),
                        "email": data.get("email"), "scopes": data.get("scopes", [])}
            await require_idle(db, [agent.id])
            saved = await archive_state(db, agent.id, "google_connector_backup")
            if action == "disconnect":
                connector = copy.deepcopy(google_config(agent))
                if not connector:
                    return {"ok": True, "already_disconnected_locally": True}
                settings = get_settings()
                base_url = str(settings.google_integration_service_url or "").rstrip("/")
                if not base_url:
                    raise OperationRejected("Layanan integrasi Google belum dikonfigurasi.")
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.delete(f"{base_url}/v1/integrations/google/disconnect", params={"external_user_id": subject, "agent_id": str(agent.id)}, headers={"X-API-Key": settings.api_key})
                    if response.status_code != 404:
                        response.raise_for_status()
                if saved is None:
                    saved = Memory(agent_id=agent.id, scope=ADMIN_SCOPE, key="google_connector_backup", value_data="")
                    db.add(saved)
                saved.value_data = encode_archive({"connector": connector})
                config = copy.deepcopy(agent.tools_config)
                mcp = config["mcp"]
                (mcp.get("servers", mcp)).pop("google_workspace", None)
                agent.tools_config = config
                agent.version += 1
                await db.commit()
                return {"ok": True, "disconnected": True, "other_agents_changed": False, "google_account_grant_revoked": False}
            connector = copy.deepcopy(google_config(agent))
            if not connector and saved is not None:
                connector = decode_archive(saved.value_data)["connector"]
            if not connector or not connector.get("oauth_scopes"):
                raise OperationRejected("Konfigurasi/scopes Google belum tersedia. Gunakan configure_assistant_runtime terlebih dahulu.")
            oauth = await start_google_oauth(external_user_id=subject, agent_id=str(agent.id), scopes=connector["oauth_scopes"], force_reauth=True)
            config = copy.deepcopy(agent.tools_config)
            mcp = config.setdefault("mcp", {})
            if "servers" in mcp or "enabled" in mcp:
                mcp.setdefault("servers", {})["google_workspace"] = connector
                mcp["enabled"] = True
            else:
                mcp["google_workspace"] = connector
            agent.tools_config = config
            agent.version += 1
            await db.commit()
            return {"ok": True, "connected": oauth.connected, "auth_url": oauth.auth_url,
                    "next_step": "Owner memilih akun di halaman Google. Koneksi belum dianggap aktif sampai diverifikasi."}

    @tool
    @safe_operation
    async def set_assistant_google_spreadsheet(agent_id: str, spreadsheet_id: str, expected_version: int,
                                              confirmed: bool = False) -> dict:
        """Change only the configured spreadsheet after reading its live metadata with owner's current connection. Requires explicit ID/current agent version and confirmation. Does not create/write any spreadsheet or broaden Google permissions."""
        if not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Konfirmasi spreadsheet target baru dan sertakan versi agent terkini."}
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", spreadsheet_id):
            raise OperationRejected("Sertakan ID spreadsheet Google yang eksplisit dan valid.")
        async with db_factory() as db:
            agent = await owned_agent(db, agent_id, owner_phone, self_agent_id, lock=True)
            await require_idle(db, [agent.id])
            if agent.version != expected_version:
                raise OperationRejected("Versi agent berubah; periksa ulang konfigurasi.")
            async with google_tools(agent) as tools:
                if "get_spreadsheet_info" not in tools:
                    raise OperationRejected("Tool pemeriksaan spreadsheet tidak tersedia.")
                text = result_text(await tools["get_spreadsheet_info"].ainvoke({"spreadsheet_id": spreadsheet_id}))
                if spreadsheet_id not in text or not text.startswith("Spreadsheet:"):
                    raise OperationRejected("Spreadsheet target belum dapat diverifikasi; konfigurasi tidak diubah.")
            config = copy.deepcopy(agent.tools_config)
            resource = config.setdefault("google_workspace_resources", {})
            if resource.get("delegated_customer_append_only") or resource.get("customer_append_enabled"):
                raise OperationRejected("Resource ini memakai kontrak append-only khusus; perubahan perlu meninjau header/kontrak tersebut terlebih dahulu.")
            old_id = resource.get("default_spreadsheet_id")
            resource["default_spreadsheet_id"] = spreadsheet_id
            resource["default_spreadsheet_url"] = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
            resource["default_spreadsheet_verified"] = True
            resource["default_spreadsheet_configured"] = True
            title = re.match(r'^Spreadsheet:\s*"([^"\n]+)"', text)
            resource["default_spreadsheet_title"] = title[1] if title else ""
            if old_id and old_id != spreadsheet_id:
                agent.instructions = (agent.instructions or "").replace(old_id, spreadsheet_id)
            agent.tools_config = config
            agent.version += 1
            await db.commit()
            return {"ok": True, "previous_spreadsheet_id": old_id, "spreadsheet_id": spreadsheet_id,
                    "version": agent.version, "spreadsheet_writes_executed": 0}

    return [set_assistant_paused, get_assistant_access, configure_assistant_access,
            manage_assistant_google_connection, set_assistant_google_spreadsheet]
