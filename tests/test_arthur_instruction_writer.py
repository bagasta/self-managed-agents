import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arthur_v2 import instruction_writer as writer, plugin
from app.models.agent import Agent
from sqlalchemy import select
from test_arthur_p1_operations import database, agent_row, OWNER


MARKDOWN = """# SYSTEM MESSAGE — Test Agent

Kamu adalah **Test Agent**, asisten untuk pengingat pribadi.

---

## 1. MISI
Bantu owner mencatat agenda.

## 2. PERSONA
Panggil owner **Kak**, gunakan bahasa Indonesia.

## 3. ALUR KERJA
1. Klarifikasi jadwal.
2. Beri ringkasan tanpa mengklaim sudah mengirim pesan.
"""


@pytest.fixture
def provider(monkeypatch):
    settings = SimpleNamespace(openrouter_api_key="test-only-key", llm_request_timeout_seconds=120,
        arthur_instruction_writer_model=writer.DEFAULT_MODEL, arthur_instruction_writer_max_tokens=16384)
    monkeypatch.setattr(writer, "get_settings", lambda: settings)
    completion = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content=MARKDOWN, tool_calls=None))],
        usage=SimpleNamespace(total_tokens=200)))
    client_options = {}
    class Client:
        def __init__(self, **kwargs):
            client_options.update(kwargs)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=completion))
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(writer, "AsyncOpenAI", Client)
    return completion, client_options, settings


async def compose():
    return await writer.compose_assistant_instructions(name="Test Agent", purpose="Pengingat pribadi",
        instructions="Panggil Kak. Jangan terima order.", workflow={}, capabilities={"scheduler": True},
        tool_policy="Gunakan scheduler hanya setelah persetujuan.")


@pytest.mark.asyncio
async def test_writer_uses_exact_openrouter_model_with_no_execution_tools(provider):
    completion, options, settings = provider
    assert await compose() == MARKDOWN.strip()
    args = completion.await_args.kwargs
    assert args["model"] == "anthropic/claude-sonnet-5.5"
    assert options["base_url"] == "https://openrouter.ai/api/v1"
    assert "tools" not in args and "tool_choice" not in args
    assert args["max_tokens"] == 16384
    payload = json.loads(args["messages"][1]["content"])
    assert payload["approved_instructions"] == "Panggil Kak. Jangan terima order."
    assert payload["capabilities"] == {"scheduler": True}
    assert "memory" not in payload and "history" not in payload
    assert "test-only-key" not in str(args)


@pytest.mark.asyncio
@pytest.mark.parametrize("content,finish,tools", [
    ("Instruksi biasa", "stop", None),
    ("```md\n" + MARKDOWN + "\n```", "stop", None),
    (MARKDOWN, "length", None),
    (MARKDOWN.replace("Test Agent", "Wrong Agent"), "stop", None),
    (MARKDOWN, "stop", [{"name": "execute"}]),
    ("", "stop", None),
])
async def test_incomplete_or_wrong_markdown_is_rejected(provider, content, finish, tools):
    completion, _, _ = provider
    completion.return_value.choices[0] = SimpleNamespace(finish_reason=finish,
        message=SimpleNamespace(content=content, tool_calls=tools))
    with pytest.raises(writer.InstructionWriterError):
        await compose()


@pytest.mark.asyncio
async def test_missing_credentials_does_not_fall_back_to_chat_model(provider):
    completion, _, settings = provider
    settings.openrouter_api_key = ""
    with pytest.raises(writer.InstructionWriterError):
        await compose()
    completion.assert_not_awaited()


def tool(factory, owner=OWNER):
    return next(t for t in plugin.build_arthur_v2_tools(db_factory=factory, owner_phone=owner,
        self_agent_id=None) if t.name == "update_assistant")


