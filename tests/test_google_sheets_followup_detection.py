from app.core.engine.agent_runner import (
    _is_google_sheets_authoring_intent,
    _needs_google_sheets_followup,
    _needs_google_sheets_verification_followup,
    _verify_google_sheet_range_once,
)
from app.core.engine.google_mcp_support import (
    _is_builder_agent_management_request,
    _fallback_unqualified_sheet_range,
    build_google_mcp_usage_notice,
    filter_google_mcp_tools_for_service_context,
    google_sheets_followup_directive,
    google_sheets_verification_followup_directive,
    infer_google_workspace_service_context,
    is_google_workspace_execution_intent,
)
from types import SimpleNamespace
import pytest


def test_sheets_authoring_intent_detected_for_table_and_formula() -> None:
    assert _is_google_sheets_authoring_intent(
        "tolong buat google sheet laporan penjualan lengkap dengan tabel dan formula"
    ) is True


def test_arthur_agent_brief_treats_google_as_target_capability() -> None:
    message = (
        'Bikin agent baru bernama "Field Interview Assistant". Tugasnya mewawancarai '
        "penerima bantuan dan hasil wawancaranya tersimpan ke Google Spreadsheet."
    )

    assert _is_builder_agent_management_request(message) is True
    assert is_google_workspace_execution_intent(message, is_builder=True) is False
    assert infer_google_workspace_service_context(
        message,
        is_builder=True,
    ) is None


def test_arthur_agent_configuration_boundary_applies_across_google_services() -> None:
    messages = (
        "Buat agent CS yang membaca Gmail dan membuat draft balasan.",
        "Bikin agent sales yang menyimpan lead ke Google Sheets.",
        "Buat agent sekretaris yang menjadwalkan meeting ke Google Calendar.",
        "Buat agent knowledge yang mengambil SOP dari Google Drive.",
        "Update agent survey agar membuat Google Form untuk responden.",
        "Perbaiki agent laporan supaya hasilnya tersimpan ke Google Docs.",
        "Perbaiki Personal Assistant agar bisa menulis catatan ke Google Sheets.",
    )

    assert all(
        not is_google_workspace_execution_intent(message, is_builder=True)
        for message in messages
    )


def test_arthur_direct_google_request_remains_execution_intent() -> None:
    message = "Arthur, buatkan Google Sheet laporan penjualan sekarang"

    assert _is_builder_agent_management_request(message) is False
    assert is_google_workspace_execution_intent(message, is_builder=True) is True
    assert infer_google_workspace_service_context(
        message,
        is_builder=True,
    ) == "sheets"


def test_blank_spreadsheet_only_does_not_trigger_authoring() -> None:
    assert _is_google_sheets_authoring_intent(
        "tolong buat spreadsheet kosong saja"
    ) is False


def test_delete_empty_sheet_row_is_not_misclassified_as_blank_spreadsheet() -> None:
    assert _is_google_sheets_authoring_intent(
        "hapus row kosong yang duplikat di Google Sheet"
    ) is True


def test_sheets_context_resolves_terse_duplicate_row_followup() -> None:
    history = [
        SimpleNamespace(role="agent", content="Ada duplikasi row Rain Tomorrow di Google Sheet."),
    ]

    assert infer_google_workspace_service_context(
        "Hilangkan row duplikat yang kosong",
        history,
    ) == "sheets"


def test_sheets_context_survives_just_do_it_after_wrong_tasks_reply() -> None:
    history = [
        SimpleNamespace(role="agent", content="Ada duplikasi row di Google Sheet."),
        SimpleNamespace(role="user", content="Hilangkan row duplikat yang kosong"),
        SimpleNamespace(role="agent", content="Google Tasks API belum aktif."),
    ]

    assert infer_google_workspace_service_context("Just do it", history) == "sheets"


def test_sheets_context_survives_generic_fix_followup() -> None:
    history = [
        SimpleNamespace(
            role="agent",
            content="Spreadsheet baru terisi sampai row 10, bukan row 100.",
        ),
        SimpleNamespace(role="user", content="ok, perbaiki sekarang"),
    ]

    assert infer_google_workspace_service_context(
        "ok, perbaiki sekarang",
        history,
    ) == "sheets"


def test_question_about_wrong_tasks_requirement_keeps_sheets_context() -> None:
    history = [
        SimpleNamespace(role="agent", content="Ada duplicate row di Google Sheet."),
        SimpleNamespace(role="user", content="Hilangkan row duplikat yang kosong"),
    ]

    assert infer_google_workspace_service_context(
        "Why do you need Google Tasks API?",
        history,
    ) == "sheets"


def test_sheets_context_filters_google_tasks_tools() -> None:
    tools = [
        SimpleNamespace(name="read_sheet_values"),
        SimpleNamespace(name="resize_sheet_dimensions"),
        SimpleNamespace(name="manage_task"),
        SimpleNamespace(name="list_tasks"),
    ]
    log = SimpleNamespace(info=lambda *args, **kwargs: None)

    filtered = filter_google_mcp_tools_for_service_context(
        tools,
        service_context="sheets",
        log=log,
    )

    assert [tool.name for tool in filtered] == [
        "read_sheet_values",
        "resize_sheet_dimensions",
    ]


def test_unresolved_context_filters_google_tasks_tools_by_default() -> None:
    tools = [
        SimpleNamespace(name="modify_sheet_values"),
        SimpleNamespace(name="manage_task_list"),
        SimpleNamespace(name="manage_task"),
    ]
    log = SimpleNamespace(info=lambda *args, **kwargs: None)

    filtered = filter_google_mcp_tools_for_service_context(
        tools,
        service_context=None,
        log=log,
    )

    assert [tool.name for tool in filtered] == ["modify_sheet_values"]


