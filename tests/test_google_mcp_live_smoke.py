from __future__ import annotations

import json
import base64
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import pytest

RUN_LIVE = os.getenv("RUN_GOOGLE_MCP_LIVE_SMOKE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STRICT = os.getenv("GOOGLE_MCP_LIVE_SMOKE_STRICT", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

if not RUN_LIVE:
    pytestmark = pytest.mark.skip(
        reason="Set RUN_GOOGLE_MCP_LIVE_SMOKE=true to run live Google MCP smoke suite"
    )


@dataclass
class SmokeConfig:
    integration_url: str
    mcp_url: str
    external_user_id: str
    agent_id: str


class MCPToolError(Exception):
    pass


class GoogleMCPClient:
    def __init__(self, cfg: SmokeConfig) -> None:
        self.cfg = cfg
        self.http = httpx.Client(timeout=120)
        self.headers: dict[str, str] = {"Accept": "application/json, text/event-stream"}
        self.session_id: str | None = None

    def close(self) -> None:
        if self.session_id:
            try:
                self.http.delete(self.cfg.mcp_url, headers=self.headers)
            except Exception:
                pass
        self.http.close()

    def _last_sse_json(self, text: str) -> dict | None:
        payload: dict | None = None
        for line in text.splitlines():
            if not line.startswith("data: "):
                continue
            raw = line[6:].strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
        return payload

    def _token(self) -> str:
        resp = self.http.get(
            f"{self.cfg.integration_url}/v1/integrations/google/token",
            params={
                "external_user_id": self.cfg.external_user_id,
                "agent_id": self.cfg.agent_id,
            },
        )
        resp.raise_for_status()
        token = resp.json().get("bearer_token")
        if not token:
            raise RuntimeError("No bearer_token from integration API")
        return token

    def connect(self) -> None:
        token = self._token()
        self.headers["Authorization"] = f"Bearer {token}"

        init_body = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "google-mcp-live-smoke", "version": "1.0"},
            },
        }
        init_resp = self.http.post(self.cfg.mcp_url, headers=self.headers, json=init_body)
        init_resp.raise_for_status()

        self.session_id = init_resp.headers.get("mcp-session-id") or init_resp.headers.get(
            "Mcp-Session-Id"
        )
        if not self.session_id:
            raise RuntimeError("MCP initialize succeeded but session id header is missing")

        self.headers["mcp-session-id"] = self.session_id
        self.http.post(
            self.cfg.mcp_url,
            headers=self.headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )

    def call_tool(self, name: str, arguments: dict, req_id: int) -> str:
        body = {
            "jsonrpc": "2.0",
            "id": str(req_id),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        resp = self.http.post(self.cfg.mcp_url, headers=self.headers, json=body)
        resp.raise_for_status()

        payload = self._last_sse_json(resp.text)
        if payload and payload.get("error"):
            raise MCPToolError(str(payload["error"]))

        result = (payload or {}).get("result", {})
        content = result.get("content", [])
        text = "\n".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        ).strip()

        if text.startswith("Error calling tool"):
            raise MCPToolError(text)

        if not text:
            text = resp.text[:1000]

        return text


