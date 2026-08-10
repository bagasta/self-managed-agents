# Deep Agents SDK — Migration Plan (Phase 1: Planning + Backend)

**Status:** Draft  
**Tanggal:** 2026-04-23  
**Scope:** Phase 1 — Planning (`write_todos`) + Filesystem Backend (DockerBackend)  
**Out of scope:** Sub-agent spawning (Phase 2)

---

## 1. Kondisi Saat Ini

### Masalah

`agent_runner.py` menggunakan `create_deep_agent` hanya sebagai thin wrapper — parameter yang dipass hanya `model`, `tools`, dan `system_prompt`. Fitur-fitur utama Deep Agents SDK tidak dimanfaatkan sama sekali:

| Fitur Deep Agents | Status |
|---|---|
| `write_todos` (planning) | ❌ Tidak aktif — tidak ada `backend` yang dipasang |
| Filesystem tools (`read_file`, `write_file`, `ls`, `glob`, `grep`, `execute`) | ❌ Digantikan manual dengan `build_sandbox_tools()` |
| `backend` parameter | ❌ Tidak diisi — `None` |
| Sub-agent spawning (`subagents`, `task` tool) | ❌ Tidak ada (Phase 2) |

### Root Cause

`write_todos` dan filesystem tools bawaan Deep Agents hanya aktif kalau `backend` diisi. Tanpa `backend`, Deep Agents berjalan persis seperti `create_react_agent` LangGraph biasa — tidak ada planning, tidak ada filesystem native.

```python
# Kondisi sekarang — sama saja dengan LangGraph biasa
graph = create_deep_agent(
    model=llm,
    tools=tools,
    system_prompt=system_prompt,
    # backend=None  ← ini masalahnya
)
```

---

## 2. Target Setelah Phase 1

```python
graph = create_deep_agent(
    model=llm,
    tools=tools,              # hanya custom tools: memory, escalation, http, wa, mcp
    system_prompt=system_prompt,
    backend=DockerBackend(sandbox),   # ← NEW: aktifkan filesystem + planning
)
```

Efek yang diharapkan:
- Agent otomatis bisa pakai `write_todos` untuk decompose task kompleks
- Filesystem tools (`read_file`, `write_file`, `edit_file`, `ls`, `glob`, `grep`, `execute`) disediakan oleh Deep Agents via backend — tidak perlu custom tools lagi
- `build_sandbox_tools()` di `agent_runner.py` dihapus
- Kode lebih bersih, tidak redundan

---

## 3. Interface BackendProtocol

Deep Agents SDK menggunakan `BackendProtocol` dari `deepagents.backends`. Semua method harus diimplementasikan (sync + async):

```python
class BackendProtocol:
    # File read
    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult: ...
    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult: ...

    # File write (overwrite)
    def write(self, file_path: str, content: str) -> WriteResult: ...
    async def awrite(self, file_path: str, content: str) -> WriteResult: ...

    # File edit (string replace)
    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult: ...
    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult: ...

    # Directory listing
    def ls(self, path: str) -> LsResult: ...
    async def als(self, path: str) -> LsResult: ...

    # Glob pattern
    def glob(self, pattern: str, path: str = "/") -> GlobResult: ...
    async def aglob(self, pattern: str, path: str = "/") -> GlobResult: ...

    # Grep search
    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult: ...
    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult: ...

    # File upload/download
    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]: ...
    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]: ...

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]: ...
    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]: ...
```

> **Catatan:** `execute` (shell command) adalah bagian dari `SandboxBackendProtocol`, subclass dari `BackendProtocol`. Kita perlu implement ini juga agar `bash`/`execute` tool aktif.

```python
class SandboxBackendProtocol(BackendProtocol):
    def execute(self, cmd: str, timeout: int = 60) -> ExecuteResult: ...
    async def aexecute(self, cmd: str, timeout: int = 60) -> ExecuteResult: ...
```

---

## 4. File yang Dibuat / Dimodifikasi

### 4.1 File Baru: `app/core/deep_agent_backend.py`

Implement `DockerBackend` yang wrap `DockerSandbox` ke interface `SandboxBackendProtocol`.

**Tanggung jawab:**
- Semua file operations (`read`, `write`, `edit`, `ls`, `glob`, `grep`) → operasi langsung ke `workspace_dir` di host (tanpa container, sama seperti `DockerSandbox` saat ini)
- `execute` → jalankan command di Docker container via `DockerSandbox.bash()`
- `upload_files` / `download_files` → baca/tulis bytes dari/ke `workspace_dir`
- Path handling: semua path relatif di-resolve ke `/workspace/` agar konsisten dengan behavior sekarang

