"""
Tests untuk Phase 3 Agent Builder — builder_tools.py

Verifikasi:
- build_builder_tools() mengembalikan 7 tools yang benar
- Setiap tool punya nama dan deskripsi
- Tool names sesuai yang diharapkan
- import build_builder_tools dari tool_builder.py berfungsi
- agent_runner.py mengimport build_builder_tools
- agent_runner.py punya blok is_system_agent untuk load builder tools
- validate_agent_config logic (via mock db)
- create_agent + list_my_agents isolation by owner_phone (via mock db)
- update_agent ownership check (via mock db)
- get_agent_detail ownership check (via mock db)
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _make_mock_db():
    db = MagicMock()
    db.return_value.__aenter__.return_value = db
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.commit = AsyncMock()
    return db


def _make_mock_agent(
    agent_id: uuid.UUID | None = None,
    name: str = "Test Agent",
    operator_ids: list[str] | None = None,
    owner_external_id: str | None = None,
    capabilities: list[str] | None = None,
    is_deleted: bool = False,
    tools_config: dict | None = None,
    channel_type: str | None = None,
):
    agent = MagicMock()
    agent.id = agent_id or uuid.uuid4()
    agent.name = name
    agent.description = "Test"
    agent.model = "openai/gpt-4.1"
    agent.temperature = 0.7
    agent.tools_config = tools_config or {"memory": True}
    agent.sandbox_config = {}
    agent.safety_policy = {}
    agent.escalation_config = {}
    agent.operator_ids = operator_ids or []
    agent.owner_external_id = owner_external_id
    agent.allowed_senders = None
    agent.capabilities = capabilities or []
    agent.is_deleted = is_deleted
    agent.api_key = "test-key"
    agent.token_quota = 4_000_000
    agent.tokens_used = 0
    agent.active_until = datetime.now(timezone.utc)
    agent.quota_period_days = 30
    agent.wa_device_id = None
    agent.channel_type = channel_type
    agent.version = 1
    agent.instructions = "You are a helpful agent."
    return agent


def _run(coro):
    """Run async coroutine in sync test."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ────────────────────────────────────────────────────────────────────────────
# Section 1: Import & structure tests
# ────────────────────────────────────────────────────────────────────────────

class TestBuilderToolsImport:
    def test_import_from_tools_module(self):
        from app.core.tools.builder_tools import build_builder_tools
        assert callable(build_builder_tools)

    def test_builder_instruction_contract_includes_business_state_workflow(self):
        import inspect
        from app.core.tools import builder_tools

        source = inspect.getsource(builder_tools)
        assert '"state_plan"' in source
        assert '"human_approval_points"' in source
        assert "waiting_payment -> payment_review -> approved -> delivery -> aftercare" in source
        assert "melanjutkan workflow customer dari konteks customer" in source

    def test_research_preset_defaults_to_chat_and_memory_not_report_files(self):
        from app.core.tools.builder_tools import AGENT_PRESETS

        skeleton = AGENT_PRESETS["research_agent"]["instruction_skeleton"]
        assert "Default: jawab hasil riset langsung di chat" in skeleton
        assert "Simpan ringkasan penting ke memory" in skeleton
        assert "Jangan membuat file laporan dengan write_file" in skeleton

    def test_builder_tool_contract_discourages_micro_approval_loops(self):
        import inspect
        from app.core.tools import builder_tools

        source = inspect.getsource(builder_tools)
        assert "Ini bukan approval gate" in source
        assert "Jangan minta user menyetujui blueprint" in source
        assert "langsung create_agent tanpa tanya approval lagi" in source

    def test_import_from_tool_builder(self):
        from app.core.engine.tool_builder import build_builder_tools
        assert callable(build_builder_tools)

    def test_agent_runner_imports_build_builder_tools(self):
        import pathlib
        source = (pathlib.Path(__file__).parent.parent / "app/core/engine/agent_runner.py").read_text()
        assert "build_builder_tools" in source, "agent_runner.py harus import build_builder_tools"

    def test_agent_runner_has_capabilities_check(self):
        import pathlib
        source = (pathlib.Path(__file__).parent.parent / "app/core/engine/agent_runner.py").read_text()
        assert "capabilities" in source, "agent_runner.py harus ada pengecekan capabilities"
        assert "build_builder_tools" in source, "agent_runner.py harus memanggil build_builder_tools"


