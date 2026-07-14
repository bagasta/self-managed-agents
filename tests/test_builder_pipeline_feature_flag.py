from unittest.mock import MagicMock

from app.config import get_settings
from app.core.tools.builder_tools import build_builder_tools


def test_optimized_builder_tool_is_reversible_with_feature_flag(monkeypatch) -> None:
    settings = get_settings()
    db_factory = MagicMock()

    monkeypatch.setattr(settings, "arthur_builder_pipeline_mode", "legacy")
    legacy_names = {tool.name for tool in build_builder_tools(db_factory, owner_phone="+628111")}
    assert "create_agent_from_brief" not in legacy_names

    monkeypatch.setattr(settings, "arthur_builder_pipeline_mode", "optimized")
    optimized_names = {tool.name for tool in build_builder_tools(db_factory, owner_phone="+628111")}
    assert "create_agent_from_brief" in optimized_names
    assert legacy_names.issubset(optimized_names)
