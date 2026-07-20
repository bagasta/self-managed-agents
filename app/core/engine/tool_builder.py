"""
tool_builder.py — Factory functions untuk semua tool yang di-inject ke agent.

Dipecah dari agent_runner.py (item 2.1 production plan).

Daftar fungsi:
  build_sandbox_binary_tool(sandbox)
  build_memory_tools(agent_id, db, scope)
  build_skill_tools(agent_id, db)
  build_tool_creator_tools(agent_id, db, sandbox)
  build_loaded_custom_tools(custom_tools_db, sandbox)
  build_wa_notify_tool(session)              ← always-on untuk WA sessions
  build_whatsapp_media_tools(session, sandbox)
  build_wa_agent_manager_tools(session)
  build_http_tools(tools_config)
  build_tavily_tools(tools_config)
  build_builder_tools(db, owner_phone)      ← hanya untuk capability "builder"
  build_deployment_tools(sandbox)           ← opt-in via tools_config deploy: true
  _is_enabled(tools_config, key, default)   ← re-exported untuk backward compat
"""
from __future__ import annotations

import ast
import base64
import json
from pathlib import Path
import sys
import uuid
from typing import Any, Optional

from langchain_core.tools import tool, StructuredTool
from pydantic import Field, create_model

from app.core.domain.custom_tool_service import create_or_update_custom_tool, list_custom_tools
from app.core.utils.phone_utils import normalize_phone
from app.core.utils.wa_identity import is_probable_whatsapp_lid
from app.core.domain.memory_service import (
    build_memory_context as _build_memory_context,
    delete_memory,
    extract_long_term_memory,
    get_memory,
    list_memories,
    memory_today,
    upsert_memory,
)
from app.core.infra.sandbox import DockerSandbox
from app.core.domain.skill_service import (
    create_or_update_skill,
    get_skill,
    list_skills as _list_skills,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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

def build_memory_tools(agent_id: uuid.UUID, db_factory: async_sessionmaker, scope: str | None = None) -> list:
    @tool
    async def remember(key: str, value: str) -> str:
        """Store or update a fact in long-term memory. Args: key (short label), value (text to remember)."""
        async with db_factory() as db:
            await upsert_memory(agent_id, key, value, db, scope=scope)
            await db.commit()
        return f"Remembered: {key} = {value}"

    @tool
    async def recall(query: str) -> str:
        """Retrieve a memory entry by its key. Args: query (the key to look up)."""
        async with db_factory() as db:
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
        async with db_factory() as db:
            deleted = await delete_memory(agent_id, key, db, scope=scope)
            await db.commit()
        return f"Forgotten: {key}" if deleted else f"No memory found for key '{key}'"

    @tool
    async def update_daily(content: str) -> str:
        """Append a note to today's daily memory. Use this to record important events from this session.
        Args: content (the note to append, one fact or event per call)."""
        today = memory_today()
        key = f"daily:{today}"
        async with db_factory() as db:
            existing = await get_memory(agent_id, key, db, scope=scope)
            new_val = (existing.value_data + f"\n- {content}") if existing else f"- {content}"
            await upsert_memory(agent_id, key, new_val, db, scope=scope)
            await db.commit()
        return f"Daily memory updated ({today})"

    @tool
    async def update_longterm(content: str) -> str:
        """Append important information to long-term curated memory (persists across all future sessions).
        Args: content (the fact or insight to remember long-term)."""
        key = "longterm"
        async with db_factory() as db:
            existing = await get_memory(agent_id, key, db, scope=scope)
            new_val = (existing.value_data + f"\n- {content}") if existing else f"- {content}"
            await upsert_memory(agent_id, key, new_val, db, scope=scope)
            await db.commit()
        return "Long-term memory updated"

    return [remember, recall, forget, update_daily, update_longterm]


# ---------------------------------------------------------------------------
# Heartbeat tools
# ---------------------------------------------------------------------------

def build_heartbeat_tools(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    db_factory: async_sessionmaker,
    scope: str | None = None,
) -> list:
    """Tools untuk mengaktifkan/menonaktifkan heartbeat proaktif per user."""

    @tool
    async def enable_heartbeat(
        interval_minutes: int = 30,
        quiet_start: str = "23:00",
        quiet_end: str = "08:00",
    ) -> str:
        """Aktifkan heartbeat berkala untuk user ini. Agent akan proaktif cek dan notif jika ada yang penting.
        Args: interval_minutes (seberapa sering cek, default 30), quiet_start/quiet_end (jam diam, format HH:MM WIB)."""
        import json as _json
        from datetime import timedelta as _td, timezone as _tz
        from app.models.scheduled_job import ScheduledJob
        from sqlalchemy import select as _select

        # Simpan config ke memory
        config = {
            "enabled": True,
            "interval_minutes": interval_minutes,
            "quiet_start": quiet_start,
            "quiet_end": quiet_end,
        }
        async with db_factory() as db:
            await upsert_memory(agent_id, "heartbeat:config", _json.dumps(config), db, scope=scope)

            # Buat cron expression dari interval
            if interval_minutes < 60:
                cron_expr = f"*/{interval_minutes} * * * *"
            else:
                hours = interval_minutes // 60
                cron_expr = f"0 */{hours} * * *"

            label = f"heartbeat:{scope or '_global'}"

            # Cancel job lama jika ada
            old = await db.execute(
                _select(ScheduledJob).where(
                    ScheduledJob.agent_id == agent_id,
                    ScheduledJob.label == label,
                    ScheduledJob.status == "active",
                )
            )
            old_job = old.scalar_one_or_none()
            if old_job:
                old_job.status = "cancelled"

            # Compute next_run
            from croniter import croniter as _croniter
            _local_tz = _tz(timedelta(hours=7))
            from datetime import datetime as _dt
            now_local = _dt.now(_local_tz)
            try:
                next_local = _croniter(cron_expr, now_local).get_next(_dt)
                next_run = next_local.astimezone(_tz.utc)
            except Exception:
                next_run = _dt.now(_tz.utc) + _td(minutes=interval_minutes)

            job = ScheduledJob(
                agent_id=agent_id,
                session_id=session_id,
                label=label,
                cron_expr=cron_expr,
                payload="[HEARTBEAT]",
                status="active",
                next_run_at=next_run,
            )
            db.add(job)
            await db.commit()

        return (
            f"Heartbeat aktif: setiap {interval_minutes} menit. "
            f"Jam diam: {quiet_start}–{quiet_end} WIB. "
            "Set checklist kamu dengan: remember('heartbeat:checklist', '- cek reminder...')"
        )

    @tool
    async def disable_heartbeat() -> str:
        """Nonaktifkan heartbeat proaktif untuk user ini."""
        import json as _json
        from app.models.scheduled_job import ScheduledJob
        from sqlalchemy import select as _select

        label = f"heartbeat:{scope or '_global'}"
        async with db_factory() as db:
            # Update config di memory
            existing = await get_memory(agent_id, "heartbeat:config", db, scope=scope)
            if existing:
                try:
                    cfg = _json.loads(existing.value_data)
                    cfg["enabled"] = False
                    await upsert_memory(agent_id, "heartbeat:config", _json.dumps(cfg), db, scope=scope)
                except Exception:
                    pass

            # Cancel scheduled job
            result = await db.execute(
                _select(ScheduledJob).where(
                    ScheduledJob.agent_id == agent_id,
                    ScheduledJob.label == label,
                    ScheduledJob.status == "active",
                )
            )
            job = result.scalar_one_or_none()
            if job:
                job.status = "cancelled"
            await db.commit()

        return "Heartbeat dinonaktifkan."

    return [enable_heartbeat, disable_heartbeat]


# ---------------------------------------------------------------------------
# Skill tools
# ---------------------------------------------------------------------------

def build_skill_tools(agent_id: uuid.UUID, db_factory: async_sessionmaker) -> list:
    @tool
    async def create_skill(name: str, description: str, content_md: str) -> str:
        """Save a reusable skill (instruction/prompt block) to the skill library.
        Args: name (unique short identifier), description (what it does), content_md (full instructions in markdown)."""
        async with db_factory() as db:
            skill = await create_or_update_skill(agent_id, name, description, content_md, db)
            await db.commit()
        return f"Skill '{skill.name}' saved successfully."

    @tool
    async def list_skills() -> str:
        """List all available skills for this agent."""
        async with db_factory() as db:
            skills = await _list_skills(agent_id, db)
        if not skills:
            return "No skills saved yet."
        lines = [f"- **{s.name}**: {s.description}" for s in skills]
        return "Available skills:\n" + "\n".join(lines)

    @tool
    async def use_skill(name: str) -> str:
        """Load and return the full content of a skill by name to use in current context.
        Args: name (the skill identifier)."""
        async with db_factory() as db:
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


def build_tool_creator_tools(agent_id: uuid.UUID, db_factory: async_sessionmaker, sandbox: DockerSandbox) -> list:
    @tool
    async def create_tool(name: str, description: str, python_code: str) -> str:
        """Save a new Python tool for this agent. The code must define a function with the same name as `name`.
        Args: name (function name, snake_case), description (what it does), python_code (valid Python code)."""
        async with db_factory() as db:
            ct, err = await create_or_update_custom_tool(agent_id, name, description, python_code, db)
            if not err:
                await db.commit()
        if err:
            return f"[error] Could not save tool: {err}"
        return f"Tool '{name}' saved successfully. It will be available in future sessions."

    @tool
    async def list_tools() -> str:
        """List all custom tools created by this agent."""
        async with db_factory() as db:
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
        async with db_factory() as db:
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
# WhatsApp notify tool (progress updates mid-run)
# ---------------------------------------------------------------------------

def build_wa_notify_tool(session: Any) -> list:
    """
    Tool `notify_user` — kirim pesan progress ke user WA selama agent masih running.
    Otomatis aktif untuk semua sesi WhatsApp. Tidak butuh escalation.
    """
    _raw_cfg = session.channel_config
    channel_cfg: dict = _raw_cfg if isinstance(_raw_cfg, dict) else {}
    device_id: str = channel_cfg.get("device_id", "")
    default_target: str = channel_cfg.get("user_phone", "")
    notify_attempted = False

    def _looks_like_delivery_claim(message: str) -> bool:
        lowered = (message or "").lower()
        if not any(marker in lowered for marker in ("file", "dokumen", "pdf", "gambar", "foto", "laporan")):
            return False
        return any(
            marker in lowered
            for marker in (
                "sudah saya kirim",
                "sudah dikirim",
                "sudah terkirim",
                "berhasil saya kirim",
                "saya kirim sekarang",
                "saya akan kirim",
                "mengirim file",
                "mengirim dokumen",
                "siap saya kirim",
                "siap dikirim",
            )
        )

    @tool
    async def notify_user(message: str) -> str:
        """Kirim pesan progress/update ke user WhatsApp saat sedang mengerjakan task panjang.
        Gunakan ini untuk memberi tahu user bahwa pekerjaan masih berjalan, BUKAN sebagai reply final.
        Contoh: notify_user('Sedang menulis file HTML...'), notify_user('Deploy sedang berjalan, hampir selesai...')
        """
        nonlocal notify_attempted
        if notify_attempted:
            return "[notify_user] suppressed: progress notification already attempted for this run"
        notify_attempted = True
        if _looks_like_delivery_claim(message):
            return (
                "[notify_user] suppressed: jangan pakai notify_user untuk klaim file siap/terkirim. "
                "Jika perlu mengirim file, panggil send_whatsapp_document/send_whatsapp_image."
            )
        if not device_id or not default_target:
            return "[notify_user] no WA device/target configured"
        try:
            from app.core.infra.wa_client import send_wa_message, start_wa_typing
            await send_wa_message(device_id, default_target, message)
            # notify_user is not the final reply. Restart typing immediately so
            # WhatsApp keeps showing the agent is still working until final send.
            try:
                await start_wa_typing(device_id, default_target)
            except Exception:
                pass
            return "notifikasi terkirim"
        except Exception as exc:
            return f"[notify_user] gagal: {exc}"

    return [notify_user]


# ---------------------------------------------------------------------------
# WhatsApp media tools
# ---------------------------------------------------------------------------

def _basename(path: str | None) -> str:
    return str(path or "").strip().rsplit("/", 1)[-1]


def _session_media_file_candidates(session: Any) -> list[dict[str, str]]:
    """Return host-readable media files that belong to the current session.

    The model sees /workspace aliases, while wa_helpers stores the actual file
    on the host session workspace. This intentionally only exposes files already
    registered in session metadata so disabling sandbox does not become arbitrary
    filesystem read access.
    """
    meta = getattr(session, "metadata_", None)
    meta = meta if isinstance(meta, dict) else {}
    incoming = meta.get("last_incoming_media")
    current = meta.get("current_attachment")

    entries: list[dict[str, str]] = []

    def add_entry(host_path: Any, *, media_type: Any, filename: Any, mimetype: Any, aliases: set[str]) -> None:
        host = str(host_path or "").strip()
        if not host:
            return
        try:
            path_obj = Path(host)
        except Exception:
            return
        if not path_obj.is_file():
            return
        name = str(filename or path_obj.name or "").strip()
        alias_values = {str(alias or "").strip() for alias in aliases if str(alias or "").strip()}
        alias_values.add(host)
        if name:
            alias_values.update({
                name,
                f"/workspace/{name}",
                f"/workspace/shared/{name}",
                f"/workspace/shared/current_input/{name}",
                f"/workspace/data/incoming/{name}",
                f"/workspace/data/incoming/current_input/{name}",
            })
        entries.append(
            {
                "host_path": host,
                "media_type": str(media_type or "").strip(),
                "filename": name,
                "mimetype": str(mimetype or "").strip(),
                "aliases": "\n".join(sorted(alias_values)),
            }
        )

    current_aliases: set[str] = set()
    current_filename = ""
    if isinstance(current, dict):
        current_filename = str(current.get("filename") or "").strip()
        for key in (
            "input_path",
            "subagent_input_path",
            "shared_path",
            "legacy_shared_path",
            "extracted_text_path",
            "extracted_text_subagent_path",
        ):
            value = str(current.get(key) or "").strip()
            if value:
                current_aliases.add(value)

    if isinstance(incoming, dict):
        filename = str(incoming.get("filename") or current_filename or "").strip()
        aliases = set(current_aliases)
        for key in (
            "workspace_alias",
            "incoming_alias",
            "current_input_path",
            "subagent_current_input_path",
            "shared_alias",
        ):
            value = str(incoming.get(key) or "").strip()
            if value:
                aliases.add(value)
        for key in (
            "current_shared_workspace_path",
            "shared_workspace_path",
            "workspace_path",
            "current_workspace_path",
            "incoming_workspace_path",
        ):
            add_entry(
                incoming.get(key),
                media_type=incoming.get("media_type"),
                filename=filename,
                mimetype=incoming.get("mimetype"),
                aliases=aliases,
            )

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in entries:
        key = item["host_path"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _resolve_session_media_file(
    session: Any,
    requested_path: str,
    *,
    allowed_media_types: set[str] | None = None,
) -> dict[str, str] | None:
    requested = str(requested_path or "").strip()
    if not requested or session is None:
        return None
    requested_name = _basename(requested)
    candidates = _session_media_file_candidates(session)
    if allowed_media_types:
        candidates = [
            item for item in candidates
            if str(item.get("media_type") or "").strip() in allowed_media_types
        ]
    if not candidates:
        return None

    for item in candidates:
        aliases = set(str(item.get("aliases") or "").splitlines())
        if requested in aliases:
            return item

    if requested_name:
        name_matches = [item for item in candidates if _basename(item.get("filename")) == requested_name]
        if len(name_matches) == 1:
            return name_matches[0]
    if len(candidates) == 1 and requested_name and requested_name == _basename(candidates[0].get("host_path")):
        return candidates[0]
    return None

def build_whatsapp_media_tools(
    session: Any,
    sandbox: DockerSandbox | None,
    *,
    device_id: str = "",
    default_target: str = "",
    allow_workspace_paths: bool = True,
) -> list:
    """
    Tools untuk mengirim gambar dan dokumen ke WhatsApp.
    send_agent_wa_qr dipisah ke build_wa_agent_manager_tools (opt-in via wa_agent_manager).

    device_id / default_target: kw-args opsional — dipakai saat session=None (misal subagent).
    Jika session diberikan, device_id/default_target diambil dari session.channel_config.

    ``allow_workspace_paths`` hanya mengontrol pembacaan path lokal. Base64 dan
    attachment yang terdaftar pada session tetap dapat dikirim tanpa sandbox.
    Builder seperti Arthur tidak memiliki workspace eksekusi, sehingga path yang
    hanya muncul di history/memory tidak boleh diperlakukan sebagai file nyata.
    """
    if session is not None:
        _raw_cfg = session.channel_config
        channel_cfg: dict = _raw_cfg if isinstance(_raw_cfg, dict) else {}
        device_id = channel_cfg.get("device_id", "") or device_id
        default_target = channel_cfg.get("user_phone", "") or default_target

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

        import re as _re

        def _looks_like_base64(s: str) -> bool:
            return len(s) >= 50 and bool(_re.fullmatch(r'[A-Za-z0-9+/]+=*', s))

        if _looks_like_base64(image_path_or_base64):
            image_b64 = image_path_or_base64.strip()
        else:
            resolved = _resolve_session_media_file(
                session,
                image_path_or_base64,
                allowed_media_types={"image", "sticker"},
            )
            if resolved is not None:
                try:
                    image_b64 = base64.b64encode(Path(resolved["host_path"]).read_bytes()).decode("ascii")
                except Exception as exc:
                    return f"[error] Gagal membaca file sesi: {exc}"
                resolved_mimetype = str(resolved.get("mimetype") or "").strip()
                if resolved_mimetype and (not mimetype or mimetype == "image/jpeg"):
                    mimetype = resolved_mimetype
            else:
                if not allow_workspace_paths or sandbox is None:
                    return (
                        "[MEDIA_SOURCE_UNAVAILABLE] Tidak ada gambar tervalidasi untuk "
                        "dikirim pada percakapan ini."
                    )
                path = image_path_or_base64 if image_path_or_base64.startswith("/workspace/") else f"/workspace/{image_path_or_base64}"
                import shlex as _shlex

                b64_output = sandbox.bash(f"base64 -w 0 {_shlex.quote(path)} 2>&1")
                if b64_output.startswith("[") or "No such file" in b64_output:
                    return f"[error] Gagal membaca file: {b64_output}"
                image_b64 = b64_output.strip()

        try:
            from app.core.infra.wa_client import send_wa_image
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

        import re as _re

        def _looks_like_base64(s: str) -> bool:
            return len(s) >= 50 and bool(_re.fullmatch(r'[A-Za-z0-9+/]+=*', s))

        if not _looks_like_base64(file_path_or_base64):
            resolved = _resolve_session_media_file(session, file_path_or_base64)
            if resolved is not None:
                try:
                    doc_b64 = base64.b64encode(Path(resolved["host_path"]).read_bytes()).decode("ascii")
                except Exception as exc:
                    return f"[error] Gagal membaca file sesi: {exc}"
                if not filename or filename == "file":
                    filename = resolved.get("filename") or _basename(file_path_or_base64) or "file"
                resolved_mimetype = str(resolved.get("mimetype") or "").strip()
                if resolved_mimetype and (not mimetype or mimetype == "application/octet-stream"):
                    mimetype = resolved_mimetype
            else:
                if not allow_workspace_paths or sandbox is None:
                    return (
                        "[MEDIA_SOURCE_UNAVAILABLE] Tidak ada dokumen tervalidasi untuk "
                        "dikirim pada percakapan ini."
                    )
                path = file_path_or_base64 if file_path_or_base64.startswith("/workspace/") else f"/workspace/{file_path_or_base64}"
                import shlex as _shlex

                b64_output = sandbox.bash(f"base64 -w 0 {_shlex.quote(path)} 2>&1")
                if b64_output.startswith("[") or "No such file" in b64_output:
                    return f"[error] Gagal membaca file: {b64_output}"
                doc_b64 = b64_output.strip()
                if not filename or filename == "file":
                    import os as _os
                    filename = _os.path.basename(path)
        else:
            doc_b64 = file_path_or_base64.strip()

        try:
            from app.core.infra.wa_client import send_wa_document
            await send_wa_document(device_id, target, doc_b64, filename, caption, mimetype)
            return f"[DOCUMENT_SENT] Dokumen '{filename}' dikirim ke {target}" + (f" dengan caption: {caption}" if caption else "")
        except Exception as exc:
            return f"[error] Gagal kirim dokumen: {exc}"

    return [send_whatsapp_image, send_whatsapp_document]


# ---------------------------------------------------------------------------
# WA Agent Manager tool
# ---------------------------------------------------------------------------

def build_wa_agent_manager_tools(session: Any, db_factory: async_sessionmaker) -> list:
    """Tool send_agent_wa_qr — hanya untuk agent yang perlu mengelola agent lain (e.g. Arthur)."""
    _raw_cfg = session.channel_config
    channel_cfg: dict = _raw_cfg if isinstance(_raw_cfg, dict) else {}
    device_id: str = channel_cfg.get("device_id", "")
    default_target: str = channel_cfg.get("user_phone", "")
    # Verified sender phone of the session owner. This — not a chat-typed number
    # nor the internal LID — is the authoritative QR recipient.
    verified_owner: str = channel_cfg.get("phone_number", "")

    def _resolve_qr_target(phone_arg: str) -> str:
        """Pick a real (non-LID) WhatsApp recipient, owner identity first.

        Prevents the production bug where a chat-typed number (or the internal
        LID user_phone) became the QR target and the owner never received it.
        """
        for candidate in (verified_owner, phone_arg, default_target):
            raw = str(candidate or "")
            # Check the raw value so an "@lid" suffix is caught before normalize
            # strips it (a 15-digit LID body passes the digit-length guard alone).
            if is_probable_whatsapp_lid(raw):
                continue
            normalized = normalize_phone(raw)
            if normalized and not is_probable_whatsapp_lid(normalized):
                return normalized
        return ""

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

        QR selalu dikirim ke nomor WhatsApp owner sesi yang terverifikasi. JANGAN
        isi `phone` dengan nomor yang disebut user di teks chat — nomor itu bisa
        berbeda dari nomor pengirim aslinya, dan QR akan nyasar.

        Args:
            agent_id : UUID agent yang QR-nya ingin dikirim (dari response saat agent dibuat)
            caption  : Caption gambar QR (opsional)
            phone    : Opsional. Hanya fallback kalau nomor owner sesi tak terbaca;
                       tidak menimpa owner terverifikasi.
        """
        target = _resolve_qr_target(phone)
        if not target:
            return (
                "[error] Tidak ada nomor WhatsApp tujuan yang valid. Nomor owner sesi "
                "belum terbaca sebagai nomor asli (kemungkinan masih ID internal/LID). "
                "Minta user mengirim pesan dari nomor WhatsApp aslinya dulu."
            )
        if not device_id:
            return "[error] Tidak ada device_id WhatsApp pada session ini"

        from app.core.infra.wa_client import create_wa_device, get_wa_qr, refresh_wa_qr, send_wa_image
        from app.models.agent import Agent as _Agent
        from sqlalchemy import select as _select
        import uuid as _uuid

        try:
            agent_uuid = _uuid.UUID(agent_id)
        except ValueError:
            return f"[error] agent_id tidak valid: '{agent_id}' — harus berupa UUID"

        async with db_factory() as _db:
            result = await _db.execute(
                _select(_Agent).where(_Agent.id == agent_uuid, _Agent.is_deleted.is_(False))
            )
            agent_row = result.scalar_one_or_none()

        if not agent_row:
            return f"[error] Agent '{agent_id}' tidak ditemukan"
        if not agent_row.wa_device_id:
            agent_row.wa_device_id = str(_uuid.uuid4())
            agent_row.channel_type = "whatsapp"
            async with db_factory() as _db:
                result = await _db.execute(
                    _select(_Agent).where(_Agent.id == agent_uuid, _Agent.is_deleted.is_(False))
                )
                writable_agent = result.scalar_one_or_none()
                if writable_agent:
                    writable_agent.wa_device_id = agent_row.wa_device_id
                    writable_agent.channel_type = "whatsapp"
                    await _db.commit()

        wa_dev_id: str = agent_row.wa_device_id

        try:
            try:
                resp = await get_wa_qr(wa_dev_id)
            except Exception:
                resp = await create_wa_device(wa_dev_id)

            qr_status: str = resp.get("status", "")
            qr_b64: str = resp.get("qr_image", "")

            if qr_status == "connected":
                return f"[INFO] Agent '{agent_id}' sudah terhubung ke WhatsApp — tidak perlu scan QR lagi."

            if not qr_b64:
                # QR kosong: channel mungkin belum ada atau expired. Paksa fresh QR.
                try:
                    resp = await refresh_wa_qr(wa_dev_id)
                except Exception:
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


# ---------------------------------------------------------------------------
# Tavily browsing tools
# ---------------------------------------------------------------------------

def build_tavily_tools(tools_config: dict[str, Any]) -> list:
    from app.core.tools.tavily_tool import build_tavily_tools as _build
    return _build(tools_config)


# ---------------------------------------------------------------------------
# Builder tools — hanya untuk agent dengan capability "builder"
# ---------------------------------------------------------------------------

def build_builder_tools(
    db_factory: async_sessionmaker,
    owner_phone: str | None = None,
    self_agent_id: str | None = None,
    device_id: str = "",
    default_target: str = "",
    session_id: str | None = None,
    sender_name: str | None = None,
) -> list:
    """
    Build tools eksklusif untuk system agent (Agent Builder / Arthur).
    Dipanggil hanya jika agent_model memiliki capability 'builder'.

    Args:
        db_factory: async_sessionmaker factory — each tool call opens its own session
        owner_phone: external_user_id caller (nomor WA/JID) untuk scoping keamanan
        self_agent_id: UUID agent ini sendiri — untuk self-modification
        device_id/default_target: konteks WhatsApp saat tersedia
        session_id: UUID sesi saat ini — untuk membaca file yang dikirim user di workspace
    """
    from app.core.tools.builder_tools import build_builder_tools as _build
    return _build(
        db_factory=db_factory,
        owner_phone=owner_phone,
        self_agent_id=self_agent_id,
        device_id=device_id,
        default_target=default_target,
        session_id=session_id,
        sender_name=sender_name,
    )


def build_deployment_tools(sandbox: "DockerSandbox") -> list:
    """
    Build deploy tools untuk agent dengan sandbox aktif + deploy: true di tools_config.
    Tools: deploy_app, stop_deployment, get_deployment_status, get_deployment_logs.
    """
    from app.core.tools.deployment_tools import build_deployment_tools as _build
    from app.config import get_settings
    settings = get_settings()
    return _build(
        session_id=sandbox.session_id,
        workspace_dir=sandbox.workspace_dir,
        sandbox_image=settings.docker_sandbox_image,
    )