class TestBuilderToolsReturnsList:
    def test_returns_list(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        assert isinstance(tools, list)

    def test_returns_builder_tool_set(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        assert len(tools) == 19, f"Harus ada 19 tools, dapat {len(tools)}"

    def test_all_tools_have_name(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        for t in tools:
            assert hasattr(t, "name") and t.name

    def test_all_tools_have_description(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        for t in tools:
            assert hasattr(t, "description") and t.description, f"Tool '{t.name}' harus punya description"

    def test_expected_tool_names(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        names = {t.name for t in tools}
        expected = {
            "get_self_config",
            "get_platform_capabilities",
            "get_user_subscription",
            "get_presets",
            "plan_agent",
            "compose_agent_blueprint",
            "compose_agent_instructions",
            "compose_agent_soul",
            "verify_agent",
            "list_available_wa_devices",
            "validate_agent_config",
            "create_agent",
            "create_wa_dev_trial_link",
            "set_agent_memory",
            "update_agent",
            "delete_agent",
            "get_agent_detail",
            "list_my_agents",
            "generate_google_auth_link",
        }
        assert names == expected, f"Tool names tidak sesuai. Dapat: {names}"

    def test_works_without_owner_phone(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone=None)
        assert len(tools) == 19

    def test_travel_planning_request_uses_personal_assistant_not_faq(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        plan = next(t for t in tools if t.name == "plan_agent")

        result = _run(plan.ainvoke({
            "user_goal": (
                "Gua mau liburan. Agent bantu susun itinerary kasar, "
                "checklist dokumen dan barang bawaan, budget, dan ingetin H-7 sama H-1."
            ),
            "agent_name": "Travgent",
            "channel": "whatsapp",
        }))
        payload = json.loads(result)

        assert payload["detected_preset"] == "personal_assistant"
        assert payload["recommended_config"]["tools_config"]["scheduler"] is True
        assert payload["detected_preset"] != "faq_webchat_rag"
        assert payload["google_workspace_option"]["should_offer"] is True
        assert payload["google_workspace_option"]["enabled"] is False
        assert "Google Calendar" in payload["google_workspace_option"]["suggested_apps"]
        assert "Google Docs" in payload["google_workspace_option"]["suggested_apps"]

    def test_plan_agent_blocks_buzzer_or_politics(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        plan = next(t for t in tools if t.name == "plan_agent")

        result = _run(plan.ainvoke({
            "user_goal": "Buat agent buzzer untuk kampanye politik",
            "agent_name": "BuzzerBot",
        }))
        payload = json.loads(result)

        assert payload["plan_status"] == "blocked_by_policy"
        assert "buzzer" in payload["validation_errors"][0].lower()

    def test_explicit_google_calendar_request_enables_google_workspace(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        plan = next(t for t in tools if t.name == "plan_agent")

        result = _run(plan.ainvoke({
            "user_goal": "Agent untuk atur meeting dan reminder pakai Google Calendar",
            "agent_name": "MeetMate",
            "channel": "whatsapp",
            "requested_features": "google, calendar",
        }))
        payload = json.loads(result)
        tools_config = payload["recommended_config"]["tools_config"]

        assert payload["google_workspace_option"]["enabled"] is True
        assert payload["google_workspace_option"]["should_offer"] is False
        assert tools_config["mcp"]["enabled"] is True
        assert "google_workspace" in tools_config["mcp"]["servers"]


class TestBuilderOwnershipHelpers:
    def test_owner_external_id_grants_access_without_operator_id(self):
        from app.core.tools.builder_tools import _agent_belongs_to_owner

        agent = _make_mock_agent(operator_ids=[], owner_external_id="62811xxx")

        assert _agent_belongs_to_owner(agent, "+62811xxx") is True

    def test_unrelated_operator_id_does_not_grant_access(self):
        from app.core.tools.builder_tools import _agent_belongs_to_owner

        agent = _make_mock_agent(operator_ids=["62999yyy"], owner_external_id="62999yyy")

        assert _agent_belongs_to_owner(agent, "+62811xxx") is False


class TestMemoryLeakGuards:
    def test_business_agent_context_detected(self):
        from app.core.domain.memory_service import _is_business_agent_context

        assert _is_business_agent_context("CS toko online mukena Veselka order pelanggan")

    def test_profile_memory_keys_are_recognized(self):
        from app.core.domain.memory_service import _is_personal_profile_memory_key

        assert _is_personal_profile_memory_key("cv_skills")
        assert _is_personal_profile_memory_key("auto_portfolio_website")
        assert not _is_personal_profile_memory_key("customer_preference")


# ────────────────────────────────────────────────────────────────────────────
# Section 2: get_platform_capabilities
# ────────────────────────────────────────────────────────────────────────────

class TestGetPlatformCapabilities:
    def test_returns_json(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "get_platform_capabilities")
        result = _run(tool.ainvoke({}))
        data = json.loads(result)
        assert "tools_config_options" in data
        assert "supported_channels" in data
        assert "recommended_models" in data
        assert "platform_limitations" in data
        assert "wa_best_practices" in data

    def test_tools_config_has_all_keys(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "get_platform_capabilities")
        result = _run(tool.ainvoke({}))
        data = json.loads(result)
        required_keys = ["memory", "skills", "escalation", "sandbox", "tool_creator",
                         "scheduler", "rag", "http", "tavily", "mcp", "whatsapp_media",
                         "wa_agent_manager", "subagents"]
        for key in required_keys:
            assert key in data["tools_config_options"], f"Key '{key}' harus ada di tools_config_options"


class TestGetUserSubscription:
    def test_tier3_unlimited_agent_slot_does_not_crash(self):
        from app.core.tools import builder_tools as bt
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        sub = SimpleNamespace(
            status="active",
            is_usable=True,
            plan_id=uuid.uuid4(),
            token_quota=100_000_000,
            tokens_used=123,
            tokens_remaining=99_999_877,
            expires_at=None,
        )
        plan = SimpleNamespace(id=sub.plan_id, code="tier_3", label="Enterprise", max_agents=None)
        agents = [_make_mock_agent(name="Agent 1"), _make_mock_agent(name="Agent 2")]

        plan_result = MagicMock()
        plan_result.scalar_one.return_value = plan
        agents_result = MagicMock()
        agents_result.scalars.return_value.all.return_value = agents
        db.execute = AsyncMock(side_effect=[plan_result, agents_result])

        async def _fake_get_or_create(_phone, _db):
            return SimpleNamespace(id=uuid.uuid4()), sub

        with patch("app.core.domain.subscription_service.get_or_create_wa_user", _fake_get_or_create):
            tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
            tool = next(t for t in tools if t.name == "get_user_subscription")
            result = _run(tool.ainvoke({}))

        data = json.loads(result)
        assert data["plan_code"] == "tier_3"
        assert data["agents_limit"] is None
        assert data["agents_remaining"] is None
        assert data["token_quota"] == 100_000_000

    def test_lid_owner_uses_real_phone_from_tool_setup(self):
        from app.core.engine.agent_tool_setup import _resolve_builder_owner_phone

        session = SimpleNamespace(
            external_user_id="123456789012345678901",
            channel_config={
                "user_phone": "123456789012345678901@lid",
                "phone_number": "628111222333",
            },
        )

        assert _resolve_builder_owner_phone(session) == "628111222333"


class TestComposeAgentBlueprint:
    def test_repairs_missing_comma_json_from_writer(self):
        from app.core.tools.builder_tools import build_builder_tools

        broken_json = """```json
{
  "agent_summary": "Reserchpedia membantu riset artikel marketing"
  "assumptions": ["User butuh ringkasan yang bisa ditanya ulang"],
  "workflow_steps": [
    {
      "step": 1,
      "name": "Intake topik",
      "agent_action": "Tentukan topik dan sumber",
      "required_user_data": ["topik"],
      "success_criteria": "Scope jelas"
    }
  ],
  "knowledge_plan": {"must_have": ["sumber"], "nice_to_have": [], "needs_upload": true},
  "tool_plan": [{"tool": "http", "why": "ambil sumber", "when_to_use": "saat ada URL"}],
  "memory_plan": [{"key": "research_summaries", "value_to_store": "hasil riset"}],
  "state_plan": [{"state": "intake", "entry_condition": "user minta riset", "allowed_actions": ["klarifikasi"], "exit_condition": "topik jelas"}],
  "human_approval_points": [],
  "escalation_rules": [],
  "conversation_examples_needed": ["ringkas artikel"],
  "validation_checklist": ["ada sumber"],
  "missing_info_questions": []
}
```"""
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "compose_agent_blueprint")

        with patch(
            "app.core.tools.builder_tools._call_instruction_writer",
            new=AsyncMock(return_value=broken_json),
        ):
            result = _run(tool.ainvoke({
                "preset_id": "research_agent",
                "user_goal": "Agent riset yang bisa baca artikel dan simpan hasilnya",
                "agent_name": "Reserchpedia",
                "business_context": "Untuk marketing",
            }))

        data = json.loads(result)
        assert data["parse_status"] == "json_repaired"
        assert data["blueprint"]["agent_summary"] == "Reserchpedia membantu riset artikel marketing"
        assert data["blueprint"]["memory_plan"][0]["key"] == "research_summaries"
        assert "warning" not in data

    def test_research_fallback_is_contextual_when_json_unrecoverable(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "compose_agent_blueprint")

        with patch(
            "app.core.tools.builder_tools._call_instruction_writer",
            new=AsyncMock(return_value="{not valid json"),
        ):
            result = _run(tool.ainvoke({
                "preset_id": "research_agent",
                "user_goal": "Agent riset yang bisa baca artikel atau topik, kasih ringkasan poin penting, dan simpan hasilnya supaya bisa diingat buat tanya ulang nanti.",
                "agent_name": "Reserchpedia",
                "business_context": "Agent ini membantu pengguna di bidang marketing untuk update tren.",
            }))

        data = json.loads(result)
        blueprint = data["blueprint"]
        assert data["parse_status"] == "deterministic_fallback"
        assert "riset" in blueprint["agent_summary"].lower()
        assert any(step["name"] == "Simpan hasil riset" for step in blueprint["workflow_steps"])
        assert any(memory["key"] == "research_summaries" for memory in blueprint["memory_plan"])
        assert blueprint["knowledge_plan"]["needs_upload"] is True
        assert "warning" not in data


# ────────────────────────────────────────────────────────────────────────────
# Section 3: validate_agent_config
# ────────────────────────────────────────────────────────────────────────────

class TestValidateAgentConfig:
    def test_valid_config_passes(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "validate_agent_config")
        result = _run(tool.ainvoke({
            "name": "CS Agent",
            "instructions": "Kamu adalah CS dari toko kami. " * 20 + "Eskalasikan jika tidak bisa menangani.",
            "tools_config": '{"memory": true}',
            "model": "openai/gpt-4.1",
        }))
        data = json.loads(result)
        assert data["valid"] is True
        assert len(data["errors"]) == 0

    def test_empty_name_fails(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "validate_agent_config")
        result = _run(tool.ainvoke({"name": "", "instructions": "test " * 20}))
        data = json.loads(result)
        assert data["valid"] is False
        assert any("Nama" in e for e in data["errors"])

    def test_tool_creator_without_sandbox_fails(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "validate_agent_config")
        result = _run(tool.ainvoke({
            "name": "Test",
            "instructions": "test instructions " * 5,
            "tools_config": '{"tool_creator": true, "sandbox": false}',
        }))
        data = json.loads(result)
        assert data["valid"] is False
        assert any("tool_creator" in e for e in data["errors"])

    def test_buzzer_or_politics_fails_policy(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "validate_agent_config")
        result = _run(tool.ainvoke({
            "name": "BuzzerBot",
            "instructions": "Agent ini mengatur kampanye politik dan opini publik. " * 5,
        }))
        data = json.loads(result)

        assert data["valid"] is False
        assert any("buzzer" in e.lower() or "politik" in e.lower() for e in data["errors"])

    def test_markdown_in_instructions_warns(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "validate_agent_config")
        result = _run(tool.ainvoke({
            "name": "Test",
            "instructions": "**Bold text** dan *italic* dan # Header " * 5,
            "channel_type": "whatsapp",
        }))
        data = json.loads(result)
        assert any("markdown" in w for w in data["warnings"])

    def test_has_quality_score(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "validate_agent_config")
        result = _run(tool.ainvoke({"name": "Test", "instructions": "Good instructions " * 10}))
        data = json.loads(result)
        assert "quality_score" in data
        assert 0 <= data["quality_score"] <= 100


# ────────────────────────────────────────────────────────────────────────────
# Section 4: create_agent
# ────────────────────────────────────────────────────────────────────────────

class TestCreateAgent:
    def test_creates_agent_in_db(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        with patch("app.core.tools.builder_tools.Agent") as MockAgent:
            instance = _make_mock_agent(operator_ids=["+62811xxx"])
            MockAgent.return_value = instance

            tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
            tool = next(t for t in tools if t.name == "create_agent")
            result = _run(tool.ainvoke({
                "name": "CS Agent",
                "instructions": "Kamu adalah CS yang membantu pelanggan.",
                "tools_config": '{"memory": true, "escalation": true}',
            }))

            data = json.loads(result)
            assert data["success"] is True
            assert "agent_id" in data
            assert "api_key" in data
            db.add.assert_called_once()
            db.flush.assert_called_once()

    def test_owner_phone_added_to_operator_ids(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        with patch("app.core.tools.builder_tools.Agent") as MockAgent:
            captured_kwargs = {}

            def capture(**kwargs):
                captured_kwargs.update(kwargs)
                return _make_mock_agent(operator_ids=["+62811xxx"])

            MockAgent.side_effect = capture

            tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
            tool = next(t for t in tools if t.name == "create_agent")
            _run(tool.ainvoke({
                "name": "Test Agent",
                "instructions": "Test instructions",
            }))

            assert "+62811xxx" in captured_kwargs.get("operator_ids", []), \
                "owner_phone harus masuk ke operator_ids saat create_agent"
            assert captured_kwargs.get("tools_config", {}).get("tavily") is True, \
                "agent yang dibuat Arthur harus default punya browsing Tavily"

    def test_create_agent_with_google_workspace_config_appends_instruction(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        with patch("app.core.tools.builder_tools.Agent") as MockAgent:
            captured_kwargs = {}

            def capture(**kwargs):
                captured_kwargs.update(kwargs)
                return _make_mock_agent(name=kwargs.get("name", "Research"))

            MockAgent.side_effect = capture

            tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
            tool = next(t for t in tools if t.name == "create_agent")
            result = _run(tool.ainvoke({
                "name": "Research Google",
                "instructions": "Kamu adalah agent riset.",
                "tools_config": json.dumps({
                    "memory": True,
                    "mcp": {
                        "enabled": True,
                        "servers": {
                            "google_workspace": {
                                "url": "https://example.test/mcp",
                                "transport": "streamable_http",
                            }
                        },
                    },
                }),
            }))

        data = json.loads(result)
        assert data["success"] is True
        assert "Google Docs" in captured_kwargs["instructions"]
        assert "MCP" not in captured_kwargs["instructions"]

    def test_create_agent_blocks_buzzer_or_politics(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "create_agent")
        result = _run(tool.ainvoke({
            "name": "Buzzer Politik",
            "instructions": "Agent untuk kampanye politik dan propaganda opini publik.",
        }))
        data = json.loads(result)

        assert "error" in data
        assert "politik" in data["error"].lower()
        db.add.assert_not_called()

    def test_invalid_name_returns_error(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "create_agent")
        result = _run(tool.ainvoke({"name": "X", "instructions": "test"}))
        assert "[error]" in result

    def test_invalid_tools_config_json_returns_error(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "create_agent")
        result = _run(tool.ainvoke({
            "name": "Test Agent",
            "instructions": "test",
            "tools_config": "not-valid-json",
        }))
        assert "[error]" in result


# ────────────────────────────────────────────────────────────────────────────
# Section 5: create_wa_dev_trial_link
# ────────────────────────────────────────────────────────────────────────────

class TestCreateWADevTrialLink:
    def test_uses_whatsapp_context_when_phone_omitted(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        my_agent = _make_mock_agent(name="Agent Baru", operator_ids=["+62811xxx"])
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        settings = MagicMock()
        settings.wa_dev_public_name = "Arthur AI Dev"
        settings.wa_dev_public_phone = "+628123456789"

        with (
            patch("app.core.tools.builder_tools.get_settings", return_value=settings),
            patch(
                "app.core.domain.wa_dev_trial_service.ensure_wa_dev_trial_code",
                new=AsyncMock(return_value="AB234C"),
            ),
            patch("app.core.infra.wa_client.send_wa_contact", new=AsyncMock()) as send_contact,
        ):
            tools = build_builder_tools(
                db_factory=db,
                owner_phone="+62811xxx",
                device_id="arthur-device",
                default_target="+62811xxx",
            )
            tool = next(t for t in tools if t.name == "create_wa_dev_trial_link")
            result = _run(tool.ainvoke({"agent_id": str(my_agent.id)}))

        data = json.loads(result)
        assert data["success"] is True
        assert data["code"] == "AB234C"
        assert data["contact_sent"] is True
        send_contact.assert_awaited_once_with(
            "arthur-device",
            "+62811xxx",
            "Arthur AI Dev",
            "628123456789",
        )

    def test_omitted_agent_id_uses_latest_owned_non_builder_agent(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        old_agent = _make_mock_agent(name="CV Maker", operator_ids=["+62811xxx"])
        latest_agent = _make_mock_agent(name="CS Toko Baju Cewek", operator_ids=["+62811xxx"])
        arthur_agent = _make_mock_agent(
            name="Arthur",
            operator_ids=["+62811xxx"],
            tools_config={"builder": True},
            capabilities=["builder"],
        )
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [arthur_agent, latest_agent, old_agent]
        db.execute = AsyncMock(return_value=mock_result)

        settings = MagicMock()
        settings.wa_dev_public_name = "Arthur AI Dev"
        settings.wa_dev_public_phone = "+628123456789"

        with (
            patch("app.core.tools.builder_tools.get_settings", return_value=settings),
            patch(
                "app.core.domain.wa_dev_trial_service.ensure_wa_dev_trial_code",
                new=AsyncMock(return_value="3HRNM4"),
            ) as ensure_code,
            patch("app.core.infra.wa_client.send_wa_contact", new=AsyncMock()),
        ):
            tools = build_builder_tools(
                db_factory=db,
                owner_phone="+62811xxx",
                self_agent_id=str(arthur_agent.id),
                device_id="arthur-device",
                default_target="+62811xxx",
            )
            tool = next(t for t in tools if t.name == "create_wa_dev_trial_link")
            result = _run(tool.ainvoke({}))

        data = json.loads(result)
        assert data["success"] is True
        assert data["agent_id"] == str(latest_agent.id)
        assert data["agent_name"] == "CS Toko Baju Cewek"
        ensure_code.assert_awaited_once()
        assert ensure_code.await_args.args[1] is latest_agent


# ────────────────────────────────────────────────────────────────────────────
# Section 6: delete_agent
# ────────────────────────────────────────────────────────────────────────────

class TestDeleteAgent:
    def test_requires_exact_name_confirmation(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        my_agent = _make_mock_agent(name="Agent Lama", operator_ids=["+62811xxx"])
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "delete_agent")
        result = _run(tool.ainvoke({"agent_id": str(my_agent.id)}))
        data = json.loads(result)
        assert data["needs_confirmation"] is True
        assert my_agent.is_deleted is False
        db.commit.assert_not_awaited()

    def test_soft_deletes_owned_agent_after_confirmation(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        my_agent = _make_mock_agent(name="Agent Lama", operator_ids=["+62811xxx"])
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "delete_agent")
        result = _run(tool.ainvoke({
            "agent_id": str(my_agent.id),
            "confirm_name": "Agent Lama",
        }))
        data = json.loads(result)
        assert data["success"] is True
        assert my_agent.is_deleted is True
        assert my_agent.version == 2
        db.commit.assert_awaited()

    def test_rejects_access_to_others_agent(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        other_agent = _make_mock_agent(name="Agent Orang", operator_ids=["+62999yyy"])
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = other_agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "delete_agent")
        result = _run(tool.ainvoke({
            "agent_id": str(other_agent.id),
            "confirm_name": "Agent Orang",
        }))
        assert "[error]" in result
        assert other_agent.is_deleted is False


# ────────────────────────────────────────────────────────────────────────────
# Section 6: list_my_agents
# ────────────────────────────────────────────────────────────────────────────

class TestListMyAgents:
    def test_returns_only_owned_agents(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        my_agent = _make_mock_agent(name="My Agent", operator_ids=["+62811xxx"])
        other_agent = _make_mock_agent(name="Other Agent", operator_ids=["+62999yyy"])

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [my_agent, other_agent]
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "list_my_agents")
        result = _run(tool.ainvoke({}))
        data = json.loads(result)

        assert data["count"] == 1
        assert data["agents"][0]["name"] == "My Agent"

    def test_returns_agents_owned_by_owner_external_id_even_without_operator_id(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        my_agent = _make_mock_agent(
            name="Owned via owner field",
            operator_ids=[],
            owner_external_id="62811xxx",
        )
        other_agent = _make_mock_agent(
            name="Other Agent",
            operator_ids=[],
            owner_external_id="62999yyy",
        )

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [my_agent, other_agent]
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "list_my_agents")
        result = _run(tool.ainvoke({}))
        data = json.loads(result)

        assert data["count"] == 1
        assert data["agents"][0]["name"] == "Owned via owner field"

    def test_empty_if_no_agents(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "list_my_agents")
        result = _run(tool.ainvoke({}))
        data = json.loads(result)
        assert data["count"] == 0

    def test_error_if_no_owner_phone(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone=None)
        tool = next(t for t in tools if t.name == "list_my_agents")
        result = _run(tool.ainvoke({}))
        assert "[error]" in result


# ────────────────────────────────────────────────────────────────────────────
# Section 6: update_agent ownership check
# ────────────────────────────────────────────────────────────────────────────

class TestUpdateAgent:
    def test_rejects_update_of_agent_not_owned(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        other_agent = _make_mock_agent(operator_ids=["+62999yyy"])
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = other_agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "update_agent")
        result = _run(tool.ainvoke({
            "agent_id": str(other_agent.id),
            "name": "Hacked Name",
        }))
        assert "[error]" in result
        assert "akses" in result.lower()

    def test_invalid_uuid_returns_error(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "update_agent")
        result = _run(tool.ainvoke({"agent_id": "not-a-uuid", "name": "Test"}))
        assert "[error]" in result

    def test_returns_info_if_no_fields_changed(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        my_agent = _make_mock_agent(operator_ids=["+62811xxx"])
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "update_agent")
        result = _run(tool.ainvoke({"agent_id": str(my_agent.id)}))
        assert "[info]" in result

    def test_update_agent_name_only_does_not_crash_google_flag(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        my_agent = _make_mock_agent(operator_ids=["+62811xxx"])
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "update_agent")
        result = _run(tool.ainvoke({
            "agent_id": str(my_agent.id),
            "name": "Agent Baru",
        }))
        data = json.loads(result)

        assert data["success"] is True
        assert "google_workspace_enabled" not in data
        assert my_agent.name == "Agent Baru"

    def test_update_agent_blocks_buzzer_or_politics(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        my_agent = _make_mock_agent(operator_ids=["+62811xxx"])
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "update_agent")
        result = _run(tool.ainvoke({
            "agent_id": str(my_agent.id),
            "instructions": "Ubah jadi agent buzzer untuk kampanye politik.",
        }))
        data = json.loads(result)

        assert "error" in data
        assert "politik" in data["error"].lower()
        db.commit.assert_not_called()

    def test_enable_google_workspace_updates_tools_and_instructions(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        my_agent = _make_mock_agent(
            name="Reserchpedia",
            tools_config={"memory": True, "tavily": True},
        )
        my_agent.instructions = "Kamu adalah agent riset."
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone=None)
        tool = next(t for t in tools if t.name == "update_agent")
        result = _run(tool.ainvoke({
            "agent_id": str(my_agent.id),
            "enable_google_workspace": True,
        }))
        data = json.loads(result)

        assert data["success"] is True
        assert data["google_workspace_enabled"] is True
        assert data["readback"]["tools_config_has_google_workspace"] is True
        assert data["readback"]["instructions_include_google_workspace"] is True
        assert my_agent.tools_config["mcp"]["enabled"] is True
        assert "google_workspace" in my_agent.tools_config["mcp"]["servers"]
        assert "Google Docs" in my_agent.instructions
        assert "MCP" not in my_agent.instructions
        assert "MCP" not in data["next_step"]


# ────────────────────────────────────────────────────────────────────────────
# Section 7: get_agent_detail ownership check
# ────────────────────────────────────────────────────────────────────────────

class TestGetAgentDetail:
    def test_returns_detail_for_owned_agent(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        my_agent = _make_mock_agent(operator_ids=["+62811xxx"])
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "get_agent_detail")
        result = _run(tool.ainvoke({"agent_id": str(my_agent.id)}))
        data = json.loads(result)
        assert data["name"] == my_agent.name
        assert "tools_config" in data
        assert "instructions_preview" in data

    def test_rejects_access_to_others_agent(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        other_agent = _make_mock_agent(operator_ids=["+62999yyy"])
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = other_agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "get_agent_detail")
        result = _run(tool.ainvoke({"agent_id": str(other_agent.id)}))
        assert "[error]" in result

    def test_not_found_returns_error(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "get_agent_detail")
        result = _run(tool.ainvoke({"agent_id": str(uuid.uuid4())}))
        assert "[error]" in result
