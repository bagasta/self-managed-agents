from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
from starlette.requests import Request

from app import meta_ingress


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, agent):
        self.agent = agent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _query):
        return _ScalarResult(self.agent)


def _request(payload: dict) -> Request:
    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/webhooks/meta",
            "headers": [(b"x-hub-signature-256", b"sha256=test")],
        },
        receive,
    )


def _payload() -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": "phone-id"},
                    "contacts": [{"wa_id": "6281", "profile": {"name": "Budi"}}],
                    "messages": [{
                        "id": "wamid.1",
                        "from": "6281",
                        "type": "text",
                        "text": {"body": "Halo"},
                    }],
                },
            }],
        }],
    }


@pytest.mark.asyncio
async def test_n8n_is_processed_inside_ingress_without_ai_staff(monkeypatch):
    agent = SimpleNamespace(id="agent-id", wa_inbound_route="n8n")
    monkeypatch.setattr(meta_ingress, "AsyncSessionLocal", lambda: _Session(agent))
    monkeypatch.setattr(meta_ingress, "verify_webhook_signature", lambda *_args: True)
    background = BackgroundTasks()

    result = await meta_ingress.receive_meta_webhook(_request(_payload()), background)

    assert result == {"ok": True, "service": "meta-ingress"}
    assert [task.func for task in background.tasks] == [meta_ingress._process_n8n]
    assert background.tasks[0].args[:2] == ("agent-id", "phone-id")


@pytest.mark.asyncio
async def test_ai_staff_is_forwarded_without_running_n8n(monkeypatch):
    agent = SimpleNamespace(id="agent-id", wa_inbound_route="ai_staff")
    monkeypatch.setattr(meta_ingress, "AsyncSessionLocal", lambda: _Session(agent))
    monkeypatch.setattr(meta_ingress, "verify_webhook_signature", lambda *_args: True)
    background = BackgroundTasks()

    await meta_ingress.receive_meta_webhook(_request(_payload()), background)

    assert [task.func for task in background.tasks] == [meta_ingress._forward_ai_staff]


@pytest.mark.asyncio
async def test_ai_staff_failure_is_contained(monkeypatch):
    class BrokenClient:
        async def __aenter__(self):
            raise RuntimeError("AI Staff is down")

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(meta_ingress.httpx, "AsyncClient", lambda **_kwargs: BrokenClient())

    # Must not propagate into the standalone ingress process.
    await meta_ingress._forward_ai_staff(b"{}", "sha256=test")
