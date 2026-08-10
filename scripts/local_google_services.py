"""Local standalone services for Google Integration API (8003) and Google Workspace MCP Server (8002).

Run this script to provide local endpoints for development, testing, and AI agent OAuth workflows.
The MCP server fetches live data from Google Sheets CSV export and caches it with a TTL.
"""
import csv
import json
import os
import sys
import threading
import time
from io import StringIO

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SPREADSHEET_ID = "1tMTtgF1nbWG_67FKEjXJJ4Cxqk0zEKHKuao1irPtyis"
SHEET_TABS = ["master", "books", "members", "borrowing", "reservations", "staf", "inventory_log", "settings"]
CACHE_TTL_SECONDS = 120  # Re-fetch from Google at most every 2 minutes

# Tab name aliases → canonical tab name
TAB_MAP = {
    "master": "master",
    "books": "books", "book": "books",
    "members": "members", "member": "members",
    "borrowing": "borrowing", "borrow": "borrowing", "booking": "borrowing", "peminjaman": "borrowing",
    "reservations": "reservations", "reservation": "reservations",
    "staf": "staf", "staff": "staf",
    "inventory_log": "inventory_log", "log": "inventory_log",
    "settings": "settings",
}

# ---------------------------------------------------------------------------
# Sheet Cache with TTL
# ---------------------------------------------------------------------------
_sheet_cache: dict[str, list[list[str]]] = {}
_cache_timestamp: float = 0.0


def _fetch_sheet_tab_sync(tab_name: str) -> list[list[str]] | None:
    """Fetch a single sheet tab as CSV from Google Sheets public export (synchronous)."""
    url = (
        f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
        f"/gviz/tq?tqx=out:csv&sheet={tab_name}"
    )
    try:
        resp = httpx.get(url, timeout=15.0, follow_redirects=True)
        if resp.status_code == 200 and resp.text.strip():
            reader = csv.reader(StringIO(resp.text))
            rows = list(reader)
            if rows:
                return rows
    except Exception as exc:
        print(f"[WARN] Failed to fetch tab '{tab_name}': {exc}")
    return None


def _refresh_cache_if_needed():
    """Re-fetch all tabs if TTL has expired. Returns True if cache has data."""
    global _cache_timestamp
    now = time.time()
    if _sheet_cache and (now - _cache_timestamp) < CACHE_TTL_SECONDS:
        return True  # Cache is fresh

    print(f"[INFO] Refreshing sheet cache (age={(now - _cache_timestamp):.0f}s)...")
    fetched_any = False
    for tab in SHEET_TABS:
        rows = _fetch_sheet_tab_sync(tab)
        if rows:
            _sheet_cache[tab] = rows
            fetched_any = True
            print(f"  [OK] {tab}: {len(rows)} rows")
        else:
            print(f"  [FAIL] {tab}: fetch failed (keeping old cache if available)")

    if fetched_any:
        _cache_timestamp = now
    return bool(_sheet_cache)


# ---------------------------------------------------------------------------
# Google Integration API (Port 8003)
# ---------------------------------------------------------------------------
app_8003 = FastAPI(title="Google Integration Service (Stub)")


@app_8003.get("/health")
def health_8003():
    return {"status": "ok", "service": "google-integration-api"}


@app_8003.get("/v1/integrations/google/status")
def google_status(external_user_id: str = "default_user", agent_id: str = None):
    return {
        "connected": True,
        "external_user_id": external_user_id,
        "agent_id": agent_id,
        "status": "connected",
        "google_email": "faizakhrianputra@gmail.com",
    }


@app_8003.post("/v1/integrations/google/connect")
async def google_connect(request: Request):
    data = await request.json() if request.headers.get("content-type") == "application/json" else {}
    user_id = data.get("external_user_id", "default_user")
    agent_id = data.get("agent_id", "")

    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8003/v1/integrations/google/callback")

    if client_id:
        scopes = "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive.file"
        auth_url = (
            f"https://accounts.google.com/o/oauth2/v2/auth?"
            f"client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&"
            f"scope={scopes}&access_type=offline&prompt=consent&state={user_id}"
        )
    else:
        auth_url = f"http://localhost:8003/mock-consent?state={user_id}&agent_id={agent_id}"

    return {"auth_url": auth_url, "authorization_url": auth_url}


