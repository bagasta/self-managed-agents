"""Shared, isolated helpers for opt-in Arthur operations tools."""
from __future__ import annotations

import ast
import copy
import re
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import structlog
from sqlalchemy import select

from app.models.agent import Agent
from app.models.run import Run
from app.models.session import Session
from app.core.domain.agent_ownership import agent_belongs_to_owner


class OperationRejected(ValueError):
    pass


async def owned_agent(db, agent_id, owner_phone, self_agent_id, *, lock=False):
    try:
        parsed = uuid.UUID(str(agent_id))
    except (ValueError, TypeError, AttributeError):
        raise OperationRejected("Assistant tidak ditemukan atau bukan milik pengguna ini.")
    query = select(Agent).where(Agent.id == parsed, Agent.is_deleted.is_(False))
    if lock:
        query = query.with_for_update()
    agent = (await db.execute(query)).scalar_one_or_none()
    if (agent is None or not agent_belongs_to_owner(agent, owner_phone)
            or str(agent.id) == str(self_agent_id)
            or "builder" in (agent.capabilities or [])
            or (agent.tools_config or {}).get("system_plugin") == "arthur_v2"):
        raise OperationRejected("Assistant tidak ditemukan atau bukan assistant operasional milik pengguna ini.")
    return agent


async def require_idle(db, agent_ids):
    query = select(Run.id).join(Session, Session.id == Run.session_id).where(
        Session.agent_id.in_(agent_ids), Run.status.in_(["running", "pending"]),
    ).limit(1)
    if (await db.execute(query)).first():
        raise OperationRejected("Assistant sedang memproses pekerjaan. Tunggu selesai lalu coba lagi.")


def google_config(agent):
    config = agent.tools_config or {}
    mcp = config.get("mcp") or {}
    servers = mcp.get("servers", mcp)
    return servers.get("google_workspace") or {}


def configured_sheet(agent):
    value = str((agent.tools_config or {}).get("google_workspace_resources", {}).get("default_spreadsheet_id") or "")
    if not value:
        raise OperationRejected("Spreadsheet target belum dikonfigurasi. Tentukan resource melalui konfigurasi agent terlebih dahulu.")
    return value


def write_allowed(agent):
    cfg = google_config(agent)
    mcp = (agent.tools_config or {}).get("mcp") or {}
    if mcp.get("enabled") is False or cfg.get("enabled") is False:
        raise OperationRejected("Koneksi Google dinonaktifkan dalam konfigurasi agent.")
    operations = cfg.get("allowed_operations") or {}
    # Respect explicit read-only policies; do not grant any new scope.
    return "sheets" not in operations or bool(set(operations["sheets"]) & {"write", "update"})


@asynccontextmanager
async def google_tools(agent, session=None):
    """Owner connection, existing service/operation policy; no OAuth creation."""
    from app.config import get_settings
    from app.core.engine.agent_google_routing import prepare_agent_google_workspace_runtime
    from app.core.engine.google_mcp_support import filter_google_mcp_tools_by_services, sanitize_google_forms_tools
    from app.core.tools.mcp_tool import mcp_client_context
    from .google_oauth import get_google_oauth_status

    cfg = google_config(agent)
    mcp = (agent.tools_config or {}).get("mcp") or {}
    if mcp.get("enabled") is False or cfg.get("enabled") is False:
        raise OperationRejected("Koneksi Google dinonaktifkan dalam konfigurasi agent.")
    if not cfg or (cfg.get("allowed_services") and "sheets" not in cfg["allowed_services"]):
        raise OperationRejected("Agent belum memiliki layanan Google Sheets.")
    subject = agent.owner_external_id or next(iter(agent.operator_ids or []), None)
    if not subject:
        raise OperationRejected("Identitas pemilik koneksi Google belum tersedia.")
    status = await get_google_oauth_status(external_user_id=subject, agent_id=str(agent.id))
    if not status.get("connected"):
        raise OperationRejected("Google belum terhubung. Gunakan tool OAuth yang sudah ada.")
    # Runtime bearer injection must never dirty the persisted agent configuration.
    view = SimpleNamespace(owner_external_id=subject, operator_ids=list(agent.operator_ids or []),
                           tools_config=copy.deepcopy(agent.tools_config), escalation_config=copy.deepcopy(agent.escalation_config))
    view.tools_config["mcp"] = {"enabled": True, "servers": {"google_workspace": copy.deepcopy(cfg)}}
    session = session or SimpleNamespace(external_user_id=subject, channel_type="whatsapp", channel_config={})
    log = structlog.get_logger(__name__)
    runtime = await prepare_agent_google_workspace_runtime(
        agent_model=view, session=session, tools_config=view.tools_config, tools=[], agent_id=agent.id,
        active_groups=[], memory_scope=session.external_user_id, api_key=get_settings().api_key,
        user_message="", system_prompt="", log=log,
    )
    if runtime.preflight_error or not runtime.connected_user_id:
        raise OperationRejected("Koneksi Google belum dapat digunakan. Jalankan diagnosis koneksi.")
    async with mcp_client_context(view.tools_config) as (tools, errors):
        if errors:
            raise OperationRejected("Tool Google belum dapat dihubungi.")
        tools = filter_google_mcp_tools_by_services(tools, tools_config=view.tools_config,
                                                  requirement_text=agent.instructions or "", log=log)
        tools = sanitize_google_forms_tools(tools, log)
        yield {t.name: t for t in tools}


