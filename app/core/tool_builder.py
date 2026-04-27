"""
tool_builder.py — Factory functions untuk semua tool yang di-inject ke agent.

Dipecah dari agent_runner.py (item 2.1 production plan).

Daftar fungsi:
  build_sandbox_binary_tool(sandbox)
  build_memory_tools(agent_id, db, scope)
  build_skill_tools(agent_id, db)
  build_tool_creator_tools(agent_id, db, sandbox)
  build_loaded_custom_tools(custom_tools_db, sandbox)
  build_whatsapp_media_tools(session, sandbox)
  build_wa_agent_manager_tools(session)
  build_http_tools(tools_config)
  _is_enabled(tools_config, key, default)   ← re-exported untuk backward compat
"""
from __future__ import annotations

import ast
import json
import sys
import uuid
from typing import Any, Optional

from langchain_core.tools import tool, StructuredTool
from pydantic import Field, create_model

from app.core.custom_tool_service import create_or_update_custom_tool, list_custom_tools
from app.core.memory_service import (
    build_memory_context as _build_memory_context,
    delete_memory,
    extract_long_term_memory,
    get_memory,
    list_memories,
    upsert_memory,
)
from app.core.sandbox import DockerSandbox
from app.core.skill_service import (
    create_or_update_skill,
    get_skill,
    list_skills as _list_skills,
)
from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_STDLIB_MODULES = set(sys.stdlib_module_names)


def _is_enabled(tools_config: dict[str, Any], key: str, default: bool = False) -> bool:
    cfg = tools_config.get(key)
    if cfg is None:
        return default
    if isinstance(cfg, bool):
        return cfg
    if isinstance(cfg, dict):
        return bool(cfg.get("enabled", default))
    return default


# ---------------------------------------------------------------------------
# Sandbox tools
# ---------------------------------------------------------------------------

def build_sandbox_binary_tool(sandbox: DockerSandbox) -> list:
    @tool
    def sandbox_write_binary_file(path: str, base64_content: str) -> str:
        """Decode a base64 string and write it as a binary file in the Docker sandbox workspace (/workspace/).
        Args: path (e.g. 'output.png'), base64_content (raw base64 string without data URI prefix)."""
        return sandbox.write_binary_file(path, base64_content)

    return [sandbox_write_binary_file]


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------

def build_memory_tools(agent_id: uuid.UUID, db: AsyncSession, scope: str | None = None) -> list:
    @tool
    async def remember(key: str, value: str) -> str:
        """Store or update a fact in long-term memory. Args: key (short label), value (text to remember)."""
        await upsert_memory(agent_id, key, value, db, scope=scope)
        return f"Remembered: {key} = {value}"

    @tool
    async def recall(query: str) -> str:
        """Retrieve a memory entry by its key. Args: query (the key to look up)."""
        mem = await get_memory(agent_id, query, db, scope=scope)
        if mem:
            return f"{mem.key}: {mem.value_data}"
        all_mems = await list_memories(agent_id, db, scope=scope)
        if not all_mems:
            return "No memories stored yet."
        keys = ", ".join(m.key for m in all_mems)
        return f"No memory found for '{query}'. Available keys: {keys}"

    @tool
    async def forget(key: str) -> str:
        """Delete a memory entry by key. Args: key (the key to remove)."""
        deleted = await delete_memory(agent_id, key, db, scope=scope)
        return f"Forgotten: {key}" if deleted else f"No memory found for key '{key}'"

    return [remember, recall, forget]


# ---------------------------------------------------------------------------
# Skill tools
# ---------------------------------------------------------------------------

def build_skill_tools(agent_id: uuid.UUID, db: AsyncSession) -> list:
    @tool
    async def create_skill(name: str, description: str, content_md: str) -> str:
        """Save a reusable skill (instruction/prompt block) to the skill library.
        Args: name (unique short identifier), description (what it does), content_md (full instructions in markdown)."""
        skill = await create_or_update_skill(agent_id, name, description, content_md, db)
        return f"Skill '{skill.name}' saved successfully."

    @tool
    async def list_skills() -> str:
        """List all available skills for this agent."""
        skills = await _list_skills(agent_id, db)
        if not skills:
            return "No skills saved yet."
        lines = [f"- **{s.name}**: {s.description}" for s in skills]
        return "Available skills:\n" + "\n".join(lines)

    @tool
    async def use_skill(name: str) -> str:
        """Load and return the full content of a skill by name to use in current context.
        Args: name (the skill identifier)."""
        skill = await get_skill(agent_id, name, db)
        if not skill:
            return f"No skill found with name '{name}'"
        return f"# Skill: {skill.name}\n\n{skill.content_md}"

    return [create_skill, list_skills, use_skill]


