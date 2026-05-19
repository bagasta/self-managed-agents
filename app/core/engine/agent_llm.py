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

    max_tokens: int = getattr(agent_model, "max_tokens", None) or settings.llm_max_tokens
    llm_raw = ChatOpenAI(
        model=bare_model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return llm_raw, llm_raw.bind(parallel_tool_calls=False)
