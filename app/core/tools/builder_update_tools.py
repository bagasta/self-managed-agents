"""Agent update tool for Arthur builder."""
from __future__ import annotations

import json
import uuid
from typing import Any, Awaitable, Callable

import structlog
from langchain_core.tools import tool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.domain.memory_service import upsert_memory
from app.core.domain.agent_sop_service import (
    ensure_operating_manual_in_tools_config,
    get_latest_agent_operating_manual,
    summarize_operating_manual,
    upsert_agent_operating_manual,
)
from app.core.launch_safety import (
    SANDBOX_DISABLED_NOTICE,
    disable_sandbox_subagent_tools_config,
    sandbox_subagents_enabled,
)
from app.core.tools.builder_google import (
    enable_google_workspace_tools as _enable_google_workspace_tools,
    has_google_workspace_tools as _has_google_workspace_tools,
)
from app.core.tools.builder_identity import (
    agent_belongs_to_owner as _agent_belongs_to_owner,
    blocked_agent_policy_reason as _blocked_agent_policy_reason,
    owner_filter as _owner_filter,
    safe_agent_str_attr as _safe_agent_str_attr,
)
from app.core.tools.builder_intent import _critical_workflow_config_errors
from app.models.agent import Agent

logger = structlog.get_logger(__name__)

AsyncCallable = Callable[..., Awaitable[Any]]
LoggerProvider = Callable[[], Any]
StringTransformer = Callable[[str], str]


class _LoggerProxy:
    def __init__(self, provider: LoggerProvider) -> None:
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider(), name)


def _normalize_refresh_memory_mode(value: str | None) -> str:
    mode = str(value or "selective").strip().lower()
    if mode in {"none", "selective", "major"}:
        return mode
    return "selective"


def _build_refreshed_agent_memory_values(
    *,
    agent: Agent,
    context_version: int,
    mode: str,
    updated_fields: list[str],
) -> dict[str, str]:
    tools_config = agent.tools_config if isinstance(agent.tools_config, dict) else {}
    agent_name = _safe_agent_str_attr(agent, "name") or "Agent"
    description = _safe_agent_str_attr(agent, "description") or ""
    channel_type = _safe_agent_str_attr(agent, "channel_type") or ""
    owner_external_id = _safe_agent_str_attr(agent, "owner_external_id")
    summary = {
        "agent_id": str(agent.id),
        "agent_name": agent_name,
        "context_version": context_version,
        "refresh_mode": mode,
        "change_level": "major" if mode == "major" else "selective",
        "updated_fields": updated_fields,
        "description": description,
        "channel_type": channel_type,
        "active_tools": [key for key, value in tools_config.items() if value and value is not False],
        "owner_external_id": owner_external_id,
    }
    blueprint = {
        "agent_name": agent_name,
        "description": description,
        "channel_type": channel_type,
        "tools_config": tools_config,
        "escalation_config": agent.escalation_config if isinstance(agent.escalation_config, dict) else {},
        "updated_fields": updated_fields,
        "context_version": context_version,
        "source": "update_agent_refresh",
    }
    return {
        f"soul:v{context_version}": agent.instructions or "",
        f"agent_blueprint:v{context_version}": json.dumps(blueprint, ensure_ascii=False, indent=2),
        f"setup_summary:v{context_version}": json.dumps(summary, ensure_ascii=False, indent=2),
        "agent_context_version": str(context_version),
    }


async def _refresh_agent_context_memory(
    *,
    db: AsyncSession,
    agent: Agent,
    mode: str,
    updated_fields: list[str],
) -> dict[str, Any]:
    normalized_mode = _normalize_refresh_memory_mode(mode)
    if normalized_mode == "none":
        return {"mode": "none", "updated": False, "keys": []}

    context_version = int((getattr(agent, "version", None) or 1))
    values = _build_refreshed_agent_memory_values(
        agent=agent,
        context_version=context_version,
        mode=normalized_mode,
        updated_fields=updated_fields,
    )

    from app.core.domain.memory_service import upsert_memory

    for key, value in values.items():
        await upsert_memory(agent.id, key, value, db, scope=None)

    return {
        "mode": normalized_mode,
        "updated": True,
        "context_version": context_version,
        "keys": list(values.keys()),
    }