**Sketsa implementasi:**

```python
# app/core/deep_agent_backend.py

from pathlib import Path
from deepagents.backends import SandboxBackendProtocol
from deepagents.backends.types import (
    ReadResult, WriteResult, EditResult, LsResult,
    GlobResult, GrepResult, ExecuteResult,
    FileUploadResponse, FileDownloadResponse,
)
from app.core.sandbox import DockerSandbox


class DockerBackend(SandboxBackendProtocol):
    """
    Adapter antara DockerSandbox dan Deep Agents BackendProtocol.
    File ops langsung ke host workspace_dir, execute via Docker container.
    """

    def __init__(self, sandbox: DockerSandbox) -> None:
        self._sandbox = sandbox
        self._root = sandbox.workspace_dir  # Path ke /tmp/agent-sandboxes/{session_id}/

    def _resolve(self, path: str) -> Path:
        """Resolve path relatif ke workspace_dir, cegah path traversal."""
        clean = path.lstrip("/").replace("..", "")
        resolved = (self._root / clean).resolve()
        if not str(resolved).startswith(str(self._root)):
            raise ValueError(f"Path traversal detected: {path}")
        return resolved

    # --- execute ---
    def execute(self, cmd: str, timeout: int = 60) -> ExecuteResult:
        output = self._sandbox.bash(cmd)
        return ExecuteResult(output=output)

    async def aexecute(self, cmd: str, timeout: int = 60) -> ExecuteResult:
        return self.execute(cmd, timeout)

    # --- read ---
    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        p = self._resolve(file_path)
        if not p.exists():
            return ReadResult(content="", error=f"File not found: {file_path}")
        lines = p.read_text(errors="replace").splitlines()
        selected = lines[offset : offset + limit]
        return ReadResult(content="\n".join(selected))

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self.read(file_path, offset, limit)

    # --- write ---
    def write(self, file_path: str, content: str) -> WriteResult:
        p = self._resolve(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return WriteResult(path=str(p))

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self.write(file_path, content)

    # --- edit ---
    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        p = self._resolve(file_path)
        if not p.exists():
            return EditResult(success=False, error=f"File not found: {file_path}")
        text = p.read_text(errors="replace")
        if old_string not in text:
            return EditResult(success=False, error="old_string not found in file")
        updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        p.write_text(updated)
        return EditResult(success=True)

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return self.edit(file_path, old_string, new_string, replace_all)

    # --- ls ---
    def ls(self, path: str) -> LsResult:
        p = self._resolve(path)
        if not p.exists():
            return LsResult(entries=[], error=f"Path not found: {path}")
        entries = [e.name + ("/" if e.is_dir() else "") for e in sorted(p.iterdir())]
        return LsResult(entries=entries)

    async def als(self, path: str) -> LsResult:
        return self.ls(path)

    # --- glob ---
    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        base = self._resolve(path)
        matches = [str(p.relative_to(self._root)) for p in base.glob(pattern)]
        return GlobResult(matches=matches)

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        return self.glob(pattern, path)

    # --- grep ---
    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        import subprocess
        base = str(self._resolve(path or "/"))
        cmd = ["grep", "-r", "-n", pattern, base]
        if glob:
            cmd += ["--include", glob]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        lines = result.stdout.strip().splitlines()
        return GrepResult(matches=lines)

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        return self.grep(pattern, path, glob)

    # --- upload / download ---
    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        results = []
        for name, data in files:
            p = self._resolve(name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            results.append(FileUploadResponse(path=str(p.relative_to(self._root))))
        return results

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self.upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        results = []
        for path in paths:
            p = self._resolve(path)
            data = p.read_bytes() if p.exists() else b""
            results.append(FileDownloadResponse(path=path, content=data))
        return results

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self.download_files(paths)
```

---

### 4.2 File Dimodifikasi: `app/core/agent_runner.py`

**Perubahan 1 — Hapus `build_sandbox_tools()`** (sekitar baris 83–111):
- Seluruh fungsi ini dihapus karena filesystem tools sekarang disediakan oleh Deep Agents via `DockerBackend`

**Perubahan 2 — Hapus pemanggilan sandbox tools dari `run_agent()`** (sekitar baris 790–792):
```python
# HAPUS ini:
if sandbox is not None:
    tools.extend(build_sandbox_tools(sandbox))
    active_groups.append("sandbox")
```

