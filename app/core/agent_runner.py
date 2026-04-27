"""
Agent runner: wires OpenRouter LLM + tools + memory + RAG, runs the agent via
Deep Agents SDK, persists all steps to DB.

Memory model
------------
Short-term  Last `short_term_memory_turns` user/agent pairs loaded from DB
            into the LLM context window. Older turns are silently dropped.

Long-term   Persistent key-value store (agent_memories table).
            Injected into every system prompt as a markdown block.
            Auto-extracted: every `ltm_extraction_every` user messages,
            the LLM reads recent turns and distils important facts.

RAG context Top-3 documents (cosine-similar to the user query) fetched
            from the vector store and injected into the system prompt.
            Agent does NOT need to call a tool — context is pre-injected.

Tool defaults (conservative)
-----------------------------
ON  by default : memory, skills, escalation
OFF by default : sandbox, tool_creator, scheduler, http, mcp,
                 whatsapp_media, wa_agent_manager
"""
from __future__ import annotations

import ast
import json
import sys
import uuid
from typing import Any, Optional

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool, StructuredTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, create_model
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.custom_tool_service import create_or_update_custom_tool, list_custom_tools
from app.core.memory_service import (
    build_memory_context,
    delete_memory,
    extract_long_term_memory,
    get_memory,
    list_memories,
    upsert_memory,
)
from app.core.sandbox import DockerSandbox
from app.core.skill_service import create_or_update_skill, get_skill, list_skills as _list_skills
from app.models.message import Message
from app.models.session import Session

logger = structlog.get_logger(__name__)
settings = get_settings()


# ---------------------------------------------------------------------------
# tools_config helpers
# ---------------------------------------------------------------------------

def _is_enabled(tools_config: dict[str, Any], key: str, default: bool = False) -> bool:
    cfg = tools_config.get(key)
    if cfg is None:
        return default
    if isinstance(cfg, bool):
        return cfg
    if isinstance(cfg, dict):
        return bool(cfg.get("enabled", default))
    return default


def _normalize_phone(p: str) -> str:
    return p.lstrip("+").split("@")[0]


# ---------------------------------------------------------------------------
# Sandbox tools (only binary write remains — no text equivalent in BackendProtocol)
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

_STDLIB_MODULES = set(sys.stdlib_module_names)


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
# Loaded custom tools (previously saved, available as direct tool calls)
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
# WhatsApp media tools (image + document only; no QR)
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
# WA Agent Manager tool (opt-in: tools_config.wa_agent_manager = true)
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
# Sub-agent builder (Phase 2)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Built-in system sub-agents — hardcoded, no DB dependency
# ---------------------------------------------------------------------------

