"""
Memory service: CRUD for agent long-term memory (key-value store).
Used both by API endpoints and by the memory tools injected into the agent.

Also contains extract_long_term_memory() which is called automatically
every SHORT_TERM_MEMORY_TURNS user messages to distil conversation
summaries into persistent memories.
"""
from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory import Memory

if TYPE_CHECKING:
    from langchain_openai import ChatOpenAI

logger = structlog.get_logger(__name__)


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


_LAYERED_KEYS = {"soul", "user_profile", "longterm"}


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
        if m.key not in _LAYERED_KEYS and not m.key.startswith("daily:") and not m.key.startswith("heartbeat:")
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

    Returns dict with keys: soul, user_profile, daily_today, daily_yesterday, today_date, yesterday_date.
    soul is global per agent (scope=None); others are scoped to external_user_id.
    """
    import datetime as _dt
    today = _dt.date.today().isoformat()
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()

    soul_mem = await get_memory(agent_id, "soul", db, scope=None)
    user_profile_mem = await get_memory(agent_id, "user_profile", db, scope=scope)
    daily_today_mem = await get_memory(agent_id, f"daily:{today}", db, scope=scope)
    daily_yesterday_mem = await get_memory(agent_id, f"daily:{yesterday}", db, scope=scope)

    return {
        "soul": soul_mem.value_data if soul_mem else "",
        "user_profile": user_profile_mem.value_data if user_profile_mem else "",
        "daily_today": daily_today_mem.value_data if daily_today_mem else "",
        "daily_yesterday": daily_yesterday_mem.value_data if daily_yesterday_mem else "",
        "today_date": today,
        "yesterday_date": yesterday,
    }


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

    # Build plain-text conversation
    lines: list[str] = []
    for m in recent_messages:
        if m.role in ("user", "agent") and m.content:
            speaker = "User" if m.role == "user" else "Assistant"
            lines.append(f"{speaker}: {m.content[:600]}")

    if not lines:
        return

    conv_text = "\n".join(lines)
    prompt = (
        "Analyze this conversation and extract ALL important facts worth remembering long-term. "
        "Focus on actionable context that would help an AI assistant continue work in future sessions.\n\n"
        "Extract facts from these categories (include ALL that appear in the conversation):\n"
        "- User identity: name, job, company, phone\n"
        "- CV/resume content: full_name, job_title, skills, education, work_experience (summarize concisely)\n"
        "- Deployed apps: deploy_url, project_name, tech_stack, port\n"
        "- Files/projects created: file names, purpose, workspace location\n"
        "- User preferences: language, framework, coding style, communication style\n"
        "- Important decisions or agreements made\n"
        "- Any task that was completed or is in progress\n\n"
        "Return ONLY a compact JSON object — keys are short snake_case labels, values are concise strings. "
        "If a value is long (e.g. CV content), summarize to max 300 chars.\n\n"
        "Example:\n"
        '{"user_name": "Bagas", "cv_skills": "Python, FastAPI, Docker, LangChain", '
        '"cv_education": "S1 Informatika Univ X 2020", "deploy_url": "https://abc.trycloudflare.com", '
        '"project_name": "portfolio website", "preferred_language": "Indonesian"}\n\n'
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
                safe_key = f"auto_{key[:80]}"
                await upsert_memory(agent_id, safe_key, str(value)[:1000], db, scope=scope)
                saved += 1

        log.info("ltm.extraction.complete", facts_saved=saved)

    except Exception as exc:
        log.warning("ltm.extraction.failed", error=str(exc))