def test_explicit_google_tasks_context_keeps_task_tools() -> None:
    tools = [
        SimpleNamespace(name="manage_task_list"),
        SimpleNamespace(name="manage_task"),
    ]
    log = SimpleNamespace(info=lambda *args, **kwargs: None)

    context = infer_google_workspace_service_context(
        "Tambahkan pengingat review laporan ke Google Tasks"
    )
    filtered = filter_google_mcp_tools_for_service_context(
        tools,
        service_context=context,
        log=log,
    )

    assert context == "tasks"
    assert [tool.name for tool in filtered] == ["manage_task_list", "manage_task"]


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


def test_sheets_followup_still_needed_after_failed_values_write() -> None:
    steps = [
        {
            "tool": "create_spreadsheet",
            "result": "ID: sheet123abc",
        },
        {
            "tool": "modify_sheet_values",
            "result": "[tool_error] SHEET_VALUES_REQUIRED",
        },
    ]

    assert _needs_google_sheets_followup(
        "buat spreadsheet laporan penjualan dengan tabel",
        steps,
    ) == (True, "sheet123abc")


def test_sheets_mutation_requires_post_write_read_verification() -> None:
    steps = [
        {
            "tool": "read_sheet_values",
            "args": {"spreadsheet_id": "sheet123", "range_name": "A1:Z100"},
            "result": "Successfully read 10 rows",
        },
        {
            "tool": "modify_sheet_values",
            "args": {"spreadsheet_id": "sheet123", "range_name": "A1:Z100"},
            "result": "Successfully updated 2600 cells",
        },
    ]

    assert _needs_google_sheets_verification_followup(steps) == (
        True,
        "sheet123",
        "A1:Z100",
    )


def test_sheets_post_write_read_satisfies_verification() -> None:
    steps = [
        {
            "tool": "modify_sheet_values",
            "args": {"spreadsheet_id": "sheet123", "range_name": "A1:Z100"},
            "result": "Successfully updated 2600 cells",
        },
        {
            "tool": "read_sheet_values",
            "args": {"spreadsheet_id": "sheet123", "range_name": "A1:Z100"},
            "result": "Successfully read 100 rows",
        },
    ]

    assert _needs_google_sheets_verification_followup(steps) == (
        False,
        "sheet123",
        "A1:Z100",
    )


@pytest.mark.asyncio
async def test_sheets_runtime_verification_reads_only_mutated_range_once() -> None:
    class ReadTool:
        name = "read_sheet_values"

        def __init__(self):
            self.calls = []

        async def ainvoke(self, args):
            self.calls.append(args)
            return "Successfully read 1 rows from range 'Sheet1!A3:C3'"

    read_tool = ReadTool()
    args, result = await _verify_google_sheet_range_once(
        tools=[read_tool],
        spreadsheet_id="sheet123",
        target_range="Sheet1!A3:C3",
        timeout_seconds=1,
    )

    assert len(read_tool.calls) == 1
    assert args == {
        "spreadsheet_id": "sheet123",
        "range_name": "Sheet1!A3:C3",
        "include_formulas": True,
    }
    assert "Successfully read 1 rows" in result


def test_sheets_post_write_read_must_cover_mutated_range() -> None:
    steps = [
        {
            "tool": "modify_sheet_values",
            "args": {"spreadsheet_id": "sheet123", "range_name": "A1:Z100"},
            "result": "Successfully updated 2600 cells",
        },
        {
            "tool": "read_sheet_values",
            "args": {"spreadsheet_id": "sheet123", "range_name": "A1:Z10"},
            "result": "Successfully read 10 rows",
        },
    ]

    assert _needs_google_sheets_verification_followup(steps) == (
        True,
        "sheet123",
        "A1:Z100",
    )


def test_failed_sheets_mutation_does_not_trigger_success_verification() -> None:
    steps = [
        {
            "tool": "modify_sheet_values",
            "args": {"spreadsheet_id": "sheet123", "range_name": "A1:Z100"},
            "result": "[tool_error] SHEET_VALUES_REQUIRED",
        },
    ]

    assert _needs_google_sheets_verification_followup(steps) == (False, None, None)


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


def test_sheets_delete_notice_routes_to_dimension_tool_not_tasks() -> None:
    notice = build_google_mcp_usage_notice(
        "Hilangkan row duplikat yang kosong",
        service_context="sheets",
    ).lower()

    assert "jangan panggil manage_task/manage_task_list" in notice
    assert "google tasks api tidak terkait" in notice
    assert "resize_sheet_dimensions" in notice
    assert "delete_rows" in notice
    assert "read_sheet_values" in notice
    assert "read-before-write wajib" in notice
    assert "values=[]" in notice
    assert "fill_value" in notice


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


def test_sheets_verification_directive_requires_actual_read() -> None:
    directive = google_sheets_verification_followup_directive(
        "sheet123",
        "A1:Z100",
    ).lower()

    assert "read_sheet_values" in directive
    assert "spreadsheet_id=sheet123" in directive
    assert "range_name=a1:z100" in directive
    assert "jumlah row/column/cell aktual" in directive


def test_sheet1_range_can_fallback_to_first_sheet_range() -> None:
    assert _fallback_unqualified_sheet_range("Sheet1!A1:F6") == "A1:F6"
    assert _fallback_unqualified_sheet_range("'Sheet 1'!A1:F6") == "A1:F6"
    assert _fallback_unqualified_sheet_range("Lembar1!A1:F6") == "A1:F6"
    assert _fallback_unqualified_sheet_range("Data!A1:F6") is None