def _looks_like_destructive_instruction_shrink(
    current_instructions: str | None,
    new_instructions: str,
) -> bool:
    """Reject accidental summary-only overwrites of established agent prompts."""
    current_len = len(current_instructions or "")
    new_len = len(new_instructions or "")
    if current_len < 1000:
        return False
    minimum_len = max(500, int(current_len * 0.35))
    return new_len < minimum_len


def build_builder_update_tools(
    db_factory: async_sessionmaker,
    *,
    owner_phone: str | None = None,
    self_agent_id: str | None = None,
    agent_model: Any = Agent,
    append_platform_staff_identity_instruction: StringTransformer,
    append_google_workspace_instruction: StringTransformer,
    get_logger: LoggerProvider | None = None,
) -> dict[str, Any]:
    _get_logger = get_logger or (lambda: logger)
    logger = _LoggerProxy(_get_logger)
    Agent = agent_model
    _append_platform_staff_identity_instruction = append_platform_staff_identity_instruction
    _append_google_workspace_instruction = append_google_workspace_instruction

    @tool
    async def update_agent(
        agent_id: str,
        name: str = "",
        instructions: str = "",
        description: str = "",
        model: str = "",
        temperature: float = -1.0,
        tools_config: str = "",
        allowed_senders: str = "",
        escalation_config: str = "",
        add_operator: str = "",
        remove_operator: str = "",
        enable_google_workspace: bool = False,
        refresh_memory_mode: str = "selective",
        business_context: str = "",
        domain: str = "",
        operating_manual: Any = None,
    ) -> str:
        """
        Update konfigurasi agent yang sudah ada. Hanya field yang diisi yang akan diubah.
        Hanya bisa mengupdate agent yang dimiliki oleh user ini (owner_phone).
        Untuk mengaktifkan kemampuan Google Docs/Sheets/Drive/Gmail/Calendar, gunakan
        enable_google_workspace=True agar tools_config dan instruksi agent diperbarui sekaligus.

        Args:
            agent_id: UUID agent yang akan diupdate
            name: Nama baru (opsional)
            instructions: System prompt baru (opsional)
            description: Deskripsi baru (opsional)
            model: Model LLM baru (opsional)
            temperature: Temperature baru 0.0-2.0, isi -1 untuk tidak mengubah (opsional)
            tools_config: JSON string tools_config baru (opsional)
            allowed_senders: JSON array nomor WA baru, kosong = tidak diubah (opsional)
            escalation_config: JSON string escalation_config baru (opsional)
            add_operator: Nomor WA operator baru yang ingin ditambahkan ke operator_ids (opsional)
            remove_operator: Nomor WA operator yang ingin dihapus dari operator_ids (opsional)
            enable_google_workspace: True untuk mengaktifkan integrasi Google Workspace
                                     dan menambahkan instruksi operasional Google ke agent.
            refresh_memory_mode: "none" | "selective" | "major".
                                 Default selective: update workflow/persona menulis memory versi aktif baru.
            business_context: Konteks bisnis terbaru untuk refresh SOP agent jika workflow berubah.
            domain: Domain bisnis terbaru jika SOP perlu dibuat ulang.
            operating_manual: Agent Operating Manual/SOP artifact baru. Jika diisi, disimpan sebagai versi baru.
        """
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError:
            return f"[error] agent_id tidak valid: {agent_id}"

        google_workspace_enabled = False
        launch_disabled_features: list[str] = []
        normalized_refresh_memory_mode = _normalize_refresh_memory_mode(refresh_memory_mode)
        memory_refresh_result: dict[str, Any] = {"mode": normalized_refresh_memory_mode, "updated": False, "keys": []}
        operating_manual_result: dict[str, Any] = {"updated": False}

        async with db_factory() as db:
            result = await db.execute(
                select(Agent).where(Agent.id == agent_uuid, Agent.is_deleted.is_(False))
            )
            agent = result.scalar_one_or_none()
            if not agent:
                return f"[error] Agent dengan ID {agent_id} tidak ditemukan"

            # Cek kepemilikan
            is_self_update = self_agent_id and str(agent_uuid) == self_agent_id
            if is_self_update:
                if owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
                    return (
                        "[error] Hanya operator yang terdaftar yang boleh memodifikasi konfigurasi agent builder ini. "
                        f"Nomor kamu ({owner_phone}) tidak ada di daftar operator."
                    )
            elif owner_phone and not _agent_belongs_to_owner(agent, owner_phone):
                return f"[error] Kamu tidak punya akses ke agent ini. Hanya agent milikmu yang bisa diubah."

            updated_fields: list[str] = []

            policy_reason = _blocked_agent_policy_reason(
                name,
                instructions,
                description,
                tools_config,
                escalation_config,
            )
            if policy_reason:
                return json.dumps({"error": policy_reason}, ensure_ascii=False)

            if name and name.strip():
                dup_result = await db.execute(
                    select(Agent).where(
                        Agent.id != agent_uuid,
                        func.lower(Agent.name) == name.strip().lower(),
                        Agent.is_deleted.is_(False),
                        _owner_filter(owner_phone),
                    )
                )
                duplicate = dup_result.scalar_one_or_none()
                if duplicate and getattr(duplicate, "id", None) != agent_uuid:
                    return json.dumps({
                        "error": f"Agent lain dengan nama '{name.strip()}' sudah ada.",
                        "existing_agent_id": str(duplicate.id),
                        "hint": "Pilih nama agent yang unik atau update agent tersebut memakai existing_agent_id.",
                    }, ensure_ascii=False)
                agent.name = name.strip()
                updated_fields.append("name")

            if instructions:
                clean_instructions = instructions.strip()
                if _looks_like_destructive_instruction_shrink(agent.instructions, clean_instructions):
                    return json.dumps(
                        {
                            "error": "Instruksi baru terlalu pendek dibanding instruksi agent yang sudah ada.",
                            "current_instructions_len": len(agent.instructions or ""),
                            "new_instructions_len": len(clean_instructions),
                            "hint": (
                                "Panggil get_agent_detail(agent_id, include_instructions=true), "
                                "gabungkan kebutuhan baru ke instruksi lama, lalu update ulang dengan instruksi lengkap."
                            ),
                        },
                        ensure_ascii=False,
                    )
                agent.instructions = clean_instructions
                updated_fields.append("instructions")
                if _has_google_workspace_tools(agent.tools_config if isinstance(agent.tools_config, dict) else {}):
                    google_workspace_enabled = True
                    updated_instructions, changed_instructions = _append_google_workspace_instruction(
                        agent.instructions or ""
                    )
                    if changed_instructions:
                        agent.instructions = updated_instructions
                        updated_fields.append("instructions+google_workspace")

            if description:
                agent.description = description
                updated_fields.append("description")

            if model:
                agent.model = model
                updated_fields.append("model")

            if temperature >= 0.0:
                agent.temperature = temperature
                updated_fields.append("temperature")

            if tools_config:
                try:
                    new_tc = json.loads(tools_config)
                    existing = dict(agent.tools_config) if agent.tools_config else {}
                    existing.update(new_tc)
                    existing.setdefault("tavily", True)
                    agent.tools_config = existing
                    updated_fields.append("tools_config")
                    if _has_google_workspace_tools(agent.tools_config):
                        google_workspace_enabled = True
                        updated_instructions, changed_instructions = _append_google_workspace_instruction(
                            agent.instructions or ""
                        )
                        if changed_instructions:
                            agent.instructions = updated_instructions
                            updated_fields.append("instructions+google_workspace")
                except json.JSONDecodeError:
                    return "[error] tools_config bukan JSON yang valid"

            if enable_google_workspace:
                before_google = _has_google_workspace_tools(
                    agent.tools_config if isinstance(agent.tools_config, dict) else {}
                )
                agent.tools_config = _enable_google_workspace_tools(
                    agent.tools_config if isinstance(agent.tools_config, dict) else {}
                )
                google_workspace_enabled = True
                if not before_google and "tools_config" not in updated_fields:
                    updated_fields.append("tools_config")
                if before_google:
                    updated_fields.append("google_workspace_already_enabled")

                updated_instructions, changed_instructions = _append_google_workspace_instruction(
                    agent.instructions or ""
                )
                if changed_instructions:
                    agent.instructions = updated_instructions
                    updated_fields.append("instructions+google_workspace")

            if not sandbox_subagents_enabled():
                current_tc = agent.tools_config if isinstance(agent.tools_config, dict) else {}
                sanitized_tc, launch_disabled_features = disable_sandbox_subagent_tools_config(current_tc)
                if launch_disabled_features:
                    agent.tools_config = sanitized_tc
                    if "tools_config" not in updated_fields:
                        updated_fields.append("tools_config")
                    updated_fields.append("launch_safety_sandbox_subagents_disabled")

            if _has_google_workspace_tools(agent.tools_config if isinstance(agent.tools_config, dict) else {}):
                google_workspace_enabled = True

            if allowed_senders and allowed_senders.strip():
                try:
                    parsed = json.loads(allowed_senders)
                    agent.allowed_senders = parsed if isinstance(parsed, list) else None
                    updated_fields.append("allowed_senders")
                except json.JSONDecodeError:
                    return "[error] allowed_senders harus JSON array"

            if escalation_config:
                try:
                    agent.escalation_config = json.loads(escalation_config)
                    updated_fields.append("escalation_config")
                except json.JSONDecodeError:
                    return "[error] escalation_config bukan JSON yang valid"

            if add_operator and add_operator.strip():
                current_ops: list[str] = list(agent.operator_ids or [])
                new_op = add_operator.strip()
                if new_op not in current_ops:
                    current_ops.append(new_op)
                    agent.operator_ids = current_ops
                    updated_fields.append(f"operator_ids+{new_op}")

            if remove_operator and remove_operator.strip():
                current_ops = list(agent.operator_ids or [])
                rm_op = remove_operator.strip()
                if rm_op in current_ops:
                    current_ops.remove(rm_op)
                    agent.operator_ids = current_ops
                    updated_fields.append(f"operator_ids-{rm_op}")

            if not updated_fields:
                return "[info] Tidak ada field yang diubah — kirim minimal satu field untuk diupdate"

            workflow_sensitive_update = any(
                field in updated_fields
                for field in (
                    "name",
                    "instructions",
                    "description",
                    "tools_config",
                    "escalation_config",
                    "instructions+google_workspace",
                )
            )
            if workflow_sensitive_update:
                critical_errors = _critical_workflow_config_errors(
                    name=agent.name or "",
                    description=agent.description or "",
                    instructions=agent.instructions or "",
                    tools_config=agent.tools_config if isinstance(agent.tools_config, dict) else {},
                    soul="",
                    blueprint="",
                )
                if critical_errors:
                    return json.dumps({
                        "error": "Konfigurasi agent belum aman untuk diupdate.",
                        "validation_errors": critical_errors,
                        "hint": (
                            "Panggil get_agent_detail(agent_id, include_instructions=true), "
                            "compose_agent_blueprint dan compose_agent_instructions ulang, lalu update_agent "
                            "dengan instructions dan tools_config lengkap."
                        ),
                    }, ensure_ascii=False, indent=2)

            manual_update_requested = bool(
                operating_manual not in (None, "", {})
                or business_context.strip()
                or domain.strip()
            )
            if workflow_sensitive_update or manual_update_requested:
                existing_manual = await get_latest_agent_operating_manual(
                    agent.id,
                    db,
                    fallback_tools_config=agent.tools_config if isinstance(agent.tools_config, dict) else {},
                )
                target_version = (agent.version or 1) + 1
                if manual_update_requested or not existing_manual:
                    updated_tc, next_manual = ensure_operating_manual_in_tools_config(
                        agent.tools_config if isinstance(agent.tools_config, dict) else {},
                        name=agent.name or "",
                        description=agent.description or "",
                        instructions=agent.instructions or "",
                        business_context=business_context,
                        domain=domain,
                        operating_manual=operating_manual,
                    )
                    agent.tools_config = updated_tc
                    if "tools_config" not in updated_fields:
                        updated_fields.append("tools_config")
                else:
                    next_manual = dict(existing_manual)
                    next_manual["version"] = target_version
                    next_manual["maturity"] = "needs_review"
                    next_manual["owner_review_required"] = True
                    missing_context = list(next_manual.get("missing_context") or [])
                    review_note = "SOP perlu review ulang karena workflow/config agent berubah."
                    if review_note not in missing_context:
                        missing_context.append(review_note)
                    next_manual["missing_context"] = missing_context
                next_manual["version"] = target_version
                if hasattr(Agent, "__table__"):
                    await upsert_agent_operating_manual(
                        agent.id,
                        next_manual,
                        db,
                        created_by_agent_id=str(self_agent_id) if self_agent_id else None,
                        version=target_version,
                    )
                operating_manual_result = summarize_operating_manual(next_manual)
                operating_manual_result["updated"] = True

            identity_sensitive_update = bool(
                instructions
                or description
                or tools_config
                or escalation_config
                or enable_google_workspace
            )
            if identity_sensitive_update:
                owner_for_identity = (
                    owner_phone
                    or getattr(agent, "owner_external_id", None)
                    or next(iter(getattr(agent, "operator_ids", None) or []), "")
                )
                if owner_for_identity:
                    agent_ec = agent.escalation_config if isinstance(agent.escalation_config, dict) else {}
                    updated_instructions, changed_instructions = _append_platform_staff_identity_instruction(
                        agent.instructions or "",
                        owner_phone=owner_for_identity,
                        operator_phone=str(agent_ec.get("operator_phone", "") or ""),
                        operator_name=str(agent_ec.get("operator_name", "") or ""),
                    )
                    if changed_instructions:
                        agent.instructions = updated_instructions
                        updated_fields.append("instructions+platform_identity")

            entitlement_sensitive_update = any(
                field in updated_fields for field in ("model", "tools_config")
            )
            if entitlement_sensitive_update and not is_self_update and owner_phone and hasattr(Agent, "__table__"):
                from app.core.domain.subscription_service import (
                    get_subscription_by_external_id,
                    validate_agent_entitlements,
                )

                sub_details = await get_subscription_by_external_id(owner_phone, db)
                if sub_details is None:
                    return json.dumps({"error": "Subscription tidak ditemukan."}, ensure_ascii=False)
                _, _, plan = sub_details
                entitlement_errors = validate_agent_entitlements(
                    plan,
                    model=agent.model,
                    tools_config=agent.tools_config if isinstance(agent.tools_config, dict) else {},
                    channel_type=agent.channel_type,
                )
                if entitlement_errors:
                    return json.dumps(
                        {
                            "error": "Konfigurasi agent melebihi entitlement plan.",
                            "plan": plan.label,
                            "violations": entitlement_errors,
                        },
                        ensure_ascii=False,
                    )

            agent.version = (agent.version or 1) + 1
            if identity_sensitive_update:
                memory_refresh_result = await _refresh_agent_context_memory(
                    db=db,
                    agent=agent,
                    mode=normalized_refresh_memory_mode,
                    updated_fields=updated_fields,
                )
            await db.commit()

        logger.info("builder_tools.update_agent.success", agent_id=agent_id, fields=updated_fields)
        response = {
            "success": True,
            "agent_id": agent_id,
            "agent_name": agent.name,
            "updated_fields": updated_fields,
            "new_version": agent.version,
            "memory_refresh": memory_refresh_result,
            "operating_manual": operating_manual_result,
            "message": f"Agent '{agent.name}' sudah saya edit.",
        }
        if google_workspace_enabled:
            response["google_workspace_enabled"] = True
            response["needs_google_auth"] = True
            response["readback"] = {
                "tools_config_has_google_workspace": _has_google_workspace_tools(
                    agent.tools_config if isinstance(agent.tools_config, dict) else {}
                ),
                "instructions_include_google_workspace": "Google Workspace" in (agent.instructions or ""),
            }
            response["next_step"] = (
                "Panggil get_agent_detail(agent_id) untuk verifikasi readback. "
                "Setelah readback benar, panggil generate_google_auth_link(agent_id, external_user_id=nomor user saat ini) "
                "dan kirim link otentikasi Google ke user jika tersedia. "
                "Saat menjelaskan ke user, sebut 'integrasi Google/Google Docs', jangan sebut istilah teknis internal/protokol tool."
            )
        if launch_disabled_features:
            response["launch_safety"] = {
                "sandbox_subagents_enabled": False,
                "disabled_features": launch_disabled_features,
                "message": SANDBOX_DISABLED_NOTICE,
            }
        return json.dumps(response, ensure_ascii=False, indent=2)

    return {"update_agent": update_agent}