def _extract_first(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None




def _extract_calendar_event_id(text: str) -> str | None:
    direct = _extract_first(r"\(ID:\s*([A-Za-z0-9_-]+)\)", text)
    if direct:
        return direct
    eid = _extract_first(r"[?&]eid=([A-Za-z0-9_-]+)", text)
    if not eid:
        return None
    padded = eid + ("=" * (-len(eid) % 4))
    try:
        decoded = base64.urlsafe_b64decode(padded).decode()
    except Exception:
        return None
    return decoded.split(" ", 1)[0].strip() or None

def _handle_optional_service_failure(err: MCPToolError) -> None:
    msg = str(err)
    disabled_markers = (
        "has not been used in project",
        "is disabled",
        "enable it by visiting",
    )
    if any(marker in msg.lower() for marker in disabled_markers):
        if STRICT:
            pytest.fail(msg)
        pytest.skip(msg)
    raise err


@pytest.fixture(scope="module")
def smoke_cfg() -> SmokeConfig:
    return SmokeConfig(
        integration_url=os.getenv("GOOGLE_MCP_INTEGRATION_URL", "http://localhost:8003"),
        mcp_url=os.getenv("GOOGLE_MCP_URL", "http://localhost:8002/mcp"),
        external_user_id=os.getenv("GOOGLE_MCP_EXTERNAL_USER_ID", "62895619356936"),
        agent_id=os.getenv("GOOGLE_MCP_AGENT_ID", "46ed1c39-c343-4d42-a5ff-2559f43efa0e"),
    )


@pytest.fixture(scope="module")
def mcp(smoke_cfg: SmokeConfig) -> GoogleMCPClient:
    client = GoogleMCPClient(smoke_cfg)
    client.connect()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="module")
def state() -> dict[str, str]:
    return {}


def test_sheets_create_and_edit(mcp: GoogleMCPClient, state: dict[str, str]) -> None:
    title = f"Smoke Sheets {datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out = mcp.call_tool("create_spreadsheet", {"title": title}, req_id=10)
    spreadsheet_id = _extract_first(r"/spreadsheets/d/([A-Za-z0-9_-]+)", out)
    assert spreadsheet_id, out
    state["spreadsheet_id"] = spreadsheet_id

    out2 = mcp.call_tool(
        "modify_sheet_values",
        {
            "spreadsheet_id": spreadsheet_id,
            "range_name": "A1:C2",
            "values": [
                ["Field", "Status", "CheckedAt"],
                ["Sheets", "OK", datetime.now().isoformat(timespec="seconds")],
            ],
            "value_input_option": "USER_ENTERED",
        },
        req_id=11,
    )
    assert "Successfully updated range" in out2, out2


def test_slides_create_and_edit(mcp: GoogleMCPClient, state: dict[str, str]) -> None:
    title = f"Smoke Slides {datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out = mcp.call_tool("create_presentation", {"title": title}, req_id=20)
    presentation_id = _extract_first(r"/presentation/d/([A-Za-z0-9_-]+)", out)
    assert presentation_id, out
    state["presentation_id"] = presentation_id

    out2 = mcp.call_tool(
        "batch_update_presentation",
        {"presentation_id": presentation_id, "requests": [{"createSlide": {}}]},
        req_id=21,
    )
    assert "Batch Update Completed" in out2, out2


def test_docs_create_and_edit(mcp: GoogleMCPClient, state: dict[str, str]) -> None:
    title = f"Smoke Doc {datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out = mcp.call_tool("create_doc", {"title": title, "content": "hello"}, req_id=30)
    document_id = _extract_first(r"/document/d/([A-Za-z0-9_-]+)", out)
    assert document_id, out
    state["document_id"] = document_id

    out2 = mcp.call_tool(
        "modify_doc_text",
        {"document_id": document_id, "start_index": 1, "text": "[edited from smoke]\\n"},
        req_id=31,
    )
    assert "Inserted text" in out2 or "Successfully" in out2, out2


def test_drive_create_and_edit(mcp: GoogleMCPClient, state: dict[str, str]) -> None:
    name = f"Smoke Drive {datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    out = mcp.call_tool(
        "create_drive_file",
        {"file_name": name, "content": "safe smoke test"},
        req_id=40,
    )
    file_id = _extract_first(r"/file/d/([A-Za-z0-9_-]+)", out) or _extract_first(
        r"File ID:\s*([A-Za-z0-9_-]+)", out
    )
    assert file_id, out
    state["drive_file_id"] = file_id

    out2 = mcp.call_tool(
        "update_drive_file",
        {"file_id": file_id, "description": "updated by live smoke suite"},
        req_id=41,
    )
    assert "Successfully updated file" in out2 or "Changes applied" in out2, out2


