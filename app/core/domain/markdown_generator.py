"""
markdown_generator.py — Stateless service that converts PostgreSQL records to
Markdown strings for LLM context injection.

Design principles:
  - Purely functional: no side effects, no filesystem I/O, no DB calls.
  - Generated Markdown is ephemeral: it exists only in memory, injected into the
    LLM prompt, then discarded. Nothing is written to disk.
  - Token-aware: all generators accept a max_chars guard to prevent context overflow.

Exported functions:
  generate_conversation_markdown(messages, max_chars) -> str
  generate_memory_markdown(layered_memory, memory_block, max_chars) -> str
  generate_summary_markdown(summary_text) -> str
  generate_full_context_markdown(summary, messages, layered_memory, memory_block) -> str
  estimate_tokens(text) -> int
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.message import Message

# Rough token estimation heuristic.
# Accurate enough for context window budgeting; not a replacement for tiktoken.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate token count from character length (heuristic: 1 token ≈ 4 chars)."""
    return max(0, len(text) // _CHARS_PER_TOKEN)


def generate_summary_markdown(summary_text: str | None) -> str:
    """
    Wrap an LLM-generated session summary in a Markdown block for system prompt injection.

    Returns empty string if no summary is available (short sessions).
    """
    if not summary_text or not summary_text.strip():
        return ""
    return (
        "## Conversation Summary\n\n"
        "_The following is a distilled summary of earlier conversation turns. "
        "It may be referenced to understand context from older messages that are "
        "no longer in the active context window._\n\n"
        f"{summary_text.strip()}\n"
    )


def generate_conversation_markdown(
    messages: list[Message],
    max_chars: int = 12_000,
    include_tool_calls: bool = False,
) -> str:
    """
    Convert a list of Message ORM rows to Markdown conversation history.

    This Markdown is generated dynamically from PostgreSQL records and is NEVER
    written to disk. It exists only for the duration of a single LLM invocation.

    Args:
        messages: Message ORM rows (user/agent/tool roles).
        max_chars: Hard cap on output length to stay within context window.
        include_tool_calls: If True, include tool call steps in the Markdown.
                            Default False — tool steps add noise without insight.

    Returns:
        A Markdown string suitable for injection into an LLM system prompt.
        Returns empty string if messages list is empty.

    Flow:
        PostgreSQL (messages table)
          → load_history() in context_service.py
          → generate_conversation_markdown()        ← you are here
          → inject into LLM system prompt
          → discard (string goes out of scope)
    """
    if not messages:
        return ""

    lines: list[str] = ["## Recent Conversation History\n"]
    total_chars = len(lines[0])

    for msg in messages:
        if msg.role == "user" and msg.content:
            block = f"**User**: {msg.content.strip()}\n\n"
        elif msg.role == "agent" and msg.content:
            block = f"**Agent**: {msg.content.strip()}\n\n"
        elif msg.role == "tool" and include_tool_calls:
            tool_name = msg.tool_name or "unknown_tool"
            result = (msg.tool_result or "").strip()
            if result:
                block = f"**Tool `{tool_name}`**: {result[:300]}\n\n"
            else:
                continue
        else:
            continue

        if total_chars + len(block) > max_chars:
            lines.append("_[earlier messages omitted — context window limit]_\n")
            break

        lines.append(block)
        total_chars += len(block)

    return "".join(lines).rstrip()


def generate_memory_markdown(
    layered_memory: dict[str, str],
    memory_block: str = "",
    max_chars: int = 8_000,
) -> str:
    """
    Convert layered memory dict + generic memory block to Markdown for LLM context.

    The layered_memory dict comes from load_layered_memory() in memory_service.py.
    The memory_block comes from build_memory_context() in memory_service.py.

    This function just formats them — no DB calls, no filesystem I/O.

    Args:
        layered_memory: Dict with keys: soul, user_profile, longterm, active_context,
                        last_turn, last_attachment, last_generated_artifact,
                        daily_today, daily_yesterday, today_date, yesterday_date.
        memory_block: Pre-formatted Markdown of generic memories (from build_memory_context).
        max_chars: Hard cap on output to avoid context overflow.
    """
    sections: list[str] = []
    total = 0

    def _add(section: str) -> bool:
        nonlocal total
        if not section.strip():
            return True
        if total + len(section) > max_chars:
            return False
        sections.append(section)
        total += len(section)
        return True

    # Daily context (today + yesterday) — highest recency signal
    today = layered_memory.get("today_date", "")
    yesterday = layered_memory.get("yesterday_date", "")
    daily_today = layered_memory.get("daily_today", "")
    daily_yesterday = layered_memory.get("daily_yesterday", "")

    if daily_today:
        _add(f"## Daily Log ({today})\n\n{daily_today.strip()}\n")

    if daily_yesterday:
        _add(f"## Daily Log ({yesterday})\n\n{daily_yesterday.strip()}\n")

    # Active context — most recent completed turn summary
    active_context = layered_memory.get("active_context", "")
    if active_context:
        _add(f"## Active Context\n\n{active_context.strip()}\n")

    # Long-term memory — persistent facts extracted across many sessions
    longterm = layered_memory.get("longterm", "")
    if longterm:
        _add(f"## Long-Term Memory\n\n{longterm.strip()}\n")

    # User profile — personal/professional info (only for profile agents)
    user_profile = layered_memory.get("user_profile", "")
    if user_profile:
        _add(f"## User Profile\n\n{user_profile.strip()}\n")

    # Generic key-value memories (from build_memory_context)
    if memory_block:
        _add(f"{memory_block.strip()}\n")

    return "\n".join(sections).strip()


def generate_full_context_markdown(
    summary_text: str | None,
    messages: list[Message],
    layered_memory: dict[str, str],
    memory_block: str = "",
    *,
    summary_max_chars: int = 3_000,
    history_max_chars: int = 10_000,
    memory_max_chars: int = 6_000,
) -> str:
    """
    Assemble the complete context block to inject into the LLM system prompt.

    This is the main entry point called by the agent runner. It combines:
      1. Conversation summary (if session is long)
      2. Recent conversation history
      3. Memory (layered + generic)

    All data is loaded from PostgreSQL and formatted in-memory.
    Nothing is written to disk. The returned string is ephemeral.

    Args:
        summary_text: LLM-generated summary from conversation_summaries table.
        messages: Recent Message rows from the messages table.
        layered_memory: Dict from load_layered_memory() in memory_service.py.
        memory_block: Pre-formatted Markdown from build_memory_context().

    Returns:
        A single Markdown string combining all context layers.
        Order: Summary → Memory → Recent History (recency last = highest attention).
    """
    parts: list[str] = []

    summary_md = generate_summary_markdown(summary_text)
    if summary_md:
        parts.append(summary_md[:summary_max_chars])

    memory_md = generate_memory_markdown(layered_memory, memory_block, max_chars=memory_max_chars)
    if memory_md:
        parts.append(memory_md)

    history_md = generate_conversation_markdown(messages, max_chars=history_max_chars)
    if history_md:
        parts.append(history_md)

    return "\n\n---\n\n".join(parts) if parts else ""
