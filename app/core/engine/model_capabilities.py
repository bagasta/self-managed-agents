"""Shared model capability checks used by runtime and builder diagnostics."""
from __future__ import annotations


def model_supports_image_input(model: str | None) -> bool:
    """Return whether the configured chat endpoint accepts image input."""
    name = str(model or "").lower()
    if not name:
        return False
    if any(marker in name for marker in ("deepseek/", "moonshotai/", "kimi-")):
        return False
    # Current OpenAI GPT-6 family accepts image input. Keep this explicit so
    # an OpenRouter-style identifier such as ``openai/gpt-6-luna`` enables the
    # computer vision loop instead of falling back to blind input-only control.
    if "gpt-6" in name:
        return True
    if "qwen3" in name and "vl" not in name:
        return False
    return any(
        marker in name
        for marker in (
            "gpt-4o",
            "gpt-4.1",
            "o4-mini",
            "gemini",
            "claude-3",
            "claude-sonnet-4",
            "pixtral",
            "llava",
            "vision",
            "-vl",
            "qwen-vl",
        )
    )
