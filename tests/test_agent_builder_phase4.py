"""
Tests untuk Phase 4 Agent Builder — Integration & Pipeline Validation.

Mensimulasikan alur percakapan Arthur end-to-end tanpa koneksi WA/DB:
- Seed script dapat dijalankan (dry-run)
- Arthur config valid (is_system_agent, tools_config, model)
- Full builder pipeline: validate → create → list → get_detail → update
- Isolation: user A tidak bisa akses agent user B
- validate_agent_config menerapkan semua WA best practices rules
- System prompt template dari rulebook bisa dipakai sebagai base instructions
- Semua komponen terintegrasi: builder_tools dimuat hanya untuk system agent
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


# ── helper ──────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_mock_db():
    db = MagicMock()
    db.return_value.__aenter__.return_value = db
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.commit = AsyncMock()
    return db


def _make_agent(operator_ids=None, name="Test", tools_config=None, wa_device_id=None):
    a = MagicMock()
    a.id = uuid.uuid4()
    a.name = name
    a.description = "desc"
    a.model = "openai/gpt-4.1"
    a.temperature = 0.7
    a.tools_config = tools_config or {"memory": True}
    a.sandbox_config = {}
    a.safety_policy = {}
    a.escalation_config = {}
    a.operator_ids = operator_ids or []
    a.allowed_senders = None
    a.capabilities = []
    a.is_deleted = False
    a.api_key = "ak-test"
    a.token_quota = 4_000_000
    a.tokens_used = 0
    a.active_until = datetime.now(timezone.utc)
    a.quota_period_days = 30
    a.wa_device_id = wa_device_id
    a.channel_type = None
    a.version = 1
    a.instructions = "You are helpful."
    return a


# ── Section 1: Seed Script ───────────────────────────────────────────────────

class TestSeedScript:
    def test_seed_script_exists(self):
        p = pathlib.Path(__file__).parent.parent / "scripts/seed_arthur.py"
        assert p.exists(), "scripts/seed_arthur.py harus ada"

    def test_seed_script_has_arthur_config(self):
        p = pathlib.Path(__file__).parent.parent / "scripts/seed_arthur.py"
        src = p.read_text()
        assert "capabilities" in src
        assert "Arthur" in src
        assert "system-message-builder.md" in src

    def test_seed_dry_run_works(self):
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "scripts/seed_arthur.py", "--dry-run"],
            cwd=str(pathlib.Path(__file__).parent.parent),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"dry-run gagal: {result.stderr}"
        assert "DRY RUN" in result.stdout
        assert "capabilities" in result.stdout


# ── Section 2: Arthur Config Validation ─────────────────────────────────────

class TestArthurConfig:
    def test_rulebook_referenced_in_seed(self):
        p = pathlib.Path(__file__).parent.parent / "scripts/seed_arthur.py"
        src = p.read_text()
        assert "system-message-builder.md" in src

    def test_arthur_tools_config_uses_internal_builder_tools_and_wa_manager(self):
        p = pathlib.Path(__file__).parent.parent / "scripts/seed_arthur.py"
        src = p.read_text()
        assert '"http": False' in src or "'http': False" in src, \
            "Arthur harus memakai builder tools internal, bukan HTTP/ngrok platform"
        assert '"tavily": True' in src or "'tavily': True" in src, \
            "Arthur harus default punya browsing Tavily"
        assert "wa_agent_manager" in src, \
            "Arthur butuh wa_agent_manager untuk kirim QR ke user"

    def test_arthur_seed_has_unlimited_quota(self):
        p = pathlib.Path(__file__).parent.parent / "scripts/seed_arthur.py"
        src = p.read_text()
        assert '"token_quota": 0' in src, "Arthur harus unlimited quota; 0 berarti tidak dibatasi"

    def test_rulebook_uses_current_arthur_model(self):
        p = pathlib.Path(__file__).parent.parent / "system-message-builder.md"
        src = p.read_text()
        assert "Model Arthur sendiri: openai/gpt-4.1-mini" in src
        assert "Model Arthur sendiri: deepseek/deepseek-v4-flash" not in src
        assert "Model writer untuk blueprint/instructions/manual/soul: deepseek/deepseek-v4-pro" in src

    def test_arthur_has_system_capabilities(self):
        p = pathlib.Path(__file__).parent.parent / "scripts/seed_arthur.py"
        src = p.read_text()
        assert "capabilities" in src
        assert '"system"' in src or "'system'" in src

    def test_arthur_allowed_senders_null(self):
        p = pathlib.Path(__file__).parent.parent / "scripts/seed_arthur.py"
        src = p.read_text()
        assert "allowed_senders': None" in src or '"allowed_senders": None' in src or \
               "allowed_senders=None" in src or "allowed_senders\": None" in src, \
            "Arthur harus allowed_senders=None agar terbuka untuk siapapun"

    def test_system_prompt_injects_current_time_and_arthur_tool_categories(self):
        from app.core.engine.prompt_builder import build_system_prompt

        agent_id = uuid.uuid4()
        agent = SimpleNamespace(
            id=agent_id,
            name="Arthur",
            model="openai/gpt-4.1-mini",
            instructions="Kamu adalah Arthur.",
            tools_config={"builder": True, "memory": True},
            safety_policy={},
            escalation_config={},
            operator_ids=[],
            owner_external_id="",
            created_by_type="system",
            created_by_agent_id="",
            created_by_agent_name="System",
            capabilities=["system", "builder"],
            _runtime_operating_manual={},
        )
        session = SimpleNamespace(
            id=uuid.uuid4(),
            agent_id=agent_id,
            channel_type="whatsapp",
            channel_config={"user_phone": "628111111111"},
            external_user_id="628111111111",
        )

        prompt = build_system_prompt(
            agent_model=agent,
            session=session,
            active_groups=["memory", "skills", "tavily", "wa_agent_manager", "builder"],
            saved_custom_tools=[],
            subagent_list=[],
            sender_name="Bagas",
            context_summary="",
            memory_block="",
            layered_memory={},
            rag_context="",
            escalation_user_jid=None,
            escalation_context=None,
            is_operator_message=False,
            user_message="edit agent saya",
            current_time=datetime(2026, 6, 4, 9, 30, tzinfo=timezone.utc),
        )

        assert "## Current Time" in prompt
        assert "Kamis, 4 Juni 2026, 16:30 WIB" in prompt
        assert "## Arthur Tool Categories" in prompt
        assert "Agent Management" in prompt
        assert "Channel Management" in prompt
        assert "Workspace/App Connectors" in prompt


# ── Section 3: Full Builder Pipeline ────────────────────────────────────────

class TestBuilderPipelineFlow:
    """Simulasi alur Arthur: validate → create → list → get_detail → update."""

    def _get_tools(self, owner="+62811xxx"):
        from app.core.tools.builder_tools import build_builder_tools
        return build_builder_tools(db_factory=_make_mock_db(), owner_phone=owner)

    def test_step1_validate_before_create(self):
        tools = self._get_tools()
        validate = next(t for t in tools if t.name == "validate_agent_config")
        result = _run(validate.ainvoke({
            "name": "CS Toko Baju Indah",
            "instructions": (
                "Kamu adalah Sari, asisten CS dari Toko Baju Indah. "
                "Tugasmu membantu pelanggan yang chat via WhatsApp. "
                "Jawab pertanyaan seputar produk, stok, dan pengiriman. "
                "Eskalasikan ke operator jika ada komplain serius. " * 5
            ),
            "tools_config": '{"memory": true, "escalation": true, "whatsapp_media": true}',
            "model": "openai/gpt-4.1",
        }))
        data = json.loads(result)
        assert data["valid"] is True
        assert data["quality_score"] >= 70

    def test_step2_create_agent_success(self):
        db = _make_mock_db()

        with patch("app.core.tools.builder_tools.Agent") as MockAgent:
            instance = _make_agent(operator_ids=["+62811xxx"])
            MockAgent.return_value = instance

            from app.core.tools.builder_tools import build_builder_tools
            tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
            create = next(t for t in tools if t.name == "create_agent")
            result = _run(create.ainvoke({
                "name": "CS Toko Baju Indah",
                "instructions": "Kamu adalah Sari, CS dari Toko Baju Indah.",
                "tools_config": '{"memory": true, "escalation": true}',
                "channel_type": "whatsapp",
            }))

            data = json.loads(result)
            assert data["success"] is True
            assert "agent_id" in data

    def test_step3_list_shows_new_agent(self):
        db = _make_mock_db()
        my_agent = _make_agent(name="CS Toko Baju Indah", operator_ids=["+62811xxx"])

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [my_agent]
        db.execute = AsyncMock(return_value=mock_result)

        from app.core.tools.builder_tools import build_builder_tools
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        list_tool = next(t for t in tools if t.name == "list_my_agents")
        result = _run(list_tool.ainvoke({}))
        data = json.loads(result)
        assert data["count"] == 1
        assert data["agents"][0]["name"] == "CS Toko Baju Indah"

    def test_step4_get_detail_for_review(self):
        db = _make_mock_db()
        my_agent = _make_agent(name="CS Toko Baju Indah", operator_ids=["+62811xxx"])

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        from app.core.tools.builder_tools import build_builder_tools
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        get_detail = next(t for t in tools if t.name == "get_agent_detail")
        result = _run(get_detail.ainvoke({
            "agent_id": str(my_agent.id),
            "include_instructions": True,
        }))
        data = json.loads(result)
        assert "tools_config" in data
        assert "instructions_preview" in data
        assert data["instructions"] == my_agent.instructions
        assert data["name"] == "CS Toko Baju Indah"

    def test_step5_update_instructions(self):
        db = _make_mock_db()
        my_agent = _make_agent(operator_ids=["+62811xxx"])

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        from app.core.tools.builder_tools import build_builder_tools
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        update = next(t for t in tools if t.name == "update_agent")
        result = _run(update.ainvoke({
            "agent_id": str(my_agent.id),
            "instructions": "Updated instructions dengan konten yang lebih baik.",
            "name": "CS Toko Baju Indah v2",
        }))
        data = json.loads(result)
        assert data["success"] is True
        assert "instructions" in data["updated_fields"]
        assert "name" in data["updated_fields"]

    def test_update_rejects_summary_overwrite_of_long_instructions(self):
        db = _make_mock_db()
        my_agent = _make_agent(operator_ids=["+62811xxx"])
        my_agent.instructions = "Instruksi operasional lengkap. " * 120

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = my_agent
        db.execute = AsyncMock(return_value=mock_result)

        from app.core.tools.builder_tools import build_builder_tools
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
        update = next(t for t in tools if t.name == "update_agent")
        result = _run(update.ainvoke({
            "agent_id": str(my_agent.id),
            "instructions": "Agent sudah diperbarui agar bisa eskalasi jika ada bukti transfer.",
        }))
        data = json.loads(result)

        assert data["error"].startswith("Instruksi baru terlalu pendek")
        assert "include_instructions=true" in data["hint"]


class TestArthurRuntimeToolGating:
    def test_builder_runtime_skips_sandbox_and_subagents_even_if_config_drifted(self):
        from app.core.engine import agent_tool_setup as setup_mod

        agent_id = uuid.uuid4()
        agent = SimpleNamespace(
            id=agent_id,
            capabilities=["system", "builder"],
            tools_config={
                "builder": True,
                "memory": True,
                "skills": True,
                "escalation": True,
                "sandbox": True,
                "deploy": True,
                "tool_creator": True,
                "subagents": {"enabled": True},
                "wa_agent_manager": True,
            },
            name="Arthur",
        )
        session = SimpleNamespace(
            id=uuid.uuid4(),
            agent_id=agent_id,
            channel_type="whatsapp",
            channel_config={
                "device_id": "arthur-device",
                "user_phone": "628111111111",
                "phone_number": "+628111111111",
            },
            external_user_id="628111111111",
        )
        log = SimpleNamespace(
            info=MagicMock(),
            warning=MagicMock(),
            debug=MagicMock(),
        )

        with patch.object(setup_mod, "DockerSandbox") as sandbox_cls, patch.object(
            setup_mod,
            "build_subagents",
            new=AsyncMock(return_value=([{"name": "should_not_exist"}], [])),
        ) as build_subagents:
            result = _run(setup_mod.build_agent_tool_setup(
                agent_model=agent,
                session=session,
                tools_config=agent.tools_config,
                raw_tools_config=agent.tools_config,
                db=MagicMock(),
                log=log,
                escalation_user_jid=None,
                sender_name="Alsa",
                user_message="edit agent saya bisa eskalasi kalau ada yang kirim bukti tf",
            ))

        sandbox_cls.assert_not_called()
        build_subagents.assert_not_awaited()
        assert "builder" in result.active_groups
        assert "sandbox" not in result.active_groups
        assert "deploy" not in result.active_groups
        assert not any(str(group).startswith("subagents(") for group in result.active_groups)
        assert not any(getattr(tool, "name", "") == "sandbox_write_binary_file" for tool in result.tools)


# ── Section 4: Tenant Isolation ─────────────────────────────────────────────

class TestTenantIsolation:
    """User A tidak bisa akses/modify agent milik user B."""

    def test_user_a_cannot_list_user_b_agents(self):
        db = _make_mock_db()
        user_b_agent = _make_agent(name="B Agent", operator_ids=["+62999yyy"])

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [user_b_agent]
        db.execute = AsyncMock(return_value=mock_result)

        from app.core.tools.builder_tools import build_builder_tools
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")  # User A
        list_tool = next(t for t in tools if t.name == "list_my_agents")
        result = _run(list_tool.ainvoke({}))
        data = json.loads(result)
        assert data["count"] == 0  # User A lihat 0 agent

    def test_user_a_cannot_update_user_b_agent(self):
        db = _make_mock_db()
        user_b_agent = _make_agent(name="B Agent", operator_ids=["+62999yyy"])

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user_b_agent
        db.execute = AsyncMock(return_value=mock_result)

        from app.core.tools.builder_tools import build_builder_tools
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")  # User A
        update = next(t for t in tools if t.name == "update_agent")
        result = _run(update.ainvoke({
            "agent_id": str(user_b_agent.id),
            "name": "Hacked",
        }))
        assert "[error]" in result
        assert "akses" in result.lower()

    def test_user_a_cannot_read_user_b_agent(self):
        db = _make_mock_db()
        user_b_agent = _make_agent(name="B Agent", operator_ids=["+62999yyy"])

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = user_b_agent
        db.execute = AsyncMock(return_value=mock_result)

        from app.core.tools.builder_tools import build_builder_tools
        tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")  # User A
        get_detail = next(t for t in tools if t.name == "get_agent_detail")
        result = _run(get_detail.ainvoke({"agent_id": str(user_b_agent.id)}))
        assert "[error]" in result

    def test_created_agent_not_system_agent(self):
        """Agent yang dibuat via create_agent tidak boleh memiliki system capabilities."""
        db = _make_mock_db()

        with patch("app.core.tools.builder_tools.Agent") as MockAgent:
            captured = {}

            def capture(**kwargs):
                captured.update(kwargs)
                return _make_agent(operator_ids=["+62811xxx"])

            MockAgent.side_effect = capture

            from app.core.tools.builder_tools import build_builder_tools
            tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
            create = next(t for t in tools if t.name == "create_agent")
            _run(create.ainvoke({
                "name": "New Agent",
                "instructions": "Be helpful.",
            }))
            assert "system" not in (captured.get("capabilities") or []), \
                "Agent yang dibuat user tidak boleh memiliki capability 'system'"


# ── Section 5: agent_runner integration ─────────────────────────────────────

class TestAgentRunnerIntegration:
    def test_agent_runner_loads_builder_tools_for_system_agent(self):
        """Verifikasi agent_runner.py muat builder tools jika capabilities contains 'system'."""
        import pathlib
        src = (pathlib.Path(__file__).parent.parent / "app/core/engine/agent_runner.py").read_text()
        assert "capabilities" in src
        assert "build_builder_tools" in src
        assert "active_groups.append" in src and "builder" in src

    def test_tool_builder_exports_build_builder_tools(self):
        from app.core.engine.tool_builder import build_builder_tools
        assert callable(build_builder_tools)

    def test_builder_tools_module_importable(self):
        from app.core.tools.builder_tools import build_builder_tools
        assert callable(build_builder_tools)

    def test_builder_tools_not_loaded_for_regular_agent(self):
        """Pastikan builder tools TIDAK dimuat untuk agent biasa.
        Test ini verifikasi pattern check di agent_runner.py."""
        import pathlib
        src = (pathlib.Path(__file__).parent.parent / "app/core/engine/agent_runner.py").read_text()
        # Harus ada kondisi guard untuk capabilities
        assert "capabilities" in src
        assert "system" in src


# ── Section 6: WA Best Practices Enforcement ────────────────────────────────

class TestWABestPracticesValidation:
    """validate_agent_config harus enforce semua WA best practices."""

    def _validate(self, **kwargs):
        from app.core.tools.builder_tools import build_builder_tools
        tools = build_builder_tools(db_factory=_make_mock_db(), owner_phone="+62811xxx")
        tool = next(t for t in tools if t.name == "validate_agent_config")
        return json.loads(_run(tool.ainvoke(kwargs)))

    def test_warns_about_markdown_bold(self):
        result = self._validate(name="Test", instructions="**Selamat datang** di toko kami " * 5, channel_type="whatsapp")
        assert any("markdown" in w for w in result["warnings"])

    def test_warns_about_markdown_heading(self):
        result = self._validate(name="Test", instructions="## Selamat Datang\nIni toko kami " * 5, channel_type="whatsapp")
        assert any("markdown" in w for w in result["warnings"])

    def test_suggests_escalation_when_missing(self):
        result = self._validate(
            name="Test",
            instructions="Kamu adalah CS yang membantu pelanggan berbelanja. " * 10,
            tools_config='{"escalation": true}',
        )
        has_esc_suggestion = any(
            "eskalasi" in s.lower() or "escalat" in s.lower()
            for s in result["suggestions"]
        )
        assert has_esc_suggestion

    def test_no_warnings_for_good_instructions(self):
        result = self._validate(
            name="CS Agent",
            instructions=(
                "Kamu adalah Sari, CS dari Toko Baju Indah. "
                "Bantu pelanggan tanya stok, harga, pengiriman. "
                "Eskalasikan ke operator jika ada komplain serius atau refund. "
                "Contoh percakapan:\n"
                "User: berapa harga kemeja?\n"
                "Sari: Harga kemeja mulai dari Rp 150.000. Mau lihat koleksi terbaru? "
            ) * 3,
            tools_config='{"memory": true, "escalation": true}',
            model="openai/gpt-4.1",
        )
        # Tidak boleh ada error
        assert result["valid"] is True
        # Tidak boleh ada markdown warning
        assert not any("markdown" in w for w in result["warnings"])

    def test_invalid_json_tools_config_caught(self):
        result = self._validate(
            name="Test",
            instructions="Good instructions " * 10,
            tools_config="bukan json",
        )
        assert result["valid"] is False
        assert any("JSON" in e or "json" in e.lower() for e in result["errors"])

    def test_quality_score_penalized_for_errors(self):
        bad = self._validate(name="", instructions="")
        good = self._validate(
            name="Test Agent",
            instructions="Kamu adalah CS. " * 20 + "Eskalasikan jika perlu.",
            model="openai/gpt-4.1",
        )
        assert bad["quality_score"] < good["quality_score"]
