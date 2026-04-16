"""
/v1/models — curated list of OpenRouter-compatible models.
"""
from typing import Any

from fastapi import APIRouter, Depends

from app.deps import verify_api_key

router = APIRouter(prefix="/v1", tags=["models"], dependencies=[Depends(verify_api_key)])

# Curated list — update as OpenRouter adds new models.
_MODELS: list[dict[str, Any]] = [
    # Anthropic
    {
        "id": "anthropic/claude-sonnet-4-6",
        "name": "Claude Sonnet 4.6",
        "provider": "Anthropic",
        "context_length": 200000,
        "description": "Best balance of intelligence and speed. Recommended default.",
    },
    {
        "id": "anthropic/claude-opus-4-6",
        "name": "Claude Opus 4.6",
        "provider": "Anthropic",
        "context_length": 200000,
        "description": "Most capable Claude model. Best for complex reasoning tasks.",
    },
    {
        "id": "anthropic/claude-haiku-4-5",
        "name": "Claude Haiku 4.5",
        "provider": "Anthropic",
        "context_length": 200000,
        "description": "Fastest and most cost-efficient Claude model.",
    },
    # OpenAI
    {
        "id": "openai/gpt-4.1",
        "name": "GPT-4.1",
        "provider": "OpenAI",
        "context_length": 128000,
        "description": "Latest GPT-4 series. Strong at coding and instruction following.",
    },
    {
        "id": "openai/gpt-4.1-mini",
        "name": "GPT-4.1 Mini",
        "provider": "OpenAI",
        "context_length": 128000,
        "description": "Cost-efficient GPT-4 class model.",
    },
    {
        "id": "openai/gpt-4o",
        "name": "GPT-4o",
        "provider": "OpenAI",
        "context_length": 128000,
        "description": "Multimodal GPT-4 variant with strong reasoning.",
    },
    # Google
    {
        "id": "google/gemini-2.5-pro",
        "name": "Gemini 2.5 Pro",
        "provider": "Google",
        "context_length": 1048576,
        "description": "Largest context window. Best for long documents and multi-file tasks.",
    },
    {
        "id": "google/gemini-2.5-flash",
        "name": "Gemini 2.5 Flash",
        "provider": "Google",
        "context_length": 1048576,
        "description": "Fast and cost-efficient Gemini model.",
    },
    # Meta
    {
        "id": "meta-llama/llama-3.3-70b-instruct",
        "name": "Llama 3.3 70B Instruct",
        "provider": "Meta",
        "context_length": 128000,
        "description": "Open-weight model. Good for privacy-sensitive use cases.",
    },
    {
        "id": "meta-llama/llama-3.1-8b-instruct",
        "name": "Llama 3.1 8B Instruct",
        "provider": "Meta",
        "context_length": 128000,
        "description": "Lightweight open-weight model. Very low cost.",
    },
    # Mistral
    {
        "id": "mistralai/mistral-large",
        "name": "Mistral Large",
        "provider": "Mistral",
        "context_length": 128000,
        "description": "Mistral's most capable model.",
    },
    {
        "id": "mistralai/mistral-nemo",
        "name": "Mistral Nemo",
        "provider": "Mistral",
        "context_length": 128000,
        "description": "Efficient open-weight model from Mistral.",
    },
    # DeepSeek
    {
        "id": "deepseek/deepseek-chat-v3-0324",
        "name": "DeepSeek Chat V3",
        "provider": "DeepSeek",
        "context_length": 64000,
        "description": "Strong reasoning and coding at low cost.",
    },
    {
        "id": "deepseek/deepseek-r1",
        "name": "DeepSeek R1",
        "provider": "DeepSeek",
        "context_length": 64000,
        "description": "Chain-of-thought reasoning model.",
    },
    # Qwen
    {
        "id": "qwen/qwen-2.5-72b-instruct",
        "name": "Qwen 2.5 72B Instruct",
        "provider": "Alibaba",
        "context_length": 131072,
        "description": "Strong multilingual model, especially for Indonesian + English.",
    },
]


@router.get("/models")
async def list_models() -> dict[str, Any]:
    """Return a curated list of models available via OpenRouter."""
    return {
        "models": _MODELS,
        "total": len(_MODELS),
        "note": "All models accessed via OpenRouter. Set model id in agent config.",
    }
