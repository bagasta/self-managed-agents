"""Main agent orchestration entry point.

`run_agent()` coordinates run records, LLM setup, tools, prompt/context,
graph execution, result persistence, and post-run memory extraction. Detailed
tool setup, HITL handling, MCP support, callbacks, and input preparation live
in sibling modules.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, TypedDict

import structlog
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.engine.context_service import (
    DELIVERY_STATUS_TAG,
    count_user_messages,
    db_messages_to_lc,
    load_history,
)
from app.core.domain.agent_sop_service import get_latest_agent_operating_manual
from app.core.domain.memory_service import (
    build_memory_context,
    extract_long_term_memory,
    load_layered_memory,
    record_runtime_memory,
)
from app.core.engine.prompt_builder import (
    build_mcp_tool_priority_notice,
    build_rag_context,
    build_system_prompt,
    maybe_summarize_context,
)
from app.core.engine.tool_builder import _is_enabled
from app.core.engine.agent_callbacks import AgentStepLogger
from app.core.engine.agent_hitl import handle_graph_interrupt, handle_pending_interrupt
from app.core.engine.agent_input import build_input_messages
from app.core.engine.agent_llm import build_agent_llms
from app.core.engine.agent_recovery import send_agent_recovery_message
from app.core.engine.agent_google_routing import (
    _append_builder_google_auth_link_if_needed,
    _builder_google_auth_agent_id,
    _extract_auth_url_from_builder_steps,
    _google_workspace_customer_blocker_reply,
    _google_workspace_mcp_unauthorized_reply,
    _google_workspace_server_has_auth,
    _is_google_chat_intent,
    _is_google_workspace_mcp_authorized_for_session,
    _remove_google_workspace_mcp_server,
    _route_google_workspace_blocker_to_owner_if_customer,
)
from app.core.engine.agent_identity import (
    _is_customer_whatsapp_session,
    _normalized_agent_operator_ids,
    _owner_notification_target,
    _session_real_phone,
    _session_sender_phone,
)
from app.core.engine.agent_step_utils import (
    _URL_RE,
    _has_whatsapp_media_send_step,
    _is_operator_envelope,
    _operator_message_payload,
    _parse_step_result_json,
)
from app.core.engine.agent_tool_setup import build_agent_tool_setup
from app.core.engine.agent_policy import (
    AgentRuntimePolicy,
    build_agent_runtime_policy,
    should_block_external_service_fallback_tool,
    should_use_google_workspace_parent_only,
)
from app.models.agent import Agent as AgentModel
from app.models.message import Message
from app.models.run import Run
from app.models.session import Session
from sqlalchemy import select
from app.core.engine.result_parser import (
    ParsedResult,
    sanitize_input_messages as _sanitize_input_messages,
    parse_agent_result,
)
from app.core.engine.reply_guard import ensure_non_empty_reply
from app.core.utils.phone_utils import normalize_phone
from app.core.domain.agent_quota_service import get_owner_subscription, is_quota_exempt_builder_agent
from app.core.domain.subscription_service import QuotaExceeded, assert_token_quota_available
from app.core.engine.google_mcp_support import (
    _build_google_mcp_auth_failure_reply,
    _build_google_mcp_unavailable_reply,
    _build_google_mcp_validation_reply,
    _candidate_external_user_ids,
    _contains_google_workspace_artifact,
    _extract_google_mcp_step_error,
    _extract_requested_slide_count,
    _fetch_google_auth_link,
    _has_google_mcp_step,
    _has_google_workspace_artifact_step,
    _is_google_auth_or_scope_error,
    _is_google_forms_authoring_intent,
    _is_google_mcp_intent,
    _is_google_sheets_authoring_intent,
    _is_google_slides_relayout_intent,
    _looks_like_progress_claim,
    _needs_google_forms_followup,
    _needs_google_sheets_followup,
    _needs_google_slides_followup,
    apply_google_mcp_reply_overrides,
    apply_mcp_error_notice,
    google_forms_create_retry_directive,
    google_forms_followup_directive,
    google_forms_followup_retry_directive,
    google_forms_request_kind_retry_directive,
    google_sheets_followup_directive,
    find_last_google_workspace_user_request,
    is_google_auth_recovery_followup,
    is_google_workspace_mcp_configured,
    prepare_google_mcp_runtime,
    sanitize_google_forms_tools,
    google_slides_dimension_retry_directive,
    google_slides_followup_directive,
    google_slides_shape_retry_directive,
)

logger = structlog.get_logger(__name__)
settings = get_settings()


from app.core.engine.agent_reply_guards import (
    _operator_escalation_reply_guard,
    _task_result_guard_reply,
    _whatsapp_media_delivery_guard_reply,
)
from app.core.engine.agent_followups import (
    _builder_create_completion_directive,
    _BUILD_PROGRESS_TOOLS,
    _deploy_followup_message,
    _extract_shared_workspace_file_from_steps,
    _has_code_creation_evidence,
    _has_external_service_fallback_blocked_step,
    _has_public_url_in_steps,
    _has_public_url_in_text,
    _is_website_or_app_request,
    _needs_builder_create_completion,
    _needs_deploy_followup,
    _needs_whatsapp_file_delivery_followup,
)


async def _graph_result_from_output(
    *,
    graph: Any,
    graph_config: dict[str, Any],
    graph_output: Any,
    log: Any,
) -> dict[str, Any]:
    """Return graph state when a checkpointer exists, otherwise use ainvoke output."""
    try:
        state = await graph.aget_state(graph_config)
        if state is not None and isinstance(getattr(state, "values", None), dict):
            return dict(state.values)
    except ValueError as exc:
        if "No checkpointer set" not in str(exc):
            raise
        log.warning("agent_run.graph_state_unavailable_no_checkpointer")

    if isinstance(graph_output, dict):
        return graph_output
    values = getattr(graph_output, "values", None)
    if isinstance(values, dict):
        return dict(values)
    output = getattr(graph_output, "output", None)
    if isinstance(output, dict):
        return output
    return {}


from app.core.engine.agent_whatsapp_guards import (
    _direct_whatsapp_send_guard_reply,
    _extract_direct_whatsapp_confirmation_payload,
    _filter_whatsapp_unsafe_mcp_tools,
    _has_prior_reply_to_user_evidence,
    _has_prior_send_to_number_evidence,
    _has_reply_to_user_step,
    _has_send_to_number_step,
    _is_direct_whatsapp_meta_request,
    _is_direct_whatsapp_send_confirmation,
    _is_direct_whatsapp_send_request,
    _is_direct_whatsapp_text_send_context,
    _looks_like_direct_send_success_claim,
    _prioritize_direct_whatsapp_text_send_tools,
)


class AgentRunResult(TypedDict):
    reply: str
    steps: list[dict]
    run_id: uuid.UUID
    tokens_used: int
    usage: dict[str, Any]


def _model_supports_image_input(model: str | None) -> bool:
    """Best-effort guard for provider endpoints that reject multimodal messages."""
    name = str(model or "").lower()
    if not name:
        return False
    if any(marker in name for marker in ("deepseek/", "moonshotai/", "kimi-")):
        return False
    if "qwen3" in name and "vl" not in name:
        return False
    return any(
        marker in name
        for marker in (
            "gpt-4o",
            "gpt-4.1",
            "o4-mini",
            "gemini",
            "claude-3",
            "claude-sonnet-4",
            "pixtral",
            "llava",
            "vision",
            "-vl",
            "qwen-vl",
        )
    )


def _build_human_content_for_model(
    *,
    user_message: str,
    model: str | None,
    media_image_b64: str | None,
    media_image_mime: str | None,
    google_auth_recovery_followup: bool,
    google_auth_recovery_request: str | None,
    log: Any | None = None,
) -> Any:
    if google_auth_recovery_followup and google_auth_recovery_request:
        return (
            "Saya sudah menyelesaikan OAuth/reconnect Google. "
            "Lanjutkan request Google Workspace sebelumnya sekarang dengan tool Google Workspace langsung:\n"
            f"{google_auth_recovery_request}"
        )
    if media_image_b64 and media_image_mime:
        if _model_supports_image_input(model):
            return [
                {"type": "text", "text": user_message},
                {"type": "image_url", "image_url": {"url": f"data:{media_image_mime};base64,{media_image_b64}"}},
            ]
        if log is not None:
            log.warning(
                "agent_run.image_input_stripped_for_non_vision_model",
                model=model,
                media_mime=media_image_mime,
            )
        note = (
            "\n\n[Catatan sistem: user mengirim gambar, tetapi model run ini tidak mendukung input gambar. "
            "Jangan mengklaim melihat isi gambar. Jika isi gambar penting untuk tugas, minta user menjelaskan "
            "detail pentingnya dalam teks.]"
        )
        return (user_message or "").strip() + note
    return user_message


def _extract_requested_image_caption(user_message: str) -> str:
    payload = _operator_message_payload(user_message).strip()
    for pattern in (
        r"(?:dengan\s+caption|with\s+caption|caption(?:nya)?)\s*[:=\-]?\s*(.+)$",
        r"(?:beri|kasih|tambahkan)\s+caption\s*[:=\-]?\s*(.+)$",
    ):
        match = re.search(pattern, payload, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        caption = match.group(1).strip()
        return caption.strip(" \t\r\n\"'`")
    return ""


def _has_explicit_external_wa_target(user_message: str) -> bool:
    payload = _operator_message_payload(user_message)
    if re.search(r"(?:@s\.whatsapp\.net|@c\.us|@lid)\b", payload, flags=re.IGNORECASE):
        return True
    return bool(re.search(r"(?:\+?\d[\d\s().-]{7,}\d)", payload))


def _current_image_attachment_delivery_request(
    *,
    session: Session,
    current_attachment_name: str | None,
    user_message: str,
) -> tuple[str, str] | None:
    """Detect explicit "send this image with caption" requests for the latest WA attachment."""
    if not current_attachment_name:
        return None
    payload = _operator_message_payload(user_message).strip()
    text = payload.lower()
    if not text or _has_explicit_external_wa_target(payload):
        return None
    if not any(marker in text for marker in ("kirim", "send", "forward", "teruskan", "bagikan")):
        return None
    if not any(marker in text for marker in ("gambar", "foto", "image", "lampiran", "attachment", "caption")):
        return None

    meta = session.metadata_ if isinstance(getattr(session, "metadata_", None), dict) else {}
    current = meta.get("current_attachment")
    incoming = meta.get("last_incoming_media")
    current = current if isinstance(current, dict) else {}
    incoming = incoming if isinstance(incoming, dict) else {}

    filename = str(current.get("filename") or incoming.get("filename") or current_attachment_name or "").strip()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    media_type = str(current.get("media_type") or incoming.get("media_type") or "").strip()
    if media_type and media_type not in {"image", "sticker"}:
        return None
    if not media_type and ext not in {"jpg", "jpeg", "png", "webp"}:
        return None

    path = (
        str(current.get("input_path") or "").strip()
        or str(current.get("shared_path") or "").strip()
        or str(incoming.get("current_input_path") or "").strip()
        or str(incoming.get("shared_alias") or "").strip()
    )
    if not path:
        return None
    return path, _extract_requested_image_caption(payload)


# Re-exported from agent_middleware (moved there to keep agent_runner as facade)
from app.core.engine.agent_middleware import (  # noqa: E402
    BlockTaskToolMiddleware,
    ExternalServiceFallbackGuardMiddleware,
    ToolErrorRecoveryMiddleware,
)


async def _pre_run_quota_gate(
    *,
    agent_model: AgentModel,
    db: AsyncSession,
    log: Any,
    run_record: Run,
    run_id: uuid.UUID,
) -> AgentRunResult | None:
    """Block before the LLM is built when the owner subscription quota is exhausted.

    Returns an AgentRunResult to short-circuit the run, or None to continue.
    Builder/system agents are exempt (platform infrastructure).
    NOTE: overlaps check_agent_quota in agent_quota_service.py; kept as explicit pre-LLM gate.
    """
    if not is_quota_exempt_builder_agent(agent_model):
        try:
            _, _owner_sub = await get_owner_subscription(agent_model, db)
            if _owner_sub is not None:
                try:
                    assert_token_quota_available(_owner_sub)
                except QuotaExceeded as _qe:
                    log.warning("agent_run.quota_exceeded_pre_run", detail=str(_qe))
                    run_record.status = "completed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    run_record.tokens_used = 0
                    await db.flush()
                    _quota_reply = (
                        "Maaf, kuota token subscription pemilik agent ini sudah habis. "
                        "Silakan upgrade plan atau tunggu reset kuota berikutnya."
                    )
                    return AgentRunResult(
                        reply=_quota_reply,
                        steps=[],
                        run_id=run_id,
                        tokens_used=0,
                        usage={},
                    )
        except Exception as _quota_exc:
            log.warning(
                "agent_run.quota_lookup_failed_allow_run",
                error=str(_quota_exc),
            )
    return None


async def _persist_inbound_user_message(
    db,
    *,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    content: str,
    step_index: int,
) -> Message:
    """Persist the inbound user message durably BEFORE running the agent graph.

    Committed in its own transaction so that a later `db.rollback()` in the
    caller (on run cancel/timeout/error) cannot silently erase the user's
    request. Without this, a failed run drops the message entirely and the next
    turn has no record of what the user asked for.
    """
    msg = Message(
        session_id=session_id,
        role="user",
        content=content,
        step_index=step_index,
        run_id=run_id,
    )
    db.add(msg)
    await db.commit()
    return msg


async def _persist_run_failure(
    db,
    *,
    run_record,
    status: str,
    error_message: str | None,
) -> None:
    """Durably record a terminal run failure so it survives the caller rollback.

    The WA caller rolls back on cancel/timeout/error; without committing here the
    failed/timed_out/cancelled status is lost and the run is left stuck as
    'running' with no audit trail. Committing leaves a durable trace.
    """
    run_record.status = status
    run_record.completed_at = datetime.now(timezone.utc)
    if error_message is not None:
        run_record.error_message = error_message[:2000]
    await db.commit()


_INLINE_TEXT_ARTIFACT_EXTS = {"txt", "md", "markdown", "csv", "json", "log", "asc"}


def _shared_text_artifact_inline_reply(
    *,
    shared_path: str,
    session_id: uuid.UUID,
) -> str | None:
    filename = (shared_path or "").rsplit("/", 1)[-1] or "file"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _INLINE_TEXT_ARTIFACT_EXTS:
        return None
    if not shared_path.startswith("/workspace/shared/"):
        return None

    relative = shared_path.removeprefix("/workspace/shared/").strip("/")
    if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
        return None

    try:
        from app.core.infra.sandbox import get_shared_dir

        host_path = (get_shared_dir(session_id) / relative).resolve()
        shared_root = get_shared_dir(session_id).resolve()
        host_path.relative_to(shared_root)
        if not host_path.is_file():
            return None
        raw = host_path.read_bytes()[:12000]
        text = raw.decode("utf-8", errors="replace").strip()
    except Exception:
        return None

    if not text:
        return None
    text = text.replace("```", "'''")
    truncated = len(text) > 3500
    if truncated:
        text = text[:3500].rstrip()
    suffix = "\n\n[Terpotong karena terlalu panjang untuk chat.]" if truncated else ""
    return f"Berikut isi {filename} dalam bentuk teks:\n```text\n{text}\n```{suffix}"


async def _deliver_shared_whatsapp_file_via_tool(
    *,
    tools: list[Any],
    shared_path: str,
    parsed: ParsedResult,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    step_index: int,
    log: Any,
    caption: str | None = None,
) -> tuple[bool, str]:
    """Send a known /workspace/shared file via the parent WA media tool without an LLM pass."""
    filename = (shared_path or "").rsplit("/", 1)[-1] or "file"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    image_exts = {"jpg", "jpeg", "png", "webp"}
    tool_name = "send_whatsapp_image" if ext in image_exts else "send_whatsapp_document"
    media_caption = caption if caption is not None else f"Berikut file {filename}."
    send_tool = next((tool for tool in tools if getattr(tool, "name", "") == tool_name), None)
    if send_tool is None:
        inline_reply = _shared_text_artifact_inline_reply(
            shared_path=shared_path,
            session_id=session_id,
        )
        if inline_reply:
            return True, inline_reply
        return (
            False,
            f"Belum saya kirim. Tool {tool_name} tidak tersedia di run ini, jadi saya tidak bisa mengirim {filename} sebagai file WhatsApp.",
        )

    if tool_name == "send_whatsapp_image":
        args = {
            "image_path_or_base64": shared_path,
            "caption": media_caption,
        }
    else:
        args = {
            "file_path_or_base64": shared_path,
            "filename": filename,
            "caption": media_caption,
        }

    try:
        raw_result = await send_tool.ainvoke(args)
    except Exception as exc:
        log.warning("agent_run.whatsapp_file_direct_delivery_exception", tool=tool_name, error=str(exc)[:300])
        return False, f"Gagal mengirim {filename} lewat WhatsApp: {exc}"

    result_text = str(raw_result or "")
    step_no = max([int(step.get("step") or 0) for step in parsed.get("steps", [])] + [0]) + 1
    parsed["steps"].append(
        {
            "step": step_no,
            "tool": tool_name,
            "args": args,
            "result": result_text[:4000],
            "tool_call_id": "deterministic_whatsapp_file_delivery",
        }
    )
    parsed["db_messages"].append(
        Message(
            session_id=session_id,
            role="tool",
            tool_name=tool_name,
            tool_args=args,
            tool_result=result_text[:2000],
            step_index=step_index,
            run_id=run_id,
        )
    )

    sent = _has_whatsapp_media_send_step(parsed["steps"])
    if not sent:
        log.warning(
            "agent_run.whatsapp_file_direct_delivery_failed",
            tool=tool_name,
            result=result_text[:300],
        )
        return False, f"Gagal mengirim {filename} lewat WhatsApp: {result_text or 'tool tidak mengembalikan hasil sukses'}"

    log.info("agent_run.whatsapp_file_direct_delivery_sent", tool=tool_name, filename=filename)
    if tool_name == "send_whatsapp_image":
        return True, f"Gambar {filename} sudah saya kirim ke WhatsApp."
    return True, f"File {filename} sudah saya kirim ke WhatsApp."


def _shared_artifact_record(shared_path: str, *, sent: bool = False) -> dict[str, Any]:
    filename = (shared_path or "").rsplit("/", 1)[-1] or "file"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "path": shared_path,
        "filename": filename,
        "extension": ext,
        "sent": bool(sent),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _remember_shared_artifact_path(
    session: Session,
    shared_path: str | None,
    *,
    sent: bool = False,
) -> str | None:
    """Persist the latest shared artifact path so later WA turns can resend it."""
    if not shared_path:
        return None
    record = _shared_artifact_record(shared_path, sent=sent)
    meta = dict(session.metadata_ or {})
    artifacts = meta.get("shared_artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
    artifacts = [
        item
        for item in artifacts
        if not (isinstance(item, dict) and item.get("path") == shared_path)
    ]
    artifacts.append(record)
    meta["shared_artifacts"] = artifacts[-10:]
    meta["latest_shared_artifact"] = record
    session.metadata_ = meta
    return shared_path


def _remember_latest_shared_artifact(
    session: Session,
    steps: list[dict[str, Any]],
    final_reply: str,
    *,
    sent: bool = False,
) -> str | None:
    """Store the newest /workspace/shared artifact mentioned by this run."""
    return _remember_shared_artifact_path(
        session,
        _extract_shared_workspace_file_from_steps(steps, final_reply),
        sent=sent,
    )


# Pre-LLM "resend the latest shared file" heuristic was intentionally removed.
# Deciding whether to send a file is the LLM's job: it calls send_whatsapp_document /
# send_whatsapp_image when the conversation calls for it. The post-run followup
# (_needs_whatsapp_file_delivery_followup) is the deterministic safety net for files
# the agent promised but forgot to actually send. Intent is never inferred from
# keyword matching against the user's message.


async def run_agent(
    *,
    agent_model: AgentModel,
    session: Session,
    user_message: str,
    db: AsyncSession,
    escalation_user_jid: str | None = None,
    escalation_context: str | None = None,
    media_image_b64: str | None = None,
    media_image_mime: str | None = None,
    current_attachment_name: str | None = None,
    sender_name: str | None = None,
    prior_run_was_interrupted: bool = False,
) -> AgentRunResult:
    """
    Jalankan agent end-to-end:
    1. Setup LLM + sandbox
    2. Build tools berdasarkan tools_config
    3. Build sub-agents (jika enabled)
    4. Inject RAG + context summary + memory ke system prompt
    5. Load history, run graph
    6. Persist messages ke DB
    7. Auto-extract long-term memory (jika triggered)
    """
    run_id = uuid.uuid4()
    agent_id: uuid.UUID = session.agent_id
    _raw_tools_cfg = agent_model.tools_config
    tools_config: dict[str, Any] = _raw_tools_cfg if isinstance(_raw_tools_cfg, dict) else {}
    temperature: float = getattr(agent_model, "temperature", 0.7)

    log = logger.bind(
        run_id=str(run_id),
        session_id=str(session.id),
        agent_id=str(agent_id),
        model=agent_model.model,
    )
    log.info("agent_run.start")

    abandoned_before_current = (
        await db.execute(
            select(Run)
            .where(
                Run.session_id == session.id,
                Run.status == "abandoned",
            )
            .order_by(Run.completed_at.desc().nullslast(), Run.started_at.desc().nullslast())
            .limit(1)
        )
    ).scalar_one_or_none()

    # --- Create Run record (status: running) ---
    _now = datetime.now(timezone.utc)
    run_record = Run(
        id=run_id,
        session_id=session.id,
        status="running",
        started_at=_now,
    )
    db.add(run_record)

    await db.flush()

    # HITL action_requests use .get("name", ...) and .get("args", ...);
    # compatibility tests also assert .get("name", ...) remains documented here.
    resumed_result = await handle_pending_interrupt(
        session=session,
        user_message=user_message,
        db=db,
        run_record=run_record,
        run_id=run_id,
        log=log,
    )
    if resumed_result is not None:
        return AgentRunResult(**resumed_result)

    # --- Token quota pre-run gate ---
    _quota_block = await _pre_run_quota_gate(
        agent_model=agent_model,
        db=db,
        log=log,
        run_record=run_record,
        run_id=run_id,
    )
    if _quota_block is not None:
        return _quota_block

    llm_raw, llm = build_agent_llms(agent_model, settings, temperature)

    # Fetch operating manual early so it can be passed to tool setup for SOP gating.
    _early_operating_manual = await get_latest_agent_operating_manual(
        agent_id,
        db,
        fallback_tools_config=tools_config,
    )

    # Tool setup lives in agent_tool_setup.py; it still gates builder tools via
    # capabilities and build_builder_tools.
    tool_setup = await build_agent_tool_setup(
        agent_model=agent_model,
        session=session,
        tools_config=tools_config,
        raw_tools_config=_raw_tools_cfg,
        db=db,
        log=log,
        escalation_user_jid=escalation_user_jid,
        sender_name=sender_name,
        user_message=user_message,
        operating_manual=_early_operating_manual,
    )
    tools = tool_setup.tools
    active_groups = tool_setup.active_groups
    saved_custom_tools = tool_setup.saved_custom_tools
    sandbox = tool_setup.sandbox
    subagent_list = tool_setup.subagent_list
    sub_sandboxes = tool_setup.sub_sandboxes
    _memory_scope = tool_setup.memory_scope

    runtime_policy = build_agent_runtime_policy(agent_model, tools_config)
    google_mcp_parent_only = should_use_google_workspace_parent_only(
        policy=runtime_policy,
        user_message=user_message,
        tools_config=tools_config,
    )
    if google_mcp_parent_only and subagent_list:
        log.info(
            "agent_run.google_mcp_subagents_removed_before_prompt",
            subagents=len(subagent_list),
            reason="google_workspace_mcp_parent_only",
        )
        subagent_list = []
        active_groups = [
            group for group in active_groups if not str(group).startswith("subagents(")
        ]

    log.debug("agent_run.tools_ready (pre-mcp)", groups=active_groups, count=len(tools))

    # ------------------------------------------------------------------ #
    # 5. Context enrichment                                               #
    # ------------------------------------------------------------------ #
    rag_context = ""
    if _is_enabled(tools_config, "rag", default=False):
        rag_context = await build_rag_context(agent_id, user_message, db, tools_config, log)

    context_summary = await maybe_summarize_context(session, db, llm, log)

    memory_block = await build_memory_context(agent_id, db, scope=_memory_scope)
    layered_memory = await load_layered_memory(agent_id, db, scope=_memory_scope)
    # Reuse the operating manual already fetched before tool setup (avoids duplicate DB query).
    operating_manual = _early_operating_manual
    setattr(agent_model, "_runtime_operating_manual", operating_manual)

    # When a context summary is already injected into the system prompt (triggered
    # after context_summary_trigger messages), loading the full short_term_memory_turns
    # is redundant — the summary covers older turns.  Reduce to half the configured
    # limit so the LLM context stays manageable on long sessions.
    _history_turns = (
        max(settings.short_term_memory_turns // 2, 5)
        if context_summary
        else settings.short_term_memory_turns
    )
    history_rows = await load_history(session.id, db, max_turns=_history_turns)
    prior_messages = db_messages_to_lc(history_rows)
    google_auth_recovery_followup = is_google_auth_recovery_followup(
        user_message,
        history_rows,
    )
    google_auth_recovery_request = (
        find_last_google_workspace_user_request(history_rows)
        if google_auth_recovery_followup
        else None
    )
    execution_user_message = google_auth_recovery_request or user_message
    log.debug("agent_run.history_loaded", turns=len(prior_messages) // 2)
    direct_wa_text_send_context = (
        getattr(session, "channel_type", None) == "whatsapp"
        and _is_direct_whatsapp_text_send_context(user_message, history_rows)
    )
    if direct_wa_text_send_context:
        log.info("agent_run.direct_wa_text_send_context_detected")
    if google_auth_recovery_followup:
        log.info(
            "agent_run.google_mcp_auth_recovery_followup_detected",
            has_prior_request=bool(google_auth_recovery_request),
        )

    is_op_msg = _is_operator_envelope(user_message)

    # ------------------------------------------------------------------ #
    # 6. System prompt                                                    #
    # ------------------------------------------------------------------ #
    system_prompt = build_system_prompt(
        agent_model=agent_model,
        session=session,
        active_groups=active_groups,
        saved_custom_tools=saved_custom_tools,
        subagent_list=subagent_list,
        sender_name=sender_name,
        context_summary=context_summary,
        memory_block=memory_block,
        layered_memory=layered_memory,
        rag_context=rag_context,
        escalation_user_jid=escalation_user_jid,
        escalation_context=escalation_context,
        is_operator_message=is_op_msg,
        user_message=user_message,
    )
    if abandoned_before_current is not None:
        system_prompt += (
            "\n\n## Restart Recovery\n"
            "Run sebelumnya terhenti karena service restart sebelum selesai. "
            "JANGAN lanjutkan atau ulangi task lama secara otomatis. "
            "Fokus pada pesan user terbaru. Jika user bertanya status task lama, jelaskan singkat bahwa proses sebelumnya terhenti saat restart dan minta konfirmasi sebelum menjalankan ulang."
        )
    if google_auth_recovery_followup:
        _prior_google_request = (
            f"\nRequest Google Workspace yang harus dilanjutkan: {google_auth_recovery_request[:800]}"
            if google_auth_recovery_request
            else ""
        )
        system_prompt += (
            "\n\n## Google Workspace Auth Recovery\n"
            "Pesan user terbaru adalah konfirmasi bahwa OAuth Google sudah dicoba/diselesaikan. "
            "Lanjutkan request Google Workspace terakhir dari percakapan sebelumnya memakai tool Google Workspace langsung. "
            "Jika integrasi Google masih belum authorized, jangan gunakan task, sandbox, atau filesystem sebagai fallback; kirim blocker auth yang berisi link reconnect baru. "
            "Jangan menyebut istilah teknis internal/protokol tool kepada user."
            f"{_prior_google_request}"
        )
    if direct_wa_text_send_context:
        system_prompt += (
            "\n\n## Direct WhatsApp Text Send — Tool Lock\n"
            "Turn ini adalah konteks kirim pesan teks WhatsApp ke nomor tertentu. "
            "Satu-satunya tool pengiriman yang boleh dipakai untuk aksi ini adalah `send_to_number(phone_or_target, message)`.\n"
            "- Jangan gunakan `send_message` karena itu Google Chat/space, bukan WhatsApp.\n"
            "- Jangan gunakan `send_whatsapp_image` atau `send_whatsapp_document` karena user meminta pesan teks, bukan media.\n"
            "- Jangan gunakan `notify_user` untuk mengklaim sukses. Setelah `send_to_number` sukses, baru tulis final singkat ke operator/user.\n"
        )

    # ------------------------------------------------------------------ #
    # 7. Persist user message                                             #
    # ------------------------------------------------------------------ #
    step_base = max((m.step_index for m in history_rows), default=-1) + 1
    # Commit the inbound message before running the graph so a later rollback
    # (cancel/timeout/error in the caller) cannot silently drop the user's request.
    await _persist_inbound_user_message(
        db,
        session_id=session.id,
        run_id=run_id,
        content=user_message,
        step_index=step_base,
    )

    # ------------------------------------------------------------------ #
    # 8. Run agent graph (with MCP tools)                                 #
    # ------------------------------------------------------------------ #
    from app.core.tools.mcp_tool import mcp_client_context

    # WA progress notify — only for WhatsApp sessions
    _ch_cfg: dict = session.channel_config if isinstance(session.channel_config, dict) else {}
    _wa_device_id: str = _ch_cfg.get("device_id", "")
    _wa_target: str = _ch_cfg.get("user_phone", "")
    _is_wa_session: bool = getattr(session, "channel_type", None) == "whatsapp" and bool(_wa_device_id and _wa_target)
    _wa_typing_started: bool = False

    async def _start_wa_run_typing() -> None:
        nonlocal _wa_typing_started
        if not _is_wa_session:
            return
        try:
            from app.core.infra.wa_client import start_wa_typing

            await start_wa_typing(_wa_device_id, _wa_target)
            _wa_typing_started = True
        except Exception as exc:
            log.warning("agent_run.wa_typing_start_failed", error=str(exc)[:200])

    async def _stop_wa_run_typing() -> None:
        if not _is_wa_session or not _wa_typing_started:
            return
        try:
            from app.core.infra.wa_client import stop_wa_typing

            await stop_wa_typing(_wa_device_id, _wa_target)
        except Exception as exc:
            log.warning("agent_run.wa_typing_stop_failed", error=str(exc)[:200])

    await _start_wa_run_typing()
    google_mcp_tools_config = tools_config
    google_mcp_role_denied = (
        is_google_workspace_mcp_configured(tools_config)
        and not _is_google_workspace_mcp_authorized_for_session(session, agent_model)
    )
    if google_mcp_role_denied:
        google_mcp_tools_config = _remove_google_workspace_mcp_server(tools_config)
        log.warning(
            "agent_run.google_mcp_denied_for_non_operator",
            sender=_session_sender_phone(session),
            reason="google_workspace_mcp_requires_owner_or_operator",
        )
        if _is_google_mcp_intent(execution_user_message) or google_auth_recovery_followup:
            final_reply = _google_workspace_mcp_unauthorized_reply()
            run_record.status = "completed"
            run_record.completed_at = datetime.now(timezone.utc)
            run_record.error_message = "google_workspace_mcp_denied_for_non_operator"
            run_record.tokens_used = 0
            run_record.prompt_tokens = 0
            run_record.completion_tokens = 0
            run_record.reasoning_tokens = 0
            run_record.cached_tokens = 0
            run_record.openrouter_cost_usd = Decimal("0")
            run_record.usage_details = None
            db.add(Message(
                session_id=session.id,
                role="assistant",
                content=final_reply,
                step_index=step_base + 1,
                run_id=run_id,
            ))
            await db.flush()
            await _stop_wa_run_typing()
            if sandbox:
                await sandbox.aclose()
            for _ssb in sub_sandboxes:
                await _ssb.aclose()
            return AgentRunResult(
                reply=final_reply,
                steps=[],
                run_id=run_id,
                tokens_used=0,
                usage={
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "reasoning_tokens": 0,
                    "cached_tokens": 0,
                    "total_tokens": 0,
                    "openrouter_cost_usd": 0,
                    "details": None,
                },
            )
    _google_fallback_external_user_id = getattr(agent_model, "owner_external_id", None)
    if not _google_fallback_external_user_id:
        _operator_ids = getattr(agent_model, "operator_ids", None)
        if isinstance(_operator_ids, list) and _operator_ids:
            _google_fallback_external_user_id = str(_operator_ids[0])

    google_mcp = await prepare_google_mcp_runtime(
        tools_config=google_mcp_tools_config,
        tools=tools,
        active_groups=active_groups,
        session=session,
        agent_id=agent_id,
        memory_scope=_memory_scope,
        api_key=settings.api_key,
        user_message=execution_user_message,
        system_prompt=system_prompt,
        log=log,
        fallback_external_user_id=_google_fallback_external_user_id,
    )
    system_prompt = google_mcp.system_prompt
    _google_mcp_auth_url = google_mcp.auth_url
    mcp_tools_config = google_mcp_tools_config
    if (
        google_mcp.enabled
        and google_mcp.workspace_server
        and not _google_workspace_server_has_auth(google_mcp)
    ):
        mcp_tools_config = _remove_google_workspace_mcp_server(google_mcp_tools_config)
        log.info(
            "agent_run.google_mcp_client_skipped_until_auth",
            reason="missing_per_user_bearer",
            auth_url_present=bool(_google_mcp_auth_url),
            integration_url=google_mcp.integration_url or None,
        )

    async with mcp_client_context(mcp_tools_config) as (mcp_tools, mcp_errors):
        if google_mcp.preflight_error and "google_workspace" not in mcp_errors:
            mcp_errors["google_workspace"] = google_mcp.preflight_error
        if mcp_tools:
            mcp_tools = sanitize_google_forms_tools(mcp_tools, log)
            if getattr(session, "channel_type", None) == "whatsapp":
                mcp_tools = _filter_whatsapp_unsafe_mcp_tools(
                    mcp_tools,
                    user_message=user_message,
                    log=log,
                )
            mcp_tool_names = [getattr(tool, "name", "") for tool in mcp_tools]
        if mcp_tools:
            if google_mcp_parent_only and subagent_list:
                log.info(
                    "agent_run.google_mcp_subagents_disabled",
                    subagents=len(subagent_list),
                    reason="google_workspace_mcp_must_run_in_parent",
                )
                subagent_list = []
            # Put MCP tools first so model/tool-router bias favors the connected
            # external service over sandbox helpers when both could appear useful.
            tools = mcp_tools + tools
            active_groups.append(f"mcp({len(mcp_tools)} tools)")
            if isinstance(system_prompt, str):
                system_prompt += build_mcp_tool_priority_notice(
                    mcp_tool_names=mcp_tool_names,
                    sandbox_active=sandbox is not None,
                )
            log.debug("agent_run.mcp_tools_added", count=len(mcp_tools), names=mcp_tool_names)
            if google_mcp_parent_only:
                task_tools = [
                    getattr(tool, "name", "")
                    for tool in tools
                    if getattr(tool, "name", "") == "task"
                ]
                if task_tools:
                    log.warning(
                        "agent_run.google_mcp_parent_only_task_tool_present",
                        count=len(task_tools),
                    )
        if direct_wa_text_send_context:
            tools = _prioritize_direct_whatsapp_text_send_tools(tools, log)
        if mcp_errors:
            log.warning("agent_run.mcp_errors", errors=mcp_errors)
            _google_mcp_auth_url, system_prompt = await apply_mcp_error_notice(
                mcp_errors=mcp_errors,
                runtime=google_mcp,
                agent_id=agent_id,
                memory_scope=_memory_scope,
                api_key=settings.api_key,
                system_prompt=system_prompt,
                log=log,
            )
            google_mcp_err = str(mcp_errors.get("google_workspace") or "")
            should_block_google_before_graph = (
                bool(google_mcp_err)
                and not mcp_tools
                and (
                    google_mcp_parent_only
                    or _is_google_mcp_intent(user_message)
                    or google_auth_recovery_followup
                )
            )
            if should_block_google_before_graph:
                if _is_google_auth_or_scope_error(google_mcp_err):
                    if not _google_mcp_auth_url:
                        _google_mcp_auth_url = await _fetch_google_auth_link(
                            integration_url=google_mcp.integration_url,
                            api_key=settings.api_key,
                            agent_id=agent_id,
                            candidate_user_ids=google_mcp.candidate_user_ids,
                        )
                    final_reply = await _build_google_mcp_auth_failure_reply(
                        llm=llm_raw,
                        user_message=execution_user_message,
                        error_text=google_mcp_err,
                        auth_url=_google_mcp_auth_url,
                    )
                else:
                    final_reply = _build_google_mcp_unavailable_reply(google_mcp_err)
                final_reply = await _route_google_workspace_blocker_to_owner_if_customer(
                    reply=final_reply,
                    session=session,
                    agent_model=agent_model,
                    user_message=execution_user_message,
                    error_text=google_mcp_err,
                    auth_url=_google_mcp_auth_url,
                    log=log,
                )
                log.warning(
                    "agent_run.google_mcp_blocked_before_graph",
                    error=google_mcp_err[:200],
                    auth_url_present=bool(_google_mcp_auth_url),
                )
                run_record.status = "completed"
                run_record.completed_at = datetime.now(timezone.utc)
                run_record.error_message = google_mcp_err[:2000]
                run_record.tokens_used = 0
                run_record.prompt_tokens = 0
                run_record.completion_tokens = 0
                run_record.reasoning_tokens = 0
                run_record.cached_tokens = 0
                run_record.openrouter_cost_usd = Decimal("0")
                run_record.usage_details = None
                db.add(Message(
                    session_id=session.id,
                    role="assistant",
                    content=final_reply,
                    step_index=step_base + 1,
                    run_id=run_id,
                ))
                await db.flush()
                await _stop_wa_run_typing()
                if sandbox:
                    await sandbox.aclose()
                for _ssb in sub_sandboxes:
                    await _ssb.aclose()
                return AgentRunResult(
                    reply=final_reply,
                    steps=[],
                    run_id=run_id,
                    tokens_used=0,
                    usage={
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "reasoning_tokens": 0,
                        "cached_tokens": 0,
                        "total_tokens": 0,
                        "openrouter_cost_usd": 0,
                        "details": None,
                    },
                )

        backend = None
        _checkpointer = None
        try:
            from langgraph.checkpoint.memory import MemorySaver
            from app.core.engine.deep_agent_backend import DockerBackend

            backend = DockerBackend(sandbox) if sandbox is not None else None
            _checkpointer = MemorySaver()
            # interrupt_on: tools_config["interrupt_on"] may be:
            #   - dict: {"tool_name": true}  → pass directly
            #   - list: ["tool_name"]         → convert to {name: True}
            _raw_interrupt_on = tools_config.get("interrupt_on") if isinstance(tools_config, dict) else None
            if isinstance(_raw_interrupt_on, dict) and _raw_interrupt_on:
                _interrupt_on: dict[str, bool] = _raw_interrupt_on
            elif isinstance(_raw_interrupt_on, list) and _raw_interrupt_on:
                _interrupt_on = {name: True for name in _raw_interrupt_on}
            else:
                _interrupt_on = {}
            if runtime_policy.is_builder:
                # Arthur is a control-plane agent. DeepAgents adds a generic
                # task tool even without explicit subagents, which lets Arthur
                # invent "updates" instead of calling builder tools.
                from langgraph.prebuilt import create_react_agent

                log.info(
                    "agent_run.builder_react_agent_mode",
                    reason="builder_must_not_receive_task_or_filesystem_tools",
                )
                graph = create_react_agent(
                    llm,
                    tools=tools,
                    prompt=system_prompt,
                    checkpointer=_checkpointer,
                )
            else:
                from deepagents import create_deep_agent

                # PENTING: gunakan llm_raw (bukan llm yang sudah .bind()) —
                # DeepAgents SDK memanggil .count() pada model untuk parse nama provider,
                # yang gagal pada RunnableBinding dan menyebabkan AttributeError ditangkap
                # sebagai TypeError → fallback ke create_react_agent tanpa backend.
                _dag_kwargs: dict[str, Any] = dict(
                    model=llm_raw,
                    tools=tools,
                    system_prompt=system_prompt,
                    backend=backend,
                    subagents=subagent_list or None,
                    checkpointer=_checkpointer,
                )
                # Outermost: turn any tool exception into recoverable feedback so one
                # failed tool call (e.g. MCP/Gmail 400) can't abort the whole run.
                _middleware: list[AgentMiddleware] = [ToolErrorRecoveryMiddleware()]
                if (
                    runtime_policy.policy_class == "operational"
                    and google_mcp.enabled
                    and google_mcp.workspace_server
                ):
                    _middleware.append(
                        ExternalServiceFallbackGuardMiddleware(
                            policy=runtime_policy,
                            google_workspace_mcp_available=True,
                            user_message=execution_user_message,
                        )
                    )
                if google_mcp_parent_only:
                    _middleware.append(BlockTaskToolMiddleware())
                    log.info(
                        "agent_run.google_mcp_deepagent_parent_only_mode",
                        reason="block_task_tool_keep_deepagents_runtime",
                    )
                if _middleware:
                    _dag_kwargs["middleware"] = _middleware
                if _interrupt_on:
                    _dag_kwargs["interrupt_on"] = _interrupt_on
                graph = create_deep_agent(**_dag_kwargs)
        except (ImportError, TypeError, AttributeError) as _dag_err:
            if sandbox is not None or backend is not None or subagent_list:
                log.error(
                    "agent_run.deepagent_required_failed",
                    error=str(_dag_err)[:300],
                    has_backend=backend is not None,
                    has_sandbox=sandbox is not None,
                    subagents=len(subagent_list or []),
                )
                raise
            log.warning(
                "agent_run.deepagent_fallback",
                error=str(_dag_err)[:300],
                has_sandbox=sandbox is not None,
            )
            from langgraph.prebuilt import create_react_agent
            graph = create_react_agent(
                llm,
                tools=tools,
                prompt=system_prompt,
                checkpointer=_checkpointer,
            )

        human_content: Any = _build_human_content_for_model(
            user_message=user_message,
            model=getattr(agent_model, "model", None),
            media_image_b64=media_image_b64,
            media_image_mime=media_image_mime,
            google_auth_recovery_followup=google_auth_recovery_followup,
            google_auth_recovery_request=google_auth_recovery_request,
            log=log,
        )

        input_messages: list[BaseMessage] = build_input_messages(
            prior_messages=prior_messages,
            history_rows=history_rows,
            human_content=human_content,
            log=log,
            current_attachment_name=current_attachment_name,
            current_attachment=(
                session.metadata_.get("current_attachment")
                if isinstance(getattr(session, "metadata_", None), dict)
                else None
            ),
        )
        step_counter = step_base + 1

        _agent_logger = AgentStepLogger(log)

        def _usage_summary() -> dict[str, Any]:
            return {
                "prompt_tokens": _agent_logger.prompt_tokens_from_callbacks,
                "completion_tokens": _agent_logger.completion_tokens_from_callbacks,
                "reasoning_tokens": _agent_logger.reasoning_tokens_from_callbacks,
                "cached_tokens": _agent_logger.cached_tokens_from_callbacks,
                "total_tokens": _agent_logger.total_tokens_from_callbacks,
                "openrouter_cost_usd": round(_agent_logger.openrouter_cost_usd_from_callbacks, 8),
                "details": _agent_logger.usage_details,
            }

        def _apply_run_usage(total_tokens: int) -> None:
            summary = _usage_summary()
            run_record.tokens_used = int(total_tokens or summary["total_tokens"] or 0)
            run_record.prompt_tokens = int(summary["prompt_tokens"] or 0)
            run_record.completion_tokens = int(summary["completion_tokens"] or 0)
            run_record.reasoning_tokens = int(summary["reasoning_tokens"] or 0)
            run_record.cached_tokens = int(summary["cached_tokens"] or 0)
            run_record.openrouter_cost_usd = Decimal(str(summary["openrouter_cost_usd"] or 0))
            run_record.usage_details = summary["details"] or None

        _thread_id = str(session.id)
        _graph_config = {
            "recursion_limit": settings.agent_max_steps * 8,
            "callbacks": [_agent_logger],
            "configurable": {"thread_id": _thread_id},
        }

        async def _cleanup_sandboxes() -> None:
            await _stop_wa_run_typing()
            if sandbox:
                await sandbox.aclose()
            for _ssb in sub_sandboxes:
                await _ssb.aclose()

        async def _complete_direct_whatsapp_file_delivery(
            *,
            path: str,
            caption: str | None,
            remember_sent_artifact: bool,
            log_event: str,
        ) -> AgentRunResult:
            parsed = ParsedResult(
                final_reply="",
                steps=[],
                total_tokens_used=0,
                db_messages=[],
                has_output=True,
            )
            tool_step_index = step_counter
            _sent, final_reply = await _deliver_shared_whatsapp_file_via_tool(
                tools=tools,
                shared_path=path,
                parsed=parsed,
                session_id=session.id,
                run_id=run_id,
                step_index=tool_step_index,
                log=log,
                caption=caption,
            )
            steps = parsed["steps"]
            if _sent and remember_sent_artifact:
                _remember_shared_artifact_path(session, path, sent=True)
            for _msg_record in parsed["db_messages"]:
                db.add(_msg_record)
            agent_step_index = tool_step_index + len(parsed["db_messages"])
            db.add(Message(
                session_id=session.id,
                role="agent",
                content=final_reply,
                step_index=agent_step_index,
                run_id=run_id,
                # A failed delivery notice is transient status, not dialogue —
                # tag it so it is never replayed as history (see context_service).
                tool_name=None if _sent else DELIVERY_STATUS_TAG,
            ))
            run_record.status = "completed"
            run_record.completed_at = datetime.now(timezone.utc)
            _apply_run_usage(0)
            if _is_enabled(tools_config, "memory", default=True):
                await record_runtime_memory(
                    agent_id=agent_id,
                    db=db,
                    scope=_memory_scope,
                    user_message=execution_user_message,
                    final_reply=final_reply,
                    current_attachment_name=current_attachment_name,
                    generated_artifact_path=path if (_sent and remember_sent_artifact) else None,
                    log=log,
                )
            await db.flush()
            await _cleanup_sandboxes()
            log.info(log_event, sent=_sent, path=path)
            return AgentRunResult(
                reply=final_reply,
                steps=steps,
                run_id=run_id,
                tokens_used=0,
                usage=_usage_summary(),
            )

        direct_wa_confirmation_payload = (
            _extract_direct_whatsapp_confirmation_payload(user_message, history_rows)
            if direct_wa_text_send_context
            else None
        )
        direct_current_image_delivery = (
            _current_image_attachment_delivery_request(
                session=session,
                current_attachment_name=current_attachment_name,
                user_message=execution_user_message,
            )
            if getattr(session, "channel_type", None) == "whatsapp"
            and _is_enabled(tools_config, "whatsapp_media", default=True)
            and not direct_wa_text_send_context
            and not runtime_policy.is_builder
            else None
        )
        if direct_current_image_delivery:
            path, caption = direct_current_image_delivery
            return await _complete_direct_whatsapp_file_delivery(
                path=path,
                caption=caption,
                remember_sent_artifact=False,
                log_event="agent_run.whatsapp_current_image_direct_delivery",
            )
        # NOTE: No pre-LLM "resend the latest shared file" shortcut. When the user
        # asks to send a file, the LLM decides and calls send_whatsapp_document /
        # send_whatsapp_image itself; the post-run followup
        # (_needs_whatsapp_file_delivery_followup) is the safety net that delivers a
        # file the agent promised but forgot to actually send. Intent is never guessed
        # from keywords in the user's message.

        if direct_wa_confirmation_payload:
            target_phone, draft_message = direct_wa_confirmation_payload
            steps = [
                {
                    "step": 1,
                    "tool": "send_to_number",
                    "args": {"phone_or_target": target_phone, "message": draft_message},
                    "result": "",
                }
            ]
            _wa_send_failed = False
            _raw_cfg = session.channel_config
            _channel_cfg = _raw_cfg if isinstance(_raw_cfg, dict) else {}
            _should_send_direct_wa = False
            try:
                from app.core.engine.wa_outbound_guard import (
                    check_wa_outbound_direct_window,
                    looks_like_outbound_wa_spam_request,
                    wa_outbound_block_reply,
                )

                _recent_direct_wa_context = "\n".join(
                    _operator_message_payload(getattr(row, "content", "") or "")
                    for row in (history_rows or [])[-12:]
                )
                if looks_like_outbound_wa_spam_request(
                    f"{execution_user_message}\n{_recent_direct_wa_context}"
                ):
                    _wa_send_failed = True
                    tool_result = f"[send_to_number blocked] {wa_outbound_block_reply('spam_request')}"
                    steps[0]["result"] = tool_result
                    final_reply = wa_outbound_block_reply("spam_request")
                    log.warning("agent_run.direct_wa_confirmation_blocked_spam_request", target=target_phone)
                else:
                    allowed, count = await check_wa_outbound_direct_window(
                        device_id=str(_channel_cfg.get("device_id", "") or ""),
                        target=target_phone,
                    )
                    if not allowed:
                        _wa_send_failed = True
                        tool_result = f"[send_to_number blocked] {wa_outbound_block_reply('rate_limit')}"
                        steps[0]["result"] = tool_result
                        final_reply = wa_outbound_block_reply("rate_limit")
                        log.warning(
                            "agent_run.direct_wa_confirmation_blocked_rate_limit",
                            target=target_phone,
                            count=count,
                        )
                    else:
                        _should_send_direct_wa = True
                if _should_send_direct_wa:
                    from app.core.infra.channel_service import send_message as _channel_send_message
                    await _channel_send_message(
                        channel_type=session.channel_type,
                        channel_config=_channel_cfg,
                        text=draft_message,
                        to_override=target_phone,
                    )
                    tool_result = f"[SENT_TO_NUMBER:{target_phone}] {draft_message}"
                    steps[0]["result"] = tool_result
                    final_reply = f"Pesan WhatsApp ke {target_phone} sudah saya kirim."
                    log.info("agent_run.direct_wa_confirmation_sent", target=target_phone)
            except Exception as exc:
                _wa_send_failed = True
                tool_result = f"[error] Gagal kirim WhatsApp ke {target_phone}: {exc}"
                steps[0]["result"] = tool_result
                final_reply = f"Gagal mengirim pesan WhatsApp ke {target_phone}: {exc}"
                log.warning("agent_run.direct_wa_confirmation_send_failed", target=target_phone, error=str(exc)[:300])

            db.add(Message(
                session_id=session.id,
                role="tool",
                tool_name="send_to_number",
                tool_args={"phone_or_target": target_phone, "message": draft_message},
                tool_result=tool_result[:2000],
                step_index=step_counter,
                run_id=run_id,
            ))
            db.add(Message(
                session_id=session.id,
                role="agent",
                content=final_reply,
                step_index=step_counter + 1,
                run_id=run_id,
                # Tag a failed send so it is not replayed as history next turn.
                tool_name=DELIVERY_STATUS_TAG if _wa_send_failed else None,
            ))
            run_record.status = "completed"
            run_record.completed_at = datetime.now(timezone.utc)
            _apply_run_usage(0)
            if _is_enabled(tools_config, "memory", default=True):
                await record_runtime_memory(
                    agent_id=agent_id,
                    db=db,
                    scope=_memory_scope,
                    user_message=execution_user_message,
                    final_reply=final_reply,
                    current_attachment_name=current_attachment_name,
                    log=log,
                )
            await db.flush()
            await _cleanup_sandboxes()
            return AgentRunResult(
                reply=final_reply,
                steps=steps,
                run_id=run_id,
                tokens_used=0,
                usage=_usage_summary(),
            )

        # Initialize so UnboundLocalError can't happen if an exception bypasses parse_agent_result
        final_reply: str = ""
        steps: list = []
        total_tokens_used: int = 0
        _current_shared_artifact_path: str | None = None
        _graph_output: Any | None = None
        parsed: dict = {"final_reply": "", "steps": [], "total_tokens_used": 0, "has_output": False, "db_messages": []}

        _agent_caps = getattr(agent_model, "capabilities", []) or []
        _has_subagents = bool(
            isinstance(tools_config, dict)
            and tools_config.get("subagents", {})
            and tools_config["subagents"].get("enabled")
        )
        _is_builder_or_system_agent = "builder" in _agent_caps or "system" in _agent_caps
        if _is_builder_or_system_agent and not _has_subagents:
            _graph_config["recursion_limit"] = max(settings.agent_max_steps * 3, 24)
        # Agents that delegate to sys_coder may build framework projects (npm install,
        # next build, pip install) — these can take 5-10 min on cold sandboxes.
        # Keep that extended ceiling for subagent-enabled flows only. Arthur
        # builder runs without subagents should fail fast enough for WhatsApp.
        if _has_subagents:
            _timeout = settings.agent_timeout_seconds * 8
        elif _is_builder_or_system_agent:
            _timeout = min(settings.agent_timeout_seconds * 4, 540)
        else:
            _timeout = settings.agent_timeout_seconds
        try:
            async with asyncio.timeout(_timeout):
                _graph_output = await graph.ainvoke(
                    {"messages": input_messages},
                    config=_graph_config,
                    version="v2",
                )
                # GraphOutput (version="v2") may only carry .interrupts; when
                # a checkpointer exists, get messages from graph state.
                result = await _graph_result_from_output(
                    graph=graph,
                    graph_config=_graph_config,
                    graph_output=_graph_output,
                    log=log,
                )
        except asyncio.CancelledError:
            # Human interrupt — user sent a new message while this run was active.
            log.info("agent_run.cancelled_by_interrupt", session_id=str(session.id))
            _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
            await _persist_run_failure(
                db,
                run_record=run_record,
                status="cancelled",
                error_message="Cancelled because a newer user message interrupted this run.",
            )
            await _cleanup_sandboxes()
            raise  # propagate so the task is properly marked cancelled
        except asyncio.TimeoutError:
            log.error(
                "agent_run.timeout",
                timeout_seconds=_timeout,
                session_id=str(session.id),
            )
            await _persist_run_failure(
                db,
                run_record=run_record,
                status="timed_out",
                error_message=f"Timeout after {_timeout}s",
            )
            await _cleanup_sandboxes()
            await send_agent_recovery_message(
                is_wa_session=_is_wa_session,
                wa_device_id=_wa_device_id,
                wa_target=_wa_target,
                llm_raw=llm_raw,
                system_prompt=system_prompt,
                reason="memakan waktu terlalu lama dan terpaksa dihentikan",
                log=log,
            )
            raise
        except Exception as exc:
            err_str = str(exc)
            # Log sub-exceptions from ExceptionGroup for debugging
            if hasattr(exc, "exceptions"):
                for _sub in exc.exceptions:
                    log.error("agent_run.exception_group_sub", sub_type=type(_sub).__name__, sub_error=str(_sub))

            # JSONDecodeError inside a subagent = OpenRouter returned a truncated
            # HTTP response (network hiccup). Retry the same graph once with a brief
            # delay — no need to rebuild or sanitize messages.
            import json as _json_mod
            if isinstance(exc.__cause__, _json_mod.JSONDecodeError) or isinstance(exc, _json_mod.JSONDecodeError) or "JSONDecodeError" in type(exc).__name__ or (exc.__context__ and isinstance(exc.__context__, _json_mod.JSONDecodeError)):
                log.warning("agent_run.subagent_json_error_retry", error=err_str[:200])
                try:
                    await asyncio.sleep(1)
                    async with asyncio.timeout(_timeout):
                        _graph_output = await graph.ainvoke(
                            {"messages": input_messages},
                            config=_graph_config,
                            version="v2",
                        )
                        result = await _graph_result_from_output(
                            graph=graph,
                            graph_config=_graph_config,
                            graph_output=_graph_output,
                            log=log,
                        )
                    log.info("agent_run.subagent_json_error_retry_ok")
                except Exception as _retry_json_exc:
                    log.error("agent_run.subagent_json_error_retry_failed", error=str(_retry_json_exc)[:300])
                    run_record.status = "failed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    run_record.error_message = str(_retry_json_exc)[:2000]
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await db.flush()
                    await _cleanup_sandboxes()
                    await send_agent_recovery_message(
                        is_wa_session=_is_wa_session,
                        wa_device_id=_wa_device_id,
                        wa_target=_wa_target,
                        llm_raw=llm_raw,
                        system_prompt=system_prompt,
                        reason="mengalami gangguan koneksi ke model",
                        log=log,
                    )
                    return AgentRunResult(
                        reply="Maaf, terjadi gangguan koneksi ke model. Silakan coba lagi.",
                        steps=[],
                        run_id=run_id,
                        tokens_used=_agent_logger.total_tokens_from_callbacks,
                        usage=_usage_summary(),
                    )

            # "No tool output found for function call" means the provider received
            # an AIMessage with tool_calls but no matching ToolMessage. This can
            # happen when the Deep Agents SDK drops a tool result mid-graph (e.g.
            # tool exception before ToolMessage is written to state).
            #
            # Retry strategy: rebuild graph using LangGraph's built-in
            # create_react_agent (more reliable tool execution than Deep Agents SDK)
            # with sanitized input so history is clean.
            elif "No tool output found for function call" in err_str:
                log.warning(
                    "agent_run.dangling_tool_call_retry",
                    error=err_str[:300],
                    input_msg_count=len(input_messages),
                )
                # Rebuild with create_react_agent as the fallback executor —
                # the Deep Agents SDK may have been the source of the dropped
                # tool result; LangGraph's ToolNode is the safer path here.
                try:
                    from langgraph.prebuilt import create_react_agent as _cra
                    _fallback_graph = _cra(llm, tools=tools, prompt=system_prompt)
                except Exception as _ge:
                    log.warning("agent_run.fallback_graph_build_failed", error=str(_ge))
                    _fallback_graph = graph

                clean_input = _sanitize_input_messages(input_messages)
                log.info(
                    "agent_run.dangling_tool_call_retry_with_fallback",
                    clean_msg_count=len(clean_input),
                )
                try:
                    async with asyncio.timeout(settings.agent_timeout_seconds):
                        result = await _fallback_graph.ainvoke(
                            {"messages": clean_input}, config=_graph_config
                        )
                    log.info("agent_run.dangling_tool_call_retry_ok")
                except Exception as retry_exc:
                    retry_err = str(retry_exc)
                    if "No tool output found for function call" in retry_err:
                        log.error(
                            "agent_run.dangling_tool_call_retry_failed",
                            error=retry_err[:300],
                        )
                        # Update Run → failed (dangling tool call)
                        run_record.status = "failed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        run_record.error_message = "Dangling tool call after retry"
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.commit()
                        await _cleanup_sandboxes()
                        return AgentRunResult(
                            reply="Maaf, terjadi kesalahan internal. Silakan coba lagi.",
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )
                    log.error("agent_run.retry_error", error=retry_err)
                    # Update Run → failed
                    run_record.status = "failed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    run_record.error_message = retry_err[:2000]
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await db.flush()
                    await _cleanup_sandboxes()
                    raise retry_exc
            else:
                _recovered_via_retry = False
                _slides_invalid_page_target = (
                    "error calling tool 'batch_update_presentation'" in err_str.lower()
                    and "invalid slides batch update request" in err_str.lower()
                    and "targets a slide/page object" in err_str.lower()
                )

                _slides_invalid_dimension = (
                    "error calling tool 'batch_update_presentation'" in err_str.lower()
                    and (
                        "invalid value" in err_str.lower()
                        or "unknown dimension unit" in err_str.lower()
                        or "unit_unspecified" in err_str.lower()
                    )
                    and "dimension" in err_str.lower()
                    and ("create_shape" in err_str.lower() or "createshape" in err_str.lower())
                )

                if _slides_invalid_dimension:
                    log.warning(
                        "agent_run.slides_dimension_retry",
                        error=err_str[:300],
                    )
                    _slides_dim_retry_directive = google_slides_dimension_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _slides_prompt = (
                            (system_prompt + "\n\n" + _slides_dim_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _slides_graph = _cra(llm, tools=tools, prompt=_slides_prompt)
                        _slides_input = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _slides_graph.ainvoke(
                                {"messages": _slides_input}, config=_graph_config
                            )
                        log.info("agent_run.slides_dimension_retry_ok")
                        _recovered_via_retry = True
                    except Exception as _slides_retry_exc:
                        log.warning(
                            "agent_run.slides_dimension_retry_failed",
                            error=str(_slides_retry_exc)[:300],
                        )
                        _reply = _build_google_mcp_validation_reply(err_str)
                        run_record.status = "completed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.flush()
                        await _cleanup_sandboxes()
                        return AgentRunResult(
                            reply=_reply,
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )

                if _slides_invalid_page_target:
                    log.warning(
                        "agent_run.slides_shape_retry",
                        error=err_str[:300],
                    )
                    _slides_retry_directive = google_slides_shape_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _slides_prompt = (
                            (system_prompt + "\n\n" + _slides_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _slides_graph = _cra(llm, tools=tools, prompt=_slides_prompt)
                        _slides_input = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _slides_graph.ainvoke(
                                {"messages": _slides_input}, config=_graph_config
                            )
                        log.info("agent_run.slides_shape_retry_ok")
                        _recovered_via_retry = True
                    except Exception as _slides_retry_exc:
                        log.warning(
                            "agent_run.slides_shape_retry_failed",
                            error=str(_slides_retry_exc)[:300],
                        )
                        _reply = _build_google_mcp_validation_reply(err_str)
                        run_record.status = "completed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.flush()
                        await _cleanup_sandboxes()
                        return AgentRunResult(
                            reply=_reply,
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )

                _forms_create_title_only_error = (
                    "error calling tool 'create_form'" in err_str.lower()
                    and "only info.title can be set when creating a form" in err_str.lower()
                )
                _forms_request_kind_error = (
                    "error calling tool 'batch_update_form'" in err_str.lower()
                    and "request kind was not provided" in err_str.lower()
                )

                if _forms_create_title_only_error:
                    log.warning(
                        "agent_run.forms_create_retry",
                        error=err_str[:300],
                    )
                    _forms_retry_directive = google_forms_create_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _forms_prompt = (
                            (system_prompt + "\n\n" + _forms_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _forms_graph = _cra(llm, tools=tools, prompt=_forms_prompt)
                        _forms_input = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _forms_graph.ainvoke(
                                {"messages": _forms_input}, config=_graph_config
                            )
                        log.info("agent_run.forms_create_retry_ok")
                        _recovered_via_retry = True
                    except Exception as _forms_retry_exc:
                        log.warning(
                            "agent_run.forms_create_retry_failed",
                            error=str(_forms_retry_exc)[:300],
                        )
                        _reply = _build_google_mcp_validation_reply(err_str)
                        run_record.status = "completed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.flush()
                        await _cleanup_sandboxes()
                        return AgentRunResult(
                            reply=_reply,
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )

                if _forms_request_kind_error:
                    log.warning(
                        "agent_run.forms_request_kind_retry",
                        error=err_str[:300],
                    )
                    _forms_kind_retry_directive = google_forms_request_kind_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _forms_kind_prompt = (
                            (system_prompt + "\n\n" + _forms_kind_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _forms_kind_graph = _cra(llm, tools=tools, prompt=_forms_kind_prompt)
                        _forms_kind_input = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _forms_kind_graph.ainvoke(
                                {"messages": _forms_kind_input}, config=_graph_config
                            )
                        log.info("agent_run.forms_request_kind_retry_ok")
                        _recovered_via_retry = True
                    except Exception as _forms_kind_retry_exc:
                        log.warning(
                            "agent_run.forms_request_kind_retry_failed",
                            error=str(_forms_kind_retry_exc)[:300],
                        )
                        _reply = _build_google_mcp_validation_reply(err_str)
                        run_record.status = "completed"
                        run_record.completed_at = datetime.now(timezone.utc)
                        _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                        await db.flush()
                        await _cleanup_sandboxes()
                        return AgentRunResult(
                            reply=_reply,
                            steps=[],
                            run_id=run_id,
                            tokens_used=_agent_logger.total_tokens_from_callbacks,
                            usage=_usage_summary(),
                        )

                if _recovered_via_retry:
                    log.info("agent_run.retry_recovered_continue")
                elif (
                    "validation error for call[batch_update_presentation]" in err_str.lower()
                    or ("missing required argument" in err_str.lower() and "requests" in err_str.lower())
                    or (
                        "error calling tool 'batch_update_presentation'" in err_str.lower()
                        and "invalid slides batch update request" in err_str.lower()
                    )
                    or (
                        "error calling tool 'create_form'" in err_str.lower()
                        and "only info.title can be set when creating a form" in err_str.lower()
                    )
                    or (
                        "error calling tool 'batch_update_form'" in err_str.lower()
                        and "request kind was not provided" in err_str.lower()
                    )
                ):
                    _reply = _build_google_mcp_validation_reply(err_str)
                    run_record.status = "completed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await db.flush()
                    await _cleanup_sandboxes()
                    return AgentRunResult(
                        reply=_reply,
                        steps=[],
                        run_id=run_id,
                        tokens_used=_agent_logger.total_tokens_from_callbacks,
                        usage=_usage_summary(),
                    )

                if (not _recovered_via_retry) and _is_google_auth_or_scope_error(err_str):
                    if not _google_mcp_auth_url:
                        _google_mcp_auth_url = await _fetch_google_auth_link(
                            integration_url=google_mcp.integration_url,
                            api_key=settings.api_key,
                            agent_id=agent_id,
                            candidate_user_ids=google_mcp.candidate_user_ids,
                        )
                    _reply = await _build_google_mcp_auth_failure_reply(
                        llm=llm_raw,
                        user_message=execution_user_message,
                        error_text=err_str,
                        auth_url=_google_mcp_auth_url,
                    )
                    _reply = await _route_google_workspace_blocker_to_owner_if_customer(
                        reply=_reply,
                        session=session,
                        agent_model=agent_model,
                        user_message=execution_user_message,
                        error_text=err_str,
                        auth_url=_google_mcp_auth_url,
                        log=log,
                    )
                    run_record.status = "completed"
                    run_record.completed_at = datetime.now(timezone.utc)
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await db.flush()
                    await _cleanup_sandboxes()
                    return AgentRunResult(
                        reply=_reply,
                        steps=[],
                        run_id=run_id,
                        tokens_used=_agent_logger.total_tokens_from_callbacks,
                        usage=_usage_summary(),
                    )

                if not _recovered_via_retry:
                    log.error("agent_run.error", error=err_str)
                    # Update Run → failed (durable trace) + tell the user instead
                    # of failing silently with a rolled-back transaction.
                    _apply_run_usage(_agent_logger.total_tokens_from_callbacks)
                    await _persist_run_failure(
                        db,
                        run_record=run_record,
                        status="failed",
                        error_message=err_str,
                    )
                    await _cleanup_sandboxes()
                    await send_agent_recovery_message(
                        is_wa_session=_is_wa_session,
                        wa_device_id=_wa_device_id,
                        wa_target=_wa_target,
                        llm_raw=llm_raw,
                        system_prompt=system_prompt,
                        reason="mengalami kendala teknis dan terpaksa dihentikan",
                        log=log,
                    )
                    raise


        interrupt_result = await handle_graph_interrupt(
            graph_output=_graph_output,
            graph=graph,
            checkpointer=_checkpointer,
            thread_id=_thread_id,
            session=session,
            db=db,
            run_record=run_record,
            run_id=run_id,
            prior_messages=prior_messages,
            user_message=execution_user_message,
            cleanup_sandboxes=_cleanup_sandboxes,
            log=log,
        )
        if interrupt_result is not None:
            return AgentRunResult(**interrupt_result)

        # ------------------------------------------------------------------ #
        # 9. Parse & persist result messages                                  #
        # ------------------------------------------------------------------ #
        parsed: ParsedResult = parse_agent_result(
            result=result,
            input_messages=input_messages,
            session_id=session.id,
            run_id=run_id,
            step_start=step_counter,
            log=log,
        )
        final_reply = parsed["final_reply"]
        steps = parsed["steps"]
        _current_shared_artifact_path = _remember_latest_shared_artifact(
            session,
            steps,
            final_reply,
            sent=False,
        )
        # Prefer callback-based counter — it captures sub-agent LLM calls too.
        # Fall back to result_parser count if callback produced nothing (e.g. mocked LLM).
        _cb_tokens = _agent_logger.total_tokens_from_callbacks
        total_tokens_used = _cb_tokens if _cb_tokens > 0 else parsed["total_tokens_used"]
        if (
            _is_google_mcp_intent(execution_user_message)
            and mcp_tools
            and not _has_google_mcp_step(steps)
            and _has_external_service_fallback_blocked_step(steps)
        ):
            log.warning(
                "agent_run.google_mcp_retry_after_blocked_fallback",
                mcp_tools=len(mcp_tools),
            )
            try:
                from langgraph.prebuilt import create_react_agent as _cra

                _mcp_only_prompt = (
                    (system_prompt if isinstance(system_prompt, str) else "")
                    + "\n\n## Google Workspace Tool Retry\n"
                    "The previous attempt incorrectly used a delegated/sandbox fallback for a Google Workspace action. "
                    "Retry now using only the Google Workspace tools. "
                    "Do not call task, filesystem, sandbox, or non-Google tools. "
                    "Return the Google Workspace URL only after the Google tool output contains it. "
                    "Do not mention internal tool protocol terms to the user."
                )
                _mcp_only_graph = _cra(llm, tools=mcp_tools, prompt=_mcp_only_prompt)
                _mcp_retry_input = _sanitize_input_messages(input_messages)
                async with asyncio.timeout(settings.agent_timeout_seconds):
                    result = await _mcp_only_graph.ainvoke(
                        {"messages": _mcp_retry_input}, config=_graph_config
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                log.info("agent_run.google_mcp_retry_after_blocked_fallback_ok")
            except Exception as _mcp_retry_exc:
                log.warning(
                    "agent_run.google_mcp_retry_after_blocked_fallback_failed",
                    error=str(_mcp_retry_exc)[:300],
                )

        if _needs_builder_create_completion(steps, is_builder=runtime_policy.is_builder):
            log.warning(
                "agent_run.builder_create_completion_continue",
                steps=len(steps),
            )
            _create_completion_input = _sanitize_input_messages(input_messages)
            _create_completion_input.append(
                HumanMessage(content=_builder_create_completion_directive())
            )
            try:
                async with asyncio.timeout(_timeout):
                    _create_completion_output = await graph.ainvoke(
                        {"messages": _create_completion_input},
                        config=_graph_config,
                        version="v2",
                    )
                    result = await _graph_result_from_output(
                        graph=graph,
                        graph_config=_graph_config,
                        graph_output=_create_completion_output,
                        log=log,
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                log.info(
                    "agent_run.builder_create_completion_continue_ok",
                    created=any(str(s.get("tool", "")) == "create_agent" for s in steps),
                )
            except Exception as _create_completion_exc:
                log.warning(
                    "agent_run.builder_create_completion_continue_failed",
                    error=str(_create_completion_exc)[:300],
                )

        if _needs_deploy_followup(execution_user_message, tools_config, steps, final_reply):
            log.warning(
                "agent_run.deploy_followup_continue",
                has_subagents=bool(subagent_list),
                steps=len(steps),
            )
            _deploy_followup_input = _sanitize_input_messages(input_messages)
            _deploy_followup_input.append(
                HumanMessage(
                    content=_deploy_followup_message(
                        final_reply,
                        steps,
                        has_subagents=bool(subagent_list),
                    )
                )
            )
            try:
                async with asyncio.timeout(_timeout):
                    _deploy_graph_output = await graph.ainvoke(
                        {"messages": _deploy_followup_input},
                        config=_graph_config,
                        version="v2",
                    )
                    result = await _graph_result_from_output(
                        graph=graph,
                        graph_config=_graph_config,
                        graph_output=_deploy_graph_output,
                        log=log,
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                log.info(
                    "agent_run.deploy_followup_continue_ok",
                    has_url=_has_public_url_in_text(final_reply) or _has_public_url_in_steps(steps),
                )
            except Exception as _deploy_followup_exc:
                log.warning(
                    "agent_run.deploy_followup_continue_failed",
                    error=str(_deploy_followup_exc)[:300],
                )

        _needs_wa_file_followup, _wa_shared_file_path = _needs_whatsapp_file_delivery_followup(
            execution_user_message,
            tools_config,
            steps,
            final_reply,
        )
        if _needs_wa_file_followup and _wa_shared_file_path:
            log.info("agent_run.whatsapp_file_delivery_followup", path=_wa_shared_file_path)
            _sent, _delivery_reply = await _deliver_shared_whatsapp_file_via_tool(
                tools=tools,
                shared_path=_wa_shared_file_path,
                parsed=parsed,
                session_id=session.id,
                run_id=run_id,
                step_index=step_counter + len(parsed["db_messages"]),
                log=log,
            )
            final_reply = _delivery_reply
            steps = parsed["steps"]
            _remember_shared_artifact_path(session, _wa_shared_file_path, sent=_sent)
            _current_shared_artifact_path = _wa_shared_file_path
            log.info(
                "agent_run.whatsapp_file_delivery_followup_ok",
                sent=_sent,
                deterministic=True,
            )
        elif _current_shared_artifact_path:
            log.info("agent_run.shared_artifact_recorded", path=_current_shared_artifact_path)
        for _msg_record in parsed["db_messages"]:
            db.add(_msg_record)

        _needs_forms_followup, _followup_form_id = _needs_google_forms_followup(execution_user_message, steps)
        if _needs_forms_followup and _followup_form_id:
            log.info("agent_run.forms_followup_continue", form_id=_followup_form_id)
            _forms_followup_directive = google_forms_followup_directive(_followup_form_id)
            try:
                from langgraph.prebuilt import create_react_agent as _cra

                _forms_prompt = (
                    (system_prompt + "\n\n" + _forms_followup_directive)
                    if isinstance(system_prompt, str)
                    else system_prompt
                )
                _forms_graph = _cra(llm, tools=tools, prompt=_forms_prompt)
                _forms_input = _sanitize_input_messages(input_messages)
                async with asyncio.timeout(settings.agent_timeout_seconds):
                    result = await _forms_graph.ainvoke(
                        {"messages": _forms_input}, config=_graph_config
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                for _msg_record in parsed["db_messages"]:
                    db.add(_msg_record)
            except Exception as _forms_followup_exc:
                _forms_followup_err = str(_forms_followup_exc)
                log.warning("agent_run.forms_followup_continue_failed", error=_forms_followup_err[:300], form_id=_followup_form_id)
                _is_missing_requests_err = (
                    "validation error for call[batch_update_form]" in _forms_followup_err.lower()
                    and "missing required argument" in _forms_followup_err.lower()
                    and "requests" in _forms_followup_err.lower()
                )
                if _is_missing_requests_err:
                    _forms_followup_retry_directive = google_forms_followup_retry_directive()
                    try:
                        from langgraph.prebuilt import create_react_agent as _cra

                        _forms_prompt_retry = (
                            (system_prompt + "\n\n" + _forms_followup_retry_directive)
                            if isinstance(system_prompt, str)
                            else system_prompt
                        )
                        _forms_graph_retry = _cra(llm, tools=tools, prompt=_forms_prompt_retry)
                        _forms_input_retry = _sanitize_input_messages(input_messages)
                        async with asyncio.timeout(settings.agent_timeout_seconds):
                            result = await _forms_graph_retry.ainvoke(
                                {"messages": _forms_input_retry}, config=_graph_config
                            )
                        parsed = parse_agent_result(
                            result=result,
                            input_messages=input_messages,
                            session_id=session.id,
                            run_id=run_id,
                            step_start=step_counter,
                            log=log,
                        )
                        final_reply = parsed["final_reply"]
                        steps = parsed["steps"]
                        total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                        for _msg_record in parsed["db_messages"]:
                            db.add(_msg_record)
                    except Exception as _forms_followup_retry_exc:
                        log.warning(
                            "agent_run.forms_followup_retry_failed",
                            error=str(_forms_followup_retry_exc)[:300],
                            form_id=_followup_form_id,
                        )

        _needs_slides_followup, _followup_presentation_id = _needs_google_slides_followup(execution_user_message, steps)
        if _needs_slides_followup and _followup_presentation_id:
            log.info("agent_run.slides_followup_continue", presentation_id=_followup_presentation_id)
            _slides_followup_directive = google_slides_followup_directive(
                _followup_presentation_id, execution_user_message
            )
            try:
                from langgraph.prebuilt import create_react_agent as _cra

                _slides_prompt = (
                    (system_prompt + "\n\n" + _slides_followup_directive)
                    if isinstance(system_prompt, str)
                    else system_prompt
                )
                _slides_graph = _cra(llm, tools=tools, prompt=_slides_prompt)
                _slides_input = _sanitize_input_messages(input_messages)
                async with asyncio.timeout(settings.agent_timeout_seconds):
                    result = await _slides_graph.ainvoke(
                        {"messages": _slides_input}, config=_graph_config
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                for _msg_record in parsed["db_messages"]:
                    db.add(_msg_record)
            except Exception as _slides_followup_exc:
                log.warning(
                    "agent_run.slides_followup_continue_failed",
                    error=str(_slides_followup_exc)[:300],
                    presentation_id=_followup_presentation_id,
                )

        _needs_sheets_followup, _followup_spreadsheet_id = _needs_google_sheets_followup(execution_user_message, steps)
        if _needs_sheets_followup and _followup_spreadsheet_id:
            log.info("agent_run.sheets_followup_continue", spreadsheet_id=_followup_spreadsheet_id)
            _sheets_followup_directive = google_sheets_followup_directive(
                _followup_spreadsheet_id, execution_user_message
            )
            try:
                from langgraph.prebuilt import create_react_agent as _cra

                _sheets_prompt = (
                    (system_prompt + "\n\n" + _sheets_followup_directive)
                    if isinstance(system_prompt, str)
                    else system_prompt
                )
                _sheets_graph = _cra(llm, tools=tools, prompt=_sheets_prompt)
                _sheets_input = _sanitize_input_messages(input_messages)
                async with asyncio.timeout(settings.agent_timeout_seconds):
                    result = await _sheets_graph.ainvoke(
                        {"messages": _sheets_input}, config=_graph_config
                    )
                parsed = parse_agent_result(
                    result=result,
                    input_messages=input_messages,
                    session_id=session.id,
                    run_id=run_id,
                    step_start=step_counter,
                    log=log,
                )
                final_reply = parsed["final_reply"]
                steps = parsed["steps"]
                total_tokens_used = _agent_logger.total_tokens_from_callbacks or parsed["total_tokens_used"]
                for _msg_record in parsed["db_messages"]:
                    db.add(_msg_record)
            except Exception as _sheets_followup_exc:
                log.warning(
                    "agent_run.sheets_followup_continue_failed",
                    error=str(_sheets_followup_exc)[:300],
                    spreadsheet_id=_followup_spreadsheet_id,
                )

    await db.flush()

    # ------------------------------------------------------------------ #
    # 10. Long-term memory auto-extraction                                #
    # ------------------------------------------------------------------ #
    if _is_enabled(tools_config, "memory", default=True):
        user_msg_count = await count_user_messages(session.id, db)
        if user_msg_count > 0 and user_msg_count % settings.ltm_extraction_every == 0:
            log.info("agent_run.ltm_trigger", user_messages=user_msg_count)
            recent_for_ltm = await load_history(session.id, db, max_turns=settings.ltm_extraction_every)
            await extract_long_term_memory(
                agent_id=agent_id,
                recent_messages=recent_for_ltm,
                llm=llm,
                db=db,
                log=log,
                scope=_memory_scope,
            )

    # cleanup
    await _cleanup_sandboxes()

    if not final_reply:
        _empty_llm = not parsed["has_output"]
        if _empty_llm:
            log.error(
                "agent_run.no_llm_output",
                session_id=str(session.id),
                run_id=str(run_id),
                user_message=execution_user_message[:100],
            )
        else:
            log.warning(
                "agent_run.missing_final_reply",
                steps=len(steps),
                run_id=str(run_id),
            )

    _google_mcp_auth_err_before_override = (
        (mcp_errors.get("google_workspace") if isinstance(mcp_errors, dict) else None)
        or _extract_google_mcp_step_error(steps)
    )
    final_reply, steps, _google_mcp_auth_url = await apply_google_mcp_reply_overrides(
        final_reply=final_reply,
        steps=steps,
        mcp_errors=mcp_errors,
        runtime=google_mcp,
        auth_url=_google_mcp_auth_url,
        llm_raw=llm_raw,
        user_message=execution_user_message,
        agent_id=agent_id,
        api_key=settings.api_key,
        log=log,
    )
    _google_mcp_auth_err = _google_mcp_auth_err_before_override or _extract_google_mcp_step_error(steps)
    _google_mcp_has_artifact = (
        _contains_google_workspace_artifact(final_reply)
        or _has_google_workspace_artifact_step(steps)
    )
    if (
        _google_mcp_auth_err
        and _is_google_auth_or_scope_error(str(_google_mcp_auth_err))
        and not _google_mcp_has_artifact
    ):
        final_reply = await _route_google_workspace_blocker_to_owner_if_customer(
            reply=final_reply,
            session=session,
            agent_model=agent_model,
            user_message=execution_user_message,
            error_text=str(_google_mcp_auth_err),
            auth_url=_google_mcp_auth_url,
            log=log,
        )
    guarded_reply = _task_result_guard_reply(final_reply, steps, execution_user_message)
    if guarded_reply != final_reply:
        log.warning("agent_run.final_reply_overridden_by_task_guard")
        final_reply = guarded_reply
    guarded_reply = _direct_whatsapp_send_guard_reply(final_reply, steps, execution_user_message, input_messages)
    if guarded_reply != final_reply:
        log.warning("agent_run.final_reply_overridden_by_direct_wa_send_guard")
        final_reply = guarded_reply
    guarded_reply = _operator_escalation_reply_guard(final_reply, steps, execution_user_message, escalation_user_jid)
    if guarded_reply != final_reply:
        log.warning("agent_run.final_reply_overridden_by_operator_escalation_guard")
        final_reply = guarded_reply
    if not runtime_policy.is_builder:
        guarded_reply = _whatsapp_media_delivery_guard_reply(final_reply, steps)
        if guarded_reply != final_reply:
            log.warning("agent_run.final_reply_overridden_by_wa_media_delivery_guard")
            final_reply = guarded_reply

    _reply_before_non_empty_guard = final_reply
    final_reply = ensure_non_empty_reply(
        final_reply,
        steps,
        tools_config=tools_config,
        active_groups=active_groups,
    )
    if final_reply != _reply_before_non_empty_guard:
        log.warning(
            "agent_run.final_reply_overridden_by_non_empty_guard",
            before_len=len(_reply_before_non_empty_guard or ""),
            after_len=len(final_reply or ""),
            before_preview=(_reply_before_non_empty_guard or "")[:220],
            after_preview=(final_reply or "")[:220],
        )

    _reply_before_google_auth_guard = final_reply
    final_reply = await _append_builder_google_auth_link_if_needed(
        final_reply,
        steps=steps,
        session=session,
        settings_obj=settings,
        log=log,
    )
    if final_reply != _reply_before_google_auth_guard:
        log.warning(
            "agent_run.final_reply_appended_google_auth_link",
            before_len=len(_reply_before_google_auth_guard or ""),
            after_len=len(final_reply or ""),
            before_preview=(_reply_before_google_auth_guard or "")[:220],
            after_preview=(final_reply or "")[:220],
        )

    if _is_enabled(tools_config, "memory", default=True):
        _memory_artifact_path = _current_shared_artifact_path or _remember_latest_shared_artifact(
            session,
            steps,
            final_reply,
            sent=False,
        )
        await record_runtime_memory(
            agent_id=agent_id,
            db=db,
            scope=_memory_scope,
            user_message=execution_user_message,
            final_reply=final_reply,
            current_attachment_name=current_attachment_name,
            generated_artifact_path=_memory_artifact_path,
            log=log,
        )

    _completed_at = datetime.now(timezone.utc)
    _duration_ms = max(0, int((_completed_at - _now).total_seconds() * 1000))
    log.info(
        "agent_run.complete",
        steps=len(steps),
        reply_len=len(final_reply),
        duration_ms=_duration_ms,
        tokens_used=total_tokens_used,
        prompt_tokens=_agent_logger.prompt_tokens_from_callbacks,
        completion_tokens=_agent_logger.completion_tokens_from_callbacks,
        openrouter_cost_usd=round(_agent_logger.openrouter_cost_usd_from_callbacks, 8),
    )

    # Update Run → completed
    run_record.status = "completed"
    run_record.completed_at = _completed_at
    _apply_run_usage(total_tokens_used)
    await db.flush()

    return {
        "reply": final_reply,
        "steps": steps,
        "run_id": run_id,
        "tokens_used": total_tokens_used,
        "usage": _usage_summary(),
    }
