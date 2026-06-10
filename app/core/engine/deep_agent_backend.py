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
    from app.core.infra.sandbox import DockerSandbox

_DEFAULT_TEXT_READ_LIMIT = 300
_MAX_TEXT_READ_LIMIT = 500


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
        # Allowed extra root: per-session shared dir (cross-subagent collaboration).
        # When sandbox is a subagent, shared/ inside workspace is a symlink that resolves
        # outside _root, so traversal check must accept this specific target too.
        self._shared_root: Path | None = (
            sandbox.shared_dir if getattr(sandbox, "parent_session_id", None) else None
        )

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
        """Resolve a path (absolute or relative) to inside workspace_dir.

        Inside Docker containers the workspace is mounted at /workspace.
        Strip that prefix so host-side paths resolve correctly:
          /workspace        → self._root
          /workspace/a.txt  → self._root/a.txt
          /             → self._root
        """
        clean = path.lstrip("/")
        # Map container path /workspace/* to the actual host workspace root
        if clean == "workspace" or clean.startswith("workspace/"):
            clean = clean[len("workspace"):].lstrip("/")
        resolved = (self._root / clean).resolve()
        root_resolved = self._root.resolve()
        try:
            resolved.relative_to(root_resolved)
            return resolved
        except ValueError:
            pass
        if self._shared_root is not None:
            shared_resolved = self._shared_root.resolve()
            try:
                resolved.relative_to(shared_resolved)
                return resolved
            except ValueError:
                pass
        raise ValueError(f"Path traversal blocked: {path!r}")

    def _rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self._root))
        except ValueError:
            if self._shared_root is not None:
                try:
                    rel = p.relative_to(self._shared_root.resolve())
                    return str(Path("shared") / rel)
                except ValueError:
                    pass
            return str(p)

    # ------------------------------------------------------------------
    # execute — runs in Docker container
    # ------------------------------------------------------------------

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        output, exit_code = self._sandbox.bash_result(command, timeout=timeout)
        truncated = len(output) > 50_000
        return ExecuteResponse(output=output[:50_000], exit_code=exit_code, truncated=truncated)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        output, exit_code = await self._sandbox.abash_result(command, timeout=timeout)
        truncated = len(output) > 50_000
        return ExecuteResponse(output=output[:50_000], exit_code=exit_code, truncated=truncated)

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    # Extensions whose content the SDK wraps in a binary content block (type != "text").
    # Reading these via read_file would create a ToolMessage with {"type": "file/image/audio",
    # "base64": ..., "mime_type": ...} lacking a "filename" field, causing OpenRouter 400.
    # Return an error instead so the SDK falls back to a plain string result.
    _BINARY_EXTENSIONS = frozenset({
        ".pdf", ".ppt", ".pptx",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp",
        ".mp3", ".wav", ".ogg", ".m4a", ".flac",
        ".mp4", ".mov", ".avi", ".webm",
        ".zip", ".tar", ".gz", ".bin", ".exe",
    })

    def read(self, file_path: str, offset: int = 0, limit: int = _DEFAULT_TEXT_READ_LIMIT) -> ReadResult:
        try:
            p = self._resolve(file_path)
        except ValueError as e:
            return ReadResult(error=str(e))
        if not p.exists():
            return ReadResult(error=f"File '{file_path}' not found")
        if not p.is_file():
            return ReadResult(error=f"'{file_path}' is not a file")
        ext = p.suffix.lower()
        if ext in self._BINARY_EXTENSIONS:
            return ReadResult(
                error=(
                    f"Binary file '{file_path}' cannot be read as text. "
                    f"Use execute() to process it — e.g. `python3 -c \"import PyPDF2; ...\"` for PDFs, "
                    f"or `base64 {file_path}` to get raw bytes."
                )
            )
        try:
            if limit <= 0:
                limit = _DEFAULT_TEXT_READ_LIMIT
            limit = min(limit, _MAX_TEXT_READ_LIMIT)
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            if offset >= len(lines) and len(lines) > 0:
                return ReadResult(error=f"Line offset {offset} exceeds file length ({len(lines)} lines)")
            selected = lines[offset: offset + limit]
            return ReadResult(file_data=FileData(content="\n".join(selected), encoding="utf-8"))
        except Exception as exc:
            return ReadResult(error=f"Error reading file '{file_path}': {exc}")

    async def aread(self, file_path: str, offset: int = 0, limit: int = _DEFAULT_TEXT_READ_LIMIT) -> ReadResult:
        return self.read(file_path, offset, limit)

    # ------------------------------------------------------------------
    # write (create-only per BackendProtocol spec — use edit() to modify)
    # ------------------------------------------------------------------

    def write(self, file_path: str, content: str) -> WriteResult:
        try:
            p = self._resolve(file_path)
        except ValueError as e:
            return WriteResult(path=None, error=str(e), files_update=None)
        if p.exists():
            return WriteResult(path=None, error=f"File '{file_path}' already exists. Use edit() to modify it.", files_update=None)
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
            cmd = ["grep", "-F", "-r", "-n", "--text"]
            if glob:
                cmd += ["--include", glob]
            cmd += ["--", pattern, str(base)]
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
