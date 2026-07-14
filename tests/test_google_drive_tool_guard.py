from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

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


@pytest.mark.asyncio
async def test_create_drive_file_rejects_untrusted_local_path() -> None:
    create_file = FakeTool("create_drive_file", "created")
    wrapped = sanitize_google_forms_tools([create_file], SimpleNamespace(warning=lambda *a, **k: None))
    guarded = next(tool for tool in wrapped if tool.name == "create_drive_file")

    result = await guarded.ainvoke(
        {
            "file_name": "report.csv",
            "fileUrl": "/workspace/shared/current_input/report.csv",
        }
    )

    assert result.startswith("DRIVE_LOCAL_FILE_UNAVAILABLE")
    assert not create_file.calls


@pytest.mark.asyncio
async def test_create_drive_file_stages_only_active_attachment_and_cleans_up(tmp_path: Path) -> None:
    source = tmp_path / "current_input" / "report.csv"
    source.parent.mkdir()
    source.write_bytes(b"name,total\nBakmi,25000\n")
    staging_dir = tmp_path / "mcp-staging"

    class InspectingTool(FakeTool):
        staged_path: Path | None = None

        async def ainvoke(self, kwargs):
            self.calls.append(kwargs)
            self.staged_path = Path(urlparse(kwargs["fileUrl"]).path)
            assert self.staged_path.parent == staging_dir.resolve()
            assert self.staged_path.read_bytes() == source.read_bytes()
            return self.result

    create_file = InspectingTool("create_drive_file", "created")
    wrapped = sanitize_google_forms_tools(
        [create_file],
        SimpleNamespace(warning=lambda *a, **k: None),
        current_attachment_path=str(source),
        trusted_attachment_aliases={"/workspace/shared/current_input/report.csv"},
        upload_staging_dir=str(staging_dir),
    )
    guarded = next(tool for tool in wrapped if tool.name == "create_drive_file")

    result = await guarded.ainvoke(
        {
            "file_name": "sales-report.csv",
            "fileUrl": "/workspace/shared/current_input/report.csv",
        }
    )

    assert result == "created"
    assert create_file.calls[0]["fileUrl"].startswith("file://")
    assert create_file.calls[0]["mime_type"] == "text/csv"
    assert create_file.staged_path is not None
    assert not create_file.staged_path.exists()
    assert source.exists()


@pytest.mark.asyncio
async def test_create_drive_file_consumes_recent_pending_attachment_after_success(tmp_path: Path) -> None:
    source = tmp_path / "current_input" / "image.jpg"
    source.parent.mkdir()
    source.write_bytes(b"jpeg-data")
    create_file = FakeTool("create_drive_file", "uploaded")
    wrapped = sanitize_google_forms_tools(
        [create_file],
        SimpleNamespace(warning=lambda *a, **k: None),
        current_attachment_path=str(source),
        trusted_attachment_aliases={
            "/workspace/shared/current_input/image.jpg",
            "/workspace/data/incoming/current_input/image.jpg",
        },
        upload_staging_dir=str(tmp_path / "staging"),
        consume_attachment_on_success=True,
    )
    guarded = next(tool for tool in wrapped if tool.name == "create_drive_file")

    result = await guarded.ainvoke(
        {
            "file_name": "Laporan.jpg",
            "folder_id": "folder123",
            "mime_type": "image/jpeg",
            "fileUrl": None,
        }
    )

    assert result == "uploaded"
    assert not source.exists()
    assert not list((tmp_path / "staging").iterdir())


@pytest.mark.asyncio
async def test_create_drive_file_rejects_different_local_alias_even_with_pending_attachment(tmp_path: Path) -> None:
    source = tmp_path / "current_input" / "image.jpg"
    source.parent.mkdir()
    source.write_bytes(b"jpeg-data")
    create_file = FakeTool("create_drive_file", "uploaded")
    wrapped = sanitize_google_forms_tools(
        [create_file],
        SimpleNamespace(warning=lambda *a, **k: None),
        current_attachment_path=str(source),
        trusted_attachment_aliases={"/workspace/shared/current_input/image.jpg"},
        upload_staging_dir=str(tmp_path / "staging"),
    )
    guarded = next(tool for tool in wrapped if tool.name == "create_drive_file")

    result = await guarded.ainvoke(
        {
            "file_name": "wrong.jpg",
            "mime_type": "image/jpeg",
            "fileUrl": "file:///workspace/shared/current_input/other.jpg",
        }
    )

    assert result.startswith("DRIVE_LOCAL_FILE_UNAVAILABLE")
    assert not create_file.calls
    assert source.exists()


@pytest.mark.asyncio
async def test_drive_download_guard_blocks_destination_folder_id() -> None:
    create_file = FakeTool("create_drive_file", "uploaded")
    download = FakeTool("get_drive_file_download_url", "downloaded")
    wrapped = sanitize_google_forms_tools(
        [create_file, download],
        SimpleNamespace(warning=lambda *a, **k: None),
    )
    guarded_create = next(tool for tool in wrapped if tool.name == "create_drive_file")
    guarded_download = next(tool for tool in wrapped if tool.name == "get_drive_file_download_url")

    create_result = await guarded_create.ainvoke(
        {"file_name": "report.jpg", "folder_id": "folder123", "mime_type": "image/jpeg"}
    )
    download_result = await guarded_download.ainvoke(
        {"file_id": "folder123", "export_format": "jpg"}
    )

    assert create_result.startswith("DRIVE_FILE_SOURCE_REQUIRED")
    assert download_result.startswith("DRIVE_FOLDER_NOT_DOWNLOADABLE")
    assert not download.calls
