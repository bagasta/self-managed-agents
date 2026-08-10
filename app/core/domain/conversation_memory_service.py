"""
conversation_memory_service.py — Production-ready conversation memory service.

This module is the single source of truth for:
  - Persisting LLM-generated conversation summaries to PostgreSQL
  - Reading summaries (with Redis cache for hot paths)
  - Deciding when to regenerate summaries (staleness check)
  - Generating Markdown context for LLM injection

Architecture:
  ┌──────────────────────────────────────────────────────────────┐
  │                     LLM Agent Runner                         │
  │  (app/core/engine/agent_runner.py)                           │
  └────────────────────┬─────────────────────────────────────────┘
                       │ calls
                       ▼
  ┌──────────────────────────────────────────────────────────────┐
  │              ConversationMemoryService                        │
  │  (this module)                                               │
  │                                                              │
  │  build_llm_context() ──► loads messages + memory from DB     │
  │                          ──► checks Redis cache for summary  │
  │                          ──► calls LLM if summary is stale   │
  │                          ──► generates Markdown (in-memory)  │
  │                          ──► returns ContextPayload           │
  └──────────────────────────────────────────────────────────────┘
                       │ reads from / writes to
                       ▼
  ┌───────────────────────────────────────────────────────────────┐
  │                    PostgreSQL                                  │
  │  messages, agent_memories, conversation_summaries, sessions   │
  └───────────────────────────────────────────────────────────────┘
                       │ optional hot-path cache
                       ▼
  ┌───────────────────────────────────────────────────────────────┐
  │                      Redis                                    │
  │  mem:{agent_id}:{scope}:{key}  →  memory values              │
  │  conv_summary:{session_id}:active  →  summary text           │
  └───────────────────────────────────────────────────────────────┘

Markdown is NEVER written to disk. It is generated dynamically, injected into
the LLM prompt, then discarded after the invocation completes.

Exported functions:
  get_active_summary(session_id, db) -> ConversationSummary | None
  save_summary(session_id, summary_text, message_count, db) -> ConversationSummary
  invalidate_old_summaries(session_id, db)
  get_or_create_summary(session, db, llm, log, trigger) -> str
  build_llm_context(session, agent_id, db, llm, scope, settings, log) -> ContextPayload
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation_summary import ConversationSummary

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Repository layer — raw DB access
# ---------------------------------------------------------------------------

async def get_active_summary(
    session_id: uuid.UUID,
    db: AsyncSession,
) -> ConversationSummary | None:
    """
    Fetch the single active summary for a session.

    Uses the ix_conv_summaries_session_active composite index for O(log n) lookup.
    Returns None if the session has no summary yet (short conversations).
    """
    result = await db.execute(
        select(ConversationSummary)
        .where(
            ConversationSummary.session_id == session_id,
            ConversationSummary.is_active.is_(True),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def invalidate_old_summaries(
    session_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    """
    Mark all existing active summaries for a session as inactive.

    Called before inserting a new summary to enforce the one-active-per-session
    invariant at the application level (the partial unique index enforces it at
    the DB level as a safety net).
    """
    await db.execute(
        update(ConversationSummary)
        .where(
            ConversationSummary.session_id == session_id,
            ConversationSummary.is_active.is_(True),
        )
        .values(is_active=False)
    )
    await db.flush()


async def save_summary(
    session_id: uuid.UUID,
    summary_text: str,
    message_count: int,
    db: AsyncSession,
) -> ConversationSummary:
    """
    Persist a new summary and deactivate all previous ones.

    This is an atomic operation from the caller's perspective:
      1. Old summaries marked inactive.
      2. New summary inserted with is_active=True.
      3. DB flushed (not committed — caller controls the transaction).

    Redis cache is invalidated asynchronously after write.
    """
    # Deactivate old summaries first
    await invalidate_old_summaries(session_id, db)

    # Estimate tokens (1 token ≈ 4 chars heuristic)
    token_estimate = max(1, len(summary_text) // 4)

    summary = ConversationSummary(
        id=uuid.uuid4(),
        session_id=session_id,
        summary_text=summary_text,
        message_count_at=message_count,
        token_estimate=token_estimate,
        is_active=True,
    )
    db.add(summary)
    await db.flush()

    # Invalidate Redis cache so next read gets the fresh value
    try:
        from app.core.infra.memory_cache import invalidate_conversation_summary_cache
        await invalidate_conversation_summary_cache(session_id)
    except Exception as exc:
        logger.debug("conv_memory.cache_invalidate_failed", session_id=str(session_id), error=str(exc))

    return summary


# ---------------------------------------------------------------------------
# Service layer — business logic
# ---------------------------------------------------------------------------

async def get_or_create_summary(
    session: Any,
    db: AsyncSession,
    llm: Any,
    log: Any,
    *,
    trigger: int = 10,
) -> str:
    """
    Return the active conversation summary, generating a new one if stale.

    This replaces `maybe_summarize_context()` in prompt_builder.py. The key
    improvements over the original:
      - Summary is stored in a dedicated table (not buried in session.metadata_)
      - Redis cache prevents DB hits on every single run
      - Staleness check is explicit (message_count_at vs current count)
      - Cache is properly invalidated on write

    Args:
        session:  Session ORM object.
        db:       Async DB session.
        llm:      LangChain LLM for summary generation.
        log:      Structlog logger bound to the current run.
        trigger:  Regenerate summary every N user messages.

    Returns:
        Summary text (Markdown) or empty string if session is short.
    """
    session_id = session.id

    try:
        from app.core.engine.context_service import count_user_messages, load_history

        user_msg_count = await count_user_messages(session_id, db)
        if user_msg_count < trigger:
            return ""

        # Try Redis cache first (hot path: no DB query needed)
        try:
            from app.core.infra.memory_cache import get_conversation_summary_cached
            cached_text = await get_conversation_summary_cached(session_id, db)
            if cached_text:
                # Check freshness: if current count is within trigger of when it was cached,
                # return the cached version without a DB round-trip.
                # (The cache itself is invalidated after each regeneration.)
                log.debug("conv_memory.summary_cache_hit", session_id=str(session_id))
                return cached_text
        except Exception as cache_exc:
            log.debug("conv_memory.cache_read_error", error=str(cache_exc))

        # DB read: check if existing active summary is still fresh enough
        active_summary = await get_active_summary(session_id, db)
        if active_summary:
            messages_since = user_msg_count - active_summary.message_count_at
            if messages_since < trigger:
                log.debug(
                    "conv_memory.summary_fresh",
                    session_id=str(session_id),
                    messages_since=messages_since,
                )
                return active_summary.summary_text

        # Generate a new summary via LLM
        log.info(
            "conv_memory.summary_generating",
            session_id=str(session_id),
            user_msg_count=user_msg_count,
        )

        # Load ALL messages for summarization (bypass the default turn cap).
        # The cap exists for normal context injection; summarization needs the full picture.
        all_rows = await load_history(session_id, db, max_turns=None)
        if not all_rows:
            return ""

        history_text = "\n".join(
            f"{'User' if m.role == 'user' else 'Agent'}: {(m.content or '')[:500]}"
            for m in all_rows
            if m.role in ("user", "agent") and m.content
        )

        from langchain_core.messages import HumanMessage as _HM
        summary_prompt = (
            "Berikut adalah riwayat percakapan antara user dan agent. "
            "Buat ringkasan padat (maksimal 300 kata) yang mencakup:\n"
            "- Topik utama yang dibahas\n"
            "- Keputusan atau hasil penting yang sudah dicapai\n"
            "- Konteks yang relevan untuk melanjutkan percakapan\n\n"
            f"Riwayat percakapan:\n{history_text[:6000]}"
        )
        resp = await llm.ainvoke([_HM(content=summary_prompt)])
        summary_text = resp.content if isinstance(resp.content, str) else str(resp.content)

        # Persist to PostgreSQL (replaces writing to session.metadata_ JSONB)
        await save_summary(session_id, summary_text, user_msg_count, db)

        log.info(
            "conv_memory.summary_saved",
            session_id=str(session_id),
            user_msg_count=user_msg_count,
            summary_len=len(summary_text),
        )
        return summary_text

    except Exception as exc:
        log.warning("conv_memory.summary_failed", error=str(exc))
        return ""


# ---------------------------------------------------------------------------
# Context payload — structured return value for the agent runner
# ---------------------------------------------------------------------------

@dataclass
class ContextPayload:
    """
    All context data needed to build the LLM system prompt.

    Returned by build_llm_context() to the agent runner. The agent runner
    passes this to prompt_builder.build_system_prompt() instead of calling
    the individual context functions separately.

    Fields:
        summary_text:     LLM-generated conversation summary (from DB).
        history_markdown: Recent conversation history as Markdown (ephemeral).
        memory_markdown:  Layered memory as Markdown (ephemeral).
        prior_messages:   LangChain BaseMessage list for chat history injection.
        context_summary:  Alias for summary_text (backward compat with prompt_builder).
    """
    summary_text: str = ""
    history_markdown: str = ""
    memory_markdown: str = ""
    prior_messages: list = field(default_factory=list)

    @property
    def context_summary(self) -> str:
        """Backward-compatible alias used by prompt_builder.build_system_prompt()."""
        return self.summary_text


async def build_llm_context(
    session: Any,
    agent_id: uuid.UUID,
    db: AsyncSession,
    llm: Any,
    scope: str | None,
    settings: Any,
    log: Any,
) -> ContextPayload:
    """
    Load all context data from PostgreSQL and format it for LLM injection.

    This is the main entry point called once per agent run. It replaces the
    scattered context-loading calls in agent_runner.py with a single, clean call.

    Data flow:
        PostgreSQL
          ├── conversation_summaries  → summary_text (via Redis cache)
          ├── messages                → prior_messages (LangChain) + history_markdown
          └── agent_memories          → memory_markdown (via Redis cache)
                       │
                       ▼
                  ContextPayload (in-memory, ephemeral)
                       │
                       ▼
              LLM System Prompt (injected, then discarded)

    Args:
        session:   Session ORM object.
        agent_id:  UUID of the agent.
        db:        Async DB session.
        llm:       LangChain LLM for summary generation (if stale).
        scope:     Memory scope (phone number or None for global).
        settings:  App Settings object.
        log:       Structlog logger.

    Returns:
        ContextPayload with all context data populated.
    """
    from app.core.domain.markdown_generator import (
        generate_conversation_markdown,
        generate_memory_markdown,
    )
    from app.core.domain.memory_service import build_memory_context, load_layered_memory
    from app.core.engine.context_service import db_messages_to_lc, load_history

    trigger = getattr(settings, "context_summary_trigger", 10)

    # Run summary generation and memory loading concurrently
    import asyncio
    summary_task = asyncio.create_task(
        get_or_create_summary(session, db, llm, log, trigger=trigger)
    )
    layered_memory_task = asyncio.create_task(
        load_layered_memory(agent_id, db, scope=scope)
    )
    memory_block_task = asyncio.create_task(
        build_memory_context(agent_id, db, scope=scope)
    )

    summary_text, layered_memory, memory_block = await asyncio.gather(
        summary_task, layered_memory_task, memory_block_task
    )

    # Determine history depth: if a summary covers older context, we need fewer turns
    _history_turns = (
        max(settings.short_term_memory_turns // 2, 5)
        if summary_text
        else settings.short_term_memory_turns
    )
    history_rows = await load_history(session.id, db, max_turns=_history_turns)
    prior_messages = db_messages_to_lc(history_rows)

    # Generate ephemeral Markdown — never written to disk
    history_markdown = generate_conversation_markdown(history_rows, max_chars=10_000)
    memory_markdown = generate_memory_markdown(layered_memory, memory_block, max_chars=6_000)

    return ContextPayload(
        summary_text=summary_text,
        history_markdown=history_markdown,
        memory_markdown=memory_markdown,
        prior_messages=prior_messages,
    )
