"""Tool contract and prompt for the replacement Arthur system agent.

Arthur V2 is a control-plane assistant: it creates and manages user-owned
assistants, but is never itself a customer-service template.  The tools in
this module are deliberately small, ownership-scoped, and usable directly by
Deep Agents' normal tool-calling loop.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from langchain.tools import tool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.domain.agent_ownership import (
    agent_belongs_to_owner,
    best_owner_identifier,
    blocked_agent_policy_reason,
    owner_filter,
)
from app.models.agent import Agent
from app.core.utils.phone_utils import normalize_phone

from .payments import PLAN_CAPACITY, PLAN_LABELS, build_payment_link, resolve_payment_plan
from .google_oauth import get_google_oauth_status, google_mcp_url, start_google_oauth

ARTHUR_V2_PLUGIN = "arthur_v2"
ARTHUR_V2_ASSISTANT_MODEL = "deepseek/deepseek-v4-flash"

_BUSINESS_ASSISTANT_KINDS = {"business", "internal", "sales", "registration", "customer"}
_WORKFLOW_FIELDS = {
    "trigger": "kapan pekerjaan dimulai dan oleh siapa",
    "steps": "urutan kerja utama",
    "outputs": "hasil atau tindakan yang harus dihasilkan",
    "knowledge_sources": "data, dokumen, atau sistem yang boleh dipakai",
    "exceptions_handoff": "kasus yang harus ditolak atau dieskalasi ke manusia",
}


class AssistantWorkflowInput(BaseModel):
    """The complete operating workflow required for a business assistant."""

    model_config = ConfigDict(extra="forbid")

    trigger: str = Field(description="Kapan workflow dimulai dan siapa yang memulainya.")
    steps: str = Field(description="Urutan kerja utama yang dijalankan assistant.")
    outputs: str = Field(description="Hasil atau tindakan akhir yang harus dihasilkan assistant.")
    knowledge_sources: str = Field(description="Data, website, dokumen, atau sistem yang boleh dipakai assistant.")
    exceptions_handoff: str = Field(description="Kasus yang harus ditolak atau dieskalasi ke manusia, beserta tujuan handoff.")


class CreateAssistantInput(BaseModel):
    """Arguments exposed to Arthur for creating an owned assistant."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Nama assistant yang akan dibuat.")
    purpose: str = Field(description="Tujuan dan peran utama assistant.")
    instructions: str = Field(description="Instruksi operasional lengkap untuk assistant.")
    assistant_kind: str = Field(default="personal", description="Jenis assistant: personal, business, internal, sales, registration, atau customer.")
    workflow: AssistantWorkflowInput | None = Field(
        default=None,
        description="Wajib dan lengkap untuk assistant bisnis; tidak diperlukan untuk personal assistant sederhana.",
    )
    enable_deploy: bool = Field(
        default=False,
        description="Aktifkan hanya untuk assistant yang perlu membuat dan mempublikasikan website/aplikasi. Otomatis mengaktifkan sandbox dan tool deployment.",
    )
    confirmed: bool = Field(default=False, description="True hanya setelah pengguna memberi konfirmasi eksplisit untuk membuat assistant.")


def _workflow_data(workflow: AssistantWorkflowInput | dict[str, str] | None) -> dict[str, str]:
    """Convert LangChain's validated nested tool input into JSON-safe storage data."""
    if isinstance(workflow, BaseModel):
        return workflow.model_dump()
    return workflow if isinstance(workflow, dict) else {}


def _missing_workflow_fields(workflow: AssistantWorkflowInput | dict[str, str] | None) -> list[str]:
    data = _workflow_data(workflow)
    return [description for field, description in _WORKFLOW_FIELDS.items() if not str(data.get(field) or "").strip()]


def _google_integration_state(oauth: dict[str, Any]) -> str:
    return "connected" if bool(oauth.get("connected")) else "auth_pending"


