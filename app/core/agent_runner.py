"""
Agent runner: wires OpenRouter LLM + tools + memory + RAG, runs the agent,
persists all steps to DB.

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

Tool selection  Driven by tools_config. Default: sandbox/memory/skills/
                tool_creator ON; http/rag OFF (opt-in).
"""
from __future__ import annotations

import ast
import json
import sys
import uuid
from typing import Any

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
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

def _is_enabled(tools_config: dict[str, Any], key: str, default: bool = True) -> bool:
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

def build_sandbox_tools(sandbox: DockerSandbox) -> list:
    @tool
    def bash(cmd: str) -> str:
        """Execute a bash command in the isolated sandbox workspace. Returns stdout+stderr."""
        return sandbox.bash(cmd)

    @tool
    def write_file(path: str, content: str) -> str:
        """Write text content to a file at the given path inside the sandbox workspace."""
        return sandbox.write_file(path, content)

    @tool
    def write_binary_file(path: str, base64_content: str) -> str:
        """Decode a base64 string and write it as a binary file in the sandbox workspace.
        Useful for saving images or other binary data. Args: path (e.g. 'output.png'), base64_content (raw base64 string without data URI prefix)."""
        return sandbox.write_binary_file(path, base64_content)

    @tool
    def read_file(path: str) -> str:
        """Read and return the full text content of a file in the sandbox workspace."""
        return sandbox.read_file(path)

    @tool
    def list_files(directory: str = ".") -> str:
        """List all files under a directory in the sandbox workspace."""
        return sandbox.list_files(directory)

    return [bash, write_file, write_binary_file, read_file, list_files]


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
        in the current session. Args: name (tool name), args_json (JSON string of kwargs)."""
        tools = await list_custom_tools(agent_id, db)
        tool_map = {t.name: t for t in tools}
        if name not in tool_map:
            return f"[error] No custom tool named '{name}'. Use list_tools() to see available tools."
        ct = tool_map[name]
        try:
            args = json.loads(args_json)
        except json.JSONDecodeError as e:
            return f"[error] Invalid args_json: {e}"
        args_json_str = json.dumps(json.dumps(args))  # double-encode so it's a safe string literal
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
    lc_tools = []
    for ct in custom_tools_db:
        def _make_runner(ct_name: str, ct_code: str, ct_desc: str):
            @tool(ct_name, description=ct_desc)
            def _runner(args_json: str = "{}") -> str:
                """Execute a saved custom tool in the sandbox."""
                try:
                    args = json.loads(args_json)
                except json.JSONDecodeError as e:
                    return f"[error] Invalid args_json: {e}"
                args_json_str = json.dumps(json.dumps(args))
                runner_code = f"""{ct_code}

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
                sandbox.write_file(f"_custom_tool_{ct_name}.py", runner_code)
                pip_cmd = _pip_prefix(ct_code)
                return sandbox.bash(f"{pip_cmd}python /workspace/_custom_tool_{ct_name}.py")
            return _runner
        lc_tools.append(_make_runner(ct.name, ct.code, ct.description))
    return lc_tools


# ---------------------------------------------------------------------------
# WhatsApp media tools
# ---------------------------------------------------------------------------

