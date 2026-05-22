from app.core.engine.prompt_builder import build_mcp_tool_priority_notice


def test_mcp_tool_priority_notice_prefers_mcp_over_sandbox() -> None:
    notice = build_mcp_tool_priority_notice(
        mcp_tool_names=["create_spreadsheet", "modify_sheet_values"],
        sandbox_active=True,
    )

    assert "MCP tools aktif: create_spreadsheet, modify_sheet_values" in notice
    assert "panggil tool MCP yang relevan sebagai sumber kebenaran" in notice
    assert "Jangan memakai sandbox" in notice
    assert "hanya sebagai pendukung" in notice


def test_mcp_tool_priority_notice_truncates_long_tool_lists() -> None:
    names = [f"tool_{i}" for i in range(45)]

    notice = build_mcp_tool_priority_notice(
        mcp_tool_names=names,
        sandbox_active=False,
    )

    assert "tool_0" in notice
    assert "tool_39" in notice
    assert "tool_40" not in notice
    assert "... (+5 more)" in notice