# ---------------------------------------------------------------------------
# Tool Creator tools
# ---------------------------------------------------------------------------

def _extract_ast_params(code: str, func_name: str) -> list[tuple[str, bool]]:
    """Return list of (param_name, has_default) for the named function."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            args = node.args
            n_args = len(args.args)
            n_defaults = len(args.defaults)
            result = []
            for i, arg in enumerate(args.args):
                if arg.arg == "self":
                    continue
                has_default = i >= (n_args - n_defaults)
                result.append((arg.arg, has_default))
            return result
    return []


def _pip_prefix(code: str) -> str:
    """
    Parse top-level imports from code and return a pip install command for
    any non-stdlib packages, or empty string if none needed.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    packages: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in _STDLIB_MODULES:
                    packages.append(top)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top not in _STDLIB_MODULES:
                    packages.append(top)
    if not packages:
        return ""
    pkg_list = " ".join(dict.fromkeys(packages))
    return f"pip install --quiet --root-user-action=ignore {pkg_list} && "


def build_tool_creator_tools(agent_id: uuid.UUID, db: AsyncSession, sandbox: DockerSandbox) -> list:
    @tool
    async def create_tool(name: str, description: str, python_code: str) -> str:
        """Save a new Python tool for this agent. The code must define a function with the same name as `name`.
        Args: name (function name, snake_case), description (what it does), python_code (valid Python code)."""
        ct, err = await create_or_update_custom_tool(agent_id, name, description, python_code, db)
        if err:
            return f"[error] Could not save tool: {err}"
        return f"Tool '{name}' saved successfully. It will be available in future sessions."

    @tool
    async def list_tools() -> str:
        """List all custom tools created by this agent."""
        tools = await list_custom_tools(agent_id, db)
        if not tools:
            return "No custom tools created yet."
        lines = [f"- **{t.name}**: {t.description}" for t in tools]
        return "Custom tools:\n" + "\n".join(lines)

    @tool
    async def run_custom_tool(name: str, args_json: str = "{}") -> str:
        """Execute a saved custom tool by running its Python code in the sandbox.
        IMPORTANT: If you just created a new tool using create_tool, use this to execute it
        in the current session.
        Args:
          name      : tool name (as given to create_tool)
          args_json : JSON object string with the keyword arguments required by the tool function.
                      Call list_tools() first to see available tools, then inspect the tool's
                      function signature to know which keys are required.
                      Example: '{"content": "Hello", "filename": "out.pdf"}'"""
        tools = await list_custom_tools(agent_id, db)
        tool_map = {t.name: t for t in tools}
        if name not in tool_map:
            available = ", ".join(tool_map.keys()) or "none"
            return f"[error] No custom tool named '{name}'. Available: {available}"
        ct = tool_map[name]

        try:
            args = json.loads(args_json)
        except json.JSONDecodeError as e:
            return f"[error] Invalid args_json (must be a JSON object string): {e}"

        if not args:
            required_params = [p for p, has_def in _extract_ast_params(ct.code, name) if not has_def]
            if required_params:
                example = json.dumps({p: "..." for p in required_params})
                return (
                    f"[error] Tool '{name}' requires arguments: {required_params}. "
                    f"Pass them as args_json, e.g. args_json='{example}'"
                )

        args_json_str = json.dumps(json.dumps(args))
        runner_code = f"""{ct.code}

if __name__ == "__main__":
    import json, inspect as _inspect
    _all_args = json.loads({args_json_str})
    _sig = _inspect.signature({name})
    _params = _sig.parameters
    _has_var_kw = any(p.kind == _inspect.Parameter.VAR_KEYWORD for p in _params.values())
    _filtered = _all_args if _has_var_kw else {{k: v for k, v in _all_args.items() if k in _params}}
    result = {name}(**_filtered)
    print(json.dumps({{"result": result}}) if result is not None else "null")
"""
        sandbox.write_file(f"_custom_tool_{name}.py", runner_code)
        pip_cmd = _pip_prefix(ct.code)
        return sandbox.bash(f"{pip_cmd}python /workspace/_custom_tool_{name}.py")

    return [create_tool, list_tools, run_custom_tool]