_SYSTEM_SUBAGENTS: list[dict] = [
    {
        "name": "sys_critic",
        "description": "Quality reviewer: evaluasi output agent lain, approve jika OK atau reject dengan feedback spesifik untuk diperbaiki.",
        "system_prompt": (
            "Kamu adalah agen critic dan quality reviewer. Tugasmu adalah mengevaluasi output yang diberikan kepadamu.\n\n"
            "Cara kerja:\n"
            "1. Baca output yang perlu direview dengan teliti\n"
            "2. Evaluasi berdasarkan: akurasi, kelengkapan, relevansi dengan task, dan kualitas\n"
            "3. Berikan verdict dengan format:\n\n"
            "   **VERDICT: APPROVED** — jika output sudah baik dan bisa digunakan\n"
            "   atau\n"
            "   **VERDICT: REJECTED** — jika output perlu diperbaiki\n\n"
            "4. Jika REJECTED, berikan feedback spesifik: apa yang salah, apa yang kurang, dan apa yang harus diperbaiki\n"
            "5. Jika APPROVED, berikan catatan singkat mengapa output sudah memenuhi standar\n\n"
            "Jadilah kritis tapi konstruktif. Jangan approve output yang mengandung informasi salah, "
            "kode yang error, atau tidak menjawab task dengan benar."
        ),
        "model": "openai/gpt-4o-mini",
        "tools_config": {"sandbox": False, "http": False},
    },
    {
        "name": "sys_researcher",
        "description": "Riset spesialis: cari dan rangkum informasi dari internet via HTTP tools.",
        "system_prompt": (
            "Kamu adalah agen riset spesialis. Tugasmu adalah mencari, mengumpulkan, dan merangkum informasi "
            "dari internet secara akurat dan terstruktur.\n\n"
            "Cara kerja:\n"
            "1. Gunakan http_get untuk mengakses URL dan mencari informasi\n"
            "2. Ringkas temuan dengan jelas dan terstruktur\n"
            "3. Sertakan sumber informasi\n"
            "4. Jika informasi tidak ditemukan, jelaskan apa yang kamu coba dan apa hasilnya\n\n"
            "Selalu kembalikan hasil riset yang lengkap, akurat, dan bisa langsung digunakan."
        ),
        "model": "openai/gpt-4o-mini",
        "tools_config": {"http": {"enabled": True}, "sandbox": False},
    },
    {
        "name": "sys_coder",
        "description": "Programmer Python spesialis: tulis dan jalankan kode di sandbox.",
        "system_prompt": (
            "Kamu adalah agen programmer spesialis Python. Tugasmu adalah menulis, menjalankan, dan men-debug kode "
            "untuk menyelesaikan task komputasi yang diberikan.\n\n"
            "Cara kerja:\n"
            "1. Pahami task yang diminta\n"
            "2. Tulis kode Python yang bersih menggunakan write_file\n"
            "3. Jalankan di sandbox menggunakan execute\n"
            "4. Jika ada error, debug dan perbaiki\n"
            "5. Kembalikan hasil eksekusi beserta penjelasan singkat\n\n"
            "Untuk library eksternal: execute('pip install <package>')"
        ),
        "model": "openai/gpt-4o-mini",
        "tools_config": {"sandbox": True, "http": False},
    },
    {
        "name": "sys_writer",
        "description": "Penulis dan editor spesialis: buat, edit, dan format konten tulisan.",
        "system_prompt": (
            "Kamu adalah agen penulis dan editor spesialis. Tugasmu adalah membuat, mengedit, dan memformat "
            "konten tulisan berkualitas tinggi.\n\n"
            "Kemampuan:\n"
            "- Menulis artikel, laporan, email, proposal, dan konten lainnya\n"
            "- Mengedit dan memperbaiki tulisan yang ada\n"
            "- Mengubah format dan tone tulisan sesuai kebutuhan\n"
            "- Menerjemahkan antara Bahasa Indonesia dan Inggris\n\n"
            "Selalu hasilkan tulisan yang jelas, terstruktur, dan sesuai tone yang diminta."
        ),
        "model": "openai/gpt-4o-mini",
        "tools_config": {"sandbox": False, "http": False},
    },
    {
        "name": "sys_analyst",
        "description": "Analis data spesialis: olah data, kalkulasi, dan buat laporan analisis.",
        "system_prompt": (
            "Kamu adalah agen analis data spesialis. Tugasmu adalah mengolah data, melakukan kalkulasi, "
            "dan membuat laporan analisis.\n\n"
            "Cara kerja:\n"
            "1. Terima data dalam bentuk teks, CSV, JSON, atau format lain\n"
            "2. Tulis kode Python dengan pandas/numpy menggunakan write_file\n"
            "3. Jalankan analisis di sandbox menggunakan execute\n"
            "4. Buat ringkasan temuan dan insight yang actionable\n"
            "5. Format hasil sebagai tabel atau laporan terstruktur\n\n"
            "Install library: execute('pip install pandas numpy')"
        ),
        "model": "openai/gpt-4o-mini",
        "tools_config": {"sandbox": True, "http": False},
    },
]


def _build_system_subagent(spec: dict, parent_session_id: uuid.UUID) -> tuple[dict, DockerSandbox | None]:
    """Build a SubAgent dict and optional DockerSandbox from a system sub-agent spec."""
    sub_cfg = spec.get("tools_config", {})
    sub_tools: list = []
    sub_sandbox: DockerSandbox | None = None

    if _is_enabled(sub_cfg, "sandbox", default=False):
        sub_session_id = f"{parent_session_id}_sys_{spec['name']}"
        sub_sandbox = DockerSandbox(sub_session_id)
        sub_tools.extend(build_sandbox_binary_tool(sub_sandbox))

    if _is_enabled(sub_cfg, "http", default=False):
        sub_tools.extend(build_http_tools(sub_cfg))

    sub_llm = ChatOpenAI(
        model=spec["model"],
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        max_tokens=4096,
        temperature=0.5,
    )

    sa = {
        "name": spec["name"],
        "description": spec["description"],
        "system_prompt": spec["system_prompt"],
        "tools": sub_tools,
        "model": sub_llm,
    }
    return sa, sub_sandbox


