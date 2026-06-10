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
    created_by_type: str | None = None,
    created_by_agent_id: str | None = None,
    created_by_agent_name: str | None = None,
):
    agent = MagicMock()
    agent.id = agent_id or uuid.uuid4()
    agent.name = name
    agent.description = "Test"
    agent.model = "openai/gpt-4.1"
    agent.temperature = 0.7
    agent.max_tokens = 4096
    agent.tools_config = tools_config or {"memory": True}
    agent.sandbox_config = {}
    agent.safety_policy = {}
    agent.escalation_config = {}
    agent.operator_ids = operator_ids or []
    agent.owner_external_id = owner_external_id
    agent.created_by_type = created_by_type
    agent.created_by_agent_id = created_by_agent_id
    agent.created_by_agent_name = created_by_agent_name
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


def _usable_operating_manual(domain: str = "ecommerce") -> dict:
    return {
        "manual_id": "agent_operating_manual",
        "version": 1,
        "source": "arthur_template",
        "domain": domain,
        "domain_confidence": "high",
        "maturity": "usable",
        "owner_review_required": False,
        "missing_context": [],
        "assumptions": [],
        "workflows": [
            {
                "workflow_id": "customer_intake",
                "name": "Terima kebutuhan customer",
                "trigger": "Customer bertanya.",
                "goal": "Mengumpulkan kebutuhan customer.",
                "required_inputs": ["nama", "kebutuhan"],
                "steps": ["Sapa customer.", "Tanyakan kebutuhan.", "Ringkas kebutuhan."],
                "decision_points": ["Jika butuh keputusan final, eskalasi."],
                "allowed_tools": [],
                "escalation_rules": ["Eskalasi jika data bisnis belum pasti."],
                "prohibited_actions": ["Jangan mengarang harga/stok/kebijakan."],
                "final_output": "Ringkasan kebutuhan customer.",
                "examples": [],
            }
        ],
    }


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
        assert len(tools) == 21, f"Harus ada 21 tools, dapat {len(tools)}"

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
            "compose_agent_operating_manual",
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
            "add_agent_knowledge",
        }
        assert names == expected, f"Tool names tidak sesuai. Dapat: {names}"

    def test_works_without_owner_phone(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone=None)
        assert len(tools) == 21

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

    def test_plan_agent_requires_purpose_for_generic_new_agent_request(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        plan = next(t for t in tools if t.name == "plan_agent")

        result = _run(plan.ainvoke({
            "user_goal": "buat agent baru",
            "agent_name": "",
            "channel": "whatsapp",
        }))
        payload = json.loads(result)

        assert payload["plan_status"] == "needs_clarification"
        assert payload["capability_clarifications"][0]["topic"] == "agent_purpose"
        assert "JANGAN create_agent dulu" in payload["next_action"]

    def test_plan_agent_defaults_unspecified_channel_to_whatsapp(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        plan = next(t for t in tools if t.name == "plan_agent")

        result = _run(plan.ainvoke({
            "user_goal": "Buat agent riset kompetitor dan rangkum insight pasar",
            "agent_name": "RisetBot",
        }))
        payload = json.loads(result)

        assert payload["channel"] == "whatsapp"
        assert payload["recommended_config"]["channel_type"] == "whatsapp"

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

    def test_personal_assistant_generate_file_enables_sandbox_and_google_workspace(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        plan = next(t for t in tools if t.name == "plan_agent")

        result = _run(plan.ainvoke({
            "user_goal": "Bikinin personal assistant yang bisa generate file dan terhubung ke Google Workspace",
            "agent_name": "Baas",
            "channel": "whatsapp",
            "requested_features": "google",
        }))
        payload = json.loads(result)
        tools_config = payload["recommended_config"]["tools_config"]

        assert payload["detected_preset"] == "personal_assistant"
        assert payload["plan_status"] == "ready"
        assert tools_config["sandbox"] is True
        assert tools_config["whatsapp_media"] is True
        assert tools_config["subagents"]["enabled"] is True
        assert tools_config["mcp"]["enabled"] is True
        assert "google_workspace" in tools_config["mcp"]["servers"]

    def test_existing_google_form_order_link_does_not_enable_workspace(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        plan = next(t for t in tools if t.name == "plan_agent")

        result = _run(plan.ainvoke({
            "user_goal": "Agent WhatsApp untuk CeritaCV, cara order pelanggan isi Google Form yang sudah ada.",
            "agent_name": "CeritaCV",
            "channel": "whatsapp",
            "business_context": (
                "Pelanggan pilih jasa CV lalu isi Google Form ini "
                "https://forms.gle/pe5C1XncFhu56E7M9 sebagai link order."
            ),
            "requested_features": "customer service, escalation, whatsapp_media",
        }))
        payload = json.loads(result)
        tools_config = payload["recommended_config"]["tools_config"]

        assert payload["google_workspace_option"]["enabled"] is False
        assert payload["google_workspace_option"]["should_offer"] is False
        assert tools_config.get("mcp") in (None, False)

    def test_approval_gated_service_agent_enables_handoff_and_file_generation_for_cv_case(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        plan = next(t for t in tools if t.name == "plan_agent")

        result = _run(plan.ainvoke({
            "user_goal": (
                "Agent WhatsApp jasa bikin CV ATS. Customer diwawancara, bayar dulu, "
                "kirim bukti transfer ke admin, lalu CV dikirim setelah admin approve."
            ),
            "agent_name": "CVin aja",
            "channel": "whatsapp",
            "requested_features": "bikin CV ATS, dokumen PDF, bukti transfer, approval admin",
        }))
        payload = json.loads(result)
        tools_config = payload["recommended_config"]["tools_config"]

        assert payload["detected_preset"] == "approval_gated_service_agent"
        assert tools_config["sandbox"] is True
        assert tools_config["escalation"] is True
        assert tools_config["whatsapp_media"] is True
        assert tools_config["subagents"]["enabled"] is True

    def test_approval_gated_service_without_file_does_not_force_sandbox(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        plan = next(t for t in tools if t.name == "plan_agent")

        result = _run(plan.ainvoke({
            "user_goal": (
                "Agent WhatsApp untuk jasa konsultasi. Customer cerita kebutuhan, bayar dulu, "
                "kirim bukti transfer ke admin, lalu sesi konsultasi dijadwalkan setelah admin approve."
            ),
            "agent_name": "KonsulPro",
            "channel": "whatsapp",
            "requested_features": "jasa konsultasi, bukti transfer, approval admin, escalation",
        }))
        payload = json.loads(result)
        tools_config = payload["recommended_config"]["tools_config"]

        assert payload["detected_preset"] == "approval_gated_service_agent"
        assert tools_config["escalation"] is True
        assert tools_config["whatsapp_media"] is True
        assert tools_config.get("sandbox") is False
        assert tools_config.get("subagents", {}).get("enabled") is False

    def test_event_customer_data_does_not_trigger_data_analyst_preset(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        plan = next(t for t in tools if t.name == "plan_agent")

        result = _run(plan.ainvoke({
            "user_goal": (
                "Agent WhatsApp bantu chat pelanggan untuk usaha persiapan acara, cek data acara, "
                "stok barang, dan harga final, tanpa Google dulu."
            ),
            "agent_name": "Event Helper",
            "channel": "whatsapp",
            "requested_features": "escalation",
            "business_context": (
                "Pelanggan memberi tanggal acara, tempat, jumlah tamu, barang yang dibutuhkan, "
                "dan minta kepastian biaya serta barang tersedia. Kepastian harus dicek pemilik dulu."
            ),
            "operator_phone": "6287700600616",
        }))
        payload = json.loads(result)
        tools_config = payload["recommended_config"]["tools_config"]

        assert payload["detected_preset"] == "cs_whatsapp_basic"
        assert tools_config["escalation"] is True
        assert tools_config.get("sandbox") is False
        assert tools_config.get("subagents", {}).get("enabled") is False
        assert tools_config["whatsapp_media"] is True
        assert "mcp" not in tools_config
        assert payload["google_workspace_option"]["should_offer"] is False
        assert payload["google_workspace_option"]["enabled"] is False

    def test_plan_agent_checks_subscription_slot_before_create(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()

        async def _fake_get_or_create(_phone, _db):
            return SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(status="trial")

        async def _fake_check_can_create(_phone, _db):
            return {
                "allowed": False,
                "reason": "Plan Trial maksimal 1 agent.",
                "plan": "Trial",
                "agents_used": 1,
                "max_agents": 1,
            }

        with patch("app.core.domain.subscription_service.get_or_create_wa_user", _fake_get_or_create), patch(
            "app.core.domain.subscription_service.check_can_create_agent",
            _fake_check_can_create,
        ):
            tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
            plan = next(t for t in tools if t.name == "plan_agent")
            result = _run(plan.ainvoke({
                "user_goal": "Buat agent customer service WhatsApp",
                "agent_name": "CS Baru",
                "channel": "whatsapp",
            }))

        payload = json.loads(result)

        assert payload["plan_status"] == "blocked_by_subscription"
        assert payload["creation_entitlement_check"]["allowed"] is False
        assert payload["creation_entitlement_check"]["agents_used"] == 1
        assert "maksimal 1 agent" in payload["validation_errors"][0]
        assert "upgrade" in payload["next_action"].lower()

    def test_plan_agent_blocks_subagent_feature_before_create_for_trial_plan(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        sub = SimpleNamespace(status="trial", tokens_remaining=2_000_000)
        plan_obj = SimpleNamespace(
            code="trial",
            label="Trial",
            allowed_models=["openai/gpt-4.1-mini"],
            subagents_allowed=False,
            wa_connect=True,
        )

        async def _fake_get_or_create(_phone, _db):
            return SimpleNamespace(id=uuid.uuid4()), sub

        async def _fake_check_can_create(_phone, _db):
            return {
                "allowed": True,
                "plan": "Trial",
                "agents_used": 0,
                "max_agents": 1,
                "tokens_remaining": sub.tokens_remaining,
                "expires_at": None,
            }

        async def _fake_get_subscription(_phone, _db):
            return SimpleNamespace(id=uuid.uuid4()), sub, plan_obj

        with patch("app.core.domain.subscription_service.get_or_create_wa_user", _fake_get_or_create), patch(
            "app.core.domain.subscription_service.check_can_create_agent",
            _fake_check_can_create,
        ), patch(
            "app.core.domain.subscription_service.get_subscription_by_external_id",
            _fake_get_subscription,
        ):
            tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
            plan = next(t for t in tools if t.name == "plan_agent")
            result = _run(plan.ainvoke({
                "user_goal": "Buat agent coding yang bisa bikin website dan deploy",
                "agent_name": "Codey",
                "requested_features": "coding, deploy",
            }))

        payload = json.loads(result)

        assert payload["plan_status"] == "blocked_by_subscription"
        assert payload["creation_entitlement_check"]["plan_code"] == "trial"
        assert any("sub-agent" in error for error in payload["validation_errors"])
        assert payload["recommended_config"]["tools_config"]["subagents"]["enabled"] is True


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

    def test_supported_channels_only_exposes_whatsapp_for_arthur(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "get_platform_capabilities")
        result = _run(tool.ainvoke({}))
        data = json.loads(result)

        assert [channel["type"] for channel in data["supported_channels"]] == ["whatsapp"]

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

    def test_phone_arg_does_not_override_session_owner(self):
        """Regression: Arthur passing a chat-typed number must NOT override the
        verified session owner. The owner's plan is always the session owner's.

        Production bug: user typed "6289477477238" in chat (a number with no
        account → auto-provisioned trial), while the real sender owner
        "62895619356936" had Enterprise. get_user_subscription resolved the
        chat number first and reported "trial" repeatedly.
        """
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        sub = SimpleNamespace(
            status="active",
            is_usable=True,
            plan_id=uuid.uuid4(),
            token_quota=100_000_000,
            tokens_used=0,
            tokens_remaining=100_000_000,
            expires_at=None,
        )
        plan = SimpleNamespace(id=sub.plan_id, code="tier_3", label="Enterprise", max_agents=None)
        plan_result = MagicMock()
        plan_result.scalar_one.return_value = plan
        agents_result = MagicMock()
        agents_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(side_effect=[plan_result, agents_result])

        seen: dict[str, str] = {}

        async def _fake_get_or_create(_phone, _db):
            seen["phone"] = _phone
            return SimpleNamespace(id=uuid.uuid4()), sub

        with patch("app.core.domain.subscription_service.get_or_create_wa_user", _fake_get_or_create):
            tools = build_builder_tools(
                db_factory=db,
                owner_phone="62895619356936",
                default_target="151414827434073@lid",
            )
            tool = next(t for t in tools if t.name == "get_user_subscription")
            result = _run(tool.ainvoke({"phone": "6289477477238"}))

        # The verified session owner must win over the chat-typed phone arg.
        assert seen.get("phone") == "62895619356936"
        data = json.loads(result)
        assert data["phone"] == "62895619356936"
        assert data["plan_code"] == "tier_3"


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

    def test_plain_text_blueprint_writer_output_falls_back_without_error_log(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "compose_agent_blueprint")

        with patch(
            "app.core.tools.builder_tools._call_instruction_writer",
            new=AsyncMock(return_value="Saya akan susun blueprint agent ini dulu."),
        ), patch("app.core.tools.builder_tools.logger") as logger:
            result = _run(tool.ainvoke({
                "preset_id": "research_agent",
                "user_goal": "Agent riset marketing yang menyimpan hasil ringkasan.",
                "agent_name": "Reserchpedia",
                "business_context": "Untuk tim marketing.",
            }))

        data = json.loads(result)
        assert data["parse_status"] == "deterministic_fallback"
        assert "riset" in data["blueprint"]["agent_summary"].lower()
        logger.warning.assert_called_once()
        assert logger.warning.call_args.args[0] == "builder_tools.compose_agent_blueprint.parse_failed"
        logger.error.assert_not_called()

    def test_approval_gated_blueprint_fallback_preserves_payment_approval_workflow(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "compose_agent_blueprint")

        with patch(
            "app.core.tools.builder_tools._call_instruction_writer",
            new=AsyncMock(return_value="bukan json"),
        ):
            result = _run(tool.ainvoke({
                "preset_id": "approval_gated_service_agent",
                "user_goal": "Agent jasa bikin CV ATS, minta pembayaran, bukti transfer ke admin, admin approve, lalu CV dikirim.",
                "agent_name": "CVin aja",
                "business_context": "Customer diwawancara dulu, bayar, bukti transfer dicek admin.",
                "requested_features": "bikin CV ATS, bukti transfer, approval admin, kirim PDF",
            }))

        data = json.loads(result)
        blueprint = data["blueprint"]
        states = [state["state"] for state in blueprint["state_plan"]]

        assert data["parse_status"] == "deterministic_fallback"
        assert states == ["intake", "waiting_payment", "payment_review", "approved", "delivery", "aftercare"]
        assert any("escalate_to_human" in rule["action"] for rule in blueprint["escalation_rules"])
        assert any("send_whatsapp_document" in item for item in blueprint["validation_checklist"])

    def test_compose_agent_operating_manual_uses_blueprint_for_specific_sop(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "compose_agent_operating_manual")

        writer_output = {
            "manual_id": "agent_operating_manual",
            "version": 1,
            "source": "arthur_operating_manual_writer",
            "domain": "rental_alat_pesta",
            "domain_confidence": "high",
            "maturity": "usable",
            "owner_review_required": False,
            "missing_context": [],
            "assumptions": ["Harga final harus dicek owner."],
            "workflows": [
                {
                    "workflow_id": "rental_inquiry",
                    "name": "Kualifikasi rental alat pesta",
                    "trigger": "Customer tanya sewa alat pesta.",
                    "goal": "Mengumpulkan kebutuhan rental sampai siap dicek owner.",
                    "required_inputs": ["tanggal acara", "lokasi", "item yang disewa", "jumlah tamu"],
                    "steps": ["Tanyakan tanggal dan lokasi.", "Kumpulkan item dan jumlah.", "Eskalasi ketersediaan dan harga final."],
                    "decision_points": ["Jika tanggal atau item belum jelas, tanya dulu."],
                    "allowed_tools": ["memory", "escalation"],
                    "escalation_rules": ["Eskalasi harga final dan ketersediaan item."],
                    "prohibited_actions": ["Jangan menjanjikan stok tersedia tanpa owner."],
                    "final_output": "Ringkasan kebutuhan rental untuk owner.",
                    "examples": [],
                }
            ],
            "knowledge_plan": {"must_have": ["Daftar item rental"], "nice_to_have": [], "needs_upload": False},
            "memory_plan": [{"key": "rental_lead", "value_to_store": "Tanggal, lokasi, item, jumlah tamu"}],
            "validation_checklist": ["Tidak mengarang harga final."],
        }

        with patch(
            "app.core.tools.builder_tools._call_instruction_writer",
            new=AsyncMock(return_value=json.dumps(writer_output)),
        ):
            result = _run(tool.ainvoke({
                "preset_id": "ecommerce_cs",
                "user_goal": "Agent WhatsApp rental alat pesta",
                "agent_name": "RentalPro",
                "business_context": (
                    "Rental alat pesta: kursi, tenda, sound system. Customer perlu sebut tanggal acara, "
                    "lokasi, item, jumlah tamu, pengiriman, DP, dan pelunasan."
                ),
                "agent_blueprint": json.dumps({
                    "agent_summary": "RentalPro mengumpulkan lead rental alat pesta.",
                    "workflow_steps": [{"name": "Kualifikasi rental", "agent_action": "Tanya tanggal, lokasi, item, dan jumlah tamu."}],
                    "state_plan": [{"state": "intake", "entry_condition": "Customer tanya rental", "allowed_actions": ["tanya kebutuhan"], "exit_condition": "lead lengkap"}],
                }),
                "channel": "whatsapp",
                "domain": "rental_alat_pesta",
            }))

        data = json.loads(result)
        manual = data["operating_manual"]

        assert data["summary"]["present"] is True
        assert manual["source"] == "arthur_operating_manual_writer"
        assert manual["domain"] == "rental_alat_pesta"
        assert manual["maturity"] == "usable"
        assert manual["workflows"][0]["required_inputs"] == ["tanggal acara", "lokasi", "item yang disewa", "jumlah tamu"]
        assert "SOP Workflow Detail" in data["prompt_preview"]


class TestComposeAgentInstructions:
    def test_event_cs_fallback_does_not_invent_payment_or_file_delivery(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "compose_agent_instructions")

        with patch(
            "app.core.tools.builder_tools._call_instruction_writer",
            new=AsyncMock(side_effect=RuntimeError("writer down")),
        ):
            result = _run(tool.ainvoke({
                "preset_id": "cs_whatsapp_basic",
                "agent_name": "Event Helper",
                "channel": "whatsapp",
                "business_context": (
                    "Usaha bantu orang nyiapin acara. Pelanggan tanya tanggal acara, tempat, jumlah tamu, "
                    "barang yang dibutuhkan, antar pasang, kepastian biaya dan ketersediaan barang harus dicek owner dulu. "
                    "Ada aturan uang muka dan pelunasan yang dijelaskan pelan-pelan."
                ),
                "escalation_info": "Eskalasi biaya dan ketersediaan ke Owner 6287700600616.",
                "agent_blueprint": json.dumps({
                    "assumptions": ["Blueprint fallback dibuat karena output JSON generator tidak bisa dipulihkan."],
                    "workflow_steps": [{"required_user_data": ["tujuan user", "konteks bisnis atau personal", "output yang diharapkan"]}],
                }),
            }))

        data = json.loads(result)
        fallback = data["fallback_skeleton"]
        assert "waiting_payment" not in fallback
        assert "payment_review" not in fallback
        assert "send_whatsapp_document" not in fallback
        assert "file final" not in fallback.lower()

    def test_event_cs_writer_payment_hallucination_is_replaced(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "compose_agent_instructions")
        bad_writer_output = (
            "Kamu adalah Event Helper.\n"
            "STATE WAJIB: intake, waiting_payment, payment_review, approved, delivery, aftercare.\n"
            "Saat user kirim bukti transfer, tunggu admin approve. Jangan kirim file final sebelum approved."
        )

        with patch(
            "app.core.tools.builder_tools._call_instruction_writer",
            new=AsyncMock(return_value=bad_writer_output),
        ):
            result = _run(tool.ainvoke({
                "preset_id": "cs_whatsapp_basic",
                "agent_name": "Event Helper",
                "channel": "whatsapp",
                "business_context": (
                    "Usaha bantu orang nyiapin acara. Pelanggan tanya tanggal acara, tempat, jumlah tamu, "
                    "barang yang dibutuhkan, antar pasang, kepastian biaya dan ketersediaan barang harus dicek owner dulu. "
                    "Ada aturan uang muka dan pelunasan yang dijelaskan pelan-pelan."
                ),
                "escalation_info": "Eskalasi biaya dan ketersediaan ke Owner 6287700600616.",
                "agent_blueprint": json.dumps({
                    "human_approval_points": [
                        {"when": "Kasus membutuhkan keputusan, akses, pembayaran, atau persetujuan manusia"}
                    ]
                }),
            }))

        data = json.loads(result)
        instructions = data["instructions"]
        assert data["fallback_status"] == "deterministic_fallback_removed_hallucinated_payment"
        assert "waiting_payment" not in instructions
        assert "payment_review" not in instructions
        assert "bukti transfer" not in instructions.lower()
        assert "file final" not in instructions.lower()

    def test_writer_invented_business_name_is_sanitized_when_owner_did_not_provide_brand(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "compose_agent_instructions")
        writer_output = (
            "Kamu adalah Event Helper, asisten virtual dari FixEvent, jasa persiapan acara. "
            "Kumpulkan tanggal acara, lokasi, jumlah tamu, barang, dan kebutuhan antar pasang. "
            "Jika customer minta harga atau stok, panggil escalate_to_human dengan ringkasan lengkap. "
            "Jangan menjanjikan harga final atau stok tersedia sebelum owner mengecek. "
        ) * 4

        with patch(
            "app.core.tools.builder_tools._call_instruction_writer",
            new=AsyncMock(return_value=writer_output),
        ):
            result = _run(tool.ainvoke({
                "preset_id": "cs_whatsapp_basic",
                "agent_name": "Event Helper",
                "channel": "whatsapp",
                "business_context": (
                    "Usaha bantu orang nyiapin acara. Pelanggan tanya tanggal, lokasi, jumlah tamu, "
                    "barang yang dibutuhkan, serta antar pasang. Harga dan stok dicek owner dulu."
                ),
                "escalation_info": "Eskalasi harga dan stok ke owner.",
            }))

        data = json.loads(result)
        assert data["business_name_sanitized"] is True
        assert "FixEvent" not in data["instructions"]
        assert "dari bisnis ini" in data["instructions"]

    def test_empty_writer_output_uses_deterministic_fallback(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "compose_agent_instructions")

        with patch(
            "app.core.tools.builder_tools._call_instruction_writer",
            new=AsyncMock(return_value=""),
        ):
            result = _run(tool.ainvoke({
                "preset_id": "cs_whatsapp_basic",
                "agent_name": "Event Helper",
                "channel": "whatsapp",
                "business_context": (
                    "Usaha bantu orang nyiapin acara. Pelanggan tanya tanggal, lokasi, jumlah tamu, "
                    "barang yang dibutuhkan, serta antar pasang. Harga dan stok dicek owner dulu."
                ),
                "escalation_info": "Eskalasi harga dan stok ke owner.",
            }))

        data = json.loads(result)
        assert data["fallback_status"] == "deterministic_fallback_empty_writer_output"
        assert data["char_count"] > 300
        assert "Event Helper" in data["instructions"]

    def test_explicit_owner_business_name_is_preserved(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "compose_agent_instructions")
        writer_output = (
            "Kamu adalah Event Helper, asisten virtual dari FixEvent, jasa persiapan acara. "
            "Kumpulkan tanggal acara, lokasi, jumlah tamu, barang, dan kebutuhan antar pasang. "
            "Jika customer minta harga atau stok, panggil escalate_to_human dengan ringkasan lengkap. "
            "Jangan menjanjikan harga final atau stok tersedia sebelum owner mengecek. "
        ) * 4

        with patch(
            "app.core.tools.builder_tools._call_instruction_writer",
            new=AsyncMock(return_value=writer_output),
        ):
            result = _run(tool.ainvoke({
                "preset_id": "cs_whatsapp_basic",
                "agent_name": "Event Helper",
                "channel": "whatsapp",
                "business_context": (
                    "Nama bisnis saya adalah FixEvent. Usaha ini bantu orang nyiapin acara. "
                    "Harga dan stok dicek owner dulu."
                ),
                "escalation_info": "Eskalasi harga dan stok ke owner.",
            }))

        data = json.loads(result)
        assert data.get("business_name_sanitized") is None
        assert "FixEvent" in data["instructions"]

    def test_approval_gated_short_writer_output_uses_deterministic_fallback(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "compose_agent_instructions")

        with patch(
            "app.core.tools.builder_tools._call_instruction_writer",
            new=AsyncMock(return_value="Kamu adalah CVin aja, asisten riset CV."),
        ):
            result = _run(tool.ainvoke({
                "preset_id": "approval_gated_service_agent",
                "agent_name": "CVin aja",
                "channel": "whatsapp",
                "business_context": "Jasa CV ATS. Customer bayar, kirim bukti transfer, admin approve, lalu CV dikirim.",
                "extra_rules": "Bukti transfer wajib ke admin. Jangan kirim CV sebelum approved.",
            }))

        data = json.loads(result)
        instructions = data["instructions"]

        assert data["fallback_status"] == "deterministic_fallback"
        assert "waiting_payment" in instructions
        assert "payment_review" in instructions
        assert "escalate_to_human" in instructions
        assert "send_whatsapp_document" in instructions


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

    def test_payment_approval_workflow_blocks_weak_research_prompt(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "validate_agent_config")

        result = _run(tool.ainvoke({
            "name": "CVin aja",
            "preset_id": "approval_gated_service_agent",
            "channel_type": "whatsapp",
            "instructions": "Kamu adalah CVin aja, asisten riset CV. Customer bayar dan admin approve.",
            "tools_config": json.dumps({
                "memory": True,
                "skills": True,
                "escalation": False,
                "sandbox": True,
                "subagents": {"enabled": True},
                "whatsapp_media": True,
            }),
        }))
        data = json.loads(result)

        assert data["valid"] is False
        assert any("waiting_payment" in error for error in data["errors"])
        assert any("escalation: true" in error for error in data["errors"])

    def test_event_payment_policy_does_not_force_approval_state_contract(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "validate_agent_config")

        instructions = (
            "Kamu adalah Event Helper, CS WhatsApp untuk usaha persiapan acara. "
            "Kumpulkan tanggal acara, lokasi, jumlah tamu, barang yang dibutuhkan, dan kebutuhan antar pasang. "
            "Jika customer minta kepastian biaya atau ketersediaan barang, panggil escalate_to_human dengan ringkasan kebutuhan. "
            "Jangan menjanjikan harga final atau stok tersedia sebelum owner memberi keputusan. "
            "Ada aturan uang muka dan pelunasan, tetapi detailnya akan dijelaskan owner pelan-pelan. "
            "Ikuti bahasa user; default Indonesia jika user tidak menentukan. "
        ) * 4

        result = _run(tool.ainvoke({
            "name": "Event Helper",
            "preset_id": "cs_whatsapp_basic",
            "channel_type": "whatsapp",
            "instructions": instructions,
            "tools_config": json.dumps({
                "memory": True,
                "skills": True,
                "escalation": True,
                "sandbox": False,
                "subagents": {"enabled": False},
                "whatsapp_media": False,
            }),
        }))
        data = json.loads(result)

        assert data["valid"] is True
        assert not any("waiting_payment" in error for error in data["errors"])

    def test_generic_fallback_blueprint_does_not_force_payment_contract(self):
        from app.core.tools.builder_tools import _critical_workflow_config_errors

        instructions = (
            "Kamu adalah Event Helper, CS WhatsApp untuk usaha persiapan acara. "
            "Kumpulkan tanggal acara, lokasi, jumlah tamu, barang yang dibutuhkan, dan kebutuhan antar pasang. "
            "Jika customer minta kepastian biaya atau ketersediaan barang, panggil escalate_to_human dengan ringkasan kebutuhan. "
            "Jangan menjanjikan harga final atau stok tersedia sebelum owner memberi keputusan. "
            "Ada aturan uang muka dan pelunasan, tetapi detailnya akan dijelaskan owner pelan-pelan. "
        ) * 4
        blueprint = json.dumps({
            "assumptions": ["Blueprint fallback dibuat karena output JSON generator tidak bisa dipulihkan."],
            "human_approval_points": [
                {"when": "Kasus membutuhkan keputusan, akses, pembayaran, atau persetujuan manusia"}
            ],
        })

        errors = _critical_workflow_config_errors(
            name="Event Helper",
            description="Agent WhatsApp untuk bantu pelanggan usaha persiapan acara.",
            instructions=instructions,
            tools_config={"memory": True, "skills": True, "escalation": True, "subagents": {"enabled": False}},
            blueprint=blueprint,
            preset_id="cs_whatsapp_basic",
        )

        assert errors == []


# ────────────────────────────────────────────────────────────────────────────
# Section 4: create_agent
# ────────────────────────────────────────────────────────────────────────────

class TestCreateAgent:
    def test_create_agent_blocks_unsafe_payment_workflow_even_if_called_directly(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "create_agent")

        result = _run(tool.ainvoke({
            "name": "CVin aja",
            "instructions": "Kamu adalah CVin aja, asisten riset CV. Customer bayar dan admin approve.",
            "channel_type": "whatsapp",
            "tools_config": {
                "memory": True,
                "skills": True,
                "escalation": False,
                "sandbox": True,
                "subagents": {"enabled": True},
                "whatsapp_media": True,
            },
        }))
        data = json.loads(result)

        assert data["error"] == "Konfigurasi agent belum aman untuk dibuat."
        assert any("waiting_payment" in error for error in data["validation_errors"])
        assert any("escalation=true" in error for error in data["validation_errors"])

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
                "file_capability": "text_only",
                "tools_config": '{"memory": true, "escalation": true}',
            }))

            data = json.loads(result)
            assert data["success"] is True
            assert "agent_id" in data
            assert "api_key" in data
            db.add.assert_called_once()
            db.flush.assert_called_once()

    def test_create_agent_normalizes_legacy_webchat_channel_to_whatsapp(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        with patch("app.core.tools.builder_tools.Agent") as MockAgent:
            captured = {}

            def capture(**kwargs):
                captured.update(kwargs)
                return _make_mock_agent(operator_ids=["+62811xxx"], channel_type=kwargs.get("channel_type"))

            MockAgent.side_effect = capture
            tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
            tool = next(t for t in tools if t.name == "create_agent")

            result = _run(tool.ainvoke({
                "name": "FAQ Agent",
                "instructions": "Kamu adalah FAQ agent yang membantu pelanggan.",
                "file_capability": "text_only",
                "tools_config": '{"memory": true, "escalation": true}',
                "channel_type": "webchat",
            }))
            data = json.loads(result)

        assert data["success"] is True
        assert captured["channel_type"] == "whatsapp"
        assert captured["wa_device_id"]
        assert data["whatsapp_onboarding_required"] is True

    def test_create_agent_creates_operating_manual_separate_from_instructions(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        writer_manual = {
            "manual_id": "agent_operating_manual",
            "version": 1,
            "source": "arthur_operating_manual_writer_auto",
            "domain": "ecommerce",
            "domain_confidence": "high",
            "maturity": "usable",
            "owner_review_required": False,
            "missing_context": [],
            "assumptions": ["Stok dan refund perlu dicek owner jika belum pasti."],
            "workflows": [
                {
                    "workflow_id": "order_intake",
                    "name": "Intake order toko online",
                    "trigger": "Customer bertanya produk atau ingin checkout.",
                    "goal": "Mengumpulkan data order sebelum diteruskan ke owner jika perlu.",
                    "required_inputs": ["nama", "produk", "varian", "jumlah", "alamat", "kontak"],
                    "steps": ["Tanya produk dan varian.", "Kumpulkan jumlah, alamat, dan kontak.", "Eskalasi stok/harga/refund jika belum pasti."],
                    "decision_points": ["Jika stok atau refund belum pasti, eskalasi."],
                    "allowed_tools": ["memory", "escalation"],
                    "escalation_rules": ["Eskalasi stok, harga final, ongkir, refund, dan pembayaran manual jika belum pasti."],
                    "prohibited_actions": ["Jangan mengarang stok, ongkir, atau refund."],
                    "final_output": "Order/customer issue siap diproses owner.",
                    "examples": [],
                }
            ],
            "knowledge_plan": {"must_have": ["Katalog", "Stok", "Aturan refund"], "nice_to_have": [], "needs_upload": False},
            "memory_plan": [{"key": "order_lead", "value_to_store": "Produk, varian, jumlah, alamat, kontak"}],
            "validation_checklist": ["Tidak mengarang stok/refund."],
        }
        with patch("app.core.tools.builder_tools.Agent") as MockAgent:
            captured_kwargs = {}

            def capture(**kwargs):
                captured_kwargs.update(kwargs)
                return _make_mock_agent(
                    name=kwargs.get("name", "Toko Agent"),
                    tools_config=kwargs.get("tools_config"),
                    created_by_type=kwargs.get("created_by_type"),
                    created_by_agent_id=kwargs.get("created_by_agent_id"),
                    created_by_agent_name=kwargs.get("created_by_agent_name"),
                )

            MockAgent.side_effect = capture

            with patch(
                "app.core.tools.builder_tools._call_instruction_writer",
                new=AsyncMock(return_value=json.dumps(writer_manual)),
            ):
                tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
                tool = next(t for t in tools if t.name == "create_agent")
                result = _run(tool.ainvoke({
                    "name": "Toko Sari Agent",
                    "description": "Agent CS untuk toko online yang menjawab produk, stok, checkout, dan refund.",
                    "instructions": "Kamu adalah CS Toko Sari. Bicara singkat, ramah, dan bantu customer belanja.",
                    "file_capability": "text_only",
                    "business_context": (
                        "Toko Sari menjual produk fashion online. Customer biasanya bertanya katalog, stok, ukuran, "
                        "alamat pengiriman, checkout, pembayaran manual, dan refund. Agent harus mengumpulkan nama, "
                        "produk, varian, jumlah, alamat, dan kontak. Untuk stok, harga final, ongkir, refund, dan "
                        "pembayaran manual, agent harus eskalasi ke owner jika data belum pasti."
                    ),
                    "domain": "ecommerce",
                    "tools_config": '{"memory": true, "escalation": true}',
                }))
            data = json.loads(result)

        manual = captured_kwargs["tools_config"]["operating_manual"]
        assert data["success"] is True
        assert data["operating_manual"]["present"] is True
        assert manual["source"] == "arthur_operating_manual_writer_auto"
        assert manual["domain"] == "ecommerce"
        assert manual["maturity"] == "usable"
        assert "operating_manual" not in captured_kwargs["instructions"]

    def test_create_agent_derives_operating_manual_from_blueprint_when_not_provided(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        blueprint = {
            "agent_summary": "RentalPro mengelola lead rental alat pesta dari intake sampai follow-up owner.",
            "assumptions": ["Harga final dan ketersediaan item harus dicek owner."],
            "workflow_steps": [
                {
                    "step": 1,
                    "name": "Intake rental",
                    "agent_action": "Tanyakan tanggal acara, lokasi, item yang dibutuhkan, jumlah tamu, dan kebutuhan pengiriman.",
                    "required_user_data": ["tanggal acara", "lokasi", "item rental", "jumlah tamu", "pengiriman"],
                    "success_criteria": "Lead rental lengkap untuk dicek owner.",
                }
            ],
            "state_plan": [
                {
                    "state": "intake",
                    "entry_condition": "Customer bertanya rental alat pesta.",
                    "allowed_actions": ["Kumpulkan tanggal, lokasi, item, dan jumlah tamu", "Simpan lead ke memory"],
                    "exit_condition": "Data lead cukup untuk eskalasi ke owner.",
                },
                {
                    "state": "owner_review",
                    "entry_condition": "Lead rental sudah lengkap.",
                    "allowed_actions": ["Eskalasi ketersediaan dan harga final ke owner"],
                    "exit_condition": "Owner memberi keputusan harga/ketersediaan.",
                },
            ],
            "escalation_rules": [
                {"condition": "Customer minta harga final atau ketersediaan item", "action": "Eskalasi ke owner dengan ringkasan lead."}
            ],
            "validation_checklist": ["Jangan menjanjikan stok atau harga final sebelum owner review."],
        }

        sub = SimpleNamespace(token_quota=10_000_000, expires_at=None, grace_until=None)
        plan_obj = SimpleNamespace(
            label="Starter",
            allowed_models=["openai/gpt-4.1-mini"],
            subagents_allowed=True,
            wa_connect=True,
        )

        async def _fake_get_or_create(_phone, _db):
            return SimpleNamespace(id=uuid.uuid4()), sub

        async def _fake_check_can_create(_phone, _db):
            return {"allowed": True, "plan": "Starter", "agents_used": 0, "max_agents": 1}

        async def _fake_get_subscription(_phone, _db):
            return SimpleNamespace(id=uuid.uuid4()), sub, plan_obj

        with patch("app.core.tools.builder_tools.Agent") as MockAgent:
            captured_kwargs = {}

            def capture(**kwargs):
                captured_kwargs.update(kwargs)
                return _make_mock_agent(
                    name=kwargs.get("name", "RentalPro"),
                    tools_config=kwargs.get("tools_config"),
                    created_by_type=kwargs.get("created_by_type"),
                    created_by_agent_name=kwargs.get("created_by_agent_name"),
                )

            MockAgent.side_effect = capture

            with patch("app.core.domain.subscription_service.get_or_create_wa_user", _fake_get_or_create), patch(
                "app.core.domain.subscription_service.check_can_create_agent",
                _fake_check_can_create,
            ), patch(
                "app.core.domain.subscription_service.get_subscription_by_external_id",
                _fake_get_subscription,
            ), patch(
                "app.core.tools.builder_tools.upsert_agent_operating_manual",
                new=AsyncMock(),
            ), patch(
                "app.core.domain.memory_service.upsert_memory",
                new=AsyncMock(),
            ):
                tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
                tool = next(t for t in tools if t.name == "create_agent")
                result = _run(tool.ainvoke({
                    "name": "RentalPro",
                    "description": "Agent WhatsApp rental alat pesta.",
                    "instructions": "Kamu adalah RentalPro, bantu customer rental alat pesta dengan ramah.",
                    "file_capability": "text_only",
                    "business_context": (
                        "Bisnis menyewakan tenda, kursi, meja, sound system, dan dekorasi untuk acara. "
                        "Customer perlu memberi tanggal, lokasi, item, jumlah tamu, pengiriman, DP, dan pelunasan."
                    ),
                    "domain": "rental_alat_pesta",
                    "tools_config": '{"memory": true, "escalation": true}',
                    "blueprint": json.dumps(blueprint),
                }))
            data = json.loads(result)

        manual = captured_kwargs["tools_config"]["operating_manual"]
        assert data["success"] is True
        assert manual["source"] == "arthur_blueprint"
        assert manual["domain"] == "rental_alat_pesta"
        assert manual["maturity"] == "usable"
        assert [workflow["workflow_id"] for workflow in manual["workflows"]] == ["intake", "owner_review"]
        assert "tanggal acara" in manual["workflows"][0]["required_inputs"]
        assert any("harga final" in item for item in manual["workflows"][1]["escalation_rules"])

    def test_missing_business_context_creates_draft_intake_safe_sop(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        with patch("app.core.tools.builder_tools.Agent") as MockAgent:
            captured_kwargs = {}

            def capture(**kwargs):
                captured_kwargs.update(kwargs)
                return _make_mock_agent(
                    name=kwargs.get("name", "Agent Minim"),
                    tools_config=kwargs.get("tools_config"),
                    created_by_type=kwargs.get("created_by_type"),
                    created_by_agent_name=kwargs.get("created_by_agent_name"),
                )

            MockAgent.side_effect = capture

            tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
            tool = next(t for t in tools if t.name == "create_agent")
            result = _run(tool.ainvoke({
                "name": "Agent Minim",
                "instructions": "Bantu customer.",
                "file_capability": "text_only",
                "tools_config": '{"memory": true}',
            }))
            data = json.loads(result)

        manual = captured_kwargs["tools_config"]["operating_manual"]
        assert data["operating_manual"]["maturity"] == "draft"
        assert manual["owner_review_required"] is True
        assert manual["workflows"][0]["workflow_id"] == "customer_intake"
        assert any("detail bisnis belum lengkap" in item for item in manual["missing_context"])

    def test_create_agent_auto_sop_uses_semantic_context_not_domain_keywords(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        writer_manual = {
            "manual_id": "agent_operating_manual",
            "version": 1,
            "source": "arthur_operating_manual_writer_auto",
            "domain": "event_equipment_operations",
            "domain_confidence": "medium",
            "maturity": "usable",
            "owner_review_required": False,
            "missing_context": [],
            "assumptions": ["Biaya dan ketersediaan akhir diputuskan pemilik."],
            "workflows": [
                {
                    "workflow_id": "event_need_intake",
                    "name": "Kumpulkan kebutuhan acara",
                    "trigger": "Calon pelanggan ingin dibantu menyiapkan perlengkapan acara.",
                    "goal": "Mengumpulkan detail acara agar pemilik bisa cek biaya dan ketersediaan.",
                    "required_inputs": ["tanggal acara", "lokasi", "jenis acara", "jumlah tamu", "perlengkapan yang dibutuhkan"],
                    "steps": ["Tanya tanggal dan lokasi.", "Tanya jenis acara dan jumlah tamu.", "Ringkas kebutuhan perlengkapan."],
                    "decision_points": ["Jika detail belum lengkap, tanya dulu sebelum eskalasi."],
                    "allowed_tools": ["memory", "escalation"],
                    "escalation_rules": ["Eskalasi saat pelanggan minta kepastian biaya, jadwal, atau ketersediaan."],
                    "prohibited_actions": ["Jangan memastikan biaya atau ketersediaan sebelum pemilik mengecek."],
                    "final_output": "Ringkasan kebutuhan acara siap dicek pemilik.",
                    "examples": [],
                },
                {
                    "workflow_id": "owner_review",
                    "name": "Review pemilik",
                    "trigger": "Data acara lengkap atau pelanggan minta kepastian.",
                    "goal": "Meminta keputusan pemilik untuk biaya, jadwal, dan ketersediaan.",
                    "required_inputs": ["ringkasan kebutuhan acara"],
                    "steps": ["Kirim ringkasan ke pemilik.", "Tunggu keputusan.", "Sampaikan hasil keputusan tanpa menambah janji."],
                    "decision_points": ["Jika pemilik belum jawab, sampaikan sedang dicek."],
                    "allowed_tools": ["escalation"],
                    "escalation_rules": ["Gunakan eskalasi untuk kepastian biaya, jadwal, dan ketersediaan."],
                    "prohibited_actions": ["Jangan membuat keputusan menggantikan pemilik."],
                    "final_output": "Pelanggan mendapat keputusan atau next step dari pemilik.",
                    "examples": [],
                },
            ],
            "knowledge_plan": {"must_have": ["Daftar perlengkapan", "Aturan uang muka", "Kontak pemilik"], "nice_to_have": [], "needs_upload": False},
            "memory_plan": [{"key": "event_lead", "value_to_store": "Tanggal, lokasi, jenis acara, jumlah tamu, kebutuhan perlengkapan"}],
            "validation_checklist": ["Agent mengumpulkan data acara.", "Agent eskalasi kepastian biaya/ketersediaan."],
        }
        with patch("app.core.tools.builder_tools.Agent") as MockAgent:
            captured_kwargs = {}

            def capture(**kwargs):
                captured_kwargs.update(kwargs)
                return _make_mock_agent(
                    name=kwargs.get("name", "Rental Agent"),
                    tools_config=kwargs.get("tools_config"),
                    created_by_type=kwargs.get("created_by_type"),
                    created_by_agent_name=kwargs.get("created_by_agent_name"),
                )

            MockAgent.side_effect = capture

            with patch(
                "app.core.tools.builder_tools._call_instruction_writer",
                new=AsyncMock(return_value=json.dumps(writer_manual)),
            ) as writer:
                tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
                tool = next(t for t in tools if t.name == "create_agent")
                _run(tool.ainvoke({
                    "name": "Event Helper",
                    "description": "Agent WhatsApp untuk bantu orang menyiapkan acara.",
                    "instructions": "Kamu membantu calon pelanggan dengan ramah dan mengumpulkan kebutuhan mereka.",
                    "file_capability": "text_only",
                    "business_context": (
                        "Kami membantu orang menyiapkan acara. Pelanggan biasanya memberi tanggal, tempat, jenis acara, "
                        "perkiraan jumlah tamu, barang yang dibutuhkan, kebutuhan antar-pasang, aturan uang muka, "
                        "dan perubahan pesanan. Keputusan biaya akhir, jadwal tim, serta barang yang tersedia harus "
                        "dicek pemilik dulu sebelum disampaikan sebagai kepastian."
                    ),
                    "tools_config": '{"memory": true, "escalation": true}',
                    "channel_type": "whatsapp",
                }))

        manual = captured_kwargs["tools_config"]["operating_manual"]
        assert writer.await_count == 1
        assert manual["source"] == "arthur_operating_manual_writer_auto"
        assert manual["domain"] == "event_equipment_operations"
        assert manual["maturity"] == "usable"
        assert manual["owner_review_required"] is False
        assert [workflow["workflow_id"] for workflow in manual["workflows"]] == ["event_need_intake", "owner_review"]
        assert "tanggal acara" in manual["workflows"][0]["required_inputs"]
        assert any("kepastian biaya" in rule for rule in manual["workflows"][1]["escalation_rules"])

    def test_create_agent_rewrites_generic_fallback_blueprint_with_semantic_sop(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        generic_blueprint = {
            "agent_summary": "Event Helper dibuat untuk bantu pelanggan.",
            "assumptions": ["Blueprint fallback dibuat karena output JSON generator tidak bisa dipulihkan."],
            "workflow_steps": [
                {
                    "step": 1,
                    "name": "Intake kebutuhan",
                    "agent_action": "Pahami intent user, konteks bisnis, dan hasil akhir yang diinginkan sebelum menjalankan workflow.",
                    "required_user_data": ["tujuan user", "konteks bisnis atau personal", "output yang diharapkan"],
                    "success_criteria": "Agent memahami konteks inti.",
                }
            ],
            "state_plan": [{"state": "intake", "entry_condition": "Percakapan baru", "allowed_actions": ["Kumpulkan data wajib"], "exit_condition": "Data inti cukup"}],
            "escalation_rules": [{"condition": "Agent tidak yakin atau kasus sensitif", "action": "Eskalasi ke operator dengan ringkasan konteks"}],
        }
        writer_manual = {
            "manual_id": "agent_operating_manual",
            "version": 1,
            "source": "arthur_operating_manual_writer_auto",
            "domain": "event_equipment_operations",
            "domain_confidence": "medium",
            "maturity": "usable",
            "owner_review_required": False,
            "missing_context": [],
            "assumptions": ["Biaya akhir dicek pemilik."],
            "workflows": [
                {
                    "workflow_id": "event_need_intake",
                    "name": "Kumpulkan kebutuhan acara",
                    "trigger": "Pelanggan ingin menyiapkan acara.",
                    "goal": "Data acara lengkap untuk dicek pemilik.",
                    "required_inputs": ["tanggal acara", "lokasi", "jumlah tamu", "barang yang dibutuhkan"],
                    "steps": ["Tanya tanggal/lokasi.", "Tanya jumlah tamu dan barang.", "Eskalasi kepastian biaya."],
                    "decision_points": ["Jika minta kepastian biaya, eskalasi."],
                    "allowed_tools": ["memory", "escalation"],
                    "escalation_rules": ["Eskalasi biaya dan ketersediaan."],
                    "prohibited_actions": ["Jangan memastikan biaya sendiri."],
                    "final_output": "Ringkasan kebutuhan acara.",
                    "examples": [],
                }
            ],
            "knowledge_plan": {"must_have": ["Daftar barang", "Aturan uang muka"], "nice_to_have": [], "needs_upload": False},
            "memory_plan": [{"key": "event_lead", "value_to_store": "Tanggal, lokasi, jumlah tamu, barang"}],
            "validation_checklist": ["Tidak mengarang biaya."],
        }

        with patch("app.core.tools.builder_tools.Agent") as MockAgent:
            captured_kwargs = {}

            def capture(**kwargs):
                captured_kwargs.update(kwargs)
                return _make_mock_agent(
                    name=kwargs.get("name", "Event Helper"),
                    tools_config=kwargs.get("tools_config"),
                    created_by_type=kwargs.get("created_by_type"),
                    created_by_agent_name=kwargs.get("created_by_agent_name"),
                )

            MockAgent.side_effect = capture

            with patch(
                "app.core.tools.builder_tools._call_instruction_writer",
                new=AsyncMock(return_value=json.dumps(writer_manual)),
            ):
                tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
                tool = next(t for t in tools if t.name == "create_agent")
                _run(tool.ainvoke({
                    "name": "Event Helper",
                    "description": "Agent WhatsApp untuk bantu pelanggan persiapan acara.",
                    "instructions": "Kamu membantu pelanggan mengumpulkan kebutuhan acara dan eskalasi kepastian biaya ke pemilik.",
                    "file_capability": "text_only",
                    "business_context": "Pelanggan memberi tanggal acara, lokasi, jumlah tamu, dan barang. Biaya akhir dicek pemilik.",
                    "tools_config": '{"memory": true, "escalation": true}',
                    "channel_type": "whatsapp",
                    "blueprint": json.dumps(generic_blueprint),
                }))

        manual = captured_kwargs["tools_config"]["operating_manual"]
        assert manual["source"] == "arthur_operating_manual_writer_auto"
        assert manual["domain"] == "event_equipment_operations"
        assert manual["workflows"][0]["workflow_id"] == "event_need_intake"
        assert "tanggal acara" in manual["workflows"][0]["required_inputs"]

    def test_event_context_does_not_fallback_to_food_or_ecommerce_sop_domain(self):
        from app.core.domain.agent_sop_service import build_agent_operating_manual

        manual = build_agent_operating_manual(
            name="Event Helper",
            description="Agent WhatsApp untuk bantu pelanggan persiapan acara.",
            instructions=(
                "Kumpulkan tanggal acara, lokasi, jumlah tamu, barang yang dibutuhkan, "
                "antar pasang, kepastian biaya, dan ketersediaan barang. Jangan mutusin biaya sendiri."
            ),
            business_context=(
                "Usaha bantu orang nyiapin acara. Pelanggan tanya tanggal acara, tempat, jumlah tamu, "
                "barang yang dibutuhkan, perlu antar pasang atau tidak. Kepastian biaya dan ketersediaan "
                "barang harus dicek pemilik dulu."
            ),
            tools_config={"memory": True, "escalation": True},
        )

        assert manual["domain"] == "event_service"
        assert manual["source"] == "arthur_template"
        assert [workflow["workflow_id"] for workflow in manual["workflows"]] == [
            "event_need_intake",
            "owner_review_follow_up",
        ]

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
                "file_capability": "text_only",
            }))

            assert "+62811xxx" in captured_kwargs.get("operator_ids", []), \
                "owner_phone harus masuk ke operator_ids saat create_agent"
            assert captured_kwargs.get("tools_config", {}).get("tavily") is True, \
                "agent yang dibuat Arthur harus default punya browsing Tavily"
            assert captured_kwargs.get("created_by_type") == "arthur_builder"
            assert captured_kwargs.get("created_by_agent_name") == "Arthur"
            assert "dibuat dan dikonfigurasi oleh Arthur" in captured_kwargs.get("instructions", "")
            assert "Owner adalah bos dan superadmin" in captured_kwargs.get("instructions", "")
            assert "Google" in captured_kwargs.get("instructions", "")

    def test_create_agent_sets_arthur_created_by_metadata(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        arthur_id = str(uuid.uuid4())
        with patch("app.core.tools.builder_tools.Agent") as MockAgent:
            captured_kwargs = {}

            def capture(**kwargs):
                captured_kwargs.update(kwargs)
                return _make_mock_agent(
                    name=kwargs.get("name", "CS Agent"),
                    created_by_type=kwargs.get("created_by_type"),
                    created_by_agent_id=kwargs.get("created_by_agent_id"),
                    created_by_agent_name=kwargs.get("created_by_agent_name"),
                )

            MockAgent.side_effect = capture

            tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx", self_agent_id=arthur_id)
            tool = next(t for t in tools if t.name == "create_agent")
            result = _run(tool.ainvoke({
                "name": "CS Agent",
                "instructions": "Kamu adalah CS yang membantu pelanggan.",
                "file_capability": "text_only",
            }))
            data = json.loads(result)

        assert captured_kwargs["created_by_type"] == "arthur_builder"
        assert captured_kwargs["created_by_agent_id"] == arthur_id
        assert captured_kwargs["created_by_agent_name"] == "Arthur"
        assert data["created_by_type"] == "arthur_builder"
        assert data["created_by_agent_id"] == arthur_id
        assert data["created_by_agent_name"] == "Arthur"

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
                "file_capability": "text_only",
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
            "Demo Agent Baru",
            "628123456789",
        )
        assert data["shared_whatsapp_name"] == "Demo Agent Baru"
        assert "AB234C" in data["wa_me_url"]
        assert "Simpan kontak Demo Agent Baru" in data["instruction_for_user"]

    def test_omitted_agent_id_requires_target_when_multiple_owned_agents(self):
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
            patch("app.core.infra.wa_client.send_wa_contact", new=AsyncMock()) as send_contact,
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
        assert data["success"] is False
        assert data["error"] == "agent_target_required"
        assert {a["agent_name"] for a in data["available_agents"]} == {"CS Toko Baju Cewek", "CV Maker"}
        ensure_code.assert_not_awaited()
        send_contact.assert_not_awaited()

    def test_agent_name_selects_requested_agent_not_latest_agent(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        rnd_agent = _make_mock_agent(name="Rnd", operator_ids=["+62811xxx"])
        mas_brew_agent = _make_mock_agent(name="Mas Brew", operator_ids=["+62811xxx"])
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [rnd_agent, mas_brew_agent]
        db.execute = AsyncMock(return_value=mock_result)

        settings = MagicMock()
        settings.wa_dev_public_phone = "+628123456789"

        with (
            patch("app.core.tools.builder_tools.get_settings", return_value=settings),
            patch(
                "app.core.domain.wa_dev_trial_service.ensure_wa_dev_trial_code",
                new=AsyncMock(return_value="79ZSXT"),
            ) as ensure_code,
            patch("app.core.infra.wa_client.send_wa_contact", new=AsyncMock()) as send_contact,
        ):
            tools = build_builder_tools(
                db_factory=db,
                owner_phone="+62811xxx",
                device_id="arthur-device",
                default_target="+62811xxx",
            )
            tool = next(t for t in tools if t.name == "create_wa_dev_trial_link")
            result = _run(tool.ainvoke({"agent_name": "masbrew"}))

        data = json.loads(result)
        assert data["success"] is True
        assert data["agent_id"] == str(mas_brew_agent.id)
        assert data["agent_name"] == "Mas Brew"
        assert data["shared_whatsapp_name"] == "Demo Mas Brew"
        ensure_code.assert_awaited_once()
        assert ensure_code.await_args.args[1] is mas_brew_agent
        send_contact.assert_awaited_once_with("arthur-device", "+62811xxx", "Demo Mas Brew", "628123456789")

    def test_ambiguous_code_request_blocks_stale_agent_name_from_history(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        baas_agent = _make_mock_agent(name="Baas", operator_ids=["+62811xxx"])
        mas_brew_agent = _make_mock_agent(name="Mas Brew", operator_ids=["+62811xxx"])
        owned_result = MagicMock()
        owned_result.scalars.return_value.all.return_value = [baas_agent, mas_brew_agent]
        message_result = MagicMock()
        message_result.scalar_one_or_none.return_value = "Bagi kodenya"
        db.execute = AsyncMock(side_effect=[owned_result, message_result])

        settings = MagicMock()
        settings.wa_dev_public_phone = "+628123456789"

        with (
            patch("app.core.tools.builder_tools.get_settings", return_value=settings),
            patch(
                "app.core.domain.wa_dev_trial_service.ensure_wa_dev_trial_code",
                new=AsyncMock(return_value="OLD123"),
            ) as ensure_code,
            patch("app.core.infra.wa_client.send_wa_contact", new=AsyncMock()) as send_contact,
        ):
            tools = build_builder_tools(
                db_factory=db,
                owner_phone="+62811xxx",
                device_id="arthur-device",
                default_target="+62811xxx",
                session_id=str(uuid.uuid4()),
            )
            tool = next(t for t in tools if t.name == "create_wa_dev_trial_link")
            result = _run(tool.ainvoke({"agent_name": "Mas Brew"}))

        data = json.loads(result)
        assert data["success"] is False
        assert data["error"] == "agent_target_ambiguous_for_current_request"
        assert data["latest_agent"]["agent_name"] == "Baas"
        assert data["provided_agent"]["agent_name"] == "Mas Brew"
        ensure_code.assert_not_awaited()
        send_contact.assert_not_awaited()

    def test_ambiguous_code_request_allows_latest_agent_id(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        baas_agent = _make_mock_agent(name="Baas", operator_ids=["+62811xxx"])
        mas_brew_agent = _make_mock_agent(name="Mas Brew", operator_ids=["+62811xxx"])
        direct_result = MagicMock()
        direct_result.scalar_one_or_none.return_value = baas_agent
        owned_result = MagicMock()
        owned_result.scalars.return_value.all.return_value = [baas_agent, mas_brew_agent]
        message_result = MagicMock()
        message_result.scalar_one_or_none.return_value = "Bagi kodenya"
        db.execute = AsyncMock(side_effect=[direct_result, owned_result, message_result])

        settings = MagicMock()
        settings.wa_dev_public_phone = "+628123456789"

        with (
            patch("app.core.tools.builder_tools.get_settings", return_value=settings),
            patch(
                "app.core.domain.wa_dev_trial_service.ensure_wa_dev_trial_code",
                new=AsyncMock(return_value="8EX446"),
            ) as ensure_code,
            patch("app.core.infra.wa_client.send_wa_contact", new=AsyncMock()) as send_contact,
        ):
            tools = build_builder_tools(
                db_factory=db,
                owner_phone="+62811xxx",
                device_id="arthur-device",
                default_target="+62811xxx",
                session_id=str(uuid.uuid4()),
            )
            tool = next(t for t in tools if t.name == "create_wa_dev_trial_link")
            result = _run(tool.ainvoke({"agent_id": str(baas_agent.id)}))

        data = json.loads(result)
        assert data["success"] is True
        assert data["agent_name"] == "Baas"
        assert data["code"] == "8EX446"
        ensure_code.assert_awaited_once()
        send_contact.assert_awaited_once_with("arthur-device", "+62811xxx", "Demo Baas", "628123456789")

    def test_stale_agent_id_conflicting_with_user_message_does_not_send_wrong_contact(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        session_id = uuid.uuid4()
        rnd_agent = _make_mock_agent(name="Rnd", operator_ids=["+62811xxx"])
        mas_brew_agent = _make_mock_agent(name="Mas Brew", operator_ids=["+62811xxx"])
        direct_result = MagicMock()
        direct_result.scalar_one_or_none.return_value = rnd_agent
        owned_result = MagicMock()
        owned_result.scalars.return_value.all.return_value = [rnd_agent, mas_brew_agent]
        message_result = MagicMock()
        message_result.scalar_one_or_none.return_value = "gua minta nomer mas brew bukan rnd"
        db.execute = AsyncMock(side_effect=[direct_result, owned_result, message_result])

        settings = MagicMock()
        settings.wa_dev_public_phone = "+628123456789"

        with (
            patch("app.core.tools.builder_tools.get_settings", return_value=settings),
            patch(
                "app.core.domain.wa_dev_trial_service.ensure_wa_dev_trial_code",
                new=AsyncMock(return_value="WRONG1"),
            ) as ensure_code,
            patch("app.core.infra.wa_client.send_wa_contact", new=AsyncMock()) as send_contact,
        ):
            tools = build_builder_tools(
                db_factory=db,
                owner_phone="+62811xxx",
                device_id="arthur-device",
                default_target="+62811xxx",
                session_id=str(session_id),
            )
            tool = next(t for t in tools if t.name == "create_wa_dev_trial_link")
            result = _run(tool.ainvoke({"agent_id": str(rnd_agent.id)}))

        data = json.loads(result)
        assert data["success"] is False
        assert data["error"] == "agent_target_conflict"
        assert data["provided_agent"]["agent_name"] == "Rnd"
        assert data["detected_agent"]["agent_name"] == "Mas Brew"
        ensure_code.assert_not_awaited()
        send_contact.assert_not_awaited()

    def test_duplicate_contact_send_is_suppressed_for_same_session_and_agent(self):
        from app.core.tools import builder_channel_tools
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()
        builder_channel_tools._contact_send_dedupe.clear()

        session_id = uuid.uuid4()
        agent = _make_mock_agent(name="Mas Brew", operator_ids=["+62811xxx"])
        first_direct = MagicMock()
        first_direct.scalar_one_or_none.return_value = agent
        first_owned = MagicMock()
        first_owned.scalars.return_value.all.return_value = [agent]
        first_message = MagicMock()
        first_message.scalar_one_or_none.return_value = ""
        second_direct = MagicMock()
        second_direct.scalar_one_or_none.return_value = agent
        second_owned = MagicMock()
        second_owned.scalars.return_value.all.return_value = [agent]
        second_message = MagicMock()
        second_message.scalar_one_or_none.return_value = ""
        db.execute = AsyncMock(side_effect=[
            first_direct,
            first_owned,
            first_message,
            second_direct,
            second_owned,
            second_message,
        ])

        settings = MagicMock()
        settings.wa_dev_public_phone = "+628123456789"

        with (
            patch("app.core.tools.builder_tools.get_settings", return_value=settings),
            patch(
                "app.core.domain.wa_dev_trial_service.ensure_wa_dev_trial_code",
                new=AsyncMock(return_value="79ZSXT"),
            ),
            patch("app.core.infra.wa_client.send_wa_contact", new=AsyncMock()) as send_contact,
        ):
            tools = build_builder_tools(
                db_factory=db,
                owner_phone="+62811xxx",
                device_id="arthur-device",
                default_target="+62811xxx",
                session_id=str(session_id),
            )
            tool = next(t for t in tools if t.name == "create_wa_dev_trial_link")
            first = json.loads(_run(tool.ainvoke({"agent_id": str(agent.id)})))
            second = json.loads(_run(tool.ainvoke({"agent_id": str(agent.id)})))

        assert first["contact_sent"] is True
        assert first["contact_already_sent"] is False
        assert second["contact_sent"] is False
        assert second["contact_already_sent"] is True
        send_contact.assert_awaited_once_with("arthur-device", "+62811xxx", "Demo Mas Brew", "628123456789")


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

        my_agent = _make_mock_agent(
            name="My Agent",
            operator_ids=["+62811xxx"],
            created_by_type="arthur_builder",
            created_by_agent_name="Arthur",
        )
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
        assert data["agents"][0]["created_by_type"] == "arthur_builder"
        assert data["agents"][0]["launch_metadata"]["created_by_arthur"] is True

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
        assert data["memory_refresh"]["updated"] is False
        assert my_agent.name == "Agent Baru"

    def test_update_blocked_when_new_config_exceeds_owner_plan(self):
        """Editing model/tools_config must be checked against the OWNER's current
        plan. A config that exceeds entitlement is rejected and not persisted.
        """
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        my_agent = _make_mock_agent(
            operator_ids=["62895619356936"],
            owner_external_id="62895619356936",
        )
        my_agent.tools_config = {}
        my_agent.channel_type = "whatsapp"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        plan = SimpleNamespace(code="trial", label="Trial", max_agents=1)
        sub = SimpleNamespace(tokens_remaining=2_000_000)

        async def _fake_get_sub(_phone, _db):
            return SimpleNamespace(id=uuid.uuid4()), sub, plan

        def _fake_validate(_plan, *, model, tools_config, channel_type):
            return ["Plan trial tidak mendukung subagents."]

        tools = build_builder_tools(db_factory=db, owner_phone="62895619356936")
        tool = next(t for t in tools if t.name == "update_agent")
        with patch(
            "app.core.domain.subscription_service.get_subscription_by_external_id",
            _fake_get_sub,
        ), patch(
            "app.core.domain.subscription_service.validate_agent_entitlements",
            _fake_validate,
        ):
            result = _run(tool.ainvoke({
                "agent_id": str(my_agent.id),
                "tools_config": json.dumps({"subagents": {"enabled": True}}),
            }))

        data = json.loads(result)
        assert "error" in data
        blob = (data.get("error", "") + " " + " ".join(data.get("violations", []))).lower()
        assert "subagents" in blob or "plan" in blob
        db.commit.assert_not_called()

    def test_update_agent_selective_refresh_writes_versioned_memory(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        my_agent = _make_mock_agent(
            operator_ids=["+62811xxx"],
            owner_external_id="62811xxx",
        )
        my_agent.description = "Agent lama"
        my_agent.instructions = "Instruksi lama yang masih cukup pendek."
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        upsert_calls: list[tuple[str, str]] = []

        async def fake_upsert_memory(agent_id, key, value, db_arg, scope=None):
            upsert_calls.append((key, value))
            return SimpleNamespace(agent_id=agent_id, key=key, value_data=value, scope=scope)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "update_agent")
        with patch("app.core.domain.memory_service.upsert_memory", fake_upsert_memory):
            result = _run(tool.ainvoke({
                "agent_id": str(my_agent.id),
                "instructions": "Instruksi baru lengkap untuk workflow agent yang sudah diperbarui dan lebih jelas.",
            }))
        data = json.loads(result)

        assert data["success"] is True
        assert data["memory_refresh"]["updated"] is True
        assert data["memory_refresh"]["mode"] == "selective"
        assert data["memory_refresh"]["context_version"] == my_agent.version
        keys = {key for key, _value in upsert_calls}
        assert f"soul:v{my_agent.version}" in keys
        assert f"agent_blueprint:v{my_agent.version}" in keys
        assert f"setup_summary:v{my_agent.version}" in keys
        assert "agent_context_version" in keys

    def test_update_agent_refresh_memory_mode_none_skips_memory_write(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        my_agent = _make_mock_agent(operator_ids=["+62811xxx"], owner_external_id="62811xxx")
        my_agent.instructions = "Instruksi lama yang masih cukup pendek."
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        upsert_mock = AsyncMock()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "update_agent")
        with patch("app.core.domain.memory_service.upsert_memory", upsert_mock):
            result = _run(tool.ainvoke({
                "agent_id": str(my_agent.id),
                "instructions": "Instruksi baru lengkap untuk workflow agent yang sudah diperbarui dan lebih jelas.",
                "refresh_memory_mode": "none",
            }))
        data = json.loads(result)

        assert data["success"] is True
        assert data["memory_refresh"]["mode"] == "none"
        assert data["memory_refresh"]["updated"] is False
        upsert_mock.assert_not_called()

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

    def test_update_agent_blocks_unsafe_payment_workflow(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        my_agent = _make_mock_agent(
            name="CVin aja",
            operator_ids=["+62811xxx"],
            tools_config={
                "memory": True,
                "skills": True,
                "escalation": False,
                "sandbox": True,
                "subagents": {"enabled": True},
                "whatsapp_media": True,
            },
            channel_type="whatsapp",
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "update_agent")
        result = _run(tool.ainvoke({
            "agent_id": str(my_agent.id),
            "instructions": "Kamu adalah CVin aja, asisten riset CV. Customer bayar dan admin approve.",
        }))
        data = json.loads(result)

        assert data["error"] == "Konfigurasi agent belum aman untuk diupdate."
        assert any("waiting_payment" in error for error in data["validation_errors"])
        assert any("escalation=true" in error for error in data["validation_errors"])
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

    def test_update_existing_google_workspace_agent_returns_auth_next_step(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        my_agent = _make_mock_agent(
            name="CVin aja",
            tools_config={
                "memory": True,
                "mcp": {"enabled": True, "servers": {"google_workspace": {"transport": "streamable_http"}}},
            },
        )
        my_agent.instructions = "Kamu adalah agent jasa CV."
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone=None)
        tool = next(t for t in tools if t.name == "update_agent")
        result = _run(tool.ainvoke({
            "agent_id": str(my_agent.id),
            "instructions": "Kamu adalah CVin aja, agent jasa CV yang memakai Google Drive untuk menyimpan dokumen referensi customer.",
        }))
        data = json.loads(result)

        assert data["success"] is True
        assert data["google_workspace_enabled"] is True
        assert data["needs_google_auth"] is True
        assert data["readback"]["tools_config_has_google_workspace"] is True
        assert data["readback"]["instructions_include_google_workspace"] is True
        assert "generate_google_auth_link" in data["next_step"]

    def test_generate_google_auth_link_reports_timeout_with_public_tunnel_context(self):
        import httpx
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "generate_google_auth_link")

        class TimeoutClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, *args, **kwargs):
                raise httpx.TimeoutException("connect timeout")

        settings = SimpleNamespace(
            google_integration_service_url="https://abc.devtunnels.ms",
            workspace_mcp_prefer_local="true",
            api_key="test-key",
        )
        with patch("app.core.tools.builder_tools.get_settings", return_value=settings), patch(
            "httpx.AsyncClient", TimeoutClient
        ):
            result = _run(tool.ainvoke({
                "agent_id": str(uuid.uuid4()),
                "external_user_id": "+628111111111",
            }))

        assert "Timeout" in result
        assert "https://abc.devtunnels.ms" in result
        assert "localhost:8003" not in result
        assert result.strip() != "[error]"

    def test_generate_google_auth_link_rejects_lid_identity(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "generate_google_auth_link")

        settings = SimpleNamespace(
            google_integration_service_url="http://localhost:8003",
            workspace_mcp_prefer_local="false",
            api_key="test-key",
        )
        with patch("app.core.tools.builder_tools.get_settings", return_value=settings):
            result = _run(tool.ainvoke({
                "agent_id": str(uuid.uuid4()),
                "external_user_id": "151414827434073@lid",
            }))

        assert "nomor whatsapp asli" in result.lower()
        assert "lid" in result.lower()


# ────────────────────────────────────────────────────────────────────────────
# Section 7: verify_agent launch readiness
# ────────────────────────────────────────────────────────────────────────────

class TestVerifyAgentReadiness:
    def test_verify_agent_blocks_launch_without_owner(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        agent = _make_mock_agent(operator_ids=[], owner_external_id=None)
        agent.instructions = (
            "IDENTITAS PLATFORM DAN OWNER\n"
            "Kamu adalah staff AI yang dibuat dan dikonfigurasi oleh Arthur."
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "verify_agent")
        result = _run(tool.ainvoke({"agent_id": str(agent.id)}))
        data = json.loads(result)

        assert data["status"] == "launch_blocked"
        assert data["launch_readiness"]["owner_present"] is False
        assert any("owner_missing" in blocker for blocker in data["launch_readiness"]["blockers"])

    def test_verify_agent_blocks_google_agent_without_auth(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        agent = _make_mock_agent(
            operator_ids=["+62811xxx"],
            owner_external_id="62811xxx",
            tools_config={
                "memory": True,
                "mcp": {
                    "enabled": True,
                    "servers": {
                        "google_workspace": {
                            "transport": "streamable_http",
                            "url": "http://localhost:8002/mcp",
                        }
                    },
                },
            },
        )
        agent.instructions = (
            "IDENTITAS PLATFORM DAN OWNER\n"
            "Kamu adalah staff AI yang dibuat dan dikonfigurasi oleh Arthur."
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "verify_agent")
        result = _run(tool.ainvoke({"agent_id": str(agent.id)}))
        data = json.loads(result)

        assert data["status"] == "launch_blocked"
        assert data["google_workspace_enabled"] is True
        assert data["needs_google_auth"] is True
        assert any("google_auth_required" in blocker for blocker in data["launch_readiness"]["blockers"])

    def test_verify_agent_warns_when_platform_identity_missing(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        agent = _make_mock_agent(operator_ids=["+62811xxx"], owner_external_id="62811xxx")
        agent.instructions = "Kamu adalah agent CS yang membantu pelanggan."
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "verify_agent")
        result = _run(tool.ainvoke({"agent_id": str(agent.id)}))
        data = json.loads(result)

        assert data["launch_readiness"]["owner_present"] is True
        assert data["launch_readiness"]["platform_identity_present"] is False
        assert data["launch_readiness"]["created_by_present"] is False
        assert any("created_by_metadata_missing" in warning for warning in data["launch_readiness"]["warnings"])
        assert any("platform_identity_missing" in warning for warning in data["launch_readiness"]["warnings"])

    def test_verify_agent_accepts_created_by_metadata_as_platform_identity(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        agent = _make_mock_agent(
            operator_ids=["+62811xxx"],
            owner_external_id="62811xxx",
            created_by_type="arthur_builder",
            created_by_agent_id=str(uuid.uuid4()),
            created_by_agent_name="Arthur",
            tools_config={"memory": True, "operating_manual": _usable_operating_manual()},
        )
        agent.instructions = "Kamu adalah agent CS yang membantu pelanggan."
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "verify_agent")
        result = _run(tool.ainvoke({"agent_id": str(agent.id)}))
        data = json.loads(result)

        assert data["status"] == "launch_ready"
        assert data["created_by_type"] == "arthur_builder"
        assert data["launch_readiness"]["created_by_present"] is True
        assert data["launch_readiness"]["platform_identity_present"] is True
        assert not any("created_by_metadata_missing" in warning for warning in data["launch_readiness"]["warnings"])

    def test_verify_agent_blocks_full_launch_without_operating_manual(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        agent = _make_mock_agent(
            operator_ids=["+62811xxx"],
            owner_external_id="62811xxx",
            created_by_type="arthur_builder",
            created_by_agent_id=str(uuid.uuid4()),
            created_by_agent_name="Arthur",
            tools_config={"memory": True},
        )
        agent.instructions = "Kamu adalah agent CS yang membantu pelanggan."
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "verify_agent")
        result = _run(tool.ainvoke({"agent_id": str(agent.id)}))
        data = json.loads(result)

        assert data["status"] == "launch_blocked"
        assert data["launch_readiness"]["operating_manual"]["present"] is False
        assert any("operating_manual_missing" in blocker for blocker in data["launch_readiness"]["blockers"])
        setup = data["setup_status_for_owner"]
        assert any(item["key"] == "operating_manual" and item["status"] == "needs_setup" for item in setup["items"])

    def test_verify_agent_blocks_draft_sop_as_intake_safe_only(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        draft_manual = _usable_operating_manual()
        draft_manual["maturity"] = "draft"
        draft_manual["owner_review_required"] = True
        draft_manual["missing_context"] = ["detail bisnis belum lengkap"]
        agent = _make_mock_agent(
            operator_ids=["+62811xxx"],
            owner_external_id="62811xxx",
            created_by_type="arthur_builder",
            created_by_agent_id=str(uuid.uuid4()),
            created_by_agent_name="Arthur",
            tools_config={"memory": True, "operating_manual": draft_manual},
        )
        agent.instructions = "Kamu adalah agent CS yang membantu pelanggan."
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "verify_agent")
        result = _run(tool.ainvoke({"agent_id": str(agent.id)}))
        data = json.loads(result)

        assert data["status"] == "launch_blocked"
        assert data["launch_readiness"]["operating_manual"]["maturity"] == "draft"
        assert any("operating_manual_draft" in blocker for blocker in data["launch_readiness"]["blockers"])
        assert any(item["key"] == "operating_manual" and item["status"] == "needs_review" for item in data["setup_status_for_owner"]["items"])

    def test_verify_agent_blocks_payment_workflow_without_escalation(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        agent = _make_mock_agent(
            name="CVin aja",
            operator_ids=["+62811xxx"],
            owner_external_id="62811xxx",
            tools_config={
                "memory": True,
                "escalation": False,
                "sandbox": True,
                "subagents": {"enabled": True},
                "whatsapp_media": True,
            },
            channel_type="whatsapp",
        )
        agent.wa_device_id = "wa-device-1"
        agent.instructions = (
            "IDENTITAS PLATFORM DAN OWNER\n"
            "Kamu adalah staff AI yang dibuat dan dikonfigurasi oleh Arthur.\n"
            "Kamu mengurus jasa CV ATS. Customer bayar, kirim bukti transfer, "
            "dan admin harus approve sebelum CV dikirim."
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        db.execute = AsyncMock(return_value=mock_result)

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "verify_agent")
        result = _run(tool.ainvoke({"agent_id": str(agent.id)}))
        data = json.loads(result)

        assert data["status"] == "launch_blocked"
        assert any(
            "Workflow pembayaran/admin approval wajib escalation=true" in blocker
            for blocker in data["launch_readiness"]["blockers"]
        )

    def test_rag_enabled_without_documents_asks_for_documents(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        agent = _make_mock_agent(
            operator_ids=["+62811xxx"],
            owner_external_id="62811xxx",
            created_by_type="arthur_builder",
            created_by_agent_name="Arthur",
            tools_config={"memory": True, "rag": True, "operating_manual": _usable_operating_manual()},
        )
        agent.instructions = "Kamu adalah agent FAQ yang menjawab berdasarkan dokumen."
        agent_result = MagicMock()
        agent_result.scalar_one_or_none.return_value = agent
        document_count_result = MagicMock()
        document_count_result.scalar_one.return_value = 0
        db.execute = AsyncMock(side_effect=[agent_result, document_count_result])

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "verify_agent")
        result = _run(tool.ainvoke({"agent_id": str(agent.id)}))
        data = json.loads(result)

        assert data["status"] == "launch_blocked"
        assert data["rag_enabled"] is True
        assert data["document_count"] == 0
        assert any("rag_documents_required" in blocker for blocker in data["launch_readiness"]["blockers"])
        setup = data["setup_status_for_owner"]
        assert setup["summary_for_owner"] == "Agent belum siap launch. Ada setup yang perlu dibereskan dulu."
        assert any(item["key"] == "knowledge_base" and item["status"] == "needs_setup" for item in setup["items"])
        assert any("Upload dokumen" in step for step in setup["next_steps"])

    def test_verify_agent_reports_plain_language_setup_status(self):
        from app.core.tools.builder_tools import build_builder_tools

        db = _make_mock_db()
        agent = _make_mock_agent(
            operator_ids=["+62811xxx"],
            owner_external_id="62811xxx",
            created_by_type="arthur_builder",
            created_by_agent_name="Arthur",
            tools_config={"memory": True, "rag": True, "escalation": True, "operating_manual": _usable_operating_manual()},
            channel_type="whatsapp",
        )
        agent.wa_device_id = "wa-device-1"
        agent.instructions = "Kamu adalah agent CS yang menjawab berdasarkan dokumen."
        agent_result = MagicMock()
        agent_result.scalar_one_or_none.return_value = agent
        document_count_result = MagicMock()
        document_count_result.scalar_one.return_value = 2
        db.execute = AsyncMock(side_effect=[agent_result, document_count_result])

        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "verify_agent")
        result = _run(tool.ainvoke({"agent_id": str(agent.id)}))
        data = json.loads(result)

        assert data["status"] == "launch_ready"
        setup = data["setup_status_for_owner"]
        assert setup["summary_for_owner"] == "Agent siap dites atau digunakan."
        assert any(item["key"] == "knowledge_base" and "2 dokumen" in item["message"] for item in setup["items"])
        assert any(item["key"] == "whatsapp" and item["status"] == "ready" for item in setup["items"])
        assert any(item["key"] == "human_handoff" and item["status"] == "ready" for item in setup["items"])


# ────────────────────────────────────────────────────────────────────────────
# Section 8: get_agent_detail ownership check
# ────────────────────────────────────────────────────────────────────────────

class TestGetAgentDetail:
    def test_returns_detail_for_owned_agent(self):
        from app.core.tools.builder_tools import build_builder_tools
        db = _make_mock_db()

        my_agent = _make_mock_agent(
            operator_ids=["+62811xxx"],
            created_by_type="arthur_builder",
            created_by_agent_id="arthur-agent-id",
            created_by_agent_name="Arthur",
        )
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
        assert data["created_by_type"] == "arthur_builder"
        assert data["created_by_agent_name"] == "Arthur"
        assert data["launch_metadata"]["created_by_arthur"] is True

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


class TestLlmJsonRecovery:
    """Regression tests untuk parsing JSON blueprint yang ke-truncate / dibungkus prosa."""

    def test_valid_json_unchanged(self):
        from app.core.tools.builder_tools import _parse_llm_json_object
        parsed, repaired = _parse_llm_json_object('{"a":1,"b":[1,2],"c":{"d":true}}')
        assert parsed == {"a": 1, "b": [1, 2], "c": {"d": True}}
        assert repaired is False

    def test_prose_wrapped_json(self):
        from app.core.tools.builder_tools import _parse_llm_json_object
        parsed, _ = _parse_llm_json_object('Here is the blueprint:\n{"a":"ok","b":2}\nDone.')
        assert parsed == {"a": "ok", "b": 2}

    def test_truncated_string_value(self):
        from app.core.tools.builder_tools import _parse_llm_json_object
        parsed, repaired = _parse_llm_json_object('{"a":"ok","b":"cut off mid')
        assert parsed["a"] == "ok"
        assert parsed["b"].startswith("cut off")
        assert repaired is True

    def test_truncated_nested_array(self):
        from app.core.tools.builder_tools import _parse_llm_json_object
        raw = ('{"agent_summary":"CS bot","workflow_steps":[{"step":1,'
               '"name":"intake","required_user_data":["nama","keluhan"')
        parsed, _ = _parse_llm_json_object(raw)
        assert parsed["agent_summary"] == "CS bot"
        assert parsed["workflow_steps"][0]["step"] == 1
        assert "nama" in parsed["workflow_steps"][0]["required_user_data"]

    def test_dangling_key_dropped(self):
        from app.core.tools.builder_tools import _parse_llm_json_object
        parsed, _ = _parse_llm_json_object('{"a":"ok","validation_che')
        assert parsed == {"a": "ok"}

    def test_dangling_colon_filled_null(self):
        from app.core.tools.builder_tools import _parse_llm_json_object
        parsed, _ = _parse_llm_json_object('{"a":"ok","needs_upload":')
        assert parsed == {"a": "ok", "needs_upload": None}

    def test_trailing_comma(self):
        from app.core.tools.builder_tools import _parse_llm_json_object
        assert _parse_llm_json_object('{"a":"ok","b":"two",')[0] == {"a": "ok", "b": "two"}
        assert _parse_llm_json_object('{"x":[1,2,3,')[0] == {"x": [1, 2, 3]}

    def test_truncated_literal_completed(self):
        from app.core.tools.builder_tools import _parse_llm_json_object
        assert _parse_llm_json_object('{"needs_upload":tru')[0] == {"needs_upload": True}
        assert _parse_llm_json_object('{"flag":fal')[0] == {"flag": False}
        assert _parse_llm_json_object('{"x":[1,nul')[0] == {"x": [1, None]}


# ────────────────────────────────────────────────────────────────────────────
# Section: file_delivery_contract_issues (A4)
# ────────────────────────────────────────────────────────────────────────────

from app.core.tools.builder_intent import (
    _looks_like_file_delivery_workflow,
    _looks_like_generated_file_workflow,
)
from app.core.tools.builder_tools import file_delivery_contract_issues


def test_parent_delivery_contract_ok():
    instr = ("Subagent simpan ke /workspace/shared/hasil.pdf, return SIAP_DIKIRIM_PARENT. "
             "Subagent tidak boleh kirim WhatsApp. Parent kirim via send_whatsapp_document.")
    assert file_delivery_contract_issues(instr, file_delivery=True) == []


def test_parent_delivery_contract_missing_markers():
    issues = file_delivery_contract_issues("Kirim file ke customer.", file_delivery=True)
    assert issues


def test_no_file_delivery_means_no_issue():
    assert file_delivery_contract_issues("CS biasa tanpa file.", file_delivery=False) == []


def test_parent_delivery_contract_image_only():
    instr = "/workspace/shared/result.png SIAP_DIKIRIM_PARENT send_whatsapp_image"
    assert file_delivery_contract_issues(instr, file_delivery=True) == []


def test_data_visualization_pdf_counts_as_generated_file_workflow():
    text = "Agent visualisasi data Titanic, buat grafik, lalu kirim laporan PDF ke WhatsApp."

    assert _looks_like_file_delivery_workflow(text) is True
    assert _looks_like_generated_file_workflow(text) is True


def test_generic_generate_file_counts_as_generated_file_workflow():
    text = "Bikinin personal assistant yang bisa generate file dan terhubung ke Google Workspace."

    assert _looks_like_file_delivery_workflow(text) is True
    assert _looks_like_generated_file_workflow(text) is True


# ---------------------------------------------------------------------------
# A5: mark_manual_needs_review_if_fallback
# ---------------------------------------------------------------------------
from app.core.tools.builder_tools import mark_manual_needs_review_if_fallback


def test_fallback_manual_forced_needs_review():
    m = mark_manual_needs_review_if_fallback({"maturity": "usable"}, used_fallback=True)
    assert m["maturity"] == "needs_review"
    assert m["owner_review_required"] is True


def test_non_fallback_unchanged():
    m = mark_manual_needs_review_if_fallback({"maturity": "usable", "owner_review_required": False}, used_fallback=False)
    assert m["maturity"] == "usable"
    assert m["owner_review_required"] is False


# ---------------------------------------------------------------------------
# A6: _build_owner_setup_status — WhatsApp media gate note when SOP is draft/needs_review
# ---------------------------------------------------------------------------
from app.core.tools.builder_tools import _build_owner_setup_status  # noqa: E402

_BASE_KWARGS: dict = dict(
    launch_status="needs_review",
    owner_present=True,
    created_by_present=True,
    google_workspace_enabled=False,
    rag_enabled=False,
    document_count=0,
    whatsapp_channel=True,
    whatsapp_ready=True,
    escalation_enabled=False,
    readiness_blockers=[],
    readiness_warnings=[],
)


def _get_manual_item(result: dict) -> dict | None:
    for item in result.get("items", []):
        if item.get("key") == "operating_manual":
            return item
    return None


def test_sop_needs_review_with_media_enabled_shows_gate_note():
    result = _build_owner_setup_status(
        **_BASE_KWARGS,
        operating_manual={"present": True, "maturity": "needs_review"},
        media_enabled=True,
    )
    item = _get_manual_item(result)
    assert item is not None
    assert "maturity=needs_review" in item["message"]
    assert "file/gambar" in item["message"]


def test_sop_draft_with_media_enabled_shows_gate_note():
    result = _build_owner_setup_status(
        **_BASE_KWARGS,
        operating_manual={"present": True, "maturity": "draft"},
        media_enabled=True,
    )
    item = _get_manual_item(result)
    assert item is not None
    assert "maturity=draft" in item["message"]
    assert "file/gambar" in item["message"]


def test_sop_needs_review_without_media_no_gate_note():
    result = _build_owner_setup_status(
        **_BASE_KWARGS,
        operating_manual={"present": True, "maturity": "needs_review"},
        media_enabled=False,
    )
    item = _get_manual_item(result)
    assert item is not None
    assert "file/gambar" not in item["message"]


def test_sop_usable_with_media_enabled_no_gate_note():
    result = _build_owner_setup_status(
        **_BASE_KWARGS,
        operating_manual={"present": True, "maturity": "usable"},
        media_enabled=True,
    )
    item = _get_manual_item(result)
    assert item is not None
    assert "file/gambar" not in item["message"]
