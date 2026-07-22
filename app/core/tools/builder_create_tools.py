"""Agent creation tool for Arthur builder."""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

import structlog
from langchain_core.tools import tool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.domain.agent_sop_service import ensure_operating_manual_in_tools_config, summarize_operating_manual, upsert_agent_operating_manual
from app.core.launch_safety import (
    SANDBOX_DISABLED_NOTICE,
    disable_sandbox_subagent_tools_config,
    sandbox_subagents_enabled,
)
from app.core.tools.builder_discovery import (
    DiscoveryEvidenceUnavailable,
    discovery_operator_phone,
    load_discovery_user_messages,
    validate_agent_discovery,
)
from app.core.tools.builder_google import has_google_workspace_tools as _has_google_workspace_tools
from app.core.tools.builder_identity import (
    agent_created_by_metadata as _agent_created_by_metadata,
    blocked_agent_policy_reason as _blocked_agent_policy_reason,
    extract_operator_phone_from_context as _extract_operator_phone_from_context,
    owner_filter as _owner_filter,
    owner_variants as _owner_variants,
)
from app.core.tools.builder_intent import (
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
from app.models.agent import Agent

logger = structlog.get_logger(__name__)

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
    append_platform_staff_identity_instruction: StringTransformer,
    append_google_workspace_instruction: StringTransformer,
    platform_staff_identity_block: BlockProvider,
    get_logger: LoggerProvider | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    _get_logger = get_logger or (lambda: logger)
    logger = _LoggerProxy(_get_logger)
    _append_platform_staff_identity_instruction = append_platform_staff_identity_instruction
    _append_google_workspace_instruction = append_google_workspace_instruction
    _platform_staff_identity_block = platform_staff_identity_block

    Agent = agent_model

    @tool
    async def create_agent(
        name: str,
        instructions: str,
        description: str = "",
        model: str = "openai/gpt-4.1-mini",
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
        discovery_answers: Any = None,
    ) -> str:
        """
        Buat agent baru di platform dan simpan ke database.
        Agent akan otomatis dikaitkan dengan user yang sedang chat (owner_phone).

        Args:
            name: Nama agent (wajib, maks 255 karakter)
            instructions: System prompt / instructions lengkap agent
            description: Deskripsi singkat fungsi agent
            model: Model LLM (default: openai/gpt-4.1-mini)
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
            operating_manual: Agent Operating Manual/SOP terstruktur. Wajib untuk agent pekerjaan/bisnis
                yang dibuat Arthur; harus berasal dari discovery terkonfirmasi dan tidak boleh memuat asumsi.
            file_capability: Keputusan eksplisit kemampuan file agent — WAJIB diisi kalau kebutuhan file ambigu.
                'enabled' = agent perlu menerima/membuat file (otomatis aktifkan sandbox+whatsapp_media+subagents);
                'text_only' = user sudah konfirmasi agent hanya butuh teks. Kosongkan hanya jika workflow file sudah jelas dari instruksi.
            discovery_answers: Salinan JSON/object discovery enam grup yang sudah lengkap dan dikonfirmasi user.
                Untuk Arthur (self_agent_id tersedia), create diblokir jika ada jawaban wajib yang kosong,
                contoh ideal kurang dari dua, eskalasi bisnis tidak detail, atau user_confirmed belum true.
                Wajib menyertakan `_evidence` untuk setiap field berupa kutipan persis pesan user.
                Jam aktif/jam operasional agent tidak diminta dan tidak menjadi bagian schema discovery.
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
        confirmed_discovery: dict[str, Any] = {}
        discovery_context_text = ""
        if self_agent_id:
            evidence_required = bool(db_factory is not None and session_id)
            try:
                user_messages = await load_discovery_user_messages(db_factory, session_id)
            except DiscoveryEvidenceUnavailable as exc:
                return json.dumps(
                    {
                        "error": str(exc),
                        "retryable": True,
                        "hint": (
                            "Ulangi create_agent secara internal satu kali dengan payload yang sama. "
                            "Jangan meminta user mengulang discovery."
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            discovery = validate_agent_discovery(
                discovery_answers,
                agent_name=name,
                operator_phone=operator_phone,
                require_confirmation=True,
                user_messages=user_messages,
                require_evidence=evidence_required,
            )
            if not discovery.get("complete"):
                return json.dumps(
                    {
                        "error": "Discovery kebutuhan agent belum lengkap atau belum dikonfirmasi user.",
                        "discovery_progress": discovery,
                        "validation_errors": discovery.get("validation_errors") or [],
                        "hint": (
                            "Jangan create. Tanyakan seluruh pertanyaan pada next_group dalam satu pesan, "
                            "panggil plan_agent lagi dengan jawaban lengkap, lalu minta konfirmasi akhir. "
                            "Jangan menanyakan jam aktif/jam operasional agent."
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            confirmed_discovery = dict(discovery.get("normalized_answers") or {})
            discovery_context_text = json.dumps(confirmed_discovery, ensure_ascii=False)
            operator_phone = operator_phone or discovery_operator_phone(discovery)
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
        confirmed_context_requires_manual = bool(
            str(business_context or "").strip()
            or str(blueprint or "").strip()
            or str(domain or "").strip()
            or (
                bool(self_agent_id)
                and str(confirmed_discovery.get("usage_context") or "") == "work"
            )
        )
        if operating_manual_input in (None, "", {}) and confirmed_context_requires_manual:
            return json.dumps({
                "error": "Operating manual terkonfirmasi wajib diisi; runtime tidak boleh menyusunnya dari asumsi.",
                "validation_errors": [
                    "Konteks bisnis/blueprint/domain sudah diberikan, tetapi operating_manual belum ada."
                ],
                "hint": (
                    "Panggil compose_agent_operating_manual. Jika hasilnya memiliki assumptions, missing_context, "
                    "maturity draft/needs_review, atau requires_user_input=true, tanyakan user dulu dan jangan create."
                ),
            }, ensure_ascii=False, indent=2)
        if operating_manual_input not in (None, "", {}):
            parsed_manual, manual_error = _parse_json_arg(
                operating_manual_input,
                {},
                expected=dict,
            )
            if manual_error:
                return json.dumps({
                    "error": "operating_manual bukan JSON/object yang valid.",
                    "validation_errors": [manual_error],
                }, ensure_ascii=False, indent=2)
            manual_assumptions = list(parsed_manual.get("assumptions") or [])
            manual_missing_context = list(parsed_manual.get("missing_context") or [])
            manual_maturity = str(parsed_manual.get("maturity") or "").strip().lower()
            if manual_assumptions or manual_missing_context or manual_maturity in {"draft", "needs_review"}:
                return json.dumps({
                    "error": "Kebutuhan agent belum boleh dibuat karena SOP masih memuat asumsi atau konteks yang belum dikonfirmasi.",
                    "assumptions": manual_assumptions,
                    "missing_context": manual_missing_context,
                    "maturity": manual_maturity or "unknown",
                    "hint": "Tanyakan poin yang belum pasti kepada user, susun ulang SOP tanpa asumsi, lalu coba create_agent lagi.",
                }, ensure_ascii=False, indent=2)
            operating_manual_input = parsed_manual
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
        tc, generated_operating_manual = ensure_operating_manual_in_tools_config(
            tc,
            name=name,
            description=description,
            instructions=instructions,
            business_context=business_context or blueprint or soul or discovery_context_text,
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
        creation_request_id = ""
        if session_id and owner_phone:
            creation_request_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"arthur-create:{session_id}:{str(owner_phone).strip()}:{name.strip().casefold()}",
                )
            )
            # Persist the deterministic request marker with the agent. If the DB
            # commit succeeds but the tool response is lost, a retry in the same
            # session returns the existing agent as success instead of creating a
            # duplicate or telling the user the build failed.
            tc["_builder_creation_request_id"] = creation_request_id

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
                duplicate_tools_config = (
                    dup.tools_config if isinstance(getattr(dup, "tools_config", None), dict) else {}
                )
                if (
                    creation_request_id
                    and duplicate_tools_config.get("_builder_creation_request_id")
                    == creation_request_id
                ):
                    duplicate_google_enabled = _has_google_workspace_tools(duplicate_tools_config)
                    return json.dumps(
                        {
                            "success": True,
                            "idempotent_replay": True,
                            "agent_id": str(dup.id),
                            "name": dup.name,
                            "model": dup.model,
                            "channel_type": dup.channel_type,
                            "google_workspace_enabled": duplicate_google_enabled,
                            "needs_google_auth": duplicate_google_enabled,
                            "whatsapp_onboarding_required": dup.channel_type == "whatsapp",
                            "api_key": dup.api_key,
                            "token_quota": dup.token_quota,
                            "active_until": dup.active_until.isoformat() if dup.active_until else None,
                            "message": (
                                f"Agent '{dup.name}' sudah berhasil dibuat pada percobaan sebelumnya. "
                                "Gunakan agent_id ini dan lanjutkan verify/demo; jangan membuat duplikat."
                            ),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
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
                "discovery_complete": bool(confirmed_discovery) if self_agent_id else None,
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
                    "Jika channel_type adalah whatsapp, jawaban ke user WAJIB mengarahkan uji coba nomor demo Arthur terlebih dahulu. "
                    "Jangan menawarkan pemasangan nomor WhatsApp user sebelum user mencoba demo dan menyatakan cocok, kecuali user memintanya sendiri. "
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