async def build_subagents(
    agent_ids: list[str],
    parent_session_id: uuid.UUID,
    db: AsyncSession,
    log: Any,
) -> tuple[list, list[DockerSandbox]]:
    """
    Build SubAgent list for Deep Agents SDK.

    - agent_ids empty → use all hardcoded system sub-agents (no DB dependency)
    - agent_ids provided → load from DB by UUID (custom agents)

    Returns (subagent_list, sandbox_list) — caller must close sandboxes in finally block.
    """
    subagents: list = []
    sub_sandboxes: list[DockerSandbox] = []

    if not agent_ids:
        for spec in _SYSTEM_SUBAGENTS:
            sa, ssb = _build_system_subagent(spec, parent_session_id)
            subagents.append(sa)
            if ssb:
                sub_sandboxes.append(ssb)
        log.info("build_subagents.system_defaults", count=len(subagents))
        return subagents, sub_sandboxes

    from app.models.agent import Agent as AgentModel

    for raw_id in agent_ids:
        try:
            agent_uuid = uuid.UUID(raw_id)
        except ValueError:
            log.warning("build_subagents.invalid_uuid", agent_id=raw_id)
            continue

        try:
            result = await db.execute(
                select(AgentModel).where(
                    AgentModel.id == agent_uuid,
                    AgentModel.is_deleted.is_(False),
                )
            )
            agent_row = result.scalar_one_or_none()
        except Exception as exc:
            log.error("build_subagents.db_error", agent_id=raw_id, error=str(exc))
            continue

        if agent_row is None:
            log.warning("build_subagents.not_found", agent_id=raw_id)
            continue

        sub_cfg: dict[str, Any] = agent_row.tools_config if isinstance(agent_row.tools_config, dict) else {}
        sub_tools: list = []
        sub_sandbox: DockerSandbox | None = None

        # Isolated sandbox for subagent (workspace namespaced under parent)
        if _is_enabled(sub_cfg, "sandbox", default=False):
            sub_session_id = f"{parent_session_id}_sub_{agent_uuid}"
            sub_sandbox = DockerSandbox(sub_session_id)
            sub_sandboxes.append(sub_sandbox)
            sub_tools.extend(build_sandbox_binary_tool(sub_sandbox))

        # Memory — isolated scope: subagent's own agent_id (no cross-contamination)
        if _is_enabled(sub_cfg, "memory", default=True):
            sub_tools.extend(build_memory_tools(agent_row.id, db, scope=None))

        # Skills
        if _is_enabled(sub_cfg, "skills", default=True):
            sub_tools.extend(build_skill_tools(agent_row.id, db))

        # HTTP (opt-in)
        if _is_enabled(sub_cfg, "http", default=False):
            sub_tools.extend(build_http_tools(sub_cfg))

        # Intentionally excluded: escalation, scheduler, wa_agent_manager, tool_creator
        # Subagents do not have channels and should not trigger external side effects.

        # Build DockerBackend for subagent if sandbox is active
        sub_backend = None
        if sub_sandbox is not None:
            from app.core.deep_agent_backend import DockerBackend
            sub_backend = DockerBackend(sub_sandbox)

        sub_llm = ChatOpenAI(
            model=agent_row.model or "openai/gpt-4o-mini",
            api_key=settings.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            max_tokens=4096,
            temperature=getattr(agent_row, "temperature", 0.7),
        )

        sa: dict = {
            "name": agent_row.name,
            "description": (agent_row.instructions or "")[:300].replace("\n", " "),
            "system_prompt": agent_row.instructions or "You are a helpful assistant.",
            "tools": sub_tools,
            "model": sub_llm,
        }

        subagents.append(sa)
        log.info("build_subagents.loaded", name=agent_row.name, tools=len(sub_tools))

    return subagents, sub_sandboxes


# ---------------------------------------------------------------------------
# HTTP tools
# ---------------------------------------------------------------------------

def build_http_tools(tools_config: dict[str, Any]) -> list:
    from app.core.tools.http_tool import build_http_tools as _build
    return _build(tools_config)


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

async def _load_history(
    session_id: uuid.UUID,
    db: AsyncSession,
    max_turns: int | None = None,
) -> list[Message]:
    if max_turns is not None:
        sub = (
            select(Message.id)
            .where(
                Message.session_id == session_id,
                Message.role.in_(["user", "agent"]),
            )
            .order_by(Message.step_index.desc(), Message.timestamp.desc())
            .limit(max_turns * 2)
            .subquery()
        )
        stmt = (
            select(Message)
            .where(Message.id.in_(select(sub.c.id)))
            .order_by(Message.step_index, Message.timestamp)
        )
    else:
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.step_index, Message.timestamp)
        )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _count_user_messages(session_id: uuid.UUID, db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count()).where(
            Message.session_id == session_id,
            Message.role == "user",
        )
    )
    return result.scalar_one()


def _db_messages_to_lc(db_messages: list[Message]) -> list[BaseMessage]:
    result: list[BaseMessage] = []
    for msg in db_messages:
        if msg.role == "user" and msg.content:
            result.append(HumanMessage(content=msg.content))
        elif msg.role == "agent" and msg.content:
            result.append(AIMessage(content=msg.content))
    return result


# ---------------------------------------------------------------------------
# RAG context builder
# ---------------------------------------------------------------------------

