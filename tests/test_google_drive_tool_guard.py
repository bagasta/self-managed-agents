from types import SimpleNamespace

import pytest

from app.core.engine.google_mcp_support import sanitize_google_forms_tools


class FakeTool:
    def __init__(self, name, result="ok"):
        self.name = name
        self.description = name
        self.args_schema = None
        self.result = result
        self.calls = []

    async def ainvoke(self, kwargs):
        self.calls.append(kwargs)
        return self.result


@pytest.mark.asyncio
async def test_create_drive_file_rejects_empty_xlsx_with_recovery_workflow() -> None:
    create_file = FakeTool("create_drive_file", "created")
    wrapped = sanitize_google_forms_tools([create_file], SimpleNamespace(warning=lambda *a, **k: None))
    guarded = next(tool for tool in wrapped if tool.name == "create_drive_file")

    result = await guarded.ainvoke(
        {
            "file_name": "Laporan.xlsx",
            "content": None,
            "fileUrl": None,
            "folder_id": "folder123",
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
    )

    assert result.startswith("DRIVE_FILE_SOURCE_REQUIRED")
    assert "create_spreadsheet" in result
    assert "update_drive_file" in result
    assert not create_file.calls


@pytest.mark.asyncio
async def test_create_drive_file_accepts_file_url_alias() -> None:
    create_file = FakeTool("create_drive_file", "created")
    wrapped = sanitize_google_forms_tools([create_file], SimpleNamespace(warning=lambda *a, **k: None))
    guarded = next(tool for tool in wrapped if tool.name == "create_drive_file")

    result = await guarded.ainvoke(
        {
            "file_name": "report.txt",
            "file_url": "https://example.com/report.txt",
            "folder_id": "root",
        }
    )

    assert result == "created"
    assert create_file.calls[0]["fileUrl"] == "https://example.com/report.txt"
    assert "file_url" not in create_file.calls[0]


@pytest.mark.asyncio
async def test_create_drive_file_drops_empty_file_url_alias() -> None:
    create_file = FakeTool("create_drive_file", "created")
    wrapped = sanitize_google_forms_tools([create_file], SimpleNamespace(warning=lambda *a, **k: None))
    guarded = next(tool for tool in wrapped if tool.name == "create_drive_file")

    result = await guarded.ainvoke(
        {
            "file_name": "report.txt",
            "content": "hello",
            "fileUrl": None,
            "file_url": None,
        }
    )

    assert result == "created"
    assert "file_url" not in create_file.calls[0]


@pytest.mark.asyncio
async def test_create_drive_file_without_extension_points_to_folder_tool() -> None:
    create_file = FakeTool("create_drive_file", "created")
    wrapped = sanitize_google_forms_tools([create_file], SimpleNamespace(warning=lambda *a, **k: None))
    guarded = next(tool for tool in wrapped if tool.name == "create_drive_file")

    result = await guarded.ainvoke({"file_name": "Test Folder", "content": None, "fileUrl": None})

    assert result.startswith("DRIVE_FOLDER_OR_CONTENT_REQUIRED")
    assert "create_drive_folder" in result
    assert not create_file.calls
