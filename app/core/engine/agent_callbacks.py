"""Callback helpers for agent graph execution."""
from __future__ import annotations

from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler

from app.core.utils.log_sanitizer import redact_pii


class AgentStepLogger(AsyncCallbackHandler):
    """Log LLM/tool activity and accumulate token usage for a graph run."""

    def __init__(self, log: Any) -> None:
        self.log = log
        self._tool_inputs: dict[str, Any] = {}
        self._tool_names: dict[str, str] = {}
        self.total_tokens_from_callbacks: int = 0

    async def on_llm_start(self, serialized, prompts, **kwargs):
        self.log.debug("agent_step.llm_thinking")

    async def on_llm_end(self, response, **kwargs):
        try:
            usage = getattr(response, "llm_output", None) or {}
            token_usage = usage.get("token_usage") or usage.get("usage") or {}
            total = (
                token_usage.get("total_tokens")
                or token_usage.get("total_token_count")
                or 0
            )
            if not total:
                for gen_list in response.generations:
                    for generation in gen_list:
                        ai_msg = getattr(generation, "message", None)
                        if ai_msg:
                            usage_metadata = getattr(ai_msg, "usage_metadata", None) or {}
                            total += usage_metadata.get("total_tokens", 0)
            self.total_tokens_from_callbacks += total

            generation = response.generations[0][0]
            text = generation.text[:200] if generation.text else ""
            ai_msg = generation.message if hasattr(generation, "message") else None
            tool_call_ids = []
            if ai_msg and hasattr(ai_msg, "tool_calls") and ai_msg.tool_calls:
                tool_call_ids = [
                    f"{tc.get('name', '?')}:{tc.get('id', '?')}"
                    for tc in ai_msg.tool_calls
                ]
            if text:
                self.log.info(
                    "agent_step.llm_response",
                    preview=text,
                    tool_calls=tool_call_ids if tool_call_ids else None,
                )
            elif tool_call_ids:
                self.log.info("agent_step.llm_tool_calls", tool_calls=tool_call_ids)
        except Exception:
            pass

    async def on_tool_start(self, serialized, input_str, **kwargs):
        tool_name = serialized.get("name", "?")
        tool_call_id = kwargs.get("tool_call_id") or kwargs.get("run_id") or "?"
        self._tool_inputs[str(tool_call_id)] = input_str
        self._tool_names[str(tool_call_id)] = str(tool_name)
        safe_input = redact_pii(str(input_str)[:300])
        self.log.info(
            "agent_step.tool_start",
            tool=tool_name,
            tool_call_id=str(tool_call_id)[:36],
            input=safe_input,
        )

    async def on_tool_end(self, output, **kwargs):
        tool_call_id = kwargs.get("tool_call_id") or kwargs.get("run_id") or "?"
        self.log.info(
            "agent_step.tool_end",
            tool_call_id=str(tool_call_id)[:36],
            output=str(output)[:300],
        )
        self._tool_inputs.pop(str(tool_call_id), None)
        self._tool_names.pop(str(tool_call_id), None)

    async def on_tool_error(self, error, **kwargs):
        tool_call_id = kwargs.get("tool_call_id") or kwargs.get("run_id") or "?"
        self.log.warning(
            "agent_step.tool_error",
            tool_call_id=str(tool_call_id)[:36],
            error=str(error)[:500],
        )

    async def on_chain_start(self, serialized, inputs, **kwargs):
        if not serialized:
            return
        name = serialized.get("name", serialized.get("id", ["?"])[-1])
        self.log.debug("agent_step.chain_start", chain=name)

    async def on_chain_end(self, outputs, **kwargs):
        self.log.debug("agent_step.chain_end")