**Perubahan 3 — Pass `backend` ke `create_deep_agent()`** (sekitar baris 989–998):
```python
# SEBELUM:
graph = create_deep_agent(
    model=llm,
    tools=tools,
    system_prompt=system_prompt,
)

# SESUDAH:
from app.core.deep_agent_backend import DockerBackend

backend = DockerBackend(sandbox) if sandbox is not None else None

graph = create_deep_agent(
    model=llm,
    tools=tools,
    system_prompt=system_prompt,
    backend=backend,
)
```

**Perubahan 4 — Update fallback** (jika `deepagents` tidak ter-install):
```python
# Fallback tetap sama, tapi tanpa backend support
except (ImportError, TypeError):
    from langgraph.prebuilt import create_react_agent
    graph = create_react_agent(llm, tools=tools, prompt=system_prompt)
```

---

## 5. Tools yang Tetap Dipertahankan (custom)

Berikut tools yang **tidak** digantikan oleh Deep Agents backend dan tetap dipass via `tools` parameter:

| Tool Group | Alasan tetap custom |
|---|---|
| `memory` (remember/recall/forget) | Terhubung ke PostgreSQL `agent_memories` table |
| `skills` (create_skill/use_skill/list_skills) | Terhubung ke PostgreSQL `agent_skills` table |
| `tool_creator` (create_tool/run_custom_tool) | Logic khusus: auto pip install, parameter extraction |
| `escalation` (escalate_to_human/reply_to_user/send_to_number) | Logic draft→confirm→send flow |
| `http` (http_get/http_post/http_patch/http_delete) | External HTTP calls |
| `whatsapp_media` (send_whatsapp_image/send_whatsapp_document) | WA-specific integration |
| `scheduler` (set_reminder/list_reminders/cancel_reminder) | APScheduler + DB |
| `mcp` | External MCP servers |

> `sandbox_write_binary_file` (base64 binary write) tidak ada padanannya di `BackendProtocol`. Tetap perlu dipertahankan sebagai custom tool atau implementasikan via `upload_files` di backend.

---

## 6. Potensi Masalah & Mitigasi

| Risiko | Mitigasi |
|---|---|
| Exact type names `ReadResult`, `WriteResult`, dll belum dikonfirmasi | Cek `deepagents.backends.types` saat implementasi, sesuaikan nama |
| `SandboxBackendProtocol` mungkin nama class berbeda di versi actual | Fallback: extend `BackendProtocol` saja, implement `execute` manual |
| Deep Agents filesystem tools menggunakan path convention berbeda | Semua path di-resolve ke `workspace_dir`, strip leading `/workspace/` kalau perlu |
| `write_todos` tidak muncul di tool list agent | Hanya aktif saat `backend` diisi — konfirmasi di log setelah implementasi |
| `sandbox_write_binary_file` hilang | Tambahkan sebagai custom tool terpisah atau encode ke `write()` dengan base64 decode di dalam backend |

---

## 7. Urutan Implementasi

1. **Cek exact API** — `pip install deepagents`, lalu `python -c "from deepagents.backends import *; help(...)"` untuk konfirmasi nama class dan method
2. **Buat `app/core/deep_agent_backend.py`** — implementasi `DockerBackend` sesuai hasil cek
3. **Hapus `build_sandbox_tools()`** dari `agent_runner.py`
4. **Pass `backend` ke `create_deep_agent()`** di `run_agent()`
5. **Test manual** — kirim pesan ke agent dengan `sandbox: true` di `tools_config`, verifikasi:
   - `write_todos` muncul di log tool calls saat task kompleks
   - File read/write via filesystem tools bawaan Deep Agents bekerja
   - `execute` / `bash` command berjalan di Docker container
6. **Update `requirements.txt`** — pastikan `deepagents>=0.5.0` ada (sudah ada, tinggal konfirmasi versi)

---

## 8. Cara Verifikasi Planning Aktif

Setelah implementasi, kirim pesan seperti:

> *"Buatkan website sederhana dengan HTML, CSS, dan JS. Buat file-filenya di workspace."*

Kalau planning aktif, di log akan terlihat agent memanggil `write_todos` terlebih dahulu sebelum mulai eksekusi:

```
agent_step.tool_call  tool=write_todos  input={"todos": ["Buat index.html", "Buat style.css", "Buat script.js"]}
agent_step.tool_call  tool=write_file   input={"path": "index.html", ...}
...
```

Tanpa planning (kondisi sekarang), agent langsung eksekusi tanpa decompose task.
