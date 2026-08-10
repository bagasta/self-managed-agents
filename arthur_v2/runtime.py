"""Deep Agents graph factory for Arthur V2."""
from __future__ import annotations

from typing import Any

from deepagents import create_deep_agent

from .plugin import build_arthur_v2_system_prompt


def build_arthur_v2_graph(*, model: Any, tools: list[Any]):
    """Create Arthur V2's Deep Agent graph with its control-plane tool contract."""
    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=build_arthur_v2_system_prompt(),
    )