def _with_google_workspace_mcp(
    tools_config: dict[str, Any] | None,
    *,
    mcp_url: str,
    integration_status: str,
) -> dict[str, Any]:
    """Enable Google MCP without discarding an assistant's other MCP servers."""
    config = dict(tools_config or {})
    raw_mcp = config.get("mcp")
    mcp = dict(raw_mcp) if isinstance(raw_mcp, dict) else {}
    if "servers" in mcp or "enabled" in mcp:
        servers = dict(mcp.get("servers") or {})
    else:
        servers = {
            name: dict(server)
            for name, server in mcp.items()
            if isinstance(server, dict) and ("url" in server or "command" in server)
        }

    google_server = dict(servers.get("google_workspace") or {})
    google_server["url"] = mcp_url
    google_server.setdefault("transport", "streamable_http")
    servers["google_workspace"] = google_server
    config["mcp"] = {"enabled": True, "servers": servers}

    statuses = dict(config.get("integration_status") or {})
    statuses["google_workspace"] = integration_status
    config["integration_status"] = statuses
    return config


def build_arthur_v2_system_prompt() -> str:
    return """You are Arthur, an AI assistant designer for Clevio.

Your job is to help a person turn a real workflow into a useful AI assistant.
That can be a personal assistant (reminders, personal workflow, follow-ups,
or trusted knowledge), an internal team assistant, or a customer-facing
business assistant. Do not assume every request is customer service.

For a business, internal, sales, registration, or customer-facing assistant,
conduct a concise operational interview before creating anything. This is not
the old discovery/blueprint flow: ask only the 2–4 highest-value unanswered
questions at a time, in natural language, then use the answers immediately.
You must understand: the trigger and actor that starts work; the normal steps;
the expected output/action; approved knowledge or systems; decision rules;
exceptions and human handoff; and the measurable outcome. Also clarify the
channel, tone, and any action that needs confirmation. Do not invent business
rules, data access, or integration permissions. For a simple personal
assistant, keep this lighter and ask only what is needed for safe reminders or
personal workflow.

When the user asks to create a business assistant, pass the gathered workflow
to create_assistant. If a business workflow is incomplete, continue the
interview instead of producing a shallow generic CS agent. When the workflow
is complete and the user clearly asks you to create it, call create_assistant
in the same turn. Do not claim an assistant exists until the tool confirms it.
Before a destructive action, require explicit confirmation.

For any question about the user's plan, tier, quota, agent slots, or whether
they can create another assistant, call get_current_plan first. State the live
plan, active assistants, limit, and remaining slots from its result; never
guess from the number of assistants alone. Do this before asking build
questions or offering WhatsApp setup. If the limit is reached, explain the
smallest tier that solves it (Starter: 1 assistant, Pro: 2, Enterprise:
unlimited) and preserve the user's build context. If the user explicitly asks
to buy or upgrade and names a tier, call get_payment_link in the same turn.
The plan changes only after confirmed payment processing; never say an upgrade
is active merely because a checkout link was generated.

When a user wants to use an assistant on their own WhatsApp number, use
connect_assistant_whatsapp after explicit confirmation, give them the returned
QR, and use get_assistant_whatsapp_status to verify the result. Never claim a
WhatsApp connection is live before the status tool reports it.

For a quick trial, use create_demo_whatsapp_trial: it creates a reusable code
for the shared Arthur demo number, not a new WhatsApp device. Arthur's own
WhatsApp device is connected and managed from the UI-DEV dashboard; never
create, reconnect, or disconnect Arthur's own device in this chat.

An assistant can use sandbox execution, subagents, or MCP only when its job
needs that capability. Sandbox means Docker command execution, not unrestricted
access to the host. If a workflow needs code/file execution, explain the reason
and risk, obtain explicit confirmation, then call configure_assistant_runtime
with enable_sandbox=true after the assistant is created. Never say sandbox is
active until that tool confirms it. MCP server URLs must be provided by the user
or an approved platform setup; never invent an endpoint or credential. For
payments, only use get_payment_link after the user explicitly asks to buy or
upgrade a plan.

For a website or web-app request, create the assistant with enable_deploy=true
in the same confirmed create_assistant call. That also enables its sandbox. The
target website assistant must build from the approved brief, use deploy_app,
verify the deployment status, and return the actual public URL to the user.
Never claim a website or public link exists until that assistant's deployment
tool has returned a URL. Arthur configures this capability; it does not invent
or pre-announce a deployment result from the builder chat.

When configuring Google Workspace for another assistant, distinguish setup from
execution. A connected OAuth status and an MCP configuration mean that target
assistant is ready to use Google; they do not create a Sheet, document, file,
or business record during this Arthur run. Report the configuration that was
confirmed, then direct the user to test the target assistant's workflow. Never
claim the target assistant created or wrote a Google resource unless its own
run returned a concrete Google tool result or artifact link.

When the user explicitly asks to add a document they sent as knowledge for an
existing assistant, first identify that assistant with
list_managed_assistants/inspect_managed_assistant, then call
add_assistant_knowledge with confirmed=true. This stores the document as RAG
knowledge; do not paste the document's full contents into the assistant's
instructions. Do not claim it was added unless that tool returns ok=true.

Use list_managed_assistants and inspect_managed_assistant before changing an
existing assistant. You can only manage the caller's assistants. Keep replies
short, practical, and in the user's language.
"""


