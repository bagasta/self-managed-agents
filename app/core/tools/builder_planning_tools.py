"""Planning tools for Arthur builder."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from langchain_core.tools import tool

from app.core.engine.google_mcp_support import _is_plain_google_form_link_reference
from app.core.domain.agent_build_state_service import (
    load_build_discovery_facts,
    merge_discovery_answers,
)
from app.core.launch_safety import (
    SANDBOX_DISABLED_NOTICE,
    disable_sandbox_subagent_tools_config,
    sandbox_subagents_enabled,
)
from app.core.tools.builder_catalog import AGENT_PRESETS, RUNTIME_LIMITATIONS, _DEFAULT_MODEL
from app.core.tools.builder_discovery import (
    DiscoveryEvidenceUnavailable,
    discovery_escalation_policy,
    discovery_operator_phone,
    load_discovery_user_messages,
    validate_agent_discovery,
)
from app.core.tools.builder_google import (
    enable_google_workspace_tools as _enable_google_workspace_tools,
    google_workspace_option as _google_workspace_option,
    negates_google_workspace as _negates_google_workspace,
)
from app.core.tools.builder_identity import blocked_agent_policy_reason as _blocked_agent_policy_reason
from app.core.tools.builder_intent import (
    _combined_context_text,
    _detect_preset,
    _file_capability_negated,
    _looks_like_approval_gated_service,
    _looks_like_file_delivery_workflow,
    _looks_like_generated_file_workflow,
    _looks_like_payment_approval_workflow,
)


def _get_post_create_steps(preset_id: str, channel: str, tc: dict) -> list[str]:
    """Return required actions user/operator must take after agent creation."""
    steps = []
    if channel == "whatsapp" or tc.get("whatsapp_media"):
        steps.append("Kirim QR ke user: gunakan send_agent_wa_qr(agent_id, caption, phone)")
        steps.append("Tunggu user scan QR, lalu cek ulang dengan send_agent_wa_qr jika butuh QR baru")
    if tc.get("rag"):
        steps.append("Upload dokumen: POST /v1/agents/{id}/documents/upload (PDF/DOCX/TXT)")
    if preset_id == "coding_deploy_agent":
        steps.append("Pastikan Docker socket tersedia di server sebelum test deploy")
    return steps


PreviewEntitlement = Callable[..., Awaitable[dict[str, Any]]]


def _needs_agent_purpose_clarification(
    *,
    user_goal: str,
    requested_features: str,
    persona: str,
    business_context: str,
) -> bool:
    """Return True when the user only asked to create an agent without a job brief."""
    if any(str(value or "").strip() for value in (requested_features, persona, business_context)):
        return False

    text = " ".join(str(user_goal or "").lower().split())
    if not text:
        return True

    generic_markers = (
        "buat agent",
        "bikin agent",
        "buatkan agent",
        "bikinkan agent",
        "agent baru",
        "new agent",
        "create agent",
        "make agent",
        "ok buat",
        "oke buat",
        "iya buat",
        "lanjut buat",
        "gas buat",
    )
    if not any(marker in text for marker in generic_markers):
        return False

    remainder = text
    for token in (
        "tolong",
        "dong",
        "ya",
        "aja",
        "agent",
        "agen",
        "baru",
        "buatkan",
        "buat",
        "bikin",
        "bikinkan",
        "create",
        "make",
        "new",
        "ok",
        "oke",
        "iya",
        "lanjut",
        "gas",
        "untuk",
        "yang",
    ):
        remainder = re_sub_word(token, "", remainder)
    meaningful_words = [word for word in remainder.split() if len(word) > 2]
    return len(meaningful_words) < 2


def _missing_agent_brief_clarifications(
    *,
    user_goal: str,
    requested_features: str,
    persona: str,
    business_context: str,
    detected_preset: str,
) -> list[dict[str, str]]:
    """Ask for a real brief when the request is too shallow to build a useful agent."""
    if any(str(value or "").strip() for value in (persona, business_context)):
        return []
    text = _combined_context_text(user_goal, requested_features)
    words = [word for word in text.split() if len(word) > 2]
    if len(words) >= 16:
        return []
    if detected_preset in {"coding_deploy_agent", "social_media_agent", "data_analyst_agent", "research_agent"}:
        return []
    if any(marker in text for marker in ("google calendar", "gmail", "google docs", "google sheets", "google drive")):
        return []
    question = (
        "Agar agentnya tidak generik, jawab singkat 3 hal ini: agent ini untuk bisnis/kebutuhan apa, "
        "siapa yang akan chat dengan agent, dan alur kerja utamanya harus bagaimana?"
    )
    if detected_preset in {"cs_whatsapp_basic", "ecommerce_cs", "approval_gated_service_agent"}:
        question = (
            "Agar agent CS-nya tepat, jawab singkat 3 hal ini: bisnisnya jual/layani apa, "
            "data apa yang harus ditanyakan ke customer, dan kapan harus diteruskan ke admin/Owner?"
        )
    elif detected_preset == "personal_assistant":
        question = (
            "Agar assistant-nya tidak generik, jawab singkat 3 hal ini: tugas utama yang harus dibantu, "
            "data/preferensi apa yang perlu diingat, dan output akhirnya berupa apa?"
        )
    return [
        {
            "topic": "agent_brief",
            "question": question,
        }
    ]


def re_sub_word(word: str, replacement: str, text: str) -> str:
    import re

    return re.sub(rf"\b{re.escape(word)}\b", replacement, text).strip()


def _discovery_clarification_payload(discovery: dict[str, Any]) -> dict[str, Any]:
    next_group = discovery.get("next_group") or {}
    return {
        "plan_status": "needs_clarification",
        "detected_preset": "",
        "discovery_progress": discovery,
        "capability_clarifications": discovery.get("next_questions") or [],
        "next_action": (
            "JANGAN create_agent atau compose artifact dulu. Tanyakan seluruh pertanyaan pada "
            f"{next_group.get('label') or 'grup discovery berikutnya'} dalam satu pesan yang ringkas, "
            "simpan jawaban faktualnya, lalu panggil plan_agent lagi dengan discovery_answers lengkap "
            "(jawaban grup sebelumnya tetap disertakan). Isi `_evidence` untuk setiap field dengan kutipan "
            "pesan user yang tersimpan. Jika user meminta Arthur menyesuaikan suatu detail, detail itu baru "
            "boleh dipakai setelah ditampilkan pada rangkuman akhir dan dikonfirmasi user. Jangan menanyakan jam "
            "aktif/jam operasional agent. Setelah semua grup lengkap, rangkum dan minta user membalas "
            "secara eksplisit `sudah`, `sesuai`, atau `sudah sesuai` sebagai konfirmasi akhir."
        ),
    }


def build_builder_planning_tools(
    *,
    preview_agent_creation_entitlement: PreviewEntitlement,
    db_factory: Any = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    _preview_agent_creation_entitlement = preview_agent_creation_entitlement

    @tool
    async def plan_agent(
        user_goal: str,
        agent_name: str = "",
        channel: str = "",
        requested_features: str = "",
        persona: str = "",
        business_context: str = "",
        operator_phone: str = "",
        escalation_policy: str = "",
        discovery_answers: Any = None,
    ) -> str:
        """
        Buat rencana agent terstruktur berdasarkan goal user sebelum create.
        Mengembalikan preset yang cocok, tools_config yang direkomendasikan,
        validation warnings, dan langkah selanjutnya.

        Gunakan ini SEBELUM create_agent untuk memastikan config sudah tepat.
        Plan adalah gerbang kelengkapan brief enam grup. Semua grup discovery (kecuali
        jam aktif/jam operasional agent) harus lengkap, lalu dirangkum dan dikonfirmasi
        user satu kali sebelum compose/create. Setelah itu tidak perlu meminta approval
        mikro untuk tiap artifact internal.

        PENGECUALIAN WAJIB: kalau plan_status == "needs_clarification" atau ada isi di
        capability_clarifications, JANGAN create dulu. Tanyakan dulu kebutuhan itu ke user
        (mis. apakah agent perlu menerima/membuat file atau visualisasi data) supaya tools
        seperti sandbox/whatsapp_media tidak salah ditebak. Pahami kebutuhan dulu, jangan asumsi.

        Args:
            user_goal: Deskripsi singkat apa yang user ingin agentnya lakukan
            agent_name: Nama agent yang diinginkan (opsional)
            channel: Channel yang diinginkan: 'whatsapp' atau kosong. Kosong berarti WhatsApp.
            requested_features: Fitur-fitur yang diminta, dipisah koma (misal: 'coding,deploy,http')
            persona: Persona/gaya bicara agent (opsional)
            business_context: Konteks bisnis untuk agent CS/FAQ (opsional)
            operator_phone: Nomor operator/admin untuk eskalasi (opsional)
            escalation_policy: Pilihan eksplisit user: 'owner', 'operator', atau 'none'. Kosong berarti belum dikonfirmasi.
            discovery_answers: JSON/object berisi seluruh jawaban discovery yang sudah user berikan:
                problem, usage_context (personal/work), agent_name, audience, main_tasks,
                capabilities, prohibited_actions, allowed_actions, tone_style,
                ideal_conversations (2-3 contoh), avoided_conversations, unknown_handling,
                escalation_target (untuk work: conditions, recipient, whatsapp_number),
                knowledge_sources, sensitive_data_policy, whatsapp_scale, daily_chat_volume,
                integrations, expected_outputs, vision_requirement, go_live_approver (work),
                dan user_confirmed=true setelah rangkuman akhir disetujui. Selalu kirim ulang
                jawaban lengkap yang sudah terkumpul. Sertakan `_evidence` berupa mapping setiap
                field ke kutipan pesan user yang mendukung jawaban tersebut. Jika user meminta Arthur
                menyesuaikan suatu detail, kutip bagian rangkuman akhir yang memuat detail itu setelah
                user mengonfirmasinya. Untuk user_confirmed, kutip pesan konfirmasi terakhir seperti
                `sudah`, `sesuai`, atau `sudah sesuai`. Jangan
                menyertakan operational_hours/jam aktif dan jangan membuat kutipan sendiri.
        """
        policy_reason = _blocked_agent_policy_reason(
            user_goal,
            agent_name,
            requested_features,
            persona,
            business_context,
        )
        if policy_reason:
            return json.dumps({
                "plan_status": "blocked_by_policy",
                "validation_errors": [policy_reason],
                "next_action": "Tolak permintaan ini dengan singkat dan tawarkan jenis agent non-politik/non-buzzer.",
            }, ensure_ascii=False, indent=2)

        evidence_required = bool(db_factory is not None and session_id)
        try:
            user_messages = await load_discovery_user_messages(db_factory, session_id)
            persisted_facts = await load_build_discovery_facts(db_factory, session_id)
        except DiscoveryEvidenceUnavailable as exc:
            return json.dumps(
                {
                    "plan_status": "temporarily_unavailable",
                    "retryable": True,
                    "error": str(exc),
                    "next_action": (
                        "Ulangi plan_agent secara internal satu kali. Jangan create_agent dan jangan "
                        "meminta user mengulang jawaban discovery."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        except Exception:
            return json.dumps(
                {
                    "plan_status": "temporarily_unavailable",
                    "retryable": True,
                    "error": "State discovery tersimpan belum dapat dibaca dengan aman.",
                    "next_action": (
                        "Ulangi plan_agent secara internal satu kali. Jangan meminta user "
                        "mengulang jawaban discovery."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        discovery_answers = merge_discovery_answers(
            discovery_answers,
            persisted_facts,
        )
        discovery = validate_agent_discovery(
            discovery_answers,
            agent_name=agent_name,
            operator_phone=operator_phone,
            require_confirmation=True,
            user_messages=user_messages,
            require_evidence=evidence_required,
            require_confirmed_summary=evidence_required,
        )
        if not discovery.get("complete"):
            # Check the basic creation slot before making the user complete a long discovery.
            early_channel = str(channel or "").strip().lower() or "whatsapp"
            if early_channel != "whatsapp":
                early_channel = "whatsapp"
            early_entitlement_check = await _preview_agent_creation_entitlement(
                tools_config={
                    "memory": True,
                    "skills": True,
                    "escalation": True,
                    "tavily": True,
                    "whatsapp_media": True,
                },
                model=_DEFAULT_MODEL,
                channel_type=early_channel,
            )
            early_entitlement_blocked = bool(
                early_entitlement_check.get("checked")
                and not early_entitlement_check.get("allowed", True)
            )
            if early_entitlement_blocked:
                entitlement_message = (
                    early_entitlement_check.get("user_message")
                    or early_entitlement_check.get("reason")
                    or "Paket kamu belum bisa membuat agent ini."
                )
                agent_count_block = bool(
                    early_entitlement_check.get("max_agents") is not None
                    and early_entitlement_check.get("agents_used", 0)
                    >= early_entitlement_check.get("max_agents", 0)
                )
                next_action = (
                    "User SUDAH punya agent dan sedang di batas jumlah agent paketnya. "
                    "Kalau user ingin MENGUBAH/MEMPERBAIKI agent yang sudah ada, pakai "
                    "list_my_agents lalu update_agent. Tawarkan upgrade hanya kalau user "
                    "benar-benar ingin membuat agent baru tambahan."
                    if agent_count_block
                    else "Jelaskan limit paket dengan bahasa sederhana dan tawarkan upgrade/top up sebelum lanjut membuat agent."
                )
                return json.dumps({
                    "plan_status": "blocked_by_subscription",
                    "validation_errors": [entitlement_message],
                    "creation_entitlement_check": early_entitlement_check,
                    "next_action": next_action,
                }, ensure_ascii=False, indent=2)
            return json.dumps(
                _discovery_clarification_payload(discovery),
                ensure_ascii=False,
                indent=2,
            )

        original_user_goal = str(user_goal or "").strip()
        confirmed_discovery = dict(discovery.get("normalized_answers") or {})
        file_capability_decision = str(discovery.get("file_capability") or "").strip().lower()
        agent_name = str(confirmed_discovery.get("agent_name") or agent_name).strip()
        user_goal = str(confirmed_discovery.get("problem") or user_goal).strip()
        persona = str(confirmed_discovery.get("tone_style") or persona).strip()
        operator_phone = operator_phone or discovery_operator_phone(discovery)
        escalation_policy = discovery_escalation_policy(discovery)
        discovery_context_text = json.dumps(confirmed_discovery, ensure_ascii=False)

        features = [f.strip().lower() for f in requested_features.split(",") if f.strip()]
        feature_text = _combined_context_text(
            original_user_goal,
            user_goal,
            requested_features,
            business_context,
            discovery_context_text,
        )
        discovery_integrations = str(confirmed_discovery.get("integrations") or "")
        google_context_text = (
            f"{original_user_goal} {user_goal} {requested_features} "
            f"{business_context} {discovery_integrations}"
        )

        # Auto-detect preset from goal keywords
        detected_preset = _detect_preset(feature_text, features, channel)

        preset = AGENT_PRESETS.get(detected_preset, {})
        tools_config = dict(preset.get("tools_config", {
            "memory": True, "skills": True, "escalation": True
        }))
        tools_config.setdefault("tavily", True)

        # Override with explicitly requested features
        feature_map = {
            "rag": "rag", "dokumen": "rag", "faq": "rag", "document": "rag",
            "scheduler": "scheduler", "reminder": "scheduler", "jadwal": "scheduler",
            "http": "http", "api": "http",
            "tavily": "tavily", "browse": "tavily", "browser": "tavily", "search": "tavily",
            "sandbox": "sandbox", "coding": "sandbox", "kode": "sandbox", "prototype": "sandbox", "website": "sandbox",
            "deploy": "deploy",
            "whatsapp_media": "whatsapp_media", "media": "whatsapp_media", "gambar": "whatsapp_media",
            "file": "whatsapp_media", "pdf": "whatsapp_media", "excel": "whatsapp_media", "docx": "whatsapp_media",
        }
        for feat in features:
            mapped = feature_map.get(feat)
            if mapped and mapped in tools_config:
                tools_config[mapped] = True

        approval_gated_service = _looks_like_approval_gated_service(feature_text)
        payment_approval_workflow = _looks_like_payment_approval_workflow(feature_text)
        file_delivery_workflow = _looks_like_file_delivery_workflow(feature_text)
        generated_file_workflow = _looks_like_generated_file_workflow(feature_text)
        wants_coding = any(k in feature_text for k in ("coding", "kode", "prototype", "website", "deploy", "sandbox"))
        wants_cv_document = any(
            k in feature_text
            for k in (
                "bikin cv",
                "buat cv",
                "cv ats",
                "resume ats",
                "bikin resume",
                "buat resume",
                "kirim cv",
                "kirim resume",
            )
        )
        wants_files = file_delivery_workflow or wants_cv_document
        wants_generated_files = generated_file_workflow or wants_cv_document
        plain_google_form_link = _is_plain_google_form_link_reference(google_context_text)
        google_negated = _negates_google_workspace(google_context_text)
        wants_google = (
            any(k in google_context_text.lower() for k in ("google", "gmail", "calendar", "drive", "docs", "sheets", "workspace"))
            and not plain_google_form_link
            and not google_negated
        )
        google_workspace_option = (
            {
                "should_offer": False,
                "enabled": False,
                "suggested_apps": [],
                "reasons": [],
                "user_facing_pitch": "",
                "if_user_declines": "Lanjutkan tanpa integrasi Google.",
            }
            if google_negated
            else _google_workspace_option(feature_text, wants_google)
        )
        if wants_coding:
            tools_config["sandbox"] = True
            tools_config["deploy"] = True
            tools_config["subagents"] = {"enabled": True}
        if wants_files:
            tools_config["whatsapp_media"] = True
        if wants_generated_files:
            tools_config["sandbox"] = True
            tools_config["subagents"] = {"enabled": True}
        explicit_media_request = any(
            feat in features
            for feat in ("media", "gambar", "foto", "file", "pdf", "excel", "xlsx", "docx", "dokumen")
        )
        # Keputusan file berasal dari discovery yang sudah dikonfirmasi, bukan
        # dari asumsi preset atau kemampuan laten yang mungkin dibutuhkan nanti.
        if file_capability_decision == "receive_only":
            tools_config["whatsapp_media"] = True
        elif file_capability_decision in {"generate", "both", "enabled"}:
            tools_config["whatsapp_media"] = True
            tools_config["sandbox"] = True
            tools_config["subagents"] = {"enabled": True}
        elif file_capability_decision == "text_only":
            tools_config["whatsapp_media"] = False
        if approval_gated_service or payment_approval_workflow:
            tools_config["escalation"] = True
            tools_config["whatsapp_media"] = True
        needs_human_handoff = bool(operator_phone) or any(
            k in feature_text
            for k in (
                "admin",
                "operator",
                "owner",
                "pemilik",
                "eskalasi",
                "approval",
                "approve",
                "harga final",
                "stok",
                "booking",
                "kepastian",
                "komplain",
                "refund",
                "bukti transfer",
                "dp",
                "pelunasan",
            )
        )
        if needs_human_handoff:
            tools_config["escalation"] = True
        normalized_escalation_policy = str(escalation_policy or "").strip().lower()
        if normalized_escalation_policy == "none" and not (approval_gated_service or payment_approval_workflow):
            tools_config["escalation"] = False
        elif normalized_escalation_policy in {"owner", "operator"}:
            tools_config["escalation"] = True
        if wants_google:
            tools_config = _enable_google_workspace_tools(tools_config)

        # --- Capability discovery: JANGAN asumsi kebutuhan file/data/visualisasi ---
        # Agent percakapan/CS sering belakangan diminta MENERIMA file (PDF/Excel/CSV/gambar)
        # atau MEMBUAT laporan/visualisasi PDF. Tanpa sandbox + whatsapp_media agent tak punya
        # tool untuk baca file maupun bikin/kirim file → balas "file tidak ditemukan" padahal
        # filenya tersimpan. Kalau sinyal kebutuhan file tidak jelas DAN tidak dinegasikan user,
        # Arthur WAJIB tanya dulu sebelum create — bukan menebak.
        file_capability_signal = bool(
            file_capability_decision
            or wants_files
            or wants_generated_files
            or explicit_media_request
            or file_delivery_workflow
            or generated_file_workflow
        )
        file_capability_negated = _file_capability_negated(feature_text)
        file_ready = bool(tools_config.get("sandbox")) and bool(tools_config.get("whatsapp_media"))
        capability_clarifications: list[dict] = []
        capability_clarifications.extend(
            _missing_agent_brief_clarifications(
                user_goal=user_goal,
                requested_features=requested_features,
                persona=persona,
                business_context=business_context,
                detected_preset=detected_preset,
            )
        )
        if normalized_escalation_policy == "none" and (approval_gated_service or payment_approval_workflow):
            capability_clarifications.append({
                "topic": "escalation_required_for_approval",
                "question": (
                    "Workflow ini membutuhkan keputusan pembayaran/approval manusia. "
                    "Siapa Owner atau operator/admin yang harus menerima eskalasi?"
                ),
            })
        if not file_capability_signal and not file_capability_negated and not file_ready:
            capability_clarifications.append(
                {
                    "topic": "file_data_visualization",
                    "question": (
                        "Apakah agent ini nantinya perlu MENERIMA file dari user (mis. PDF, Excel, "
                        "CSV, gambar) ATAU MEMBUAT file/laporan/visualisasi data (mis. grafik atau PDF) "
                        "untuk dikirim balik ke user?"
                    ),
                    "if_yes": (
                        "Panggil plan_agent LAGI dengan requested_features menambahkan "
                        "'file,visualisasi' (tambah 'sandbox' bila perlu analisa/olah data) supaya "
                        "sandbox + whatsapp_media + subagents otomatis aktif sebelum create."
                    ),
                    "if_no": "Lanjut tanpa tools file (sandbox & whatsapp_media tetap nonaktif).",
                }
            )

        # Validate tool dependencies
        validation_errors: list[str] = []
        validation_warnings: list[str] = []

        if tools_config.get("deploy") and not tools_config.get("sandbox"):
            tools_config["sandbox"] = True
            validation_warnings.append("deploy membutuhkan sandbox — sandbox otomatis diaktifkan")

        if tools_config.get("tool_creator") and not tools_config.get("sandbox"):
            tools_config["sandbox"] = True
            validation_warnings.append("tool_creator membutuhkan sandbox — sandbox otomatis diaktifkan")

        if not sandbox_subagents_enabled():
            tools_config, disabled_launch_features = disable_sandbox_subagent_tools_config(tools_config)
            if disabled_launch_features:
                validation_warnings.append(SANDBOX_DISABLED_NOTICE)
            if wants_coding or wants_generated_files:
                validation_errors.append(
                    "Request coding/deploy/generate file tidak bisa dibuat dengan sandbox/subagent untuk sementara. "
                    "Tawarkan versi agent chat/escalation dulu, atau tunda fitur file/deploy sampai stabilisasi selesai."
                )
            if detected_preset in {"coding_deploy_agent", "social_media_agent", "data_analyst_agent", "research_agent"}:
                validation_errors.append(
                    f"Preset {detected_preset} membutuhkan sandbox/subagent, jadi sementara tidak boleh dibuat."
                )

        # Channel validation. Arthur only offers user-facing WhatsApp onboarding.
        requested_channel = str(channel or "").strip().lower()
        effective_channel = requested_channel or preset.get("default_channel", "whatsapp")
        if effective_channel != "whatsapp":
            validation_warnings.append(
                f"Channel {effective_channel} belum tersedia untuk onboarding Arthur; agent disiapkan lewat WhatsApp."
            )
            effective_channel = "whatsapp"
        if effective_channel == "whatsapp" and not tools_config.get("escalation"):
            validation_warnings.append("Agent WhatsApp sebaiknya mengaktifkan escalation untuk operator handoff")

        effective_model = preset.get("default_model", _DEFAULT_MODEL)
        creation_entitlement_check = await _preview_agent_creation_entitlement(
            tools_config=tools_config,
            model=effective_model,
            channel_type=effective_channel,
        )
        entitlement_blocked = bool(
            creation_entitlement_check.get("checked")
            and not creation_entitlement_check.get("allowed", True)
        )
        if entitlement_blocked:
            entitlement_message = (
                creation_entitlement_check.get("user_message")
                or creation_entitlement_check.get("reason")
                or "Paket kamu belum bisa membuat agent ini."
            )
            validation_errors.append(entitlement_message)
            for violation in creation_entitlement_check.get("violations") or []:
                validation_errors.append(str(violation))
        elif not creation_entitlement_check.get("checked"):
            validation_warnings.append(
                "Cek tier/slot awal belum bisa diverifikasi; create_agent tetap akan melakukan hard gate sebelum menyimpan agent."
            )

        # Distinguish an agent-COUNT block (user already has agent[s] → likely
        # wants to MODIFY, which is not count-limited) from other blocks.
        agent_count_block = bool(
            entitlement_blocked
            and creation_entitlement_check.get("max_agents") is not None
            and creation_entitlement_check.get("agents_used", 0)
            >= creation_entitlement_check.get("max_agents", 0)
        )

        # Surface critical limitations
        critical_limitations = []
        for lid in preset.get("runtime_limitations", []):
            lim = RUNTIME_LIMITATIONS.get(lid)
            if lim:
                if lim["severity"] == "critical":
                    critical_limitations.append(lim["user_message"])
                elif lim["severity"] == "warning":
                    validation_warnings.append(lim["user_message"])

        # Build recommended config
        plan_status = (
            "blocked_by_subscription"
            if entitlement_blocked
            else "has_errors"
            if validation_errors
            else "needs_clarification"
            if capability_clarifications
            else "ready"
        )

        if agent_count_block:
            next_action = (
                "User SUDAH punya agent dan sedang di batas jumlah agent paketnya. "
                "Kalau user ingin MENGUBAH/MEMPERBAIKI agent yang sudah ada (mis. eskalasi, notifikasi, "
                "instruksi, fitur) — itu TIDAK kena limit jumlah agent. JANGAN suruh upgrade. "
                "Pakai list_my_agents lalu update_agent pada agent yang dimaksud. "
                "Tawarkan upgrade HANYA kalau user benar-benar ingin MEMBUAT agent BARU tambahan."
            )
        elif entitlement_blocked:
            next_action = "Jelaskan limit paket dengan bahasa sederhana dan tawarkan upgrade/top up sebelum lanjut membuat agent."
        elif validation_errors:
            next_action = "Perbaiki validation_errors sebelum create."
        elif capability_clarifications:
            next_action = (
                "JANGAN create_agent dulu — PAHAMI kebutuhan user, jangan menebak. "
                "Tanyakan ke user (bahasa awam) pertanyaan di capability_clarifications[].question. "
                "Jika ada beberapa klarifikasi, gabungkan maksimal 3 pertanyaan paling penting dalam satu pesan. "
                "Kalau klarifikasi file dijawab YA → ikuti if_yes (panggil plan_agent ulang dengan fitur file). "
                "Kalau dijawab TIDAK → ikuti if_no, lalu lanjut compose_agent_blueprint/compose_agent_instructions setelah brief cukup."
            )
        elif google_workspace_option.get("should_offer"):
            next_action = (
                "Tawarkan opsi integrasi Google Workspace dengan bahasa awam memakai google_workspace_option.user_facing_pitch. "
                "Jika user setuju, panggil plan_agent lagi dengan requested_features berisi google sebelum create. "
                "Jika user menolak, lanjutkan compose_agent_blueprint/compose_agent_instructions tanpa Google."
            )
        else:
            next_action = (
                "Untuk agent bisnis/custom, panggil compose_agent_blueprint lalu compose_agent_instructions. "
                "Setelah workflow, eskalasi, dan rangkuman kebutuhan dikonfirmasi user, lanjut validate_agent_config dan create_agent."
            )
        plan = {
            "plan_status": plan_status,
            "detected_preset": detected_preset,
            "preset_label": preset.get("label", "Custom"),
            "agent_name": agent_name,
            "business_goal": user_goal,
            "escalation_policy": normalized_escalation_policy,
            "channel": effective_channel,
            "persona": persona or "ramah dan profesional",
            "business_context": business_context,
            "discovery": discovery,
            "confirmed_discovery": confirmed_discovery,
            "blueprint_seed": {
                "agent_summary": f"{agent_name or 'Agent ini'} dibuat untuk {user_goal}",
                "customization_goal": (
                    "Gunakan compose_agent_blueprint jika agent perlu SOP/workflow spesifik per bisnis, "
                    "produk, tim, atau industri. Jangan hanya mengandalkan persona generik."
                ),
                "known_business_context": business_context,
                "confirmed_discovery": confirmed_discovery,
                "requested_features": features,
                "design_considerations": [
                    "Langkah kerja ideal agent dari awal sampai selesai.",
                    "Data yang wajib dikumpulkan dari user/pelanggan.",
                    "Pengetahuan produk/SOP yang wajib agent tahu.",
                    "Kapan agent harus eskalasi ke manusia.",
                    "Apakah ada pembayaran, approval admin, atau deliverable yang baru boleh dikirim setelah disetujui.",
                ],
            },
            "recommended_config": {
                "model": effective_model,
                "temperature": preset.get("default_temperature", 0.7),
                "max_tokens": preset.get("default_max_tokens", 1024),
                "tools_config": tools_config,
                "file_capability": file_capability_decision,
                "channel_type": effective_channel,
                "escalation_config": (
                    {"channel_type": "whatsapp", "operator_phone": operator_phone}
                    if operator_phone else {}
                ),
            },
            "required_post_create_steps": _get_post_create_steps(detected_preset, effective_channel, tools_config),
            "validation_errors": validation_errors,
            "validation_warnings": validation_warnings,
            "capability_clarifications": capability_clarifications,
            "critical_limitations": critical_limitations,
            "creation_entitlement_check": creation_entitlement_check,
            "google_workspace_option": google_workspace_option,
            "smoke_test_guidance": preset.get("smoke_test", {}).get("steps", []),
            "next_action": next_action,
        }
        return json.dumps(plan, ensure_ascii=False, indent=2)


    return {"plan_agent": plan_agent}