# ---------------------------------------------------------------------------
# Loaded custom tools (previously saved)
# ---------------------------------------------------------------------------

def build_loaded_custom_tools(custom_tools_db: list, sandbox: DockerSandbox) -> list:
    """
    Build LangChain tools from saved custom tools, exposing the real function
    parameters so the LLM knows exactly what arguments to pass.
    """
    lc_tools = []
    for ct in custom_tools_db:
        lc_tools.append(_make_custom_tool_runner(ct.name, ct.code, ct.description, sandbox))
    return lc_tools


def _make_custom_tool_runner(ct_name: str, ct_code: str, ct_desc: str, sandbox: DockerSandbox):
    """Build a StructuredTool with the real parameters from the custom tool function."""
    params = _extract_ast_params(ct_code, ct_name)

    def _build_runner_code(kwargs: dict) -> str:
        args_json_str = json.dumps(json.dumps(kwargs))
        return f"""{ct_code}

if __name__ == "__main__":
    import json, inspect as _inspect
    _all_args = json.loads({args_json_str})
    _sig = _inspect.signature({ct_name})
    _params = _sig.parameters
    _has_var_kw = any(p.kind == _inspect.Parameter.VAR_KEYWORD for p in _params.values())
    _filtered = _all_args if _has_var_kw else {{k: v for k, v in _all_args.items() if k in _params}}
    result = {ct_name}(**_filtered)
    print(json.dumps({{"result": result}}) if result is not None else "null")
"""

    def _execute(**kwargs) -> str:
        runner_code = _build_runner_code(kwargs)
        sandbox.write_file(f"_custom_tool_{ct_name}.py", runner_code)
        pip_cmd = _pip_prefix(ct_code)
        return sandbox.bash(f"{pip_cmd}python /workspace/_custom_tool_{ct_name}.py")

    if params:
        fields: dict = {}
        for pname, has_default in params:
            if has_default:
                fields[pname] = (Optional[str], Field(default=None, description=pname))
            else:
                fields[pname] = (str, Field(..., description=pname))
        SchemaModel = create_model(f"_{ct_name}_schema", **fields)
        return StructuredTool.from_function(
            func=_execute,
            name=ct_name,
            description=ct_desc,
            args_schema=SchemaModel,
        )

    return StructuredTool.from_function(
        func=lambda: _execute(),
        name=ct_name,
        description=ct_desc,
    )


# ---------------------------------------------------------------------------
# WhatsApp media tools
# ---------------------------------------------------------------------------

