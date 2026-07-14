"""Agent creation tool for Arthur builder."""
from __future__ import annotations

import json
import uuid
from typing import Any, Awaitable, Callable

import structlog
from langchain_core.tools import tool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.domain.agent_sop_service import (
    build_agent_operating_manual_from_blueprint,
    ensure_operating_manual_in_tools_config,
    summarize_operating_manual,
    upsert_agent_operating_manual,
)
from app.core.launch_safety import (
    SANDBOX_DISABLED_NOTICE,
    disable_sandbox_subagent_tools_config,
    sandbox_subagents_enabled,
)
from app.core.tools.builder_fallbacks import (
    _blueprint_needs_semantic_operating_manual,
    _fallback_agent_blueprint,
    mark_manual_needs_review_if_fallback,
)
from app.core.tools.builder_google import has_google_workspace_tools as _has_google_workspace_tools
from app.core.tools.builder_identity import (
    agent_created_by_metadata as _agent_created_by_metadata,
    blocked_agent_policy_reason as _blocked_agent_policy_reason,
    extract_operator_phone_from_context as _extract_operator_phone_from_context,
    owner_filter as _owner_filter,
    owner_variants as _owner_variants,
)
from app.core.tools.builder_json import parse_llm_json_object as _parse_llm_json_object
from app.core.tools.builder_intent import (
    _detect_preset_from_config,
    _file_capability_negated,
    _has_approval_state_contract,
    _looks_like_approval_gated_service,
    _looks_like_file_delivery_workflow,
    _looks_like_generated_file_workflow,
    _looks_like_payment_approval_workflow,
    _sanitize_unverified_business_name,
    file_delivery_contract_issues,
)
from app.core.tools.builder_json import parse_json_arg as _parse_json_arg
from app.core.model_defaults import CREATED_AGENT_DEFAULT_MODEL
from app.models.agent import Agent

logger = structlog.get_logger(__name__)

_BLUEPRINT_WRITER_MODEL = "deepseek/deepseek-v4-pro"

AsyncCallable = Callable[..., Awaitable[Any]]
LoggerProvider = Callable[[], Any]
StringTransformer = Callable[[str], str]
BlockProvider = Callable[[], str]


class _LoggerProxy:
    def __init__(self, provider: LoggerProvider) -> None:
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider(), name)


