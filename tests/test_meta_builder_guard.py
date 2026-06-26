"""Arthur tidak boleh membuat agent yang fungsinya membuat AI agent lain.

Permintaan "buatkan agent builder / agent yang bisa bikin agent / agent seperti
Arthur" harus ditolak di SEMUA jalur builder (plan_agent, validate_agent_config,
create_agent, update_agent) lewat satu gerbang `blocked_agent_policy_reason`.
"""
import asyncio
import json

import pytest

from app.core.tools.builder_identity import (
    META_BUILDER_AGENT_POLICY_MESSAGE,
    blocked_agent_policy_reason,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_mock_db():
    from tests.test_builder_tools import _make_mock_db as mk
    return mk()


META_BUILDER_REQUESTS = [
    "Buatkan agent builder yang bisa bikin AI agent lain",
    "Aku mau agent yang tugasnya membuat agent baru otomatis",
    "Bikin agent pembuat agent dong",
    "Mau agent yang cara kerjanya seperti Arthur",
    "Tolong clone Arthur jadi agent baru",
    "Buatkan Arthur kedua untuk tim aku",
    "I want an agent builder that creates other agents",
    "Bikin meta-agent yang generate agent sesuai kebutuhan",
    "Mau AI yang bisa membuat AI lain",
    "Agent yang fungsinya mirip kamu, bisa bangun agent",
]

BENIGN_REQUESTS = [
    "Buatkan agent CS WhatsApp untuk toko kue",
    "Bikin agent baru untuk reservasi salon",
    "Mau agent asisten pribadi buat atur jadwal",
    "Agent untuk follow up customer yang belum bayar",
    "Buat agent riset yang cari info produk kompetitor",
    "Agent FAQ untuk klinik gigi pakai Google Calendar",
]


@pytest.mark.parametrize("text", META_BUILDER_REQUESTS)
def test_blocks_meta_builder_requests(text):
    reason = blocked_agent_policy_reason(text)
    assert reason == META_BUILDER_AGENT_POLICY_MESSAGE


@pytest.mark.parametrize("text", BENIGN_REQUESTS)
def test_allows_benign_agent_requests(text):
    assert blocked_agent_policy_reason(text) == ""


def test_empty_input_not_blocked():
    assert blocked_agent_policy_reason("", None) == ""


def test_buzzer_politics_still_blocked():
    # Regression: kategori lama tetap diblok.
    assert blocked_agent_policy_reason("agent buzzer kampanye politik") != ""


# ── Integration: all builder paths reject meta-builder ──────────────────────

def _tools():
    from app.core.tools.builder_tools import build_builder_tools
    return build_builder_tools(db_factory=_make_mock_db(), owner_phone="+62811xxx")


def test_plan_agent_blocks_meta_builder():
    plan = next(t for t in _tools() if t.name == "plan_agent")
    payload = json.loads(_run(plan.ainvoke({
        "user_goal": "Buat agent yang bisa membuat AI agent lain seperti Arthur",
        "agent_name": "MiniArthur",
    })))
    assert payload["plan_status"] == "blocked_by_policy"
    assert "agent" in payload["validation_errors"][0].lower()


def test_create_agent_blocks_meta_builder():
    db = _make_mock_db()
    from app.core.tools.builder_tools import build_builder_tools
    tools = build_builder_tools(db_factory=db, owner_phone="+62811xxx")
    create = next(t for t in tools if t.name == "create_agent")
    data = json.loads(_run(create.ainvoke({
        "name": "Agent Builder Mini",
        "instructions": "Agent ini bertugas membuat dan mengelola AI agent lain untuk user. " * 3,
    })))
    assert "error" in data
    db.add.assert_not_called()


def test_validate_agent_config_blocks_meta_builder():
    validate = next(t for t in _tools() if t.name == "validate_agent_config")
    data = json.loads(_run(validate.ainvoke({
        "name": "PembuatAgent",
        "instructions": "Agent yang fungsinya membuat agent baru sesuai permintaan user. " * 3,
    })))
    assert data["valid"] is False