def build_whatsapp_media_tools(session: Any, sandbox: DockerSandbox | None) -> list:
    """
    Tools untuk mengirim gambar dan dokumen ke WhatsApp.
    send_agent_wa_qr dipisah ke build_wa_agent_manager_tools (opt-in via wa_agent_manager).
    """
    _raw_cfg = session.channel_config
    channel_cfg: dict = _raw_cfg if isinstance(_raw_cfg, dict) else {}
    device_id: str = channel_cfg.get("device_id", "")
    default_target: str = channel_cfg.get("user_phone", "")

    @tool
    async def send_whatsapp_image(
        image_path_or_base64: str,
        caption: str = "",
        phone: str = "",
        mimetype: str = "image/jpeg",
    ) -> str:
        """
        Kirim gambar ke WhatsApp.

        Args:
            image_path_or_base64: Path file di /workspace (misal '/workspace/chart.png') ATAU
                                  string base64 langsung (tanpa prefix 'data:image/...')
            caption             : Teks caption yang menyertai gambar (opsional)
            phone               : Nomor tujuan WA atau JID. Biarkan kosong untuk kirim ke user saat ini.
            mimetype            : MIME type gambar, default 'image/jpeg'
        """
        target = phone or default_target
        if not target:
            return "[error] Tidak ada target nomor WhatsApp — set phone atau pastikan session punya user_phone"
        if not device_id:
            return "[error] Tidak ada device_id WhatsApp pada session ini"

        import base64 as _b64, re as _re

        def _looks_like_base64(s: str) -> bool:
            return len(s) >= 50 and bool(_re.fullmatch(r'[A-Za-z0-9+/]+=*', s))

        if _looks_like_base64(image_path_or_base64):
            image_b64 = image_path_or_base64.strip()
        else:
            if sandbox is None:
                return "[error] Tool send_whatsapp_image membutuhkan sandbox aktif untuk membaca file. Gunakan base64 langsung atau aktifkan sandbox."
            path = image_path_or_base64 if image_path_or_base64.startswith("/workspace/") else f"/workspace/{image_path_or_base64}"
            b64_output = sandbox.bash(f"base64 -w 0 {path} 2>&1")
            if b64_output.startswith("[") or "No such file" in b64_output:
                return f"[error] Gagal membaca file: {b64_output}"
            image_b64 = b64_output.strip()

        try:
            from app.core.wa_client import send_wa_image
            await send_wa_image(device_id, target, image_b64, caption, mimetype)
            return f"[IMAGE_SENT] Gambar dikirim ke {target}" + (f" dengan caption: {caption}" if caption else "")
        except Exception as exc:
            return f"[error] Gagal kirim gambar: {exc}"

    @tool
    async def send_whatsapp_document(
        file_path_or_base64: str,
        filename: str = "file",
        caption: str = "",
        phone: str = "",
        mimetype: str = "",
    ) -> str:
        """
        Kirim dokumen/file ke WhatsApp (PDF, DOCX, XLSX, ZIP, dll).

        Args:
            file_path_or_base64: Path file di /workspace ATAU string base64 langsung
            filename            : Nama file yang akan ditampilkan di WhatsApp
            caption             : Teks caption yang menyertai dokumen (opsional)
            phone               : Nomor tujuan WA atau JID. Biarkan kosong untuk kirim ke user saat ini.
            mimetype            : MIME type file. Jika kosong, otomatis ditentukan dari ekstensi filename.
        """
        target = phone or default_target
        if not target:
            return "[error] Tidak ada target nomor WhatsApp — set phone atau pastikan session punya user_phone"
        if not device_id:
            return "[error] Tidak ada device_id WhatsApp pada session ini"

        if not mimetype and filename:
            import mimetypes
            guessed, _ = mimetypes.guess_type(filename)
            mimetype = guessed or "application/octet-stream"
        elif not mimetype:
            mimetype = "application/octet-stream"

        import base64 as _b64, re as _re

        def _looks_like_base64(s: str) -> bool:
            return len(s) >= 50 and bool(_re.fullmatch(r'[A-Za-z0-9+/]+=*', s))

        if not _looks_like_base64(file_path_or_base64):
            if sandbox is None:
                return "[error] Tool send_whatsapp_document membutuhkan sandbox aktif untuk membaca file."
            path = file_path_or_base64 if file_path_or_base64.startswith("/workspace/") else f"/workspace/{file_path_or_base64}"
            b64_output = sandbox.bash(f"base64 -w 0 {path} 2>&1")
            if b64_output.startswith("[") or "No such file" in b64_output:
                return f"[error] Gagal membaca file: {b64_output}"
            doc_b64 = b64_output.strip()
            if not filename or filename == "file":
                import os as _os
                filename = _os.path.basename(path)
        else:
            doc_b64 = file_path_or_base64.strip()

        try:
            from app.core.wa_client import send_wa_document
            await send_wa_document(device_id, target, doc_b64, filename, caption, mimetype)
            return f"[DOCUMENT_SENT] Dokumen '{filename}' dikirim ke {target}" + (f" dengan caption: {caption}" if caption else "")
        except Exception as exc:
            return f"[error] Gagal kirim dokumen: {exc}"

    return [send_whatsapp_image, send_whatsapp_document]


# ---------------------------------------------------------------------------
# WA Agent Manager tool
# ---------------------------------------------------------------------------

