"""LLM setup helpers for agent runs."""
from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI


def build_agent_llms(agent_model: Any, settings: Any, temperature: float) -> tuple[ChatOpenAI, Any]:
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
    mcp_config = tools_config.get("mcp") if isinstance(tools_config, dict) else None
    mcp_servers = (
        mcp_config.get("servers", mcp_config)
        if isinstance(mcp_config, dict)
        else {}
    )
    is_google_agent = isinstance(mcp_servers, dict) and isinstance(
        mcp_servers.get("google_workspace"), dict
    )
    if is_coding_agent:
        default_max_tokens = settings.coding_agent_max_tokens
    elif is_google_agent:
        default_max_tokens = getattr(settings, "google_agent_max_tokens", 4096)
    else:
        default_max_tokens = settings.llm_max_tokens
    max_tokens = int(configured_max_tokens or default_max_tokens)
    if is_google_agent:
        max_tokens = max(max_tokens, int(getattr(settings, "google_agent_max_tokens", 4096)))
    llm_raw = ChatOpenAI(
        model=bare_model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=getattr(settings, "llm_request_timeout_seconds", 120.0),
        max_retries=getattr(settings, "llm_max_retries", 1),
    )
    return llm_raw, llm_raw.bind(parallel_tool_calls=False)
