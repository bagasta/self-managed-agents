"""LLM setup helpers for agent runs."""
from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI


class _WorkforceChatOpenAI(ChatOpenAI):
    """Report an isolated provider name so workforce harness policy is scoped."""

    _workforce_harness_profile = "workforce_internal"

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> Any:
        params = super()._get_ls_params(stop=stop, **kwargs)
        params["ls_provider"] = self._workforce_harness_profile
        return params


def build_agent_llms(
    agent_model: Any,
    settings: Any,
    temperature: float,
    *,
    workforce_internal: bool = False,
    workforce_harness_profile: str = "workforce_internal",
    max_tokens_override: int | None = None,
) -> tuple[ChatOpenAI, Any]:
    model_name = agent_model.model or ""
    if model_name.startswith("mistral/") or model_name.startswith("mistral-"):
        api_key = settings.mistral_api_key
        base_url = "https://api.mistral.ai/v1"
        bare_model = model_name.removeprefix("mistral/")
    else:
        api_key = settings.openrouter_api_key
        base_url = "https://openrouter.ai/api/v1"
        bare_model = model_name

    configured_max_tokens = getattr(agent_model, "max_tokens", None)
    tools_config = getattr(agent_model, "tools_config", None)
    is_coding_agent = isinstance(tools_config, dict) and bool(
        tools_config.get("sandbox") or tools_config.get("deploy")
    )
    max_tokens: int = max_tokens_override or configured_max_tokens or (
        settings.coding_agent_max_tokens if is_coding_agent else settings.llm_max_tokens
    )
    model_class = _WorkforceChatOpenAI if workforce_internal else ChatOpenAI
    llm_raw = model_class(
        model=bare_model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=getattr(settings, "llm_request_timeout_seconds", 120.0),
        max_retries=getattr(settings, "llm_max_retries", 1),
    )
    if workforce_internal:
        llm_raw._workforce_harness_profile = workforce_harness_profile
    return llm_raw, llm_raw.bind(parallel_tool_calls=False)