def build_wa_agent_manager_tools(session: Any) -> list:
    """Tool send_agent_wa_qr — hanya untuk agent yang perlu mengelola agent lain (e.g. Arthur)."""
    _raw_cfg = session.channel_config
    channel_cfg: dict = _raw_cfg if isinstance(_raw_cfg, dict) else {}
    device_id: str = channel_cfg.get("device_id", "")
    default_target: str = channel_cfg.get("user_phone", "")

    @tool
    async def send_agent_wa_qr(
        agent_id: str,
        caption: str = "Scan QR code ini untuk menghubungkan WhatsApp ke agent.",
        phone: str = "",
    ) -> str:
        """
        Kirimkan QR code WhatsApp dari sebuah agent ke user.

        Gunakan tool ini SETIAP KALI perlu mengirim QR WhatsApp — baik untuk agent baru
        maupun saat user minta QR baru / QR refresh. Tool ini selalu menggunakan
        wa_device_id yang tersimpan di DB untuk agent tersebut.

        Args:
            agent_id : UUID agent yang QR-nya ingin dikirim (dari response saat agent dibuat)
            caption  : Caption gambar QR (opsional)
            phone    : Nomor tujuan WA. Biarkan kosong untuk kirim ke user saat ini.
        """
        target = phone or default_target
        if not target:
            return "[error] Tidak ada target nomor WhatsApp — set phone atau pastikan session punya user_phone"
        if not device_id:
            return "[error] Tidak ada device_id WhatsApp pada session ini"

        from app.core.wa_client import create_wa_device, get_wa_qr, send_wa_image
        from app.database import get_db as _get_db
        from app.models.agent import Agent as _Agent
        from sqlalchemy import select as _select
        import uuid as _uuid

        try:
            agent_uuid = _uuid.UUID(agent_id)
        except ValueError:
            return f"[error] agent_id tidak valid: '{agent_id}' — harus berupa UUID"

        async for _db in _get_db():
            result = await _db.execute(
                _select(_Agent).where(_Agent.id == agent_uuid, _Agent.is_deleted.is_(False))
            )
            agent_row = result.scalar_one_or_none()
            break

        if not agent_row:
            return f"[error] Agent '{agent_id}' tidak ditemukan"
        if not agent_row.wa_device_id:
            return f"[error] Agent '{agent_id}' tidak memiliki WhatsApp device"

        wa_dev_id: str = agent_row.wa_device_id

        try:
            try:
                resp = await get_wa_qr(wa_dev_id)
            except Exception:
                resp = {"status": "not_found", "qr_image": ""}

            qr_status: str = resp.get("status", "")
            qr_b64: str = resp.get("qr_image", "")

            if qr_status == "connected":
                return f"[INFO] Agent '{agent_id}' sudah terhubung ke WhatsApp — tidak perlu scan QR lagi."

            if not qr_b64:
                resp = await create_wa_device(wa_dev_id)
                qr_status = resp.get("status", "")
                qr_b64 = resp.get("qr_image", "")

                if qr_status == "connected":
                    return f"[INFO] Agent '{agent_id}' sudah terhubung kembali — tidak perlu scan QR."
                if not qr_b64:
                    return (
                        f"[error] Gagal generate QR untuk agent '{agent_id}' (device: {wa_dev_id}). "
                        f"Status: {qr_status}. Coba beberapa saat lagi."
                    )

            if "," in qr_b64:
                qr_b64 = qr_b64.split(",", 1)[1]

            await send_wa_image(device_id, target, qr_b64, caption, "image/png")
            return (
                f"[QR_SENT] QR untuk agent '{agent_id}' dikirim ke {target}. "
                f"Scan sekarang — QR expire dalam ~20 detik. "
                f"Jika belum sempat scan, minta user ketik 'kirim QR baru' dan panggil tool ini lagi."
            )
        except Exception as exc:
            return f"[error] Gagal mengirim QR: {exc}"

    return [send_agent_wa_qr]


# ---------------------------------------------------------------------------
# HTTP tools (thin wrapper)
# ---------------------------------------------------------------------------

def build_http_tools(tools_config: dict[str, Any]) -> list:
    from app.core.tools.http_tool import build_http_tools as _build
    return _build(tools_config)
