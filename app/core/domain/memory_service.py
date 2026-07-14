"""
Memory service: CRUD for agent long-term memory (key-value store).
Used both by API endpoints and by the memory tools injected into the agent.

Also contains extract_long_term_memory() which is called automatically
every SHORT_TERM_MEMORY_TURNS user messages to distil conversation
summaries into persistent memories.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory import Memory
from app.models.message import Message

if TYPE_CHECKING:
    from langchain_openai import ChatOpenAI

logger = structlog.get_logger(__name__)

_WIB = timezone(timedelta(hours=7), name="WIB")

_BUSINESS_DOMAIN_MARKERS = (
    "customer service",
    " cs ",
    "ecommerce",
    "e-commerce",
    "toko",
    "shop",
    "order",
    "pesanan",
    "produk",
    "pelanggan",
    "mukena",
)

_PERSONAL_PROFILE_MARKERS = (
    "cv",
    "resume",
    "portfolio",
    "profil personal",
    "personal profile",
    "job_title",
    "work_experience",
)

_TOOL_FAILURE_MARKERS = (
    "[error]",
    "[tool_error]",
    "error calling tool",
    "api error in ",
    "wrong_google_service",
    "media_source_unavailable",
)

_EXTERNAL_STATUS_CLAIM_MARKERS = (
    "sudah",
    "berhasil",
    "selesai",
    "gagal",
    "tidak dapat",
    "tidak bisa",
    "memerlukan",
    "diperlukan",
    "aktifkan",
    "api",
    "console.cloud.google.com",
)


def _tool_result_indicates_failure(result: Any) -> bool:
    lowered = str(result or "").lower()
    return any(marker in lowered for marker in _TOOL_FAILURE_MARKERS)


def _contains_status_claim(text: str) -> bool:
    return any(
        re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", text)
        for marker in _EXTERNAL_STATUS_CLAIM_MARKERS
    )


def _step_is_verified_external_success(step: dict[str, Any], service_context: str) -> bool:
    tool_name = str((step or {}).get("tool", "") or "").lower()
    result = str((step or {}).get("result", "") or "")
    if not tool_name or not result or _tool_result_indicates_failure(result):
        return False
    if service_context == "sheets":
        return (
            "sheet" in tool_name
            or "spreadsheet" in tool_name
            or tool_name in {"resize_sheet_dimensions", "move_sheet_rows"}
        )
    return True


def _is_business_agent_context(agent_text: str) -> bool:
    text = f" {agent_text.lower()} "
    return any(marker in text for marker in _BUSINESS_DOMAIN_MARKERS)


def _conversation_mentions_personal_profile(conv_text: str) -> bool:
    text = conv_text.lower()
    return any(marker in text for marker in _PERSONAL_PROFILE_MARKERS)


def _is_personal_profile_memory_key(key: str) -> bool:
    clean = key.lower()
    return (
        clean.startswith("cv_")
        or clean.startswith("resume_")
        or "cv_" in clean
        or "resume" in clean
        or "portfolio" in clean
    )


def memory_today() -> str:
    """Return the memory-layer local date used for daily:* keys."""
    return datetime.now(_WIB).date().isoformat()


def memory_yesterday() -> str:
    """Return yesterday relative to the memory-layer local date."""
    today = datetime.now(_WIB).date()
    return (today - timedelta(days=1)).isoformat()


async def upsert_memory(
    agent_id: uuid.UUID,
    key: str,
    value: str,
    db: AsyncSession,
    scope: str | None = None,
) -> Memory:
    """Insert or update a memory entry scoped to agent + optional phone number."""
    # When scope is NULL, PostgreSQL unique constraints treat NULL != NULL so
    # ON CONFLICT never fires. Handle manually via SELECT + UPDATE or INSERT.
    if scope is None:
        result = await db.execute(
            select(Memory).where(
                Memory.agent_id == agent_id,
                Memory.key == key,
                Memory.scope.is_(None),
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.value_data = value
            await db.flush()
            return existing
        row = Memory(id=uuid.uuid4(), agent_id=agent_id, scope=None, key=key, value_data=value)
        db.add(row)
        await db.flush()
        return row

    stmt = (
        pg_insert(Memory)
        .values(
            id=uuid.uuid4(),
            agent_id=agent_id,
            scope=scope,
            key=key,
            value_data=value,
        )
        .on_conflict_do_update(
            constraint="uq_agent_memory_scope_key",
            set_={"value_data": value},
        )
        .returning(Memory)
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.scalar_one()


async def get_memory(
    agent_id: uuid.UUID,
    key: str,
    db: AsyncSession,
    scope: str | None = None,
) -> Memory | None:
    stmt = select(Memory).where(
        Memory.agent_id == agent_id,
        Memory.scope == scope,
        Memory.key == key,
    )
    return (await db.execute(stmt)).scalars().first()


async def list_memories(
    agent_id: uuid.UUID,
    db: AsyncSession,
    scope: str | None = None,
) -> list[Memory]:
    stmt = (
        select(Memory)
        .where(Memory.agent_id == agent_id, Memory.scope == scope)
        .order_by(Memory.key)
    )
    return list((await db.execute(stmt)).scalars().all())


async def delete_memory(
    agent_id: uuid.UUID,
    key: str,
    db: AsyncSession,
    scope: str | None = None,
) -> bool:
    stmt = delete(Memory).where(
        Memory.agent_id == agent_id,
        Memory.scope == scope,
        Memory.key == key,
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount > 0


_LAYERED_KEYS = {
    "soul",
    "user_profile",
    "longterm",
    "agent_context_version",
    "active_context",
    "last_turn",
    "last_attachment",
    "last_generated_artifact",
}


def _parse_active_context_version(value: str | None) -> int | None:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


async def get_active_context_version(
    agent_id: uuid.UUID,
    db: AsyncSession,
) -> int | None:
    version_mem = await get_memory(agent_id, "agent_context_version", db, scope=None)
    return _parse_active_context_version(version_mem.value_data if version_mem else None)


async def get_versioned_memory(
    agent_id: uuid.UUID,
    base_key: str,
    db: AsyncSession,
    *,
    active_version: int | None = None,
    scope: str | None = None,
) -> Memory | None:
    """Prefer base_key:vN when an active context version exists; fallback to base_key."""
    if active_version is None and scope is None:
        active_version = await get_active_context_version(agent_id, db)
    if active_version:
        versioned = await get_memory(agent_id, f"{base_key}:v{active_version}", db, scope=scope)
        if versioned:
            return versioned
    return await get_memory(agent_id, base_key, db, scope=scope)


async def build_memory_context(
    agent_id: uuid.UUID,
    db: AsyncSession,
    scope: str | None = None,
) -> str:
    """Return a compact markdown block of scoped memories to inject into system prompt.

    Layered-memory keys (soul, user_profile, daily:*, longterm) are excluded here —
    they are rendered separately via load_layered_memory() in agent_runner.
    """
    memories = await list_memories(agent_id, db, scope=scope)
    filtered = [
        m for m in memories
        if (
            m.key not in _LAYERED_KEYS
            and not m.key.startswith("daily:")
            and not m.key.startswith("heartbeat:")
            and not re.match(r"^(soul|agent_blueprint|setup_summary):v\d+$", m.key)
        )
    ]
    if not filtered:
        return ""
    lines = ["## Long-Term Memory", ""]
    for m in filtered:
        lines.append(f"- **{m.key}**: {m.value_data}")
    return "\n".join(lines)


async def load_layered_memory(
    agent_id: uuid.UUID,
    db: AsyncSession,
    scope: str | None = None,
) -> dict[str, str]:
    """Load OpenClaw-style memory layers for system prompt injection.

    soul is global per agent (scope=None); user/runtime layers are scoped to
    external_user_id. Runtime layers are intentionally loaded separately from
    generic memories so the prompt can make "latest context wins" explicit.
    """
    today = memory_today()
    yesterday = memory_yesterday()

    active_version = await get_active_context_version(agent_id, db)
    soul_mem = await get_versioned_memory(agent_id, "soul", db, active_version=active_version, scope=None)
    user_profile_mem = await get_memory(agent_id, "user_profile", db, scope=scope)
    longterm_mem = await get_memory(agent_id, "longterm", db, scope=scope)
    active_context_mem = await get_memory(agent_id, "active_context", db, scope=scope)
    last_turn_mem = await get_memory(agent_id, "last_turn", db, scope=scope)
    last_attachment_mem = await get_memory(agent_id, "last_attachment", db, scope=scope)
    last_generated_artifact_mem = await get_memory(agent_id, "last_generated_artifact", db, scope=scope)
    daily_today_mem = await get_memory(agent_id, f"daily:{today}", db, scope=scope)
    daily_yesterday_mem = await get_memory(agent_id, f"daily:{yesterday}", db, scope=scope)

    return {
        "soul": soul_mem.value_data if soul_mem else "",
        "agent_context_version": str(active_version or ""),
        "user_profile": user_profile_mem.value_data if user_profile_mem else "",
        "longterm": longterm_mem.value_data if longterm_mem else "",
        "active_context": active_context_mem.value_data if active_context_mem else "",
        "last_turn": last_turn_mem.value_data if last_turn_mem else "",
        "last_attachment": last_attachment_mem.value_data if last_attachment_mem else "",
        "last_generated_artifact": last_generated_artifact_mem.value_data if last_generated_artifact_mem else "",
        "daily_today": daily_today_mem.value_data if daily_today_mem else "",
        "daily_yesterday": daily_yesterday_mem.value_data if daily_yesterday_mem else "",
        "today_date": today,
        "yesterday_date": yesterday,
    }


def _compact_memory_text(value: str | None, *, max_chars: int = 420) -> str:
    """Compact a chat payload before persisting it into durable memory."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(
        r"(?is)(Isi dokumen:\s*)```.*?```",
        r"\1[konten dokumen dipangkas; gunakan file terbaru yang disebut user]",
        text,
    )
    text = re.sub(
        r"(?is)```.*?```",
        "[blok panjang dipangkas]",
        text,
    )
    text = re.sub(
        r"data:[^;\s]+;base64,[A-Za-z0-9+/=\s]+",
        "[base64 media dipangkas]",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _append_memory_line(
    existing: str | None,
    line: str,
    *,
    max_lines: int = 80,
    max_chars: int = 6000,
) -> str:
    clean_line = line.strip()
    lines = [item.rstrip() for item in str(existing or "").splitlines() if item.strip()]
    if clean_line and (not lines or lines[-1] != clean_line):
        lines.append(clean_line)
    lines = lines[-max_lines:]
    value = "\n".join(lines)
    if len(value) <= max_chars:
        return value
    return value[-max_chars:].lstrip()


async def record_runtime_memory(
    *,
    agent_id: uuid.UUID,
    db: AsyncSession,
    scope: str | None,
    user_message: str,
    final_reply: str,
    current_attachment_name: str | None = None,
    generated_artifact_path: str | None = None,
    tool_steps: list[dict[str, Any]] | None = None,
    external_service_context: str | None = None,
    log: Any = None,
) -> None:
    """Persist deterministic scoped memory after a completed run.

    This does not replace LLM-curated memory tools. It guarantees every
    completed turn has a fresh active_context/daily anchor even when the model
    forgot to call update_daily/update_longterm itself.
    """
    if log is None:
        log = logger

    if str(user_message or "").lstrip().startswith("[HEARTBEAT]"):
        return

    try:
        today = memory_today()
        user_summary = _compact_memory_text(user_message, max_chars=360)
        tool_steps = tool_steps or []
        tool_failed = any(
            _tool_result_indicates_failure((step or {}).get("result"))
            for step in tool_steps
        )
        verified_external_success = bool(
            external_service_context
            and any(
                _step_is_verified_external_success(step, external_service_context)
                for step in tool_steps
            )
        )
        reply_has_external_status_claim = _contains_status_claim(
            str(final_reply or "").lower()
        )
        durable_reply = not tool_failed and not (
            external_service_context
            and reply_has_external_status_claim
            and not verified_external_success
        )
        reply_summary = (
            _compact_memory_text(final_reply, max_chars=360)
            if durable_reply
            else ""
        )
        safe_status = ""
        if tool_failed:
            safe_status = "Status: aksi tool gagal dan belum selesai; jangan anggap jawaban agent sebagai fakta."
        elif not durable_reply:
            safe_status = "Status: klaim aksi eksternal belum terverifikasi oleh hasil tool."
        attachment = _compact_memory_text(current_attachment_name, max_chars=180)
        artifact = _compact_memory_text(generated_artifact_path, max_chars=220)

        daily_parts = [f"- {today}: User: {user_summary or '(kosong)'}"]
        if attachment:
            daily_parts.append(f"Lampiran terbaru: {attachment}")
        if artifact:
            daily_parts.append(f"Artifact: {artifact}")
        if reply_summary:
            daily_parts.append(f"Agent: {reply_summary}")
        if safe_status:
            daily_parts.append(safe_status)
        daily_line = " | ".join(daily_parts)

        daily_key = f"daily:{today}"
        daily_existing = await get_memory(agent_id, daily_key, db, scope=scope)
        await upsert_memory(
            agent_id,
            daily_key,
            _append_memory_line(
                daily_existing.value_data if daily_existing else "",
                daily_line,
                max_lines=120,
                max_chars=9000,
            ),
            db,
            scope=scope,
        )

        active_lines = [
            f"Tanggal: {today}",
            f"Pesan terbaru user: {user_summary or '(kosong)'}",
        ]
        if attachment:
            active_lines.append(f"Lampiran terbaru: {attachment}")
        if artifact:
            active_lines.append(f"Artifact terakhir: {artifact}")
        if reply_summary:
            active_lines.append(f"Jawaban final agent: {reply_summary}")
        if safe_status:
            active_lines.append(safe_status)
        active_lines.append(
            "Prioritas: konteks runtime ini adalah konteks terbaru dan mengalahkan history, daily, atau longterm lama jika bertentangan."
        )
        await upsert_memory(
            agent_id,
            "active_context",
            "\n".join(active_lines),
            db,
            scope=scope,
        )

        await upsert_memory(
            agent_id,
            "last_turn",
            f"User: {user_summary or '(kosong)'}\nAgent: {reply_summary or safe_status or '(kosong)'}",
            db,
            scope=scope,
        )
        if attachment:
            await upsert_memory(agent_id, "last_attachment", attachment, db, scope=scope)
        if artifact:
            await upsert_memory(agent_id, "last_generated_artifact", artifact, db, scope=scope)

        longterm_existing = await get_memory(agent_id, "longterm", db, scope=scope)
        longterm_parts = [f"- {today}: Latest completed user turn: {user_summary or '(kosong)'}"]
        if attachment:
            longterm_parts.append(f"latest attachment={attachment}")
        if artifact:
            longterm_parts.append(f"latest artifact={artifact}")
        if reply_summary:
            longterm_parts.append(f"result={reply_summary}")
        if safe_status:
            longterm_parts.append(safe_status)
        await upsert_memory(
            agent_id,
            "longterm",
            _append_memory_line(
                longterm_existing.value_data if longterm_existing else "",
                " | ".join(longterm_parts),
                max_lines=80,
                max_chars=8000,
            ),
            db,
            scope=scope,
        )
    except Exception as exc:
        log.warning("memory.runtime_record_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Automatic long-term memory extraction
# ---------------------------------------------------------------------------

async def extract_long_term_memory(
    *,
    agent_id: uuid.UUID,
    recent_messages: list,           # list of app.models.message.Message ORM rows
    llm: "ChatOpenAI",
    db: AsyncSession,
    log: Any = None,
    scope: str | None = None,
) -> None:
    """
    Call the LLM to extract important facts from recent_messages and persist
    them to agent_memories with an 'auto_' prefix on the key.

    Called automatically every N user messages from agent_runner.
    Failures are caught and logged — never raised to the caller.
    """
    if log is None:
        log = logger

    # Assistant prose is not evidence by itself. Only assistant rows backed by
    # a successful tool result may contribute operational facts.
    run_ids = {
        m.run_id
        for m in recent_messages
        if getattr(m, "run_id", None)
    }
    successful_tool_runs: set[uuid.UUID] = set()
    failed_tool_runs: set[uuid.UUID] = set()
    tool_evidence_candidates: list[tuple[uuid.UUID, str]] = []
    if run_ids:
        try:
            tool_rows = (
                await db.execute(
                    select(Message.run_id, Message.tool_name, Message.tool_result).where(
                        Message.run_id.in_(run_ids),
                        Message.role == "tool",
                    )
                )
            ).all()
            for run_id, tool_name, tool_result in tool_rows:
                if not run_id:
                    continue
                if _tool_result_indicates_failure(tool_result):
                    failed_tool_runs.add(run_id)
                    continue
                if str(tool_result or "").strip():
                    successful_tool_runs.add(run_id)
                    tool_evidence_candidates.append(
                        (
                            run_id,
                            f"Verified tool success ({tool_name or 'tool'}): "
                            f"{_compact_memory_text(tool_result, max_chars=360)}",
                        )
                    )
            successful_tool_runs -= failed_tool_runs
        except Exception as exc:
            log.warning("ltm.tool_provenance_load_failed", error=str(exc))

    # Build plain-text conversation from user facts plus verified outcomes.
    lines: list[str] = []
    for m in recent_messages:
        if m.role == "user" and m.content:
            lines.append(f"User: {m.content[:600]}")
        elif (
            m.role == "agent"
            and m.content
            and getattr(m, "run_id", None) in successful_tool_runs
        ):
            lines.append(f"Assistant (tool-verified run): {m.content[:600]}")
    lines.extend(
        evidence
        for run_id, evidence in tool_evidence_candidates[:20]
        if run_id in successful_tool_runs
    )

    if not lines:
        return

    conv_text = "\n".join(lines)
    agent_text = ""
    try:
        from app.models.agent import Agent

        agent = await db.get(Agent, agent_id)
        if agent:
            agent_text = " ".join(
                str(part or "")
                for part in (
                    getattr(agent, "name", ""),
                    getattr(agent, "description", ""),
                    getattr(agent, "instructions", ""),
                )
            )
    except Exception as exc:
        log.warning("ltm.agent_context_load_failed", error=str(exc))

    business_agent_context = _is_business_agent_context(agent_text)
    profile_context_allowed = (
        _conversation_mentions_personal_profile(conv_text)
        and not business_agent_context
    )
    profile_line = (
        "- Personal profile/CV/resume content: full_name, job_title, skills, education, work_experience (only when the current agent is explicitly a personal/profile/career assistant)\n"
        if profile_context_allowed
        else ""
    )
    prompt = (
        "Analyze this conversation and extract ALL important facts worth remembering long-term. "
        "Focus on actionable context that would help an AI assistant continue work in future sessions.\n\n"
        "Assistant statements are not facts unless explicitly labeled as a tool-verified run. "
        "Never store tool failures, API activation requirements, error links, blockers, or unverified completion claims.\n\n"
        "Extract facts from these categories (include ALL that appear in the conversation):\n"
        "- User identity: name, job, company, phone\n"
        f"{profile_line}"
        "- Deployed apps: deploy_url, project_name, tech_stack, port\n"
        "- Files/projects created: file names, purpose, workspace location\n"
        "- User preferences: language, framework, coding style, communication style\n"
        "- For customer-service/ecommerce agents: customer name, product preference, order status, complaint, escalation status, and next action\n"
        "- Important decisions or agreements made\n"
        "- Any task that was completed or is in progress\n\n"
        "Return ONLY a compact JSON object — keys are short snake_case labels, values are concise strings. "
        "If a value is long, summarize to max 300 chars. "
        "Do not create CV/resume/portfolio keys unless the current task and agent domain are explicitly about personal profile/career content.\n\n"
        "Example:\n"
        '{"user_name": "Bagas", "customer_preference": "mukena warna pink", '
        '"order_status": "menunggu pembayaran", "deploy_url": "https://abc.trycloudflare.com", '
        '"project_name": "landing page", "preferred_language": "Indonesian"}\n\n'
        f"Conversation:\n{conv_text}\n\nJSON:"
    )

    try:
        from langchain_core.messages import HumanMessage
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        raw = response.content.strip()

        # Strip optional markdown code fence
        if "```" in raw:
            parts = raw.split("```")
            raw = parts[1] if len(parts) >= 2 else raw
            if raw.startswith("json"):
                raw = raw[4:]

        # Extract just the JSON object
        start, end = raw.find("{"), raw.rfind("}") + 1
        if start < 0 or end <= start:
            raise ValueError("No JSON object found in LLM response")
        facts: dict = json.loads(raw[start:end])

        saved = 0
        for key, value in facts.items():
            if isinstance(key, str) and value is not None:
                if _is_personal_profile_memory_key(key) and not profile_context_allowed:
                    log.info("ltm.skip_profile_memory_key", key=key, business_agent=business_agent_context)
                    continue
                safe_key = f"auto_{key[:80]}"
                await upsert_memory(agent_id, safe_key, str(value)[:1000], db, scope=scope)
                saved += 1

        log.info("ltm.extraction.complete", facts_saved=saved)

    except Exception as exc:
        log.warning("ltm.extraction.failed", error=str(exc))