@pytest.mark.asyncio
async def test_update_persists_markdown_and_keeps_runtime_model_and_permissions(database, provider, monkeypatch):
    agent = await agent_row(database, name="Test Agent", model="deepseek/deepseek-v4-flash")
    old_config = agent.tools_config.copy()
    monkeypatch.setattr(plugin, "compose_assistant_instructions", writer.compose_assistant_instructions)
    reply = await tool(database).ainvoke({"agent_id": str(agent.id), "instructions": "Panggil Kak.", "confirmed": True})
    assert reply["ok"]
    async with database() as db:
        stored = await db.get(Agent, agent.id)
        assert stored.instructions.startswith(MARKDOWN.strip())
        assert stored.instructions.endswith(plugin._build_target_tool_usage(google_services=["sheets"], scheduler=False))
        assert stored.model == "deepseek/deepseek-v4-flash"
        assert stored.tools_config["mcp"] == old_config["mcp"]
        receipt = stored.tools_config["assistant_profile"]["instruction_writer"]
        assert receipt == writer.writer_receipt("Panggil Kak.")
    assert provider[0].await_args.kwargs["model"] == writer.DEFAULT_MODEL


@pytest.mark.asyncio
async def test_provider_failure_leaves_all_fields_untouched(database, provider, monkeypatch):
    agent = await agent_row(database)
    monkeypatch.setattr(plugin, "compose_assistant_instructions", writer.compose_assistant_instructions)
    provider[0].side_effect = RuntimeError("private provider detail")
    reply = await tool(database).ainvoke({"agent_id": str(agent.id), "name": "Changed",
        "purpose": "Changed", "instructions": "Changed", "confirmed": True})
    assert not reply["ok"] and "private provider" not in str(reply)
    async with database() as db:
        stored = await db.get(Agent, agent.id)
        assert stored.name == agent.name and stored.description == agent.description
        assert stored.instructions == "Existing SOP" and stored.version == 7
        assert stored.tools_config == agent.tools_config


@pytest.mark.asyncio
async def test_writer_waits_for_confirmation_and_ownership_and_skips_name_only(database, provider, monkeypatch):
    agent = await agent_row(database)
    monkeypatch.setattr(plugin, "compose_assistant_instructions", writer.compose_assistant_instructions)
    args = {"agent_id": str(agent.id), "instructions": "Changed"}
    assert (await tool(None).ainvoke(args))["needs_confirmation"]
    assert not (await tool(database, "6289999999999").ainvoke({**args, "confirmed": True}))["ok"]
    assert (await tool(database).ainvoke({"agent_id": str(agent.id), "name": "New name", "confirmed": True}))["ok"]
    provider[0].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_creation_compiles_before_inserting_and_preserves_default_runtime(database, provider, monkeypatch, failure):
    from app.core.domain import subscription_service
    user = SimpleNamespace(id=uuid.uuid4(), phone_number=OWNER, external_id=OWNER)
    subscription = SimpleNamespace(status="active", is_usable=True, expires_at=None)
    plan = SimpleNamespace(code="tier_3", label="Test", max_agents=10)
    monkeypatch.setattr(subscription_service, "get_best_subscription_by_external_ids",
        AsyncMock(return_value=(user, subscription, plan)))
    # SQLite cannot compile PostgreSQL JSON containment in the shared owner OR.
    monkeypatch.setattr(plugin, "owner_filter", lambda *_: Agent.owner_external_id == OWNER)
    monkeypatch.setattr(plugin, "compose_assistant_instructions", writer.compose_assistant_instructions)
    if failure:
        provider[0].side_effect = RuntimeError("provider unavailable")
    create = next(t for t in plugin.build_arthur_v2_tools(db_factory=database, owner_phone=OWNER,
        self_agent_id=None) if t.name == "create_assistant")
    reply = await create.ainvoke({"name": "Test Agent", "purpose": "Catat agenda pribadi",
        "instructions": "Panggil Kak. Bantu catat agenda.", "confirmed": True})
    assert reply["ok"] is (not failure)
    async with database() as db:
        agents = list((await db.execute(select(Agent))).scalars())
        if failure:
            assert agents == []
        else:
            assert len(agents) == 1
            assert agents[0].instructions.startswith(MARKDOWN.strip())
            assert agents[0].model == plugin.ASSISTANT_MODELS["normal"]
            assert agents[0].tools_config["assistant_profile"]["instruction_writer"]["format"] == "markdown"