def result_text(result):
    if isinstance(result, str):
        try:
            parsed = ast.literal_eval(result)
        except (ValueError, SyntaxError):
            return result
        if isinstance(parsed, (list, dict)):
            return result_text(parsed)
        return result
    if isinstance(result, dict):
        if result.get("isError"):
            raise OperationRejected("Tool Google mengembalikan kegagalan.")
        return str(result.get("text", "")) if "text" in result else result_text(result.get("content", []))
    if isinstance(result, list):
        return "\n".join(result_text(item) for item in result)
    return str(result)


def range_shape(range_name, *, max_cells=100):
    match = re.fullmatch(r"(?:'([^']+)'|([A-Za-z_][\w .-]*))!([A-Z]{1,3})([1-9]\d*)(?::([A-Z]{1,3})([1-9]\d*))?", range_name)
    if not match:
        raise OperationRejected("Gunakan range A1 eksplisit dengan nama tab, misalnya 'Jawaban'!A2:C2.")
    def col(value):
        n = 0
        for c in value:
            n = n * 26 + ord(c) - 64
        return n
    tab = match[1] or match[2]
    first_col, first_row = col(match[3]), int(match[4])
    last_col, last_row = col(match[5] or match[3]), int(match[6] or match[4])
    h, w = last_row - first_row + 1, last_col - first_col + 1
    if min(h, w) < 1 or h * w > max_cells:
        raise OperationRejected(f"Range maksimal {max_cells} sel dan harus berurutan.")
    return tab, first_row, first_col, h, w


def canonical_values(values, h, w):
    if len(values) > h or any(not isinstance(row, list) or len(row) > w for row in values):
        raise OperationRejected("Ukuran data berbeda dari range tujuan.")
    return [[str(values[r][c]) if r < len(values) and c < len(values[r]) and values[r][c] is not None else ""
             for c in range(w)] for r in range(h)]


def read_values(result, range_name):
    text = result_text(result)
    if not re.search(r"Successfully read\s+\d+\s+rows?", text, re.I) and "No data found" not in text:
        raise OperationRejected("Hasil baca Google tidak dapat diverifikasi.")
    rows = []
    for line in text.splitlines():
        match = re.match(r"\s*Row\s+\d+:\s*(\[.*\])\s*$", line)
        if match:
            try:
                row = ast.literal_eval(match[1])
            except (ValueError, SyntaxError):
                raise OperationRejected("Hasil baca Google terpotong atau tidak valid.")
            rows.append(row)
    count = re.search(r"Successfully read\s+(\d+)", text, re.I)
    if count and int(count[1]) != len(rows):
        raise OperationRejected("Hasil baca Google tidak lengkap; penulisan dibatalkan.")
    _, _, _, h, w = range_shape(range_name)
    return canonical_values(rows, h, w)


async def read_range(tools, spreadsheet_id, range_name):
    if "read_sheet_values" not in tools:
        raise OperationRejected("Tool baca spreadsheet tidak tersedia.")
    return read_values(await tools["read_sheet_values"].ainvoke({"spreadsheet_id": spreadsheet_id, "range_name": range_name}), range_name)


async def write_range(tools, spreadsheet_id, range_name, values):
    if "modify_sheet_values" not in tools:
        raise OperationRejected("Tool tulis spreadsheet tidak tersedia.")
    text = result_text(await tools["modify_sheet_values"].ainvoke({"spreadsheet_id": spreadsheet_id, "range_name": range_name,
                                                                "values": values, "value_input_option": "RAW"}))
    if not re.search(r"Successfully (?:updated|modified|wrote)", text, re.I):
        raise OperationRejected("Hasil penulisan belum pasti; periksa data sebelum mencoba kembali.")
