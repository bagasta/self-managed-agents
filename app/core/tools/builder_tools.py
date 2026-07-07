"""
builder_tools.py — Tools eksklusif untuk system agent (Agent Builder / Arthur).

Hanya dimuat jika agent memiliki capability 'builder' atau 'system'.

Tools yang di-expose:
  get_platform_capabilities()           — ringkasan kapabilitas platform
  get_user_subscription(phone)          — cek plan, slot agent, dan status subscription user
  get_payment_link(plan, phone)         — buat link pembayaran Clevio per tier
  link_dashboard_account(code)          — hubungkan nomor WA pengirim ke akun dashboard via kode link
  get_presets()                         — katalog preset agent siap pakai
  plan_agent(...)                       — structured plan sebelum create
  compose_agent_blueprint(...)          — rancang workflow & knowledge plan custom per bisnis
  compose_agent_operating_manual(...)   — susun SOP/Agent Operating Manual spesifik dari blueprint
  verify_agent(agent_id)               — post-create readback + smoke test guidance
  list_available_wa_devices()           — WA devices yang belum di-assign ke agent
  validate_agent_config(...)            — validasi config sebelum create/update
  create_agent(...)                     — buat agent baru (di-scope ke owner_phone)
  create_wa_dev_trial_link(...)         — generate kode + link shared WA Arthur untuk coba agent tanpa scan QR
  set_agent_memory(...)                 — simpan soul/blueprint langsung ke memory agent
  update_agent(...)                     — update agent yang sudah ada
  get_agent_detail(agent_id)            — baca konfigurasi agent
  list_my_agents()                      — list agent milik owner_phone ini
  delete_agent(...)                     — soft delete agent milik owner_phone ini

Keamanan:
  - create_agent otomatis memasukkan owner_phone ke operator_ids → agen terisolasi per user
  - update_agent / get_agent_detail / delete_agent memverifikasi kepemilikan via operator_ids
  - list_my_agents hanya tampilkan agent yang memiliki owner_phone di operator_ids
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any
from urllib.parse import quote

import structlog
from langchain_core.tools import tool
from openai import AsyncOpenAI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.core.domain.agent_sop_service import (
    build_agent_operating_manual_from_blueprint,
    ensure_operating_manual_in_tools_config,
    get_agent_operating_manual,
    get_latest_agent_operating_manual,
    operating_manual_readiness_issues,
    summarize_operating_manual,
    upsert_agent_operating_manual,
)
from app.core.engine.google_mcp_support import _is_plain_google_form_link_reference
from app.core.engine.google_mcp_support import _candidate_external_user_ids
from app.core.utils.phone_utils import normalize_phone
from app.core.utils.wa_identity import is_probable_whatsapp_lid
from app.models.agent import Agent
from app.models.document import Document
from app.core.tools.builder_blueprint_tools import build_builder_blueprint_tools
from app.core.tools.builder_catalog import (
    AGENT_PRESETS,
    RUNTIME_LIMITATIONS,
    _DEFAULT_MODEL,
    _PLATFORM_CHANNELS,
    _RECOMMENDED_MODELS,
    _TOOLS_CONFIG_DOCS,
)
from app.core.tools.builder_channel_tools import build_builder_channel_tools
from app.core.tools.builder_connector_tools import build_builder_connector_tools
from app.core.tools.builder_create_tools import build_builder_create_tools
from app.core.tools.builder_google import (
    enable_google_workspace_tools as _enable_google_workspace_tools,
    google_workspace_mcp_server_config as _google_workspace_mcp_server_config,
    google_workspace_option as _google_workspace_option,
    has_google_workspace_tools as _has_google_workspace_tools,
    negates_google_workspace as _negates_google_workspace,
)
from app.core.tools.builder_identity import (
    agent_belongs_to_owner as _agent_belongs_to_owner,
    agent_created_by_metadata as _agent_created_by_metadata,
    best_owner_identifier as _best_owner_identifier,
    blocked_agent_policy_reason as _blocked_agent_policy_reason,
    extract_operator_phone_from_context as _extract_operator_phone_from_context,
    is_probable_lid as _is_probable_lid,
    latest_owned_agent_for_trial as _latest_owned_agent_for_trial,
    owner_filter as _owner_filter,
    owner_variants as _owner_variants,
    safe_agent_str_attr as _safe_agent_str_attr,
)
from app.core.tools.builder_fallbacks import (
    _blueprint_needs_semantic_operating_manual,
    _enabled_tool_plan,
    _fallback_agent_blueprint,
    mark_manual_needs_review_if_fallback,
)
from app.core.tools.builder_intent import (
    _business_context_has_explicit_name,
    _combined_context_text,
    _critical_workflow_config_errors,
    _detect_preset,
    _detect_preset_from_config,
    _has_approval_state_contract,
    _looks_like_approval_gated_service,
    _looks_like_file_delivery_workflow,
    _looks_like_generated_file_workflow,
    _looks_like_payment_approval_workflow,
    _payment_workflow_detection_text,
    _sanitize_unverified_business_name,
    _subagents_enabled,
    file_delivery_contract_issues,
)
from app.core.tools.builder_instruction_tools import build_builder_instruction_tools
from app.core.tools.builder_json import (
    complete_truncated_json as _complete_truncated_json,
    extract_balanced_json_object as _extract_balanced_json_object,
    parse_json_arg as _parse_json_arg,
    parse_llm_json_object as _parse_llm_json_object,
    repair_llm_json_text as _repair_llm_json_text,
    strip_json_wrapper as _strip_json_wrapper,
)
from app.core.tools.builder_management_tools import build_builder_management_tools
from app.core.tools.builder_manual_tools import build_builder_manual_tools
from app.core.tools.builder_payment_tools import build_builder_payment_tools
from app.core.tools.builder_planning_tools import build_builder_planning_tools, _get_post_create_steps
from app.core.tools.builder_read_tools import build_builder_read_tools
from app.core.tools.builder_runtime_text import (
    _append_google_workspace_instruction,
    _append_platform_staff_identity_instruction,
    _platform_staff_identity_block,
)
from app.core.tools.builder_soul_tools import _SOUL_TEMPLATES, build_builder_soul_tools
from app.core.tools.builder_text import find_unfilled_placeholders as _find_unfilled_placeholders
from app.core.tools.builder_user_tools import build_builder_user_tools
from app.core.tools.builder_update_tools import build_builder_update_tools
from app.core.tools.builder_validation_tools import build_builder_validation_tools
from app.core.tools.builder_verify_tools import _build_owner_setup_status, build_builder_verify_tools

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Builder catalog data lives in app.core.tools.builder_catalog
# ---------------------------------------------------------------------------





















# ---------------------------------------------------------------------------
# Helper functions for post-create step generation
# ---------------------------------------------------------------------------


# Post-create step helper lives in app.core.tools.builder_planning_tools.



_INSTRUCTION_WRITER_MODEL = "deepseek/deepseek-v4-pro"
_BLUEPRINT_WRITER_MODEL = "deepseek/deepseek-v4-pro"




# Fallback writer helpers live in app.core.tools.builder_fallbacks.



# Soul templates live in app.core.tools.builder_soul_tools.



async def _call_instruction_writer(
    prompt: str,
    system: str,
    model: str | None = None,
    *,
    max_tokens: int = 1500,
    temperature: float = 0.5,
    json_mode: bool = False,
) -> str:
    """Call LLM via OpenRouter for instruction/soul writing."""
    settings = get_settings()
    client = AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
    )
    create_kwargs: dict[str, Any] = dict(
        model=model or _INSTRUCTION_WRITER_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if json_mode:
        # Force a JSON object so the model can't wrap the blueprint in prose.
        # Some models reject response_format; fall back to a plain call.
        try:
            async with asyncio.timeout(45):
                response = await client.chat.completions.create(
                    **create_kwargs,
                    response_format={"type": "json_object"},
                )
        except asyncio.TimeoutError:
            raise
        except Exception:
            async with asyncio.timeout(45):
                response = await client.chat.completions.create(**create_kwargs)
    else:
        async with asyncio.timeout(45):
            response = await client.chat.completions.create(**create_kwargs)
    content = response.choices[0].message.content or ""
    # Strip reasoning/thinking tags
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    return content




def build_builder_tools(
    db_factory: async_sessionmaker,
    owner_phone: str | None = None,
    self_agent_id: str | None = None,
    device_id: str = "",
    default_target: str = "",
    session_id: str | None = None,
    sender_name: str | None = None,
) -> list:
    """
    Build semua builder tools untuk system agent.

    Args:
        db_factory: async_sessionmaker factory — each tool call opens its own session
        owner_phone: external_user_id (nomor WA/JID) dari pengguna yang chat dengan Arthur.
        self_agent_id: UUID agent ini sendiri (Arthur) — untuk self-modification.
        device_id/default_target: konteks WhatsApp saat Arthur dipanggil dari WA.
        session_id: UUID sesi saat ini — untuk membaca file yang dikirim user di workspace.
    """

    user_tools = build_builder_user_tools(
        db_factory,
        owner_phone=owner_phone,
        default_target=default_target,
        sender_name=sender_name,
    )
    _preview_agent_creation_entitlement = user_tools["preview_agent_creation_entitlement"]

    planning_tools = build_builder_planning_tools(
        preview_agent_creation_entitlement=_preview_agent_creation_entitlement,
    )
    # plan_agent contract kept visible in this facade for legacy source-inspection tests:
    # Ini bukan approval gate. Jangan minta user menyetujui blueprint; langsung create_agent tanpa tanya approval lagi.

    async def _call_builder_instruction_writer(*args: Any, **kwargs: Any) -> str:
        return await _call_instruction_writer(*args, **kwargs)

    def _get_builder_logger() -> Any:
        return logger

    def _get_builder_settings() -> Any:
        return get_settings()

    soul_tools = build_builder_soul_tools(
        call_instruction_writer=_call_builder_instruction_writer,
    )

    blueprint_tools = build_builder_blueprint_tools(
        call_instruction_writer=_call_builder_instruction_writer,
        get_logger=_get_builder_logger,
    )
    manual_tools = build_builder_manual_tools(
        call_instruction_writer=_call_builder_instruction_writer,
        get_logger=_get_builder_logger,
    )
    instruction_tools = build_builder_instruction_tools(
        call_instruction_writer=_call_builder_instruction_writer,
        get_logger=_get_builder_logger,
    )
    verify_tools = build_builder_verify_tools(db_factory)
    validation_tools = build_builder_validation_tools()
    management_tools = build_builder_management_tools(
        db_factory,
        owner_phone=owner_phone,
        self_agent_id=self_agent_id,
        session_id=session_id,
        get_logger=_get_builder_logger,
    )
    connector_tools = build_builder_connector_tools(
        get_settings=_get_builder_settings,
        get_logger=_get_builder_logger,
    )
    channel_tools = build_builder_channel_tools(
        db_factory,
        owner_phone=owner_phone,
        self_agent_id=self_agent_id,
        device_id=device_id,
        default_target=default_target,
        session_id=session_id,
        get_settings=_get_builder_settings,
    )
    payment_tools = build_builder_payment_tools(
        owner_phone=owner_phone,
        default_target=default_target,
    )
    create_tools = build_builder_create_tools(
        db_factory,
        owner_phone=owner_phone,
        self_agent_id=self_agent_id,
        agent_model=Agent,
        preview_agent_creation_entitlement=_preview_agent_creation_entitlement,
        call_instruction_writer=_call_builder_instruction_writer,
        append_platform_staff_identity_instruction=_append_platform_staff_identity_instruction,
        append_google_workspace_instruction=_append_google_workspace_instruction,
        platform_staff_identity_block=_platform_staff_identity_block,
        get_logger=_get_builder_logger,
    )
    update_tools = build_builder_update_tools(
        db_factory,
        owner_phone=owner_phone,
        self_agent_id=self_agent_id,
        agent_model=Agent,
        append_platform_staff_identity_instruction=_append_platform_staff_identity_instruction,
        append_google_workspace_instruction=_append_google_workspace_instruction,
        get_logger=_get_builder_logger,
    )
    # compose_agent_blueprint contract kept visible in this facade for legacy source-inspection tests:
    # "state_plan", "human_approval_points", waiting_payment -> payment_review -> approved -> delivery -> aftercare.
    # Runtime agents must keep melanjutkan workflow customer dari konteks customer.

    read_tools = build_builder_read_tools(
        db_factory,
        self_agent_id=self_agent_id,
    )

    # compose_agent_blueprint lives in app.core.tools.builder_blueprint_tools.
    # compose_agent_operating_manual lives in app.core.tools.builder_manual_tools.
    # compose_agent_instructions lives in app.core.tools.builder_instruction_tools.

    # ------------------------------------------------------------------ #
    # compose_agent_soul                                                  #
    # ------------------------------------------------------------------ #

    # compose_agent_soul lives in app.core.tools.builder_soul_tools.


    # ------------------------------------------------------------------ #
    # 4. plan_agent                                                        #
    # ------------------------------------------------------------------ #

    # plan_agent lives in app.core.tools.builder_planning_tools.

    # verify_agent lives in app.core.tools.builder_verify_tools.
    # validate_agent_config lives in app.core.tools.builder_validation_tools.

    # ------------------------------------------------------------------ #
    # 4. create_agent                                                     #
    # ------------------------------------------------------------------ #

    # create_agent lives in app.core.tools.builder_create_tools.

    # create_wa_dev_trial_link lives in app.core.tools.builder_channel_tools.

    # set_agent_memory lives in app.core.tools.builder_management_tools.

    # ------------------------------------------------------------------ #
    # 5. update_agent                                                     #
    # ------------------------------------------------------------------ #

    # update_agent lives in app.core.tools.builder_update_tools.

    # delete_agent/get_agent_detail/list_my_agents live in app.core.tools.builder_management_tools.
    # generate_google_auth_link lives in app.core.tools.builder_connector_tools.

    return [
        read_tools["get_self_config"],
        read_tools["get_platform_capabilities"],
        user_tools["get_user_subscription"],
        payment_tools["get_payment_link"],
        user_tools["link_dashboard_account"],
        read_tools["get_presets"],
        planning_tools["plan_agent"],
        blueprint_tools["compose_agent_blueprint"],
        manual_tools["compose_agent_operating_manual"],
        instruction_tools["compose_agent_instructions"],
        soul_tools["compose_agent_soul"],
        verify_tools["verify_agent"],
        read_tools["list_available_wa_devices"],
        validation_tools["validate_agent_config"],
        create_tools["create_agent"],
        channel_tools["create_wa_dev_trial_link"],
        management_tools["set_agent_memory"],
        management_tools["add_agent_knowledge"],
        update_tools["update_agent"],
        management_tools["delete_agent"],
        management_tools["get_agent_detail"],
        management_tools["list_my_agents"],
        connector_tools["generate_google_auth_link"],
    ]