def build_whatsapp_media_tools(session: Any, sandbox: DockerSandbox) -> list:
    """
    Tools untuk mengirim gambar/media ke WhatsApp.
    Hanya diaktifkan jika session memiliki channel_type == 'whatsapp'.
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
            phone               : Nomor tujuan WA atau JID (misal '+62812...@s.whatsapp.net').
                                  Biarkan kosong untuk kirim ke user saat ini.
            mimetype            : MIME type gambar, default 'image/jpeg'
        """
        target = phone or default_target
        if not target:
            return "[error] Tidak ada target nomor WhatsApp — set phone atau pastikan session punya user_phone"
        if not device_id:
            return "[error] Tidak ada device_id WhatsApp pada session ini"

        # Tentukan apakah input adalah path file atau base64 langsung
        if image_path_or_base64.startswith("/workspace/") or not image_path_or_base64.startswith("/"):
            # Path file di workspace — baca via bash base64
            path = image_path_or_base64 if image_path_or_base64.startswith("/workspace/") else f"/workspace/{image_path_or_base64}"
            b64_output = sandbox.bash(f"base64 -w 0 {path} 2>&1")
            if b64_output.startswith("["):
                return f"[error] Gagal membaca file: {b64_output}"
            image_b64 = b64_output.strip()
        else:
            # Base64 langsung
            image_b64 = image_path_or_base64.strip()

        try:
            from app.core.wa_client import send_wa_image
            await send_wa_image(device_id, target, image_b64, caption, mimetype)
            return f"[IMAGE_SENT] Gambar dikirim ke {target}" + (f" dengan caption: {caption}" if caption else "")
        except Exception as exc:
            return f"[error] Gagal kirim gambar: {exc}"

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
        wa_device_id yang tersimpan di DB untuk agent tersebut, sehingga tidak akan
        membuat device orphan.

        Jika QR tidak tersedia (device disconnected / logged out), tool ini otomatis
        re-init device dan generate QR baru.

        PENTING: Gunakan `agent_id` (UUID agent di platform ini), BUKAN wa_device_id.

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

        # Lookup wa_device_id dari DB menggunakan agent_id
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
            return f"[error] Agent '{agent_id}' tidak memiliki WhatsApp device — pastikan agent dibuat dengan channel_type='whatsapp'"

        wa_dev_id: str = agent_row.wa_device_id

        try:
            # Coba ambil QR yang sedang aktif
            try:
                resp = await get_wa_qr(wa_dev_id)
            except Exception:
                resp = {"status": "not_found", "qr_image": ""}

            qr_status: str = resp.get("status", "")
            qr_b64: str = resp.get("qr_image", "")

            if qr_status == "connected":
                return f"[INFO] Agent '{agent_id}' sudah terhubung ke WhatsApp — tidak perlu scan QR lagi."

            # QR tidak tersedia → re-init device (handle logout / disconnected)
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
            file_path_or_base64: Path file di /workspace (misal '/workspace/laporan.pdf') ATAU
                                  string base64 langsung dari file tersebut
            filename            : Nama file yang akan ditampilkan di WhatsApp (misal 'laporan.pdf')
            caption             : Teks caption yang menyertai dokumen (opsional)
            phone               : Nomor tujuan WA atau JID. Biarkan kosong untuk kirim ke user saat ini.
            mimetype            : MIME type file. Jika kosong, otomatis ditentukan dari ekstensi filename.
        """
        target = phone or default_target
        if not target:
            return "[error] Tidak ada target nomor WhatsApp — set phone atau pastikan session punya user_phone"
        if not device_id:
            return "[error] Tidak ada device_id WhatsApp pada session ini"

        # Auto-detect mimetype dari ekstensi jika tidak disediakan
        if not mimetype and filename:
            import mimetypes
            guessed, _ = mimetypes.guess_type(filename)
            mimetype = guessed or "application/octet-stream"
        elif not mimetype:
            mimetype = "application/octet-stream"

        # Tentukan apakah input adalah path file atau base64 langsung
        if file_path_or_base64.startswith("/workspace/") or (not file_path_or_base64.startswith("/") and len(file_path_or_base64) < 500):
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

    return [send_whatsapp_image, send_agent_wa_qr, send_whatsapp_document]


# ---------------------------------------------------------------------------
# HTTP tools (from http_tool.py module)
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
    """
    Load conversation history ordered chronologically.
    If max_turns is given, load only the last max_turns user+agent pairs
    (tool messages are excluded from the count but still omitted by
    _db_messages_to_lc — we don't need them here).
    """
    if max_turns is not None:
        # Subquery: get IDs of last (max_turns * 2) user/agent messages DESC
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
    """Total number of user messages in this session (for LTM trigger)."""
    result = await db.execute(
        select(func.count()).where(
            Message.session_id == session_id,
            Message.role == "user",
        )
    )
    return result.scalar_one()


def _db_messages_to_lc(db_messages: list[Message]) -> list[BaseMessage]:
    """Convert ORM message rows to LangChain message objects (user/agent only)."""
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
    """
    Embed user_message, fetch top-3 similar documents, return a formatted
    markdown block ready to inject into the system prompt.
    Returns "" if RAG is disabled, no documents match, or any error occurs.
    """
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

        # Fallback to keyword search if vector search returns nothing
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

    # --- LLM ---
    llm = ChatOpenAI(
        model=agent_model.model,
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        max_tokens=4096,
        temperature=temperature,
    )

    # --- Sandbox ---
    sandbox = DockerSandbox(session.id)

    # --- Tools ---
    # Default ON: sandbox, memory, skills, tool_creator, rag, scheduler, escalation
    # Default OFF: http, mcp
    tools: list = []
    active_groups: list[str] = []

    if _is_enabled(tools_config, "sandbox"):
        tools.extend(build_sandbox_tools(sandbox))
        active_groups.append("sandbox")

    _memory_scope = getattr(session, "external_user_id", None)
    if _is_enabled(tools_config, "memory"):
        tools.extend(build_memory_tools(agent_id, db, scope=_memory_scope))
        active_groups.append("memory")

    if _is_enabled(tools_config, "skills"):
        tools.extend(build_skill_tools(agent_id, db))
        active_groups.append("skills")

    if _is_enabled(tools_config, "tool_creator"):
        tools.extend(build_tool_creator_tools(agent_id, db, sandbox))
        saved_custom_tools = await list_custom_tools(agent_id, db)
        tools.extend(build_loaded_custom_tools(saved_custom_tools, sandbox))
        active_groups.append("tool_creator")

    if _is_enabled(tools_config, "scheduler"):
        from app.core.tools.scheduler_tool import build_scheduler_tools
        tools.extend(build_scheduler_tools(session.id, agent_id, db))
        active_groups.append("scheduler")

    if _is_enabled(tools_config, "escalation"):
        from app.core.tools.escalation_tool import build_escalation_tools
        _raw_cfg = session.channel_config
        _channel_cfg = _raw_cfg if isinstance(_raw_cfg, dict) else {}
        _user_jid = (
            escalation_user_jid                          # operator session: target = escalated user
            or _channel_cfg.get("user_phone")            # user session: own channel
            or getattr(session, "external_user_id", None)
        )
        tools.extend(build_escalation_tools(session.id, agent_id, db, user_jid=_user_jid))
        active_groups.append("escalation")

    if _is_enabled(tools_config, "http", default=False):
        tools.extend(build_http_tools(tools_config))
        active_groups.append("http")

    # WhatsApp media tools: aktif otomatis jika session channel adalah whatsapp dan sandbox aktif
    if getattr(session, "channel_type", None) == "whatsapp" and _is_enabled(tools_config, "sandbox"):
        tools.extend(build_whatsapp_media_tools(session, sandbox))
        active_groups.append("whatsapp_media")

    log.debug("agent_run.tools_ready (pre-mcp)", groups=active_groups, count=len(tools))

    # --- RAG context (auto-injected, not a tool) ---
    rag_context = ""
    if _is_enabled(tools_config, "rag"):
        rag_context = await _build_rag_context(agent_id, user_message, db, tools_config, log)

    # --- Short-term memory: load last N turns ---
    history_rows = await _load_history(
        session.id, db, max_turns=settings.short_term_memory_turns
    )
    prior_messages = _db_messages_to_lc(history_rows)
    log.debug("agent_run.history_loaded", turns=len(prior_messages) // 2)

    # --- Detect message context: operator command vs escalation mode ---
    is_operator_message = user_message.startswith("[OPERATOR] ")

    # --- System prompt ---
    system_prompt = agent_model.instructions or "You are a helpful assistant."

    # 1. Long-term memories (scoped per phone number to prevent cross-user leakage)
    memory_block = await build_memory_context(agent_id, db, scope=_memory_scope)
    if memory_block:
        system_prompt += f"\n\n{memory_block}"

    # 2. Safety policy
    if agent_model.safety_policy:
        system_prompt += f"\n\n## Safety Policy\n{json.dumps(agent_model.safety_policy, indent=2)}"

    # 3. RAG context (top-3 docs most relevant to this query)
    if rag_context:
        system_prompt += f"\n\n{rag_context}"

    # 4. Channel-specific + escalation context
    is_whatsapp = getattr(session, "channel_type", None) == "whatsapp"

    if is_whatsapp and not is_operator_message and not escalation_user_jid:
        # Untuk channel WhatsApp: agent harus selalu reply langsung dengan teks.
        # reply_to_user / send_to_number hanya untuk perintah operator.
        system_prompt += (
            "\n\n## WhatsApp Channel\n"
            "Balas user LANGSUNG dengan teks biasa sebagai output akhirmu. "
            "JANGAN gunakan tool `reply_to_user` untuk menjawab user secara normal — cukup tulis jawabanmu. "
            "Tool `reply_to_user` dan `send_to_number` HANYA dipakai saat menerima perintah dari OPERATOR.\n\n"
            "### Setelah memanggil `escalate_to_human`:\n"
            "- Tool tersebut SUDAH mengirim notifikasi ke operator secara otomatis. "
            "JANGAN tulis atau kirim pesan apapun ke operator.\n"
            "- Output akhirmu adalah pesan singkat untuk USER (bukan operator): "
            "beritahu user bahwa pertanyaannya sedang diteruskan ke tim dan akan segera dibalas.\n"
            "- JANGAN minta info tambahan ke operator, JANGAN mention nomor/JID apapun."
        )

    if escalation_user_jid:
        # Sesi OPERATOR: agent menerima instruksi operator dan langsung kirim ke user
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
            "- Kamu WAJIB menyusun *draft* pesan yang rapi & sopan (perbaiki ejaan), menampilkannya kepada operator sebagai pesan biasa, lalu diakhiri dengan pertanyaan:\n"
            "  \"Sudah OK? Ketik 'kirim' untuk meneruskannya ke customer.\"\n"
            "- SETELAH operator membalas dengan 'kirim', 'ya', atau 'ok', BARULAH kamu diizinkan memanggil tool `reply_to_user(message)` membawa pesan draft tadi.\n"
            "- Balas operator dengan singkat setelah terkirim: \"Terkirim ✓\"\n"
            "Pelanggaran terhadap aturan ini (mengirim langsung tanpa konfirmasi) adalah kesalahan fatal!\n"
        )
    elif is_operator_message:
        # Legacy: operator command via [OPERATOR] prefix di session user
        _raw_cfg = session.channel_config
        _ch_cfg = _raw_cfg if isinstance(_raw_cfg, dict) else {}
        user_wa_jid = _ch_cfg.get("user_phone") or getattr(session, "external_user_id", None) or "unknown"
        system_prompt += (
            f"\n\n## MODE: OPERATOR COMMAND — ALUR KONFIRMASI\n"
            f"WhatsApp JID user dalam eskalasi: `{user_wa_jid}`\n"
            "Pesan berikut adalah PERINTAH dari human operator.\n\n"
            "### INSTRUKSI WAJIB\n"
            "- Alur DRAFT -> KONFIRMASI -> KIRIM:\n"
            "  1. Agent menyusun draft rapi dari pesanan operator (JANGAN tambah konten).\n"
            "  2. Tampilkan draft + tanya: \"Sudah OK? Ketik 'kirim'...\"\n"
            "  3. JANGAN panggil `reply_to_user` atau kirim ke user sebelum dikonfirmasi operator.\n"
            "- Setelah operator konfirmasi (contoh: \"ok\", \"kirim\"), panggil tool `reply_to_user(message)`.\n"
            "- Sesudah sukses, balas operator: \"Terkirim ✓\"\n"
            "- Jika operator berkata 'selesai' atau 'tangani sendiri', balas singkat dan kembali normal.\n"
        )

    # 5. Available capabilities description
    cap_parts: list[str] = []
    if "memory" in active_groups:
        cap_parts.append("memory tools (remember/recall/forget)")
    if "skills" in active_groups:
        cap_parts.append("skill tools (create_skill/list_skills/use_skill)")
    if "tool_creator" in active_groups:
        cap_parts.append("tool creator (create_tool/list_tools/run_custom_tool)")
    if "scheduler" in active_groups:
        cap_parts.append("scheduler tools (set_reminder/list_reminders/cancel_reminder)")
    if "escalation" in active_groups:
        cap_parts.append("escalation tools (escalate_to_human/reply_to_user/send_to_number)")
    if "http" in active_groups:
        cap_parts.append("HTTP tools (http_get/http_post)")
    if "whatsapp_media" in active_groups:
        cap_parts.append("WhatsApp media tools (send_whatsapp_image, send_agent_wa_qr, send_whatsapp_document)")

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

    # --- Build and run agent graph (MCP client kept alive for entire run) ---
    from app.core.tools.mcp_tool import mcp_client_context

    async with mcp_client_context(tools_config) as mcp_tools:
        if mcp_tools:
            tools = tools + mcp_tools
            active_groups.append(f"mcp({len(mcp_tools)} tools)")
            log.debug("agent_run.mcp_tools_added", count=len(mcp_tools))

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

        try:
            result = await graph.ainvoke(
                {"messages": input_messages},
                config={"recursion_limit": settings.agent_max_steps * 2},
            )
        except Exception as exc:
            log.error("agent_run.error", error=str(exc))
            final_reply = f"Agent error: {exc}"
            db.add(Message(
                session_id=session.id,
                role="agent",
                content=final_reply,
                step_index=step_counter,
                run_id=run_id,
            ))
            await db.flush()
            sandbox.close()
            return {"reply": final_reply, "steps": [], "run_id": run_id, "tokens_used": 0}

        # --- Parse result messages ---
        all_messages: list[BaseMessage] = result.get("messages", [])
        new_messages = all_messages[len(input_messages):]
        tool_step = 0
        pending_tool_records: list[Message] = []
        total_tokens_used = 0

        for msg in new_messages:
            if isinstance(msg, AIMessage):
                # accumulate token usage across all LLM calls in the graph
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
    if _is_enabled(tools_config, "memory"):
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

    sandbox.close()
    log.info(
        "agent_run.complete",
        steps=len(steps),
        reply_len=len(final_reply),
        tokens_used=total_tokens_used,
    )
    return {"reply": final_reply, "steps": steps, "run_id": run_id, "tokens_used": total_tokens_used}
