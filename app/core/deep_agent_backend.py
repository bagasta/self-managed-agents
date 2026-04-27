"""
DockerBackend — adapter between DockerSandbox and Deep Agents SandboxBackendProtocol.

File operations (read, write, edit, ls, glob, grep) run directly on the host
workspace directory without spinning up a container. Only execute() uses Docker.
"""
from __future__ import annotations

import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)
from deepagents.backends.protocol import FileData  # TypedDict

if TYPE_CHECKING:
    from app.core.sandbox import DockerSandbox


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DockerBackend(SandboxBackendProtocol):
    """
    Wraps DockerSandbox to satisfy Deep Agents SandboxBackendProtocol.

    - All filesystem operations target the host workspace_dir directly.
    - execute() delegates to DockerSandbox.bash() (ephemeral container per call).
    """

    def __init__(self, sandbox: DockerSandbox) -> None:
        self._sandbox = sandbox
        self._root: Path = sandbox.workspace_dir

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def id(self) -> str:
        return str(self._sandbox.session_id)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        """Resolve a path (absolute or relative) to inside workspace_dir."""
        clean = path.lstrip("/")
        resolved = (self._root / clean).resolve()
        if not str(resolved).startswith(str(self._root.resolve())):
            raise ValueError(f"Path traversal blocked: {path!r}")
        return resolved

    def _rel(self, p: Path) -> str:
        return str(p.relative_to(self._root))

    # ------------------------------------------------------------------
    # execute — runs in Docker container
    # ------------------------------------------------------------------

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        output = self._sandbox.bash(command)
        truncated = len(output) > 50_000
        return ExecuteResponse(output=output[:50_000], exit_code=None, truncated=truncated)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self.execute(command, timeout=timeout)

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        try:
            p = self._resolve(file_path)
        except ValueError as e:
            return ReadResult(error=str(e))
        if not p.exists():
            return ReadResult(error=f"File '{file_path}' not found")
        if not p.is_file():
            return ReadResult(error=f"'{file_path}' is not a file")
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            if offset >= len(lines) and len(lines) > 0:
                return ReadResult(error=f"Line offset {offset} exceeds file length ({len(lines)} lines)")
            selected = lines[offset: offset + limit]
            return ReadResult(file_data=FileData(content="\n".join(selected), encoding="utf-8"))
        except Exception as exc:
            return ReadResult(error=f"Error reading file '{file_path}': {exc}")

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self.read(file_path, offset, limit)

    # ------------------------------------------------------------------
    # write (always overwrites — sandbox semantic, unlike FilesystemBackend)
    # ------------------------------------------------------------------

    def write(self, file_path: str, content: str) -> WriteResult:
        try:
            p = self._resolve(file_path)
        except ValueError as e:
            return WriteResult(path=None, error=str(e), files_update=None)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return WriteResult(path=file_path, error=None, files_update=None)
        except Exception as exc:
            return WriteResult(path=None, error=f"Error writing '{file_path}': {exc}", files_update=None)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self.write(file_path, content)

    # ------------------------------------------------------------------
    # edit (string replace)
    # ------------------------------------------------------------------

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        try:
            p = self._resolve(file_path)
        except ValueError as e:
            return EditResult(path=None, error=str(e), files_update=None, occurrences=None)
        if not p.exists():
            return EditResult(path=None, error=f"File '{file_path}' not found", files_update=None, occurrences=None)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            count = text.count(old_string)
            if count == 0:
                return EditResult(path=None, error="old_string not found in file", files_update=None, occurrences=0)
            updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
            p.write_text(updated, encoding="utf-8")
            return EditResult(path=file_path, error=None, files_update=None, occurrences=count if replace_all else 1)
        except Exception as exc:
            return EditResult(path=None, error=f"Error editing '{file_path}': {exc}", files_update=None, occurrences=None)

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return self.edit(file_path, old_string, new_string, replace_all)

    # ------------------------------------------------------------------
    # ls
    # ------------------------------------------------------------------

    def ls(self, path: str) -> LsResult:
        try:
            p = self._resolve(path)
        except ValueError as e:
            return LsResult(error=str(e))
        if not p.exists():
            return LsResult(error=f"Path not found: {path}")
        if not p.is_dir():
            return LsResult(error=f"'{path}' is not a directory")
        try:
            entries = []
            for child in sorted(p.iterdir()):
                rel = self._rel(child)
                if child.is_dir():
                    entries.append({"path": rel + "/", "is_dir": True})
                else:
                    entries.append({"path": rel, "is_dir": False})
            return LsResult(entries=entries)
        except Exception as exc:
            return LsResult(error=f"Error listing '{path}': {exc}")

    async def als(self, path: str) -> LsResult:
        return self.ls(path)

    # ------------------------------------------------------------------
    # glob
    # ------------------------------------------------------------------

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        try:
            base = self._resolve(path)
        except ValueError as e:
            return GlobResult(error=str(e))
        try:
            matches = []
            for p in sorted(base.glob(pattern)):
                rel = self._rel(p)
                matches.append({"path": rel + "/" if p.is_dir() else rel, "is_dir": p.is_dir()})
            return GlobResult(matches=matches)
        except Exception as exc:
            return GlobResult(error=f"Error globbing '{pattern}': {exc}")

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        return self.glob(pattern, path)

    # ------------------------------------------------------------------
    # grep (literal string search on host, no container)
    # ------------------------------------------------------------------

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        try:
            base = self._resolve(path or "/")
        except ValueError as e:
            return GrepResult(error=str(e))
        try:
            cmd = ["grep", "-r", "-n", "--text", pattern, str(base)]
            if glob:
                cmd += ["--include", glob]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            matches = []
            for line in result.stdout.strip().splitlines():
                # format: path:line_num:text
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    raw_path, line_num_str, text = parts
                    try:
                        # Make path relative to workspace
                        abs_p = Path(raw_path)
                        rel = str(abs_p.relative_to(self._root))
                    except ValueError:
                        rel = raw_path
                    matches.append({"path": rel, "line": int(line_num_str), "text": text})
            return GrepResult(matches=matches)
        except subprocess.TimeoutExpired:
            return GrepResult(error="grep timed out")
        except Exception as exc:
            return GrepResult(error=f"Error grepping: {exc}")

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        return self.grep(pattern, path, glob)

    # ------------------------------------------------------------------
    # upload / download
    # ------------------------------------------------------------------

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        results = []
        for name, data in files:
            try:
                p = self._resolve(name)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                results.append(FileUploadResponse(path=self._rel(p), error=None))
            except ValueError:
                results.append(FileUploadResponse(path=name, error="invalid_path"))
            except Exception:
                results.append(FileUploadResponse(path=name, error="permission_denied"))
        return results

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self.upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        results = []
        for path in paths:
            try:
                p = self._resolve(path)
                if not p.exists():
                    results.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
                elif p.is_dir():
                    results.append(FileDownloadResponse(path=path, content=None, error="is_directory"))
                else:
                    results.append(FileDownloadResponse(path=path, content=p.read_bytes(), error=None))
            except ValueError:
                results.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
        return results

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self.download_files(paths)
