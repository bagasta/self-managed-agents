from app.core.tools.builder_runtime_text import _append_google_workspace_instruction


def test_google_workspace_instruction_requires_schema_aware_sheet_append():
    text, changed = _append_google_workspace_instruction("Agent membantu administrasi.")

    assert changed is True
    assert "ATURAN GOOGLE SHEETS" in text
    assert "read_sheet_values" in text
    assert "append_table_rows" in text
    assert "Jangan gunakan modify_sheet_values" in text


def test_existing_google_instruction_receives_sheet_safety_rule_once():
    existing = "Google Workspace tools aktif untuk Google Docs dan Google Drive."

    text, changed = _append_google_workspace_instruction(existing)
    repeated, repeated_changed = _append_google_workspace_instruction(text)

    assert changed is True
    assert text.count("ATURAN GOOGLE SHEETS") == 1
    assert repeated == text
    assert repeated_changed is False
