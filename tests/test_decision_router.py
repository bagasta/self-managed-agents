import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.tools import tool

from app.core.engine.computer_task_policy import guard_computer_task_reply
from app.core.engine.decision_router import DECISIONS_URL, MODEL, DecisionRouter, decision_prompt


@pytest.fixture
def settings():
    return SimpleNamespace(openrouter_api_key="test-secret", decision_router_enabled=True,
                           decision_router_model=MODEL, decision_router_timeout_seconds=1,
                           decision_router_min_confidence=0.7, decision_router_max_calls_per_run=2)


def available(name):
    return SimpleNamespace(name=name, description=f"Use {name}")


def mock_provider(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr("app.core.engine.decision_router.httpx.AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


def response(request, choices, confidence=0.95):
    return httpx.Response(200, json={"answers": {
        name: {"type": "choice", "choice": value, "confidence": confidence,
               "probabilities": {value: 1.0}} for name, value in choices.items()
    }})


@pytest.mark.asyncio
async def test_routes_to_real_tool_and_uses_decisions_protocol(settings, monkeypatch):
    def handler(request):
        assert str(request.url) == DECISIONS_URL
        data = json.loads(request.content)
        assert data["model"] == MODEL
        assert "messages" not in data
        assert "computer" not in data["questions"]["route"]["criteria"]
        assert set(data["questions"]["next_tool"]["criteria"]) == {"none", "search_web"}
        return response(request, {"route": "tools", "next_tool": "search_web"})
    mock_provider(monkeypatch, handler)
    records = []
    router = DecisionRouter(settings=settings, request="Cari harga terbaru", context="", tools=[available("search_web")], record=records.append)
    result = await router.decide()
    assert result["status"] == "accepted"
    assert records == [result]
    assert "search_web" in decision_prompt(result)


@pytest.mark.asyncio
async def test_computer_route_and_inbound_image(settings, monkeypatch):
    def handler(request):
        data = json.loads(request.content)
        assert data["state"][1]["image_url"]["url"] == "data:image/png;base64,YQ=="
        assert "computer" in data["questions"]["route"]["criteria"]
        assert "visual_progress" not in data["questions"]  # An attachment is not the live desktop.
        return response(request, {"route": "computer", "next_tool": "computer_screenshot", "visual_progress": "continue"})
    mock_provider(monkeypatch, handler)
    router = DecisionRouter(settings=settings, request="Bikin seperti referensi ini", context="Canva", tools=[available("computer_screenshot")])
    result = await router.decide(image_url="data:image/png;base64,YQ==")
    assert result["status"] == "accepted"
    assert result["has_image"] is True


@pytest.mark.asyncio
async def test_uncertain_next_tool_preserves_confident_route(settings, monkeypatch):
    def handler(request):
        result = response(request, {"route": "computer", "next_tool": "computer_screenshot"}).json()
        result["answers"]["next_tool"]["confidence"] = .4
        return httpx.Response(200, json=result)
    mock_provider(monkeypatch, handler)
    router = DecisionRouter(settings=settings, request="Buat di Canva", context="", tools=[available("computer_screenshot")])
    result = await router.decide()
    assert result["status"] == "accepted"
    assert result["route"]["accepted"] is True
    assert result["next_tool"]["accepted"] is False
    assert "computer_screenshot" not in decision_prompt(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,confidence,expected", [("unknown_tool", .95, "unavailable"), ("search_web", .4, "uncertain"), ("search_web", float("inf"), "unavailable")])
async def test_invalid_or_uncertain_choices_do_not_force_route(settings, monkeypatch, choice, confidence, expected):
    # json.dumps permits Infinity; the adapter must reject it after decoding.
    def handler(request):
        data = {"answers": {name: {"type": "choice", "choice": value, "confidence": confidence, "probabilities": {value: 1.0}}
                            for name, value in {"route": "tools", "next_tool": choice}.items()}}
        return httpx.Response(200, content=json.dumps(data))
    mock_provider(monkeypatch, handler)
    router = DecisionRouter(settings=settings, request="help", context="", tools=[available("search_web")])
    result = await router.decide()
    assert result["status"] == expected
    assert decision_prompt(result) == ""


@pytest.mark.asyncio
async def test_timeout_is_safe_and_budget_is_bounded(settings, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("test-secret: confidential body")
    mock_provider(monkeypatch, handler)
    router = DecisionRouter(settings=settings, request="help", context="", tools=[])
    for _ in range(2):
        result = await router.decide()
        assert result["reason"] == "ReadTimeout"
        assert "secret" not in json.dumps(result)
    assert (await router.decide())["reason"] == "decision_budget_exhausted"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_visual_review_respects_existing_screenshot_gate(settings, monkeypatch):
    @tool
    async def computer_screenshot() -> dict:
        """Capture only when allowed."""
        return {"ok": False, "code": "human_input_active"}
    def handler(request):
        pytest.fail("A blocked screenshot must not reach the provider")
    mock_provider(monkeypatch, handler)
    router = DecisionRouter(settings=settings, request="poster", context="", tools=[computer_screenshot])
    result = await router.as_tool().ainvoke({"progress": "checking", "inspect_computer": True})
    assert result["reason"] == "screenshot_unavailable"


@pytest.mark.asyncio
async def test_visual_review_sends_fresh_screen(settings, monkeypatch):
    @tool
    async def computer_screenshot() -> list:
        """Capture the current screen."""
        return [{"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}]
    def handler(request):
        data = json.loads(request.content)
        assert data["state"][1]["type"] == "image_url"
        return response(request, {"route": "computer", "next_tool": "computer_screenshot", "visual_progress": "replan"})
    mock_provider(monkeypatch, handler)
    router = DecisionRouter(settings=settings, request="Buat poster", context="", tools=[computer_screenshot])
    result = await router.as_tool().ainvoke({"progress": "Template masih kosong", "inspect_computer": True})
    assert result["visual_progress"]["choice"] == "replan"


def test_semantic_routing_overrides_keyword_guard():
    assert guard_computer_task_reply("Ini ide poster Canva.", [], "Kasih ide poster Canva", computer_enabled=True, task_required=False)[1] is None
    assert guard_computer_task_reply("Selesai", [], "Kerjakan yang tadi", computer_enabled=True, task_required=True)[1] == "missing_computer_action"


def test_uncertain_route_preserves_independently_accepted_visual_review():
    prompt = decision_prompt({
        "status": "uncertain",
        "route": {"choice": "computer", "accepted": False},
        "visual_progress": {"choice": "replan", "accepted": True},
    })
    assert "replan" in prompt
    assert '"route"' not in prompt


def test_observability_record_excludes_request_and_image():
    record = {
        "model": MODEL,
        "status": "accepted",
        "has_image": True,
        "route": {"choice": "computer", "confidence": .84, "accepted": True},
        "usage": {"input_tokens": 500, "cost": .00005},
    }
    serialized = json.dumps(record)
    assert "latest_request" not in serialized
    assert "image_url" not in serialized