def build_builder_create_tools(
    db_factory: async_sessionmaker,
    *,
    owner_phone: str | None = None,
    self_agent_id: str | None = None,
    agent_model: Any = Agent,
    preview_agent_creation_entitlement: AsyncCallable,
    call_instruction_writer: AsyncCallable,
    append_platform_staff_identity_instruction: StringTransformer,
    append_google_workspace_instruction: StringTransformer,
    platform_staff_identity_block: BlockProvider,
    get_logger: LoggerProvider | None = None,
) -> dict[str, Any]:
    _get_logger = get_logger or (lambda: logger)
    logger = _LoggerProxy(_get_logger)
    _preview_agent_creation_entitlement = preview_agent_creation_entitlement
    _call_instruction_writer = call_instruction_writer
    _append_platform_staff_identity_instruction = append_platform_staff_identity_instruction
    _append_google_workspace_instruction = append_google_workspace_instruction
    _platform_staff_identity_block = platform_staff_identity_block

    async def _compose_semantic_operating_manual_from_context(
        *,
        name: str,
        description: str,
        instructions: str,
        business_context: str,
        domain: str,
        channel_type: str,
        tools_config: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Ask the writer to infer a SOP semantically when Arthur skipped manual/blueprint."""
        context = "\n".join(
            part
            for part in (
                f"agent_name: {name}" if name else "",
                f"description: {description}" if description else "",
                f"business_context: {business_context}" if business_context else "",
                f"instructions: {instructions}" if instructions else "",
            )
            if part.strip()
        )
        if not context.strip():
            return None

        system_msg = (
            "Kamu adalah senior operations designer untuk AI agent bisnis. "
            "Tugasmu menyusun Agent Operating Manual/SOP dari makna konteks user, bukan dari pencocokan keyword. "
            "Baca niat bisnis, aktor yang terlibat, data yang perlu dikumpulkan, keputusan yang harus ditahan untuk manusia, "
            "risiko salah janji, dan definisi selesai. "
            "Jangan bergantung pada nama domain atau kata kunci eksplisit; kalau user menjelaskan alur dengan bahasa sehari-hari, infer workflow dari alur itu. "
            "Return HANYA JSON valid, tanpa markdown dan tanpa penjelasan di luar JSON."
        )
        user_msg = (
            "Susun Agent Operating Manual/SOP dari konteks ini.\n\n"
            f"{context}\n"
            f"domain_hint: {domain or '-'}\n"
            f"channel_type: {channel_type or '-'}\n"
            f"tools_config: {json.dumps(tools_config, ensure_ascii=False)}\n\n"
            "Schema JSON wajib:\n"
            "{\n"
            '  "manual_id": "agent_operating_manual",\n'
            '  "version": 1,\n'
            '  "source": "arthur_operating_manual_writer_auto",\n'
            '  "domain": "domain hasil inferensi semantik",\n'
            '  "domain_confidence": "high|medium|low",\n'
            '  "maturity": "usable|needs_review|draft",\n'
            '  "owner_review_required": false,\n'
            '  "missing_context": ["hanya data kritis yang benar-benar belum ada"],\n'
            '  "assumptions": ["asumsi operasional"],\n'
            '  "workflows": [{\n'
            '    "workflow_id": "snake_case_id",\n'
            '    "name": "Nama workflow",\n'
            '    "trigger": "Kapan workflow dimulai",\n'
            '    "goal": "Tujuan workflow",\n'
            '    "required_inputs": ["data wajib"],\n'
            '    "steps": ["langkah konkret berurutan"],\n'
            '    "decision_points": ["kondisi dan keputusan"],\n'
            '    "allowed_tools": ["tool relevan"],\n'
            '    "escalation_rules": ["kapan harus eskalasi ke manusia"],\n'
            '    "prohibited_actions": ["hal yang tidak boleh dilakukan"],\n'
            '    "final_output": "Definisi selesai yang nyata",\n'
            '    "examples": ["contoh pendek jika perlu"]\n'
            "  }],\n"
            '  "knowledge_plan": {"must_have": ["..."], "nice_to_have": ["..."], "needs_upload": false},\n'
            '  "memory_plan": [{"key": "...", "value_to_store": "..."}],\n'
            '  "validation_checklist": ["..."]\n'
            "}\n\n"
            "Aturan kualitas:\n"
            "- Buat SOP spesifik dari alur user, bukan SOP generic customer service.\n"
            "- Untuk agent yang bicara dengan customer, minimal ada intake data, boundary keputusan manusia, dan follow-up/closing.\n"
            "- Jika ada harga final, ketersediaan, booking, pembayaran, refund, komplain, atau approval, agent wajib berhenti dan eskalasi sebelum menjanjikan keputusan.\n"
            "- Jika konteks cukup untuk bekerja aman, set maturity usable. Jangan needs_review hanya karena daftar harga/detail lengkap belum diberikan."
        )
        try:
            raw = await _call_instruction_writer(
                user_msg,
                system_msg,
                model=_BLUEPRINT_WRITER_MODEL,
                max_tokens=7000,
                temperature=0.1,
                json_mode=True,
            )
            manual, _ = _parse_llm_json_object(raw)
        except Exception as exc:
            logger.warning(
                "builder_tools.semantic_operating_manual.failed",
                error=str(exc),
                agent_name=name,
                domain=domain,
            )
            return None

        if not isinstance(manual.get("workflows"), list) or not manual["workflows"]:
            logger.warning(
                "builder_tools.semantic_operating_manual.empty_workflows",
                agent_name=name,
                domain=domain,
            )
            return None
        manual.setdefault("manual_id", "agent_operating_manual")
        manual.setdefault("version", 1)
        manual.setdefault("source", "arthur_operating_manual_writer_auto")
        manual.setdefault("domain", domain or "semantic_business_workflow")
        manual.setdefault("domain_confidence", "medium")
        manual.setdefault("maturity", "usable")
        manual.setdefault("owner_review_required", manual.get("maturity") in {"draft", "needs_review"})
        manual.setdefault("missing_context", [])
        manual.setdefault("assumptions", [])
        manual.setdefault("knowledge_plan", {"must_have": [], "nice_to_have": [], "needs_upload": bool(tools_config.get("rag"))})
        manual.setdefault("memory_plan", [])
        manual.setdefault("validation_checklist", [])
        return manual
    Agent = agent_model

    @tool
    async def create_agent(
        name: str,
        instructions: str,
        description: str = "",
        model: str = CREATED_AGENT_DEFAULT_MODEL,
        temperature: float = 0.7,
        tools_config: Any = '{"memory": true, "skills": true, "escalation": true}',
        allowed_senders: Any = "",
        channel_type: str = "",
        escalation_config: Any = "{}",
        operator_phone: str = "",
        operator_name: str = "",
        token_quota: int = 4_000_000,
        max_tokens: int = 0,
        soul: str = "",
        blueprint: str = "",
        business_context: str = "",
        domain: str = "",
        operating_manual: Any = None,
        file_capability: str = "",
    ) -> str:
        """
        Buat agent baru di platform dan simpan ke database.
        Agent akan otomatis dikaitkan dengan user yang sedang chat (owner_phone).

        Args:
            name: Nama agent (wajib, maks 255 karakter)
            instructions: System prompt / instructions lengkap agent
            description: Deskripsi singkat fungsi agent
            model: Model LLM (default: deepseek/deepseek-v4-flash)
            temperature: Kreativitas respons, 0.0-2.0 (default: 0.7)
            tools_config: JSON string atau object konfigurasi tools, contoh: '{"memory": true, "scheduler": true}'
            allowed_senders: JSON array/string nomor WA yang diizinkan, contoh: '["+62811xxx"]'. Kosong = semua.
            channel_type: Channel yang dipakai: 'whatsapp' atau kosong. Arthur tidak membuat channel webchat/API.
            escalation_config: JSON string konfigurasi eskalasi, contoh: '{"channel_type": "whatsapp", "operator_phone": "+62xxx"}'
            operator_phone: Nomor WA operator/admin yang akan dapat notifikasi eskalasi
            operator_name: Nama operator/admin (misal: "Budi", "Tim CS"). Wajib diisi agar agent tahu siapa operatornya.
            token_quota: Batas token per periode (default: 4,000,000)
            max_tokens: Batas token per reply LLM. WA CS: 512-800, default platform: 1024. Isi 0 untuk pakai default.
            soul: Identitas permanen agent hasil compose_agent_soul. Jika diisi, disimpan otomatis ke memory key='soul'.
            blueprint: Agent Blueprint hasil compose_agent_blueprint. Jika diisi, disimpan otomatis ke memory key='agent_blueprint'.
            business_context: Ringkasan konteks bisnis Owner dari interview Arthur. Dipakai untuk membuat SOP terpisah.
            domain: Bidang bisnis jika sudah diketahui, misal food_beverage, travel, ecommerce, local_service, clinic_wellness, education, property.
            operating_manual: Agent Operating Manual/SOP artifact terstruktur. Jika kosong, Arthur/runtime membuat draft dari konteks.
            file_capability: Keputusan eksplisit kemampuan file agent — WAJIB diisi kalau kebutuhan file ambigu.
                'enabled' = agent perlu menerima/membuat file (otomatis aktifkan sandbox+whatsapp_media+subagents);
                'text_only' = user sudah konfirmasi agent hanya butuh teks. Kosongkan hanya jika workflow file sudah jelas dari instruksi.
        """
        if not name or len(name.strip()) < 2:
            return "[error] Nama agent minimal 2 karakter"
        policy_reason = _blocked_agent_policy_reason(
            name,
            description,
            instructions,
            tools_config,
            escalation_config,
            soul,
            blueprint,
        )
        if policy_reason:
            return json.dumps({"error": policy_reason}, ensure_ascii=False)
        if not owner_phone:
            return (
                "[error] Tidak bisa membuat agent karena owner_external_id tidak tersedia. "
                "Pastikan Arthur dijalankan dari session user yang memiliki external_user_id."
            )
        requested_channel_type = str(channel_type or "").strip().lower()
        channel_type = "whatsapp" if requested_channel_type != "whatsapp" else requested_channel_type

        tc, tc_error = _parse_json_arg(
            tools_config,
            {"memory": True, "skills": True, "escalation": True},
            expected=dict,
        )
        if tc_error:
            return f"[error] tools_config bukan JSON/object yang valid: {tc_error}"
        tc.setdefault("tavily", True)
        inferred_operator_phone = _extract_operator_phone_from_context(
            operator_phone,
            escalation_config,
            business_context,
            description,
            instructions,
            blueprint,
            soul,
        )
        if not operator_phone and inferred_operator_phone:
            operator_phone = inferred_operator_phone
        if operator_phone:
            tc["escalation"] = True
        operating_manual_input = operating_manual
        used_fallback = False
        if operating_manual_input in (None, "", {}) and str(blueprint or "").strip():
            if _blueprint_needs_semantic_operating_manual(blueprint):
                operating_manual_input = await _compose_semantic_operating_manual_from_context(
                    name=name,
                    description=description,
                    instructions=instructions,
                    business_context=business_context,
                    domain=domain,
                    channel_type=channel_type,
                    tools_config=tc,
                )
            if operating_manual_input in (None, "", {}):
                operating_manual_input = build_agent_operating_manual_from_blueprint(
                    blueprint,
                    name=name,
                    description=description,
                    business_context=business_context,
                    domain=domain,
                    tools_config=tc,
                )
        if operating_manual_input in (None, "", {}) and (business_context.strip() or description.strip()):
            operating_manual_input = await _compose_semantic_operating_manual_from_context(
                name=name,
                description=description,
                instructions=instructions,
                business_context=business_context,
                domain=domain,
                channel_type=channel_type,
                tools_config=tc,
            )
        if operating_manual_input in (None, "", {}) and (business_context.strip() or description.strip()):
            preset_for_manual = _detect_preset_from_config(tc, channel_type or "")
            fallback_blueprint = _fallback_agent_blueprint(
                preset_id=preset_for_manual,
                user_goal=description or name,
                agent_name=name,
                business_context=business_context or instructions,
                target_users="customer" if channel_type == "whatsapp" else "",
                channel=channel_type or "",
                requested_features=json.dumps(tc, ensure_ascii=False),
                known_constraints="",
                tools_config=tc,
            )
            operating_manual_input = build_agent_operating_manual_from_blueprint(
                fallback_blueprint,
                name=name,
                description=description,
                business_context=business_context or instructions,
                domain=domain,
                tools_config=tc,
            )
            used_fallback = True
        approval_gated_service = _looks_like_approval_gated_service(
            name,
            description,
            instructions,
            tools_config,
            soul,
            blueprint,
        )
        payment_approval_workflow = _looks_like_payment_approval_workflow(
            name,
            description,
            instructions,
            tools_config,
            soul,
            blueprint,
        ) or approval_gated_service
        file_delivery_workflow = _looks_like_file_delivery_workflow(
            name,
            description,
            instructions,
            tools_config,
            soul,
            blueprint,
        )
        generated_file_workflow = _looks_like_generated_file_workflow(
            name,
            description,
            instructions,
            tools_config,
            soul,
            blueprint,
        )
        file_decision = str(file_capability or "").strip().lower()
        fallback_file_workflow_is_safe_to_run = (
            used_fallback
            and channel_type == "whatsapp"
            and not payment_approval_workflow
            and (file_decision == "enabled" or file_delivery_workflow or generated_file_workflow)
        )
        operating_manual_input = mark_manual_needs_review_if_fallback(
            operating_manual_input,
            used_fallback=used_fallback,
            allow_usable_fallback=fallback_file_workflow_is_safe_to_run,
        )
        tc, generated_operating_manual = ensure_operating_manual_in_tools_config(
            tc,
            name=name,
            description=description,
            instructions=instructions,
            business_context=business_context or blueprint or soul,
            domain=domain,
            operating_manual=operating_manual_input,
        )
        if _has_google_workspace_tools(tc):
            instructions, _ = _append_google_workspace_instruction(instructions)
        instructions, business_name_sanitized = _sanitize_unverified_business_name(
            instructions,
            business_context=business_context or description,
        )
        if soul:
            soul, soul_business_name_sanitized = _sanitize_unverified_business_name(
                soul,
                business_context=business_context or description,
            )
        else:
            soul_business_name_sanitized = False
        platform_identity_added = False

        critical_errors: list[str] = []
        # Fix #3: gerbang keras keputusan kemampuan file. Heuristik file_delivery/generated
        # bisa meleset untuk agent yang baru diminta file saat RUNTIME (mis. user kirim CSV
        # lalu minta visualisasi). Kalau sinyal file ambigu — tidak terdeteksi, tidak dinegasikan
        # user, dan config belum file-ready (sandbox+whatsapp_media) — Arthur WAJIB memutuskan
        # eksplisit via file_capability, bukan menebak diam-diam lalu mengirim agent cacat.
        disabled_launch_features: list[str] = []
        if file_decision == "enabled":
            tc["whatsapp_media"] = True
            tc["sandbox"] = True
            _subc = tc.get("subagents")
            if isinstance(_subc, dict):
                _subc["enabled"] = True
            else:
                tc["subagents"] = {"enabled": True}
        if not sandbox_subagents_enabled():
            tc, disabled_launch_features = disable_sandbox_subagent_tools_config(tc)
            launch_blocked_workflow = (
                generated_file_workflow
                or bool(disabled_launch_features and file_decision == "enabled")
                or bool(disabled_launch_features and any(
                    key in disabled_launch_features
                    for key in ("sandbox", "deploy", "tool_creator", "subagents")
                ) and _looks_like_generated_file_workflow(
                    name, description, instructions, tools_config, soul, blueprint
                ))
            )
            if launch_blocked_workflow:
                return json.dumps({
                    "error": "Fitur sandbox/subagent sementara dinonaktifkan untuk launch.",
                    "validation_errors": [
                        "Agent yang membutuhkan coding, deploy, analisis/generate file, atau subagent belum boleh dibuat sementara."
                    ],
                    "hint": (
                        "Tanyakan user apakah mau dibuat versi agent chat/CS/escalation dulu tanpa analisis/generate file, "
                        "atau tunda fitur sandbox/subagent sampai stabilisasi selesai."
                    ),
                    "launch_safety": {
                        "sandbox_subagents_enabled": False,
                        "disabled_features": disabled_launch_features,
                        "message": SANDBOX_DISABLED_NOTICE,
                    },
                }, ensure_ascii=False, indent=2)
        if payment_approval_workflow:
            if len((instructions or "").strip()) < 1200:
                critical_errors.append("Instructions terlalu pendek untuk workflow pembayaran/admin approval.")
            if not _has_approval_state_contract(instructions):
                critical_errors.append(
                    "Instructions wajib memuat state intake, waiting_payment, payment_review, approved, delivery, dan aftercare."
                )
            if not tc.get("escalation"):
                critical_errors.append("Workflow pembayaran/admin approval wajib escalation=true.")
            if "escalate_to_human" not in instructions:
                critical_errors.append("Instructions wajib menyebut escalate_to_human untuk bukti transfer/admin approval.")
        if critical_errors:
            return json.dumps({
                "error": "Konfigurasi agent belum aman untuk dibuat.",
                "validation_errors": critical_errors,
                "hint": "Panggil compose_agent_blueprint dan compose_agent_instructions ulang, lalu validate_agent_config sebelum create_agent.",
            }, ensure_ascii=False, indent=2)
        file_ready = bool(tc.get("sandbox")) and bool(tc.get("whatsapp_media"))
        file_signal = file_delivery_workflow or generated_file_workflow
        file_negated = _file_capability_negated(
            name, description, instructions, blueprint, soul, business_context
        )
        if (
            channel_type == "whatsapp"
            and not file_signal
            and not file_negated
            and not file_ready
            and file_decision not in {"enabled", "text_only", "not_needed"}
        ):
            return json.dumps({
                "error": "Kemampuan file belum diputuskan — jangan menebak.",
                "validation_errors": [
                    "Agent WhatsApp ini belum jelas perlu menerima/membuat file atau tidak, "
                    "dan tools_config belum file-ready (sandbox+whatsapp_media)."
                ],
                "hint": (
                    "Tanyakan ke user (bahasa awam): apakah agent perlu MENERIMA file (PDF/Excel/CSV/gambar) "
                    "ATAU MEMBUAT file/laporan/visualisasi untuk dikirim balik? "
                    "Jika YA → create_agent lagi dengan file_capability='enabled' "
                    "(sandbox+whatsapp_media+subagents otomatis diaktifkan). "
                    "Jika TIDAK → create_agent lagi dengan file_capability='text_only'."
                ),
            }, ensure_ascii=False, indent=2)
        if file_delivery_workflow:
            if not tc.get("whatsapp_media"):
                critical_errors.append("Workflow delivery file wajib whatsapp_media=true.")
            critical_errors.extend(file_delivery_contract_issues(instructions, file_delivery=True))
        if generated_file_workflow:
            subagents_cfg = tc.get("subagents", {})
            subagents_enabled = bool(
                subagents_cfg.get("enabled") if isinstance(subagents_cfg, dict) else subagents_cfg
            )
            if not tc.get("sandbox") or not subagents_enabled:
                critical_errors.append("Workflow pembuatan file final wajib sandbox=true dan subagents.enabled=true.")
        if critical_errors:
            return json.dumps({
                "error": "Konfigurasi agent belum aman untuk dibuat.",
                "validation_errors": critical_errors,
                "hint": "Panggil compose_agent_blueprint dan compose_agent_instructions ulang, lalu validate_agent_config sebelum create_agent.",
            }, ensure_ascii=False, indent=2)
        instructions, platform_identity_added = _append_platform_staff_identity_instruction(
            instructions,
            owner_phone=owner_phone,
            operator_phone=operator_phone,
            operator_name=operator_name,
        )
        owner_ids = _owner_variants(owner_phone)

        ec, ec_error = _parse_json_arg(escalation_config, {}, expected=dict)
        if ec_error:
            return f"[error] escalation_config bukan JSON/object yang valid: {ec_error}"

        # Parse allowed_senders
        senders: list[str] | None = None
        if allowed_senders:
            parsed_senders, sender_error = _parse_json_arg(allowed_senders, None, expected=list)
            if sender_error:
                return f"[error] allowed_senders harus berupa JSON array/list, contoh: [\"+62811xxx\"] ({sender_error})"
            senders = parsed_senders

        # Duplicate check: cegah agent dengan nama sama milik user yang sama
        if owner_phone and hasattr(Agent, "__table__"):
            async with db_factory() as db:
                dup_result = await db.execute(
                    select(Agent).where(
                        func.lower(Agent.name) == name.strip().lower(),
                        Agent.is_deleted.is_(False),
                        _owner_filter(owner_phone),
                    )
                )
                dup = dup_result.scalar_one_or_none()
            if dup:
                return json.dumps({
                    "error": f"Agent dengan nama '{name.strip()}' sudah ada.",
                    "existing_agent_id": str(dup.id),
                    "hint": "Gunakan update_agent(agent_id, ...) untuk mengubah agent yang sudah ada, atau pilih nama yang berbeda.",
                }, ensure_ascii=False)

        # operator_ids: selalu include owner_phone + operator_phone yang diminta
        op_ids: list[str] = []
        for owner_id in owner_ids:
            if owner_id and owner_id not in op_ids:
                op_ids.append(owner_id)
        if operator_phone and operator_phone not in op_ids:
            op_ids.append(operator_phone)

        if ec and operator_phone and "operator_phone" not in ec:
            ec["operator_phone"] = operator_phone
        if operator_name and "operator_name" not in ec:
            ec["operator_name"] = operator_name

        try:
            from app.core.domain.subscription_service import (
                check_can_create_agent,
                get_subscription_by_external_id,
                get_or_create_wa_user,
                validate_agent_entitlements,
            )

            logger.info("builder_tools.create_agent.start", owner_phone=owner_phone, name=name)

            async with db_factory() as db:
                # Auto-provision user + Tier 1 subscription untuk WA user.
                # Saat unit test mem-patch Agent menjadi mock, skip integrasi subscription.
                if owner_phone and hasattr(Agent, "__table__"):
                    _user, _sub = await get_or_create_wa_user(owner_phone, db)
                    logger.info("builder_tools.create_agent.user_provisioned", user_id=str(_user.id), sub_status=_sub.status)

                    # Cek apakah boleh buat agent (slot & status subscription)
                    _check = await check_can_create_agent(owner_phone, db)
                    logger.info("builder_tools.create_agent.slot_check", check=_check)
                    if not _check["allowed"]:
                        return json.dumps({"error": _check["reason"]}, ensure_ascii=False)

                    sub_details = await get_subscription_by_external_id(owner_phone, db)
                    if sub_details is None:
                        return json.dumps({"error": "Subscription tidak ditemukan."}, ensure_ascii=False)
                    _, _, plan = sub_details
                    entitlement_errors = validate_agent_entitlements(
                        plan,
                        model=model,
                        tools_config=tc,
                        channel_type=channel_type or None,
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

                    # Override token_quota & active_until dari subscription
                    token_quota = _sub.token_quota
                    _active_until = _sub.expires_at or _sub.grace_until
                else:
                    _active_until = None

                wa_device_id = str(uuid.uuid4()) if channel_type == "whatsapp" else None

                agent = Agent(
                    name=name.strip(),
                    description=description or None,
                    instructions=instructions,
                    model=model,
                    temperature=temperature,
                    tools_config=tc,
                    sandbox_config={},
                    safety_policy={},
                    escalation_config=ec,
                    operator_ids=op_ids,
                    allowed_senders=senders,
                    capabilities=[],
                    max_tokens=max_tokens if max_tokens > 0 else None,
                    token_quota=token_quota,
                    quota_period_days=30,
                    channel_type=channel_type or None,
                    wa_device_id=wa_device_id,
                    owner_external_id=owner_phone,
                    created_by_type="arthur_builder",
                    created_by_agent_id=str(self_agent_id) if self_agent_id else None,
                    created_by_agent_name="Arthur",
                )
                if _active_until:
                    agent.active_until = _active_until

                db.add(agent)
                await db.flush()
                await db.refresh(agent)
                if hasattr(Agent, "__table__"):
                    await upsert_agent_operating_manual(
                        agent.id,
                        generated_operating_manual,
                        db,
                        created_by_agent_id=str(self_agent_id) if self_agent_id else None,
                        version=int(generated_operating_manual.get("version") or 1),
                    )

                memory_keys_seeded: list[str] = []
                builder_memory_updated = False
                if soul.strip() or blueprint.strip() or (platform_identity_added and hasattr(Agent, "__table__")):
                    from app.core.domain.memory_service import upsert_memory

                    if soul.strip():
                        await upsert_memory(agent.id, "soul", soul.strip(), db, scope=None)
                        memory_keys_seeded.append("soul")
                    if blueprint.strip():
                        await upsert_memory(agent.id, "agent_blueprint", blueprint.strip(), db, scope=None)
                        memory_keys_seeded.append("agent_blueprint")
                    if platform_identity_added and hasattr(Agent, "__table__"):
                        await upsert_memory(
                            agent.id,
                            "platform_identity",
                            _platform_staff_identity_block(
                                owner_phone=owner_phone,
                                operator_phone=operator_phone,
                                operator_name=operator_name,
                            ),
                            db,
                            scope=None,
                        )
                        memory_keys_seeded.append("platform_identity")

                if self_agent_id:
                    try:
                        from app.core.domain.memory_service import upsert_memory

                        self_uuid = uuid.UUID(str(self_agent_id))
                        await upsert_memory(self_uuid, "last_agent_id", str(agent.id), db, scope=owner_phone)
                        await upsert_memory(
                            self_uuid,
                            f"agent_id:{agent.name.strip().lower()}",
                            str(agent.id),
                            db,
                            scope=owner_phone,
                        )
                        builder_memory_updated = True
                    except Exception as exc:
                        logger.warning(
                            "builder_tools.create_agent.builder_memory_update_failed",
                            error=str(exc),
                            self_agent_id=self_agent_id,
                            owner_phone=owner_phone,
                        )

                await db.commit()

            logger.info(
                "builder_tools.create_agent.success",
                agent_id=str(agent.id),
                name=agent.name,
                owner_phone=owner_phone,
            )
            created_by_metadata = _agent_created_by_metadata(agent)

            return json.dumps({
                "success": True,
                "agent_id": str(agent.id),
                "name": agent.name,
                "model": agent.model,
                "channel_type": agent.channel_type,
                "google_workspace_enabled": _has_google_workspace_tools(tc),
                "needs_google_auth": _has_google_workspace_tools(tc),
                **created_by_metadata,
                "platform_identity_added": platform_identity_added,
                "operating_manual": summarize_operating_manual(generated_operating_manual),
                "whatsapp_onboarding_required": agent.channel_type == "whatsapp",
                "api_key": agent.api_key,
                "token_quota": agent.token_quota,
                "active_until": agent.active_until.isoformat() if agent.active_until else None,
                "memory_keys_seeded": memory_keys_seeded,
                "builder_memory_updated": builder_memory_updated,
                **({
                    "launch_safety": {
                        "sandbox_subagents_enabled": False,
                        "disabled_features": disabled_launch_features,
                        "message": SANDBOX_DISABLED_NOTICE,
                    }
                } if disabled_launch_features else {}),
                "message": (
                    f"Agent '{agent.name}' berhasil dibuat dengan ID: {agent.id}. "
                    "Simpan agent_id ini sebagai target utama untuk aksi lanjutan pada percakapan ini. "
                    "Jika channel_type adalah whatsapp, jawaban ke user WAJIB langsung lanjut ke onboarding: "
                    "'Mau agent ini langsung dipasang ke nomor WhatsApp kamu sendiri, atau dicoba dulu lewat nomor demo Arthur yang sudah siap pakai?' "
                    "Jangan berhenti hanya dengan menyebut agent sudah jadi atau ID agent. "
                    "Jika google_workspace_enabled=true, langkah berikutnya adalah generate_google_auth_link lalu kirim link login Google; "
                    "jangan menunggu user bertanya cara koneknya. "
                    "Jika user meminta nomor trial/link coba setelah ini, panggil create_wa_dev_trial_link "
                    "dengan agent_id ini, bukan agent lama dari history. "
                    "Jangan panggil compose_agent_soul setelah create hanya untuk melengkapi memory; itu opsional dan boleh ditunda. "
                    "Lebih efisien: untuk create berikutnya, isi parameter soul dan blueprint langsung saat create_agent jika sudah tersedia."
                ),
            }, ensure_ascii=False, indent=2)

        except Exception as exc:
            logger.error("builder_tools.create_agent.error", error=str(exc), owner_phone=owner_phone)
            return f"[error] Gagal membuat agent: {exc}"

    return {"create_agent": create_agent}