async def _build_rag_context(
    agent_id: uuid.UUID,
    user_message: str,
    db: AsyncSession,
    tools_config: dict[str, Any],
    log: Any,
) -> str:
    raw = tools_config.get("rag", {})
    cfg: dict[str, Any] = raw if isinstance(raw, dict) else {}
    max_results: int = int(cfg.get("max_results", 3))

    try:
        from app.core.document_service import (
            search_documents_keyword,
            search_documents_vector,
        )
        from app.core.embedding_service import embed_text

        query_embedding = await embed_text(user_message)
        docs = await search_documents_vector(agent_id, query_embedding, db, max_results)

        if not docs:
            docs = await search_documents_keyword(agent_id, user_message, db, max_results)

        if not docs:
            return ""

        parts: list[str] = []
        for i, doc in enumerate(docs, 1):
            src = f" — *{doc.source}*" if doc.source else ""
            excerpt = doc.content[:1200]
            if len(doc.content) > 1200:
                excerpt += "\n…"
            parts.append(f"**[{i}] {doc.title}**{src}\n{excerpt}")

        context_block = (
            "## Relevant Knowledge Base Context\n"
            "*The following documents were retrieved based on your query. "
            "Use them to inform your answer.*\n\n"
            + "\n\n---\n\n".join(parts)
        )
        log.debug("agent_run.rag_context", docs_found=len(docs))
        return context_block

    except Exception as exc:
        log.warning("agent_run.rag_context_failed", error=str(exc))
        return ""


# ---------------------------------------------------------------------------
# Memory Summarizer (Phase 3)
# ---------------------------------------------------------------------------

_SUMMARY_TRIGGER = 10  # summarize when session has >= this many user messages

async def _maybe_summarize_context(
    session: Any,
    db: AsyncSession,
    llm: Any,
    log: Any,
) -> str:
    """
    If session has >= _SUMMARY_TRIGGER user messages, summarize older turns via LLM
    and cache the result in session.metadata_['context_summary'].
    Returns the summary string (empty if not triggered or failed).
    """
    try:
        user_msg_count = await _count_user_messages(session.id, db)
        if user_msg_count < _SUMMARY_TRIGGER:
            return ""

        meta: dict = session.metadata_ if isinstance(session.metadata_, dict) else {}
        cached_at = meta.get("context_summary_at_msg", 0)

        # Re-summarize every _SUMMARY_TRIGGER messages
        if user_msg_count - cached_at < _SUMMARY_TRIGGER and meta.get("context_summary"):
            log.debug("agent_run.context_summary_cached", user_messages=user_msg_count)
            return meta["context_summary"]

        # Load all history for summarization (no turn limit)
        all_rows = await _load_history(session.id, db)
        if not all_rows:
            return ""

        history_text = "\n".join(
            f"{'User' if m.role == 'user' else 'Agent'}: {(m.content or '')[:500]}"
            for m in all_rows
            if m.role in ("user", "agent") and m.content
        )

        from langchain_core.messages import HumanMessage as _HM
        summary_prompt = (
            "Berikut adalah riwayat percakapan antara user dan agent. "
            "Buat ringkasan padat (maksimal 300 kata) yang mencakup:\n"
            "- Topik utama yang dibahas\n"
            "- Keputusan atau hasil penting yang sudah dicapai\n"
            "- Konteks yang relevan untuk melanjutkan percakapan\n\n"
            f"Riwayat percakapan:\n{history_text[:6000]}"
        )
        resp = await llm.ainvoke([_HM(content=summary_prompt)])
        summary = resp.content if isinstance(resp.content, str) else str(resp.content)

        # Persist to session metadata
        new_meta = {**meta, "context_summary": summary, "context_summary_at_msg": user_msg_count}
        session.metadata_ = new_meta
        db.add(session)
        await db.flush()

        log.info("agent_run.context_summarized", user_messages=user_msg_count, summary_len=len(summary))
        return summary

    except Exception as exc:
        log.warning("agent_run.context_summary_failed", error=str(exc))
        return ""


# ---------------------------------------------------------------------------
# Agent Context Block builder (PRD2 §3.3 + §3.4)
# ---------------------------------------------------------------------------