def test_calendar_create_and_edit(mcp: GoogleMCPClient, state: dict[str, str]) -> None:
    start = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)
    end = start + timedelta(minutes=20)
    out = mcp.call_tool(
        "manage_event",
        {
            "action": "create",
            "summary": "Smoke Calendar Event",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "description": "safe smoke create",
        },
        req_id=50,
    )

    event_id = _extract_calendar_event_id(out)
    assert event_id, out
    state["event_id"] = event_id

    start2 = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0)
    end2 = start2 + timedelta(minutes=25)
    out2 = mcp.call_tool(
        "manage_event",
        {
            "action": "update",
            "event_id": event_id,
            "summary": "Smoke Calendar Event Updated",
            "description": "safe smoke update",
            "start_time": start2.isoformat(),
            "end_time": end2.isoformat(),
        },
        req_id=51,
    )
    assert "Successfully modified event" in out2 or "Successfully updated" in out2, out2


def test_gmail_draft_only(mcp: GoogleMCPClient) -> None:
    out = mcp.call_tool(
        "draft_gmail_message",
        {
            "subject": "Smoke Draft",
            "body": "This is a safe smoke draft. Not sent.",
            "to": "bagasbgs2516@gmail.com",
        },
        req_id=60,
    )
    assert "Draft created" in out, out


def test_tasks_create_list_and_task(mcp: GoogleMCPClient, state: dict[str, str]) -> None:
    out = mcp.call_tool(
        "manage_task_list",
        {"action": "create", "title": f"Smoke Tasks {datetime.now().strftime('%H%M%S')}"},
        req_id=70,
    )
    task_list_id = _extract_first(r"\bID:\s*([A-Za-z0-9_-]+)", out)
    assert task_list_id, out
    state["task_list_id"] = task_list_id

    out2 = mcp.call_tool(
        "manage_task",
        {
            "action": "create",
            "task_list_id": task_list_id,
            "title": "Smoke Task",
            "notes": "safe smoke",
        },
        req_id=71,
    )
    assert "Task Created" in out2, out2


def test_forms_create_and_edit(mcp: GoogleMCPClient, state: dict[str, str]) -> None:
    title = f"Smoke Form {datetime.now().strftime('%Y%m%d-%H%M%S')}"
    try:
        out = mcp.call_tool("create_form", {"title": title}, req_id=80)
    except MCPToolError as err:
        _handle_optional_service_failure(err)
        return

    form_id = _extract_first(r"Form ID:\s*([A-Za-z0-9_-]+)", out)
    assert form_id, out
    state["form_id"] = form_id

    out2 = mcp.call_tool(
        "batch_update_form",
        {
            "form_id": form_id,
            "requests": [
                {
                    "updateFormInfo": {
                        "info": {"description": "updated by live smoke suite"},
                        "updateMask": "description",
                    }
                }
            ],
        },
        req_id=81,
    )
    assert "Batch Update Completed" in out2, out2


def test_contacts_create_and_edit(mcp: GoogleMCPClient, state: dict[str, str]) -> None:
    try:
        out = mcp.call_tool(
            "manage_contact",
            {
                "action": "create",
                "given_name": "Smoke",
                "family_name": "LiveSuite",
                "emails": [{"value": "smoke.livesuite@example.com", "type": "work"}],
            },
            req_id=90,
        )
    except MCPToolError as err:
        _handle_optional_service_failure(err)
        return

    contact_id = _extract_first(r"Contact ID:\s*([^\s]+)", out)
    assert contact_id, out
    state["contact_id"] = contact_id

    out2 = mcp.call_tool(
        "manage_contact",
        {
            "action": "update",
            "contact_id": contact_id,
            "given_name": "Smoke",
            "family_name": "LiveSuiteUpdated",
        },
        req_id=91,
    )
    assert "Contact Updated" in out2, out2