@app_8003.get("/v1/integrations/google/callback")
async def google_callback(code: str = "", state: str = "default_user", scope: str = ""):
    from fastapi.responses import HTMLResponse

    # Notify main API if accessible
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                "http://localhost:8000/v1/integrations/google/oauth-success",
                json={"external_user_id": state, "google_email": "faizakhrianputra@gmail.com"},
                headers={"X-API-Key": os.getenv("API_KEY", "42523db14d86f993409fba4984764be01fb169ddf7e5e401efab2f33442c9a7b")},
            )
    except Exception:
        pass

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Autentikasi Google Berhasil</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #f8fafc; color: #0f172a; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
            .card { background: #ffffff; padding: 32px; border-radius: 16px; box-shadow: 0 10px 25px -5px rgba(0,0,0,0.1); text-align: center; max-width: 420px; width: 90%; }
            .icon { font-size: 48px; margin-bottom: 16px; }
            h2 { margin: 0 0 8px 0; color: #166534; font-size: 22px; }
            p { color: #475569; font-size: 14px; line-height: 1.6; margin: 0 0 24px 0; }
            .badge { background: #dcfce7; color: #15803d; padding: 6px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; display: inline-block; margin-bottom: 16px; }
            .btn { background: #2563eb; color: #ffffff; border: none; padding: 10px 20px; border-radius: 8px; font-weight: 600; cursor: pointer; font-size: 14px; text-decoration: none; }
        </style>
    </head>
    <body>
        <div class="card">
            <div class="icon">✅</div>
            <div class="badge">Google Connected</div>
            <h2>Autentikasi Berhasil!</h2>
            <p>Akun Google kamu telah terhubung secara aman. Sekarang kamu bisa kembali ke WhatsApp / Chat untuk menggunakan fitur Google Sheets &amp; Drive.</p>
            <button class="btn" onclick="window.close()">Tutup Halaman Ini</button>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app_8003.get("/mock-consent")
def mock_consent(state: str = "default_user", agent_id: str = ""):
    from fastapi.responses import HTMLResponse

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><title>Google OAuth Consent (Local Test)</title></head>
    <body style="font-family:sans-serif; text-align:center; padding:50px;">
        <h2>Google Workspace OAuth Authorization (Development Mode)</h2>
        <p>User ID: <strong>{state}</strong></p>
        <p>Agent ID: <strong>{agent_id}</strong></p>
        <div style="background:#e8f0fe; padding:15px; display:inline-block; border-radius:8px;">
            <p>✅ Standard Google Sheets &amp; Drive Scopes Allowed</p>
            <p><em>To use Google's live consent page, set <code>GOOGLE_OAUTH_CLIENT_ID</code> in <code>.env</code>.</em></p>
        </div>
        <br/><br/>
        <button onclick="window.close()" style="padding:10px 20px; font-size:16px; cursor:pointer;">
            Close &amp; Return to App
        </button>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app_8003.get("/v1/integrations/google/token")
def google_token(external_user_id: str = "default_user", agent_id: str = None):
    return {
        "bearer_token": "mock_google_workspace_jwt_token_12345",
        "external_user_id": external_user_id,
        "agent_id": agent_id,
    }


# ---------------------------------------------------------------------------
# Google Workspace MCP Server (Port 8002)
#
# Implements the JSON-RPC 2.0 protocol expected by langchain-mcp-adapters
# with streamable_http transport. Every request is a POST with a JSON-RPC
# body; the server returns a JSON-RPC response.
# ---------------------------------------------------------------------------
app_8002 = FastAPI(title="Google Workspace MCP Server")


@app_8002.get("/health")
def health_8002():
    return {"status": "ok", "service": "google-workspace-mcp"}


def _jsonrpc_response(result: dict, req_id: int | str | None) -> dict:
    """Build a standard JSON-RPC 2.0 response."""
    return {"jsonrpc": "2.0", "result": result, "id": req_id}


def _jsonrpc_error(code: int, message: str, req_id: int | str | None) -> dict:
    """Build a standard JSON-RPC 2.0 error response."""
    return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": req_id}


def _tool_result(payload: dict) -> dict:
    """Wrap a tool execution result in the MCP content envelope."""
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
        ]
    }


def _handle_tools_list() -> dict:
    """Return the list of available tools."""
    return {
        "tools": [
            {
                "name": "search_drive_files",
                "description": "Search files in Google Drive",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "read_sheet_values",
                "description": "Read values from a Google Sheets spreadsheet tab. Use range_name like 'books!A1:Z100' to specify the tab.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": {"type": "string"},
                        "range_name": {"type": "string"},
                    },
                    "required": ["spreadsheet_id"],
                },
            },
        ]
    }


def _handle_tools_call(tool_name: str, arguments: dict) -> dict:
    """Execute a tool and return the result payload."""
    t_low = tool_name.lower()

    # --- read_sheet_values ---
    if "sheet" in t_low or "value" in t_low or "read" in t_low or "data" in t_low:
        target_range = str(arguments.get("range_name") or arguments.get("range") or "books!A1:Z100").strip()

        # Parse tab name from range (e.g. "members!A1:Z100" → "members")
        if "!" in target_range:
            raw_sheet = target_range.split("!", 1)[0].strip("'\" ").casefold()
        else:
            raw_sheet = target_range.casefold()

        tab_name = TAB_MAP.get(raw_sheet, "books")

        # Refresh cache if stale
        _refresh_cache_if_needed()

        live_values = _sheet_cache.get(tab_name)
        if not live_values:
            # Explicit error — never fabricate data
            return _tool_result({
                "error": f"Failed to fetch data from tab '{tab_name}'. The Google Sheets CSV export may be unavailable.",
                "range": target_range,
                "values": [],
            })

        return _tool_result({
            "range": target_range,
            "majorDimension": "ROWS",
            "values": live_values,
        })

    # --- search_drive_files ---
    if "search" in t_low or "drive" in t_low or "file" in t_low:
        q = str(arguments.get("query") or arguments.get("q") or "").strip()
        return _tool_result({
            "files": [
                {
                    "id": SPREADSHEET_ID,
                    "name": q if q else "PerpustakaanAegleseeker",
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                }
            ]
        })

    # Unknown tool
    return _tool_result({"error": f"Unknown tool: {tool_name}"})


@app_8002.api_route("/mcp", methods=["GET", "POST", "OPTIONS"])
@app_8002.api_route("/tools/call", methods=["POST", "OPTIONS"])
async def mcp_endpoint(request: Request):
    """Handle MCP JSON-RPC 2.0 requests."""
    if request.method == "OPTIONS":
        return JSONResponse({"status": "ok"})

    # Parse JSON-RPC body
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    req_id = body.get("id")
    method = body.get("method", "")

    # --- initialize ---
    if method == "initialize":
        return JSONResponse(_jsonrpc_response(
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "google-workspace-mcp", "version": "1.0.0"},
            },
            req_id,
        ))

    # --- notifications/initialized ---
    if method == "notifications/initialized":
        return JSONResponse(_jsonrpc_response({}, req_id))

    # --- tools/list ---
    if method == "tools/list":
        return JSONResponse(_jsonrpc_response(_handle_tools_list(), req_id))

    # --- tools/call ---
    if method == "tools/call" or method == "":
        params = body.get("params", {})
        tool_name = params.get("name") or body.get("name") or body.get("tool") or ""
        arguments = params.get("arguments") or body.get("arguments") or {}

        if not tool_name:
            return JSONResponse(_jsonrpc_error(-32602, "Missing tool name", req_id))

        result = _handle_tools_call(tool_name, arguments)
        return JSONResponse(_jsonrpc_response(result, req_id))

    # --- unknown method ---
    return JSONResponse(_jsonrpc_error(-32601, f"Method not found: {method}", req_id))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def run_8003():
    uvicorn.run(app_8003, host="0.0.0.0", port=8003, log_level="warning")


def run_8002():
    uvicorn.run(app_8002, host="0.0.0.0", port=8002, log_level="warning")


if __name__ == "__main__":
    # Pre-warm the cache at startup
    print("Pre-warming sheet cache...")
    _refresh_cache_if_needed()
    print(f"Cache ready: {list(_sheet_cache.keys())}")

    t1 = threading.Thread(target=run_8003, daemon=True)
    t2 = threading.Thread(target=run_8002, daemon=True)
    t1.start()
    t2.start()
    print("Starting local Google Integration Service on http://localhost:8003")
    print("Starting local Google Workspace MCP Server on http://localhost:8002")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping services...")
