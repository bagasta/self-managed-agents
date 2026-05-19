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
        self.prompt_tokens_from_callbacks: int = 0
        self.completion_tokens_from_callbacks: int = 0
        self.reasoning_tokens_from_callbacks: int = 0
        self.cached_tokens_from_callbacks: int = 0
        self.openrouter_cost_usd_from_callbacks: float = 0.0
        self.usage_details: list[dict[str, Any]] = []

    @staticmethod
    def _usage_from_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
        token_usage = mapping.get("token_usage") or mapping.get("usage") or mapping
        if not isinstance(token_usage, dict):
            return {}

        input_tokens = token_usage.get("prompt_tokens", token_usage.get("input_tokens", 0)) or 0
        output_tokens = token_usage.get("completion_tokens", token_usage.get("output_tokens", 0)) or 0
        total_tokens = (
            token_usage.get("total_tokens")
            or token_usage.get("total_token_count")
            or token_usage.get("total_tokens_used")
            or ((input_tokens or 0) + (output_tokens or 0))
            or 0
        )

        prompt_details = token_usage.get("prompt_tokens_details") or token_usage.get("input_token_details") or {}
        completion_details = token_usage.get("completion_tokens_details") or token_usage.get("output_token_details") or {}
        cost = token_usage.get("cost")
        if cost is None:
            cost = token_usage.get("total_cost") or token_usage.get("openrouter_cost")

        return {
            "prompt_tokens": int(input_tokens or 0),
            "completion_tokens": int(output_tokens or 0),
            "total_tokens": int(total_tokens or 0),
            "reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0),
            "cached_tokens": int(prompt_details.get("cached_tokens") or 0),
            "cache_write_tokens": int(prompt_details.get("cache_write_tokens") or 0),
            "cost_usd": float(cost or 0),
            "is_byok": token_usage.get("is_byok"),
            "cost_details": token_usage.get("cost_details") or {},
        }

    def _record_usage(self, usage: dict[str, Any], *, model: str | None, run_id: Any, agent_name: str | None) -> None:
        total = int(usage.get("total_tokens") or 0)
        if total <= 0 and not usage.get("cost_usd"):
            return

        self.total_tokens_from_callbacks += total
        self.prompt_tokens_from_callbacks += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens_from_callbacks += int(usage.get("completion_tokens") or 0)
        self.reasoning_tokens_from_callbacks += int(usage.get("reasoning_tokens") or 0)
        self.cached_tokens_from_callbacks += int(usage.get("cached_tokens") or 0)
        self.openrouter_cost_usd_from_callbacks += float(usage.get("cost_usd") or 0)

        detail = {
            "run_id": str(run_id) if run_id else None,
            "agent": agent_name,
            "model": model,
            **usage,
        }
        self.usage_details.append(detail)
        self.log.info(
            "agent_step.llm_usage",
            agent=agent_name,
            model=model,
            prompt_tokens=detail["prompt_tokens"],
            completion_tokens=detail["completion_tokens"],
            total_tokens=detail["total_tokens"],
            cost_usd=detail["cost_usd"],
        )

    async def on_llm_start(self, serialized, prompts, **kwargs):
        self.log.debug("agent_step.llm_thinking")

    async def on_llm_end(self, response, **kwargs):
        try:
            usage = getattr(response, "llm_output", None) or {}
            metadata = kwargs.get("metadata") or {}
            agent_name = metadata.get("lc_agent_name") or metadata.get("agent_name")
            run_id = kwargs.get("run_id")
            model = usage.get("model_name") or usage.get("model")
            usage_record = self._usage_from_mapping(usage)
            if usage_record.get("total_tokens") or usage_record.get("cost_usd"):
                self._record_usage(usage_record, model=model, run_id=run_id, agent_name=agent_name)
            else:
                for gen_list in response.generations:
                    for generation in gen_list:
                        ai_msg = getattr(generation, "message", None)
                        if ai_msg:
                            usage_metadata = getattr(ai_msg, "usage_metadata", None) or {}
                            response_metadata = getattr(ai_msg, "response_metadata", None) or {}
                            merged_usage = dict(usage_metadata)
                            if isinstance(response_metadata, dict):
                                merged_usage.update(response_metadata.get("token_usage") or {})
                                merged_usage.update(response_metadata.get("usage") or {})
                                model = model or response_metadata.get("model_name") or response_metadata.get("model")
                            self._record_usage(
                                self._usage_from_mapping(merged_usage),
                                model=model,
                                run_id=run_id,
                                agent_name=agent_name,
                            )

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
