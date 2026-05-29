from app.core.engine.agent_runner import (
    _is_google_sheets_authoring_intent,
    _needs_google_sheets_followup,
)
from app.core.engine.google_mcp_support import (
    _fallback_unqualified_sheet_range,
    build_google_mcp_usage_notice,
    google_sheets_followup_directive,
)


def test_sheets_authoring_intent_detected_for_table_and_formula() -> None:
    assert _is_google_sheets_authoring_intent(
        "tolong buat google sheet laporan penjualan lengkap dengan tabel dan formula"
    ) is True


def test_blank_spreadsheet_only_does_not_trigger_authoring() -> None:
    assert _is_google_sheets_authoring_intent(
        "tolong buat spreadsheet kosong saja"
    ) is False


def test_sheets_followup_needed_when_only_create_spreadsheet_step() -> None:
    steps = [
        {
            "tool": "create_spreadsheet",
            "result": (
                "Successfully created spreadsheet 'Laporan'. "
                "ID: sheet123abc | URL: https://docs.google.com/spreadsheets/d/sheet123abc/edit"
            ),
        }
    ]
    needed, spreadsheet_id = _needs_google_sheets_followup(
        "buat spreadsheet laporan penjualan dengan tabel dan rumus total", steps
    )
    assert needed is True
    assert spreadsheet_id == "sheet123abc"


def test_sheets_followup_not_needed_after_values_written() -> None:
    steps = [
        {"tool": "create_spreadsheet", "result": "ID: sheet123abc"},
        {
            "tool": "modify_sheet_values",
            "result": "Successfully updated range 'Sheet1!A1:D5' in spreadsheet sheet123abc.",
        },
    ]
    needed, spreadsheet_id = _needs_google_sheets_followup(
        "buat spreadsheet laporan penjualan dengan tabel dan formula", steps
    )
    assert needed is False
    assert spreadsheet_id == "sheet123abc"


def test_sheets_followup_still_needed_after_only_creating_extra_tab() -> None:
    steps = [
        {"tool": "create_spreadsheet", "result": "ID: sheet123abc"},
        {"tool": "create_sheet", "result": "Successfully created sheet 'Data' in spreadsheet sheet123abc."},
    ]
    needed, spreadsheet_id = _needs_google_sheets_followup(
        "buat spreadsheet laporan penjualan dengan tabel dan formula", steps
    )
    assert needed is True
    assert spreadsheet_id == "sheet123abc"


def test_sheets_notice_requires_values_and_formulas() -> None:
    notice = build_google_mcp_usage_notice(
        "tolong buat google sheet budget lengkap dengan tabel dan formula"
    ).lower()
    assert "create_spreadsheet hanya membuat file kosong" in notice
    assert "modify_sheet_values" in notice
    assert "range_name" in notice
    assert "user_entered" in notice
    assert "=sum" in notice
    assert "jangan hardcode sheet1" in notice


def test_sheets_notice_with_jadwal_does_not_trigger_calendar_workflow() -> None:
    notice = build_google_mcp_usage_notice(
        "tolong edit google sheet jadwal kerja dan tambah kolom status"
    ).lower()
    assert "sheets workflow mode" in notice
    assert "jangan panggil manage_event" in notice
    assert "calendar edit workflow" not in notice


def test_explicit_calendar_notice_still_triggers_calendar_workflow() -> None:
    notice = build_google_mcp_usage_notice(
        "tolong edit event di google calendar untuk meeting proposal"
    ).lower()
    assert "calendar edit workflow" in notice


def test_sheets_followup_directive_populates_existing_file() -> None:
    directive = google_sheets_followup_directive(
        "sheet123abc", "buat spreadsheet budget dengan tabel dan formula"
    ).lower()
    assert "spreadsheet_id=sheet123abc" in directive
    assert "modify_sheet_values" in directive
    assert "range_name" in directive
    assert "user_entered" in directive
    assert "read_sheet_values" in directive
    assert "jangan hardcode sheet1" in directive


def test_sheet1_range_can_fallback_to_first_sheet_range() -> None:
    assert _fallback_unqualified_sheet_range("Sheet1!A1:F6") == "A1:F6"
    assert _fallback_unqualified_sheet_range("'Sheet 1'!A1:F6") == "A1:F6"
    assert _fallback_unqualified_sheet_range("Lembar1!A1:F6") == "A1:F6"
    assert _fallback_unqualified_sheet_range("Data!A1:F6") is None