def _build_agent_context_block(
    agent_model: Any,
    session: Session,
    active_groups: list[str],
    custom_tools_db: list,
    subagent_list: list | None = None,
    sender_name: str | None = None,
) -> str:
    agent_id = session.agent_id
    _raw_cfg = session.channel_config
    _ch_cfg = _raw_cfg if isinstance(_raw_cfg, dict) else {}
    user_phone = _ch_cfg.get("user_phone") or getattr(session, "external_user_id", None) or ""
    channel_type = getattr(session, "channel_type", None) or "api"

    # Operator awareness: check operator_ids list on agent
    operator_ids: list = getattr(agent_model, "operator_ids", None) or []
    if isinstance(operator_ids, list) and user_phone:
        norm_user = _normalize_phone(user_phone)
        is_operator = any(_normalize_phone(oid) == norm_user for oid in operator_ids)
    else:
        is_operator = False
    user_role = "OPERATOR" if is_operator else "user"

    lines = [
        "## Platform Context",
        f"- Agent ID: {agent_id}",
        f"- Agent Name: {agent_model.name}",
        f"- Model: {agent_model.model}",
        f"- Active Tools: {', '.join(active_groups) if active_groups else 'none'}",
    ]

    if custom_tools_db:
        ct_lines = [f"  - {ct.name}: {ct.description}" for ct in custom_tools_db]
        lines.append("- Custom Tools:\n" + "\n".join(ct_lines))

    lines.append(f"- Channel: {channel_type}")
    if user_phone:
        lines.append(f"- Current User Phone: {user_phone}")
    if sender_name:
        lines.append(f"- Current User Name: {sender_name}")
    lines.append(f"- Current User Role: {user_role}")
    lines.append(f"- Session ID: {session.id}")

    if subagent_list:
        lines.append("\n## Available Subagents")
        lines.append(
            "Delegate specific tasks using `task(name=..., task=...)`. "
            "Always use write_todos to plan before delegating."
        )
        for sa in subagent_list:
            lines.append(f"- **{sa['name']}**: {sa['description']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

async def run_agent(
    *,
    agent_model: Any,
    session: Session,
    user_message: str,
    db: AsyncSession,
    escalation_user_jid: str | None = None,
    escalation_context: str | None = None,
    media_image_b64: str | None = None,
    media_image_mime: str | None = None,
    sender_name: str | None = None,
) -> dict[str, Any]:
    run_id = uuid.uuid4()
    agent_id: uuid.UUID = session.agent_id
    _raw_tools_cfg = agent_model.tools_config
    tools_config: dict[str, Any] = _raw_tools_cfg if isinstance(_raw_tools_cfg, dict) else {}
    temperature: float = getattr(agent_model, "temperature", 0.7)

    log = logger.bind(
        run_id=str(run_id),
        session_id=str(session.id),
        agent_id=str(agent_id),
        model=agent_model.model,
    )
    log.info("agent_run.start")

    # --- LLM (kept as ChatOpenAI with OpenRouter) ---
    llm = ChatOpenAI(
        model=agent_model.model,
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        max_tokens=4096,
        temperature=temperature,
    )

    # --- Sandbox (lazy init: only if sandbox is enabled) ---
    sandbox: DockerSandbox | None = None
    if _is_enabled(tools_config, "sandbox", default=False):
        sandbox = DockerSandbox(session.id)

    # --- Tools ---
    # Conservative defaults: only memory, skills, escalation ON by default.
    # sandbox, tool_creator, scheduler → opt-in (default=False).
    tools: list = []
    active_groups: list[str] = []
    saved_custom_tools: list = []

    if sandbox is not None:
        tools.extend(build_sandbox_binary_tool(sandbox))
        active_groups.append("sandbox")

    _memory_scope = getattr(session, "external_user_id", None)
    if _is_enabled(tools_config, "memory", default=True):
        tools.extend(build_memory_tools(agent_id, db, scope=_memory_scope))
        active_groups.append("memory")

    if _is_enabled(tools_config, "skills", default=True):
        tools.extend(build_skill_tools(agent_id, db))
        active_groups.append("skills")

    if _is_enabled(tools_config, "tool_creator", default=False):
        if sandbox is None:
            log.warning("agent_run.tool_creator_requires_sandbox")
        else:
            tools.extend(build_tool_creator_tools(agent_id, db, sandbox))
            saved_custom_tools = await list_custom_tools(agent_id, db)
            tools.extend(build_loaded_custom_tools(saved_custom_tools, sandbox))
            active_groups.append("tool_creator")

    if _is_enabled(tools_config, "scheduler", default=False):
        from app.core.tools.scheduler_tool import build_scheduler_tools
        tools.extend(build_scheduler_tools(session.id, agent_id, db))
        active_groups.append("scheduler")

    if _is_enabled(tools_config, "escalation", default=True):
        from app.core.tools.escalation_tool import build_escalation_tools
        _raw_cfg = session.channel_config
        _channel_cfg = _raw_cfg if isinstance(_raw_cfg, dict) else {}
        _user_jid = (
            escalation_user_jid
            or _channel_cfg.get("user_phone")
            or getattr(session, "external_user_id", None)
        )
        tools.extend(build_escalation_tools(session.id, agent_id, db, user_jid=_user_jid))
        active_groups.append("escalation")

    if _is_enabled(tools_config, "http", default=False):
        tools.extend(build_http_tools(tools_config))
        active_groups.append("http")

    # WhatsApp media (image + document): opt-in, default ON for WA channel
    if getattr(session, "channel_type", None) == "whatsapp":
        if _is_enabled(tools_config, "whatsapp_media", default=True):
            tools.extend(build_whatsapp_media_tools(session, sandbox))
            active_groups.append("whatsapp_media")

        # send_agent_wa_qr: opt-in only, for agent-manager agents (e.g. Arthur)
        if _is_enabled(tools_config, "wa_agent_manager", default=False):
            tools.extend(build_wa_agent_manager_tools(session))
            active_groups.append("wa_agent_manager")

    # --- Sub-agents (Phase 2) ---
    subagent_list: list = []
    sub_sandboxes: list[DockerSandbox] = []
    if _is_enabled(tools_config, "subagents", default=False):
        _sub_ids: list[str] = tools_config.get("subagents", {}).get("agent_ids", [])
        subagent_list, sub_sandboxes = await build_subagents(
            _sub_ids, session.id, db, log
        )
        if subagent_list:
            active_groups.append(f"subagents({len(subagent_list)})")
            log.info("agent_run.subagents_ready", names=[s["name"] for s in subagent_list])

    log.debug("agent_run.tools_ready (pre-mcp)", groups=active_groups, count=len(tools))

    # --- RAG context (auto-injected, not a tool) ---
    rag_context = ""
    if _is_enabled(tools_config, "rag", default=False):
        rag_context = await _build_rag_context(agent_id, user_message, db, tools_config, log)

    # --- Context summarizer (Phase 3) ---
    context_summary = await _maybe_summarize_context(session, db, llm, log)

    # --- Short-term memory: load last N turns ---
    history_rows = await _load_history(
        session.id, db, max_turns=settings.short_term_memory_turns
    )
    prior_messages = _db_messages_to_lc(history_rows)
    log.debug("agent_run.history_loaded", turns=len(prior_messages) // 2)

    # --- Detect message context ---
    is_operator_message = user_message.startswith("[OPERATOR] ")

    # --- Agent Context Block (PRD2 §3.3 + §3.4) ---
    context_block = _build_agent_context_block(
        agent_model, session, active_groups, saved_custom_tools, subagent_list,
        sender_name=sender_name,
    )

    # --- System prompt ---
    base_instructions = agent_model.instructions or "You are a helpful assistant."
    system_prompt = f"{context_block}\n\n{base_instructions}"

    # 1. Conversation context summary (injected when session is long)
    if context_summary:
        system_prompt += (
            f"\n\n## Conversation Context Summary\n"
            f"*Ringkasan percakapan sebelumnya (pesan lama sudah dikompresi):*\n{context_summary}"
        )

    # 2. Long-term memories
    memory_block = await build_memory_context(agent_id, db, scope=_memory_scope)
    if memory_block:
        system_prompt += f"\n\n{memory_block}"

    # 3. Safety policy
    if agent_model.safety_policy:
        system_prompt += f"\n\n## Safety Policy\n{json.dumps(agent_model.safety_policy, indent=2)}"

    # 4. RAG context
    if rag_context:
        system_prompt += f"\n\n{rag_context}"

    # 5. Channel-specific + escalation context
    is_whatsapp = getattr(session, "channel_type", None) == "whatsapp"

    if is_whatsapp and not is_operator_message and not escalation_user_jid:
        _name_hint = (
            f" Nama user saat ini adalah **{sender_name}** — gunakan namanya saat menyapa atau membalas."
            if sender_name else ""
        )
        system_prompt += (
            "\n\n## WhatsApp Channel\n"
            "Balas user LANGSUNG dengan teks biasa sebagai output akhirmu. "
            "JANGAN gunakan tool `reply_to_user` untuk menjawab user secara normal — cukup tulis jawabanmu. "
            "Tool `reply_to_user` dan `send_to_number` HANYA dipakai saat menerima perintah dari OPERATOR.\n"
            f"{_name_hint}\n\n"
            "### Kirim Gambar ke User\n"
            "Jika kamu perlu mengirim gambar ke user, panggil tool yang sesuai:\n"
            "- `send_whatsapp_image(image_path_or_base64='...')` — untuk kirim gambar/chart dari workspace.\n"
            "JANGAN hanya mendeskripsikan gambar dalam teks — panggil tool-nya agar gambar benar-benar terkirim.\n\n"
            "### Setelah memanggil `escalate_to_human`:\n"
            "- Tool tersebut SUDAH mengirim notifikasi ke operator secara otomatis. "
            "JANGAN tulis atau kirim pesan apapun ke operator.\n"
            "- Output akhirmu adalah pesan singkat untuk USER: "
            "beritahu user bahwa pertanyaannya sedang diteruskan ke tim dan akan segera dibalas.\n"
        )

    if escalation_user_jid:
        ctx_block = ""
        if escalation_context:
            ctx_block = f"\n\n### Pesan terakhir dari user yang dieskalasi:\n{escalation_context}"
        system_prompt += (
            f"\n\n## SESI OPERATOR\n"
            f"Kamu sedang berbicara dengan OPERATOR/ADMIN.\n"
            f"Target user WhatsApp (Chat ID): `{escalation_user_jid}`"
            f"{ctx_block}\n\n"
            "### 🚨 ATURAN PALING KRITIS: DRAFT DULU, JANGAN LANGSUNG KIRIM 🚨\n"
            "- Apabila operator memberikan instruksi/jawaban untuk diteruskan ke customer, KAMU DILARANG KERAS langsung memanggil tool `reply_to_user`.\n"
            "- Kamu WAJIB menyusun *draft* pesan yang rapi & sopan, menampilkannya kepada operator, lalu diakhiri dengan:\n"
            "  \"Sudah OK? Ketik 'kirim' untuk meneruskannya ke customer.\"\n"
            "- SETELAH operator membalas dengan 'kirim', 'ya', atau 'ok', BARULAH kamu diizinkan memanggil tool `reply_to_user(message)`.\n"
            "- Balas operator singkat setelah terkirim: \"Terkirim ✓\"\n"
            "Pelanggaran terhadap aturan ini adalah kesalahan fatal!\n"
        )
    elif is_operator_message:
        _raw_cfg = session.channel_config
        _ch_cfg = _raw_cfg if isinstance(_raw_cfg, dict) else {}
        user_wa_jid = _ch_cfg.get("user_phone") or getattr(session, "external_user_id", None) or "unknown"
        system_prompt += (
            f"\n\n## MODE: OPERATOR COMMAND — ALUR KONFIRMASI\n"
            f"WhatsApp JID user dalam eskalasi: `{user_wa_jid}`\n"
            "Pesan berikut adalah PERINTAH dari human operator.\n\n"
            "### INSTRUKSI WAJIB\n"
            "- Alur DRAFT -> KONFIRMASI -> KIRIM:\n"
            "  1. Agent menyusun draft rapi dari pesanan operator.\n"
            "  2. Tampilkan draft + tanya: \"Sudah OK? Ketik 'kirim'...\"\n"
            "  3. JANGAN panggil `reply_to_user` sebelum dikonfirmasi operator.\n"
            "- Setelah operator konfirmasi ('ok', 'kirim'), panggil tool `reply_to_user(message)`.\n"
            "- Sesudah sukses, balas operator: \"Terkirim ✓\"\n"
        )

    # 6. Available capabilities description
    cap_parts: list[str] = []
    if "memory" in active_groups:
        cap_parts.append("memory tools (remember/recall/forget)")
    if "skills" in active_groups:
        cap_parts.append("skill tools (create_skill/list_skills/use_skill)")
    if "tool_creator" in active_groups:
        custom_tool_names = [ct.name for ct in saved_custom_tools]
        ct_str = f" — custom tools available: {', '.join(custom_tool_names)}" if custom_tool_names else ""
        cap_parts.append(f"tool creator (create_tool/list_tools/run_custom_tool){ct_str}")
    if "scheduler" in active_groups:
        cap_parts.append("scheduler tools (set_reminder/list_reminders/cancel_reminder)")
    if "escalation" in active_groups:
        cap_parts.append("escalation tools (escalate_to_human/reply_to_user/send_to_number)")
    if "http" in active_groups:
        cap_parts.append("HTTP tools (http_get/http_post/http_patch/http_delete)")
    if "whatsapp_media" in active_groups:
        cap_parts.append("WhatsApp media tools (send_whatsapp_image, send_whatsapp_document)")
    if "wa_agent_manager" in active_groups:
        cap_parts.append("WA agent manager (send_agent_wa_qr)")

    if cap_parts:
        system_prompt += (
            "\n\n## Available Capabilities\n"
            "You have access to: " + ", ".join(cap_parts) + ".\n"
            "CRITICAL RULES:\n"
            "1. To apply a skill: call `use_skill(name='X')` first — never guess its content.\n"
            "2. After creating a new tool with `create_tool`, use `run_custom_tool(name, args_json)` "
            "to execute it in this session (it won't be a direct tool yet)."
        )

    # --- Persist user message ---
    step_base = max((m.step_index for m in history_rows), default=-1) + 1
    db.add(Message(
        session_id=session.id,
        role="user",
        content=user_message,
        step_index=step_base,
        run_id=run_id,
    ))
    await db.flush()

    # --- Build and run agent via Deep Agents SDK ---
    from app.core.tools.mcp_tool import mcp_client_context

    async with mcp_client_context(tools_config) as mcp_tools:
        if mcp_tools:
            tools = tools + mcp_tools
            active_groups.append(f"mcp({len(mcp_tools)} tools)")
            log.debug("agent_run.mcp_tools_added", count=len(mcp_tools))

        try:
            from deepagents import create_deep_agent
            from app.core.deep_agent_backend import DockerBackend

            backend = DockerBackend(sandbox) if sandbox is not None else None

            graph = create_deep_agent(
                model=llm,
                tools=tools,
                system_prompt=system_prompt,
                backend=backend,
                subagents=subagent_list or None,
            )
        except (ImportError, TypeError):
            # Fallback: deepagents not installed or doesn't accept LLM object
            from langgraph.prebuilt import create_react_agent
            graph = create_react_agent(llm, tools=tools, prompt=system_prompt)

        if media_image_b64 and media_image_mime:
            human_content: Any = [
                {"type": "text", "text": user_message},
                {"type": "image_url", "image_url": {"url": f"data:{media_image_mime};base64,{media_image_b64}"}},
            ]
        else:
            human_content = user_message
        input_messages: list[BaseMessage] = prior_messages + [HumanMessage(content=human_content)]
        steps: list[dict[str, Any]] = []
        final_reply = ""
        step_counter = step_base + 1

        from langchain_core.callbacks import AsyncCallbackHandler

        class _AgentLogger(AsyncCallbackHandler):
            async def on_llm_start(self, serialized, prompts, **kwargs):
                log.debug("agent_step.llm_thinking")

            async def on_llm_end(self, response, **kwargs):
                text = ""
                try:
                    text = response.generations[0][0].text[:200]
                except Exception:
                    pass
                if text:
                    log.info("agent_step.llm_response", preview=text)

            async def on_tool_start(self, serialized, input_str, **kwargs):
                tool_name = serialized.get("name", "?")
                log.info("agent_step.tool_call", tool=tool_name, input=str(input_str)[:300])

            async def on_tool_end(self, output, **kwargs):
                log.info("agent_step.tool_result", output=str(output)[:300])

            async def on_tool_error(self, error, **kwargs):
                log.warning("agent_step.tool_error", error=str(error))

            async def on_chain_start(self, serialized, inputs, **kwargs):
                if not serialized:
                    return
                name = serialized.get("name", serialized.get("id", ["?"])[-1])
                log.debug("agent_step.chain_start", chain=name)

            async def on_chain_end(self, outputs, **kwargs):
                log.debug("agent_step.chain_end")

        try:
            result = await graph.ainvoke(
                {"messages": input_messages},
                config={
                    "recursion_limit": settings.agent_max_steps * 2,
                    "callbacks": [_AgentLogger()],
                },
            )
        except Exception as exc:
            log.error("agent_run.error", error=str(exc))
            if sandbox:
                sandbox.close()
            for _ssb in sub_sandboxes:
                _ssb.close()
            raise

        # --- Parse result messages ---
        all_messages: list[BaseMessage] = result.get("messages", [])
        new_messages = all_messages[len(input_messages):]
        tool_step = 0
        pending_tool_records: list[Message] = []
        total_tokens_used = 0

        for msg in new_messages:
            if isinstance(msg, AIMessage):
                usage = getattr(msg, "usage_metadata", None)
                if usage:
                    total_tokens_used += usage.get("total_tokens", 0)

                if msg.content:
                    text = msg.content if isinstance(msg.content, str) else str(msg.content)
                    final_reply = text
                    db.add(Message(
                        session_id=session.id,
                        role="agent",
                        content=text,
                        step_index=step_counter,
                        run_id=run_id,
                    ))
                    step_counter += 1
                for tc in (msg.tool_calls or []):
                    tool_step += 1
                    steps.append({"step": tool_step, "tool": tc["name"], "args": tc.get("args", {}), "result": ""})
                    record = Message(
                        session_id=session.id,
                        role="tool",
                        tool_name=tc["name"],
                        tool_args=tc.get("args", {}),
                        step_index=step_counter,
                        run_id=run_id,
                    )
                    db.add(record)
                    pending_tool_records.append(record)
                    step_counter += 1
            elif isinstance(msg, ToolMessage):
                output = msg.content if isinstance(msg.content, str) else str(msg.content)
                for entry in reversed(steps):
                    if entry["result"] == "":
                        entry["result"] = output[:500]
                        break
                if pending_tool_records:
                    pending_tool_records.pop(0).tool_result = output[:2000]

    await db.flush()

    # --- Long-term memory auto-extraction ---
    if _is_enabled(tools_config, "memory", default=True):
        user_msg_count = await _count_user_messages(session.id, db)
        if user_msg_count > 0 and user_msg_count % settings.ltm_extraction_every == 0:
            log.info("agent_run.ltm_trigger", user_messages=user_msg_count)
            recent_for_ltm = await _load_history(
                session.id, db, max_turns=settings.ltm_extraction_every
            )
            await extract_long_term_memory(
                agent_id=agent_id,
                recent_messages=recent_for_ltm,
                llm=llm,
                db=db,
                log=log,
                scope=_memory_scope,
            )

    if sandbox:
        sandbox.close()
    for _ssb in sub_sandboxes:
        _ssb.close()
    log.info(
        "agent_run.complete",
        steps=len(steps),
        reply_len=len(final_reply),
        tokens_used=total_tokens_used,
    )
    return {"reply": final_reply, "steps": steps, "run_id": run_id, "tokens_used": total_tokens_used}
