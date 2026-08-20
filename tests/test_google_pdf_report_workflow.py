from app.core.engine.google_mcp_support import (
    google_spreadsheet_pdf_report_directive,
    has_verified_google_pdf_artifact,
    is_google_spreadsheet_pdf_report_intent,
    resolve_google_spreadsheet_pdf_report_intent,
    scope_google_pdf_report_tools,
)


class _Log:
    def __init__(self) -> None:
        self.events: list[str] = []

    def info(self, event: str, **_kwargs) -> None:
        self.events.append(event)

    def warning(self, event: str, **_kwargs) -> None:
        self.events.append(event)


def test_google_spreadsheet_pdf_intent_requires_source_and_pdf() -> None:
    assert is_google_spreadsheet_pdf_report_intent("Buatkan PDF laporan dari spreadsheet Cashflow")
    assert not is_google_spreadsheet_pdf_report_intent("Buatkan PDF laporan dari catatan ini")
    assert not is_google_spreadsheet_pdf_report_intent("Baca spreadsheet Cashflow")


def test_google_pdf_intent_is_preserved_for_explicit_follow_up() -> None:
    history = [
        type("Message", (), {"role": "user", "content": "Buatkan PDF laporan dari spreadsheet Cashflow"})(),
        type("Message", (), {"role": "agent", "content": "Siap, saya akan kerjakan"})(),
    ]

    assert resolve_google_spreadsheet_pdf_report_intent("langsung eksekusi sekarang", history)
    assert not resolve_google_spreadsheet_pdf_report_intent("ganti topik, cuaca hari ini", history)


def test_google_pdf_tools_are_scoped_to_verified_export_path() -> None:
    tools = [type("Tool", (), {"name": name})() for name in (
        "read_sheet_values", "export_doc_to_pdf", "create_report_doc", "send_gmail_message"
    )]
    log = _Log()

    scoped = scope_google_pdf_report_tools(tools, log=log)

    assert [tool.name for tool in scoped] == [
        "read_sheet_values", "export_doc_to_pdf", "create_report_doc"
    ]
    assert "agent_run.google_pdf_report_tools_scoped" in log.events


def test_google_pdf_tools_keep_original_list_if_export_path_is_incomplete() -> None:
    tools = [type("Tool", (), {"name": "read_sheet_values"})()]
    log = _Log()

    assert scope_google_pdf_report_tools(tools, log=log) == tools
    assert "agent_run.google_pdf_report_tools_scope_skipped" in log.events


def test_verified_google_pdf_requires_successful_export_url() -> None:
    assert not has_verified_google_pdf_artifact([
        {"tool": "read_sheet_values", "result": "Successfully read rows"},
        {"tool": "export_doc_to_pdf", "result": "Error: export failed"},
    ])
    assert has_verified_google_pdf_artifact([
        {"tool": "export_doc_to_pdf", "result": "PDF exported: https://drive.google.com/file/d/abc/view"},
    ])


def test_google_pdf_directive_requires_export_before_final_url() -> None:
    directive = google_spreadsheet_pdf_report_directive("PDF dari spreadsheet")

    assert "export_doc_to_pdf" in directive
    assert "Jangan gunakan sandbox/Python" in directive