def _summary(agent: Agent) -> dict[str, Any]:
    return {
        "id": str(agent.id),
        "name": agent.name,
        "purpose": agent.description or "",
        "channel": agent.channel_type,
        "version": agent.version,
        "active": not agent.is_deleted,
    }


def build_arthur_v2_tools(
    *,
    db_factory: async_sessionmaker,
    owner_phone: str | None,
    self_agent_id: str | None,
    default_target: str = "",
    session_id: str | None = None,
) -> list:
    """Build ownership-scoped tools exposed to Arthur V2's Deep Agent graph."""

    async def _owned(agent_id: str) -> Agent | None:
        try:
            parsed_id = uuid.UUID(str(agent_id))
        except (TypeError, ValueError, AttributeError):
            return None
        async with db_factory() as db:
            result = await db.execute(
                select(Agent).where(Agent.id == parsed_id, Agent.is_deleted.is_(False))
            )
            agent = result.scalar_one_or_none()
            if agent is None or str(agent.id) == str(self_agent_id):
                return None
            if not agent_belongs_to_owner(agent, owner_phone):
                return None
            return agent

    async def _current_plan_snapshot() -> dict[str, Any]:
        """Read the caller's subscription and assistant capacity from verified identifiers."""
        from app.core.domain.subscription_service import get_best_subscription_by_external_ids

        identifiers = [owner_phone, default_target]
        async with db_factory() as db:
            details = await get_best_subscription_by_external_ids(identifiers, db)
            if details is None:
                return {
                    "ok": False,
                    "error": "Status plan untuk nomor WhatsApp ini belum ditemukan.",
                }
            user, subscription, plan = details
            owner_identifier = best_owner_identifier(
                getattr(user, "phone_number", None),
                getattr(user, "external_id", None),
                owner_phone,
                default_target,
            )
            agents_result = await db.execute(
                select(Agent).where(Agent.is_deleted.is_(False), owner_filter(owner_identifier))
            )
            managed_agents = [
                agent for agent in agents_result.scalars().all()
                if str(agent.id) != str(self_agent_id)
                and not bool((getattr(agent, "tools_config", None) or {}).get("builder"))
            ]
            limit = plan.max_agents
            used = len(managed_agents)
            remaining = None if limit is None else max(0, limit - used)
            return {
                "ok": True,
                "user_id": str(user.id),
                "plan_code": plan.code,
                "plan_label": plan.label,
                "subscription_status": subscription.status,
                "subscription_active": bool(subscription.is_usable),
                "agents_used": used,
                "agents_limit": limit,
                "agents_remaining": remaining,
                "active_assistant_names": [agent.name for agent in managed_agents],
                "tokens_remaining": getattr(subscription, "tokens_remaining", None),
                "expires_at": subscription.expires_at.isoformat() if subscription.expires_at else None,
            }

    @tool
    async def list_managed_assistants() -> dict[str, Any]:
        """List the caller's active personal or business assistants."""
        async with db_factory() as db:
            result = await db.execute(
                select(Agent)
                .where(Agent.is_deleted.is_(False), owner_filter(owner_phone))
                .order_by(Agent.updated_at.desc())
            )
            agents = [a for a in result.scalars().all() if str(a.id) != str(self_agent_id)]
        return {"assistants": [_summary(agent) for agent in agents]}

    @tool
    async def get_current_plan() -> dict[str, Any]:
        """Get the caller's live Clevio tier, active assistant count, remaining slots, token balance, and expiry."""
        return await _current_plan_snapshot()

    @tool
    async def inspect_managed_assistant(agent_id: str) -> dict[str, Any]:
        """Read one caller-owned assistant before changing it."""
        agent = await _owned(agent_id)
        if agent is None:
            return {"ok": False, "error": "Assistant tidak ditemukan atau bukan milik pengguna ini."}
        return {"ok": True, "assistant": {**_summary(agent), "instructions": agent.instructions}}

    @tool(args_schema=CreateAssistantInput)
    async def create_assistant(
        name: str,
        purpose: str,
        instructions: str,
        assistant_kind: str = "personal",
        workflow: AssistantWorkflowInput | dict[str, str] | None = None,
        enable_deploy: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Create an assistant after explicit confirmation; business assistants require a concrete operating workflow."""
        if not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Minta konfirmasi eksplisit sebelum membuat assistant."}
        combined = f"{name}\n{purpose}\n{instructions}"
        blocked_reason = blocked_agent_policy_reason(combined)
        if blocked_reason:
            return {"ok": False, "error": blocked_reason}
        if not name.strip() or not purpose.strip() or not instructions.strip():
            return {"ok": False, "error": "Nama, tujuan, dan instruksi assistant wajib diisi."}
        normalized_kind = assistant_kind.strip().lower()
        workflow_data = _workflow_data(workflow)
        if normalized_kind in _BUSINESS_ASSISTANT_KINDS:
            missing = _missing_workflow_fields(workflow_data)
            if missing:
                return {
                    "ok": False,
                    "needs_workflow_details": True,
                    "error": "Workflow bisnis belum cukup detail untuk dibuat dengan aman.",
                    "missing": missing,
                }
        plan_snapshot = await _current_plan_snapshot()
        if not plan_snapshot.get("ok"):
            return plan_snapshot
        if not plan_snapshot.get("subscription_active"):
            return {
                "ok": False,
                "needs_plan_action": True,
                "error": "Plan kamu tidak aktif, jadi assistant baru belum bisa dibuat.",
                "plan": plan_snapshot,
            }
        if plan_snapshot.get("agents_remaining") == 0:
            return {
                "ok": False,
                "needs_plan_upgrade": True,
                "error": "Slot assistant di plan kamu sudah penuh. Upgrade plan sebelum membuat assistant baru.",
                "plan": plan_snapshot,
                "recommended_plan": "tier_2" if plan_snapshot.get("agents_limit") == 1 else "tier_3",
            }
        async with db_factory() as db:
            agent = Agent(
                name=name.strip(),
                description=purpose.strip(),
                instructions=instructions.strip(),
                model=ARTHUR_V2_ASSISTANT_MODEL,
                channel_type="whatsapp",
                owner_external_id=owner_phone,
                operator_ids=[owner_phone] if owner_phone else [],
                tools_config={
                    "sandbox": bool(enable_deploy),
                    "deploy": bool(enable_deploy),
                    "assistant_profile": {
                        "kind": normalized_kind,
                        "workflow": workflow_data,
                    },
                },
                created_by_type="arthur_v2",
                created_by_agent_id=str(self_agent_id or ""),
                created_by_agent_name="Arthur",
            )
            db.add(agent)
            await db.commit()
            await db.refresh(agent)
        return {
            "ok": True,
            "assistant": _summary(agent),
            "runtime": {"sandbox": bool(enable_deploy), "deploy": bool(enable_deploy)},
            "next_step": (
                "Assistant website siap menerima brief dan akan mengirim URL publik setelah deployment berhasil."
                if enable_deploy
                else "Assistant dibuat. Hubungkan WhatsApp saat user siap mencoba."
            ),
        }

    @tool
    async def update_assistant(
        agent_id: str,
        purpose: str = "",
        instructions: str = "",
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Update purpose or instructions of a caller-owned assistant after explicit confirmation."""
        if not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Minta konfirmasi eksplisit sebelum mengubah assistant."}
        agent = await _owned(agent_id)
        if agent is None:
            return {"ok": False, "error": "Assistant tidak ditemukan atau bukan milik pengguna ini."}
        if not purpose.strip() and not instructions.strip():
            return {"ok": False, "error": "Berikan tujuan atau instruksi yang ingin diubah."}
        combined = f"{purpose}\n{instructions}"
        blocked_reason = blocked_agent_policy_reason(combined)
        if blocked_reason:
            return {"ok": False, "error": blocked_reason}
        async with db_factory() as db:
            managed = await db.get(Agent, agent.id)
            if purpose.strip():
                managed.description = purpose.strip()
            if instructions.strip():
                managed.instructions = instructions.strip()
            managed.version += 1
            await db.commit()
            await db.refresh(managed)
            return {"ok": True, "assistant": _summary(managed)}

    @tool
    async def add_assistant_knowledge(
        agent_id: str,
        filename: str = "",
        title: str = "",
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Add a document from this WhatsApp session to one owned assistant's RAG knowledge base.

        The document is extracted, chunked, embedded, and saved to the target
        assistant. Use only after the user explicitly asks or confirms that the
        uploaded document should become that assistant's knowledge.
        """
        if not confirmed:
            return {
                "ok": False,
                "needs_confirmation": True,
                "error": "Minta konfirmasi eksplisit sebelum menambahkan dokumen ke knowledge assistant.",
            }
        if not session_id:
            return {
                "ok": False,
                "error": "Konteks sesi file tidak tersedia. Minta user mengirim ulang dokumen.",
            }

        agent = await _owned(agent_id)
        if agent is None:
            return {"ok": False, "error": "Assistant tidak ditemukan atau bukan milik pengguna ini."}

        from app.config import get_settings
        from app.core.domain.document_service import create_document
        from app.core.domain.file_processor import SUPPORTED_EXTENSIONS, chunk_text, extract_text
        from app.core.infra.sandbox import get_workspace_dir

        workspace = get_workspace_dir(session_id).resolve()
        search_roots = (workspace / "shared" / "current_input", workspace / "shared", workspace)
        requested_name = Path(filename.strip()).name if filename.strip() else ""
        candidates: list[Path] = []
        for root in search_roots:
            if not root.is_dir():
                continue
            for path in root.iterdir():
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                if requested_name and path.name != requested_name:
                    continue
                candidates.append(path)
        if not candidates:
            requested = f" '{requested_name}'" if requested_name else ""
            return {
                "ok": False,
                "error": (
                    f"Dokumen{requested} tidak ditemukan pada sesi ini. "
                    "Kirim ulang file PDF, DOCX, PPTX, TXT, MD, atau CSV."
                ),
            }
        target_file = max(candidates, key=lambda path: path.stat().st_mtime)
        try:
            target_file.resolve().relative_to(workspace)
            raw = target_file.read_bytes()
        except (OSError, ValueError):
            return {"ok": False, "error": "Dokumen sesi tidak dapat dibaca dengan aman."}
        if not raw:
            return {"ok": False, "error": f"Dokumen {target_file.name} kosong."}

        try:
            full_text = await extract_text(
                content=raw,
                filename=target_file.name,
                content_type=None,
                mistral_api_key=get_settings().mistral_api_key,
            )
        except Exception as exc:
            return {"ok": False, "error": f"Gagal mengekstrak teks dari {target_file.name}: {exc}"}
        if not full_text.strip():
            return {"ok": False, "error": f"Tidak ada teks yang bisa diekstrak dari {target_file.name}."}
        chunks = chunk_text(full_text)
        if not chunks:
            return {"ok": False, "error": f"Dokumen {target_file.name} tidak menghasilkan knowledge yang dapat disimpan."}

        doc_title = title.strip() or target_file.name
        try:
            async with db_factory() as db:
                managed = await db.get(Agent, agent.id)
                if managed is None or managed.is_deleted or not agent_belongs_to_owner(managed, owner_phone):
                    return {"ok": False, "error": "Assistant tidak ditemukan atau bukan milik pengguna ini."}
                total = len(chunks)
                for index, content in enumerate(chunks, start=1):
                    chunk_title = doc_title if total == 1 else f"{doc_title} (Part {index}/{total})"
                    await create_document(
                        agent_id=managed.id,
                        title=chunk_title,
                        content=content,
                        source=target_file.name,
                        doc_metadata={
                            "original_filename": target_file.name,
                            "chunk_index": index,
                            "total_chunks": total,
                            "added_by": "arthur_v2",
                        },
                        db=db,
                    )
                config = dict(managed.tools_config or {})
                rag_was_enabled = bool(config.get("rag"))
                if not rag_was_enabled:
                    config["rag"] = True
                    managed.tools_config = config
                managed.version += 1
                await db.commit()
                assistant = _summary(managed)
        except Exception as exc:
            return {"ok": False, "error": f"Gagal menyimpan knowledge ke assistant: {exc}"}

        return {
            "ok": True,
            "assistant": assistant,
            "filename": target_file.name,
            "title": doc_title,
            "chunks_added": len(chunks),
            "extracted_chars": len(full_text),
            "rag_enabled": True,
            "rag_was_already_enabled": rag_was_enabled,
        }

    @tool
    async def delete_assistant(agent_id: str, confirmed: bool = False) -> dict[str, Any]:
        """Soft-delete a caller-owned assistant only after explicit confirmation."""
        if not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Minta konfirmasi eksplisit sebelum menghapus assistant."}
        agent = await _owned(agent_id)
        if agent is None:
            return {"ok": False, "error": "Assistant tidak ditemukan atau bukan milik pengguna ini."}
        async with db_factory() as db:
            managed = await db.get(Agent, agent.id)
            managed.is_deleted = True
            managed.updated_at = datetime.now(timezone.utc)
            await db.commit()
        return {"ok": True, "deleted_agent_id": str(agent.id)}

    @tool
    async def configure_assistant_runtime(
        agent_id: str,
        enable_sandbox: bool = False,
        enable_deploy: bool = False,
        subagent_ids: list[str] | None = None,
        mcp_servers: dict[str, str] | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Configure sandbox, public deployment, owned subagents, and approved MCP servers after explicit confirmation."""
        if not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Minta konfirmasi eksplisit sebelum mengaktifkan sandbox, deploy, subagent, atau MCP."}
        agent = await _owned(agent_id)
        if agent is None:
            return {"ok": False, "error": "Assistant tidak ditemukan atau bukan milik pengguna ini."}
        requested_subagents = list(dict.fromkeys(subagent_ids or []))
        for subagent_id in requested_subagents:
            subagent = await _owned(subagent_id)
            if subagent is None or "builder" in (subagent.capabilities or []):
                return {"ok": False, "error": "Setiap subagent harus merupakan assistant aktif milik user dan bukan builder."}
        servers: dict[str, dict[str, str]] = {}
        for name, url in (mcp_servers or {}).items():
            parsed = urlparse(str(url))
            if not str(name).strip() or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return {"ok": False, "error": "Setiap MCP server harus memiliki nama dan URL http(s) yang valid."}
            servers[str(name).strip()] = {"url": str(url)}
        async with db_factory() as db:
            managed = await db.get(Agent, agent.id)
            config = dict(managed.tools_config or {})
            sandbox_enabled = bool(enable_sandbox or enable_deploy)
            config["sandbox"] = sandbox_enabled
            config["deploy"] = bool(enable_deploy)
            config["subagents"] = {"enabled": bool(requested_subagents), "agent_ids": requested_subagents}
            config["mcp"] = {"enabled": bool(servers), "servers": servers}
            managed.tools_config = config
            managed.version += 1
            await db.commit()
            await db.refresh(managed)
        return {
            "ok": True,
            "assistant": _summary(managed),
            "runtime": {"sandbox": sandbox_enabled, "deploy": bool(enable_deploy), "subagent_count": len(requested_subagents), "mcp_servers": sorted(servers)},
        }

    @tool
    async def get_payment_link(plan: str) -> dict[str, Any]:
        """Create a payment link only when the caller explicitly asks to buy or upgrade a Clevio plan."""
        plan_code = resolve_payment_plan(plan)
        if plan_code is None:
            return {"ok": False, "error": "Pilih paket Starter/tier_1, Pro/tier_2, atau Enterprise/tier_3."}
        phone = normalize_phone(owner_phone or default_target)
        if not phone:
            return {"ok": False, "error": "Nomor WhatsApp pemilik belum tersedia; link pembayaran tidak dibuat."}
        return {
            "ok": True,
            "plan_code": plan_code,
            "plan_label": PLAN_LABELS[plan_code],
            "max_agents": PLAN_CAPACITY[plan_code],
            "payment_link": build_payment_link(plan_code, phone),
            "message": "Harga dan periode final ditampilkan di checkout. Paket aktif setelah notifikasi pembayaran berhasil diproses.",
        }

    @tool
    async def connect_assistant_whatsapp(agent_id: str, confirmed: bool = False) -> dict[str, Any]:
        """Create or reconnect a caller-owned assistant's WhatsApp device and return the QR code after explicit confirmation."""
        if not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Minta konfirmasi eksplisit sebelum membuat atau menyambungkan perangkat WhatsApp."}
        agent = await _owned(agent_id)
        if agent is None:
            return {"ok": False, "error": "Assistant tidak ditemukan atau bukan milik pengguna ini."}
        async with db_factory() as db:
            managed = await db.get(Agent, agent.id)
            if not managed.wa_device_id:
                managed.wa_device_id = str(uuid.uuid4())
                managed.channel_type = "whatsapp"
                await db.commit()
                await db.refresh(managed)
            device_id = managed.wa_device_id
        try:
            from app.core.infra.wa_client import create_wa_device

            result = await create_wa_device(device_id)
        except Exception as exc:
            return {"ok": False, "error": "WhatsApp service belum dapat membuat QR.", "detail": str(exc)[:200], "device_id": device_id}
        return {
            "ok": True,
            "assistant": _summary(agent),
            "device_id": device_id,
            "qr_image": result.get("qr_image", ""),
            "status": result.get("status", "waiting_qr"),
            "next_step": "Minta user scan QR dari WhatsApp. Setelah itu cek status koneksi.",
        }

    @tool
    async def get_assistant_whatsapp_status(agent_id: str) -> dict[str, Any]:
        """Check whether a caller-owned assistant's WhatsApp device is waiting for QR or connected."""
        agent = await _owned(agent_id)
        if agent is None:
            return {"ok": False, "error": "Assistant tidak ditemukan atau bukan milik pengguna ini."}
        if not agent.wa_device_id:
            return {"ok": False, "error": "Assistant belum memiliki perangkat WhatsApp. Gunakan connect_assistant_whatsapp terlebih dahulu."}
        try:
            from app.core.infra.wa_client import get_wa_status

            result = await get_wa_status(agent.wa_device_id)
        except Exception as exc:
            return {"ok": False, "error": "Status WhatsApp belum dapat diperiksa.", "detail": str(exc)[:200]}
        return {"ok": True, "device_id": agent.wa_device_id, "status": result.get("status", "unknown"), "phone_number": result.get("phone_number", "")}

    @tool
    async def refresh_assistant_whatsapp_qr(agent_id: str, confirmed: bool = False) -> dict[str, Any]:
        """Generate a fresh QR for a caller-owned assistant; this may replace its current WhatsApp session, so confirmation is required."""
        if not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Minta konfirmasi eksplisit sebelum memperbarui QR karena sesi WhatsApp dapat diganti."}
        agent = await _owned(agent_id)
        if agent is None or not agent.wa_device_id:
            return {"ok": False, "error": "Assistant belum memiliki perangkat WhatsApp yang dapat diperbarui."}
        try:
            from app.core.infra.wa_client import refresh_wa_qr

            result = await refresh_wa_qr(agent.wa_device_id)
        except Exception as exc:
            return {"ok": False, "error": "QR WhatsApp baru belum dapat dibuat.", "detail": str(exc)[:200]}
        return {"ok": True, "device_id": agent.wa_device_id, "qr_image": result.get("qr_image", ""), "status": result.get("status", "waiting_qr")}

    @tool
    async def create_demo_whatsapp_trial(agent_id: str, confirmed: bool = False, force_new_code: bool = False) -> dict[str, Any]:
        """Create a reusable code to try one caller-owned assistant through the shared Arthur demo WhatsApp number."""
        if not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Minta konfirmasi eksplisit sebelum mengaktifkan trial di nomor demo Arthur."}
        agent = await _owned(agent_id)
        if agent is None:
            return {"ok": False, "error": "Assistant tidak ditemukan atau bukan milik pengguna ini."}
        async with db_factory() as db:
            managed = await db.get(Agent, agent.id)
            from app.core.domain.wa_dev_trial_service import ensure_wa_dev_trial_code

            code = await ensure_wa_dev_trial_code(db, managed, force_new=force_new_code)
            await db.commit()
        from app.config import get_settings

        shared_phone = normalize_phone(get_settings().wa_dev_public_phone)
        if not shared_phone:
            try:
                from app.core.infra.wa_client import get_wa_dev_status

                shared_phone = normalize_phone((await get_wa_dev_status()).get("phone_number") or "")
            except Exception:
                shared_phone = ""
        if not shared_phone:
            return {"ok": True, "assistant": _summary(agent), "code": code, "warning": "Kode dibuat, tetapi nomor demo belum tersedia dari konfigurasi atau wa-dev-service."}
        prefill = quote(f"Halo Arthur, saya mau coba agent saya. Kode saya: {code}")
        return {"ok": True, "assistant": _summary(agent), "code": code, "shared_whatsapp_phone": f"+{shared_phone}", "wa_me_url": f"https://wa.me/{shared_phone}?text={prefill}", "next_step": f"Buka link nomor demo dan kirim kode {code}. Gunakan /stop di nomor demo untuk mengakhiri trial."}

    @tool
    async def start_google_mcp_oauth(agent_id: str, scopes: list[str] | None = None, confirmed: bool = False) -> dict[str, Any]:
        """Start OAuth for Google Workspace MCP on one caller-owned assistant, then return the authorization link."""
        if not confirmed:
            return {"ok": False, "needs_confirmation": True, "error": "Minta konfirmasi eksplisit sebelum menghubungkan akun Google ke assistant ini."}
        agent = await _owned(agent_id)
        if agent is None:
            return {"ok": False, "error": "Assistant tidak ditemukan atau bukan milik pengguna ini."}
        if not owner_phone:
            return {"ok": False, "error": "Identitas pemilik belum tersedia; OAuth tidak dapat dimulai dengan aman."}
        requested_scopes = [scope.strip() for scope in (scopes or []) if scope.strip()]
        try:
            mcp_url = google_mcp_url()
            oauth_start = await start_google_oauth(
                external_user_id=owner_phone,
                agent_id=str(agent.id),
                scopes=requested_scopes,
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": "Google OAuth belum dapat dimulai.",
                "detail": f"{type(exc).__name__}: {str(exc) or 'tanpa detail dari service'}"[:240],
            }
        async with db_factory() as db:
            managed = await db.get(Agent, agent.id)
            managed.tools_config = _with_google_workspace_mcp(
                managed.tools_config,
                mcp_url=mcp_url,
                integration_status="connected" if oauth_start.connected else "auth_pending",
            )
            managed.version += 1
            await db.commit()
        if oauth_start.connected:
            return {
                "ok": True,
                "assistant": _summary(agent),
                "status": "connected",
                "next_step": "Google Workspace sudah terhubung dan siap dipakai oleh assistant ini.",
            }
        return {
            "ok": True,
            "assistant": _summary(agent),
            "auth_url": oauth_start.auth_url,
            "status": "auth_pending",
            "next_step": "Buka link OAuth, pilih akun Google, lalu cek status sebelum memakai tool Google MCP.",
        }

    @tool
    async def get_google_mcp_oauth_status(agent_id: str) -> dict[str, Any]:
        """Verify OAuth status for Google Workspace MCP before claiming the assistant can use Google tools."""
        agent = await _owned(agent_id)
        if agent is None:
            return {"ok": False, "error": "Assistant tidak ditemukan atau bukan milik pengguna ini."}
        if not owner_phone:
            return {"ok": False, "error": "Identitas pemilik belum tersedia; status OAuth tidak dapat diperiksa."}
        try:
            oauth = await get_google_oauth_status(external_user_id=owner_phone, agent_id=str(agent.id))
        except Exception as exc:
            return {
                "ok": False,
                "error": "Status Google OAuth belum dapat diperiksa.",
                "detail": f"{type(exc).__name__}: {str(exc) or 'tanpa detail dari service'}"[:240],
            }
        integration_state = _google_integration_state(oauth)
        async with db_factory() as db:
            managed = await db.get(Agent, agent.id)
            config = dict(managed.tools_config or {})
            integration_status = dict(config.get("integration_status") or {})
            integration_status["google_workspace"] = integration_state
            config["integration_status"] = integration_status
            managed.tools_config = config
            managed.version += 1
            await db.commit()
        return {
            "ok": True,
            "assistant_id": str(agent.id),
            "oauth": oauth,
            "integration_status": integration_state,
        }

    return [
        list_managed_assistants,
        get_current_plan,
        inspect_managed_assistant,
        create_assistant,
        update_assistant,
        add_assistant_knowledge,
        delete_assistant,
        configure_assistant_runtime,
        get_payment_link,
        connect_assistant_whatsapp,
        get_assistant_whatsapp_status,
        refresh_assistant_whatsapp_qr,
        create_demo_whatsapp_trial,
        start_google_mcp_oauth,
        get_google_mcp_oauth_status,
    ]
