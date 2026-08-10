"""Preset detection must survive spaceless input.

Tim QA menemukan agent hasil Arthur tidak konsisten dapat sandbox saat user
mengetik goal tanpa spasi sama sekali (mis. "buatagentcodingdeploywebsite").
Penyebab: `has_keyword` memakai word-boundary `\\b`, sehingga keyword yang
tertanam di dalam blob tanpa spasi tidak pernah cocok → preset jatuh ke
fallback non-sandbox (`cs_whatsapp_basic` / `faq_webchat_rag`).
"""
import pytest

from app.core.tools.builder_catalog import AGENT_PRESETS
from app.core.tools.builder_intent import _detect_preset


SPACELESS_CODING = [
    "buatagentcodingyangbisadeploywebsite",
    "bikinwebsitelandingpage",
    "agentcodingdeploywebsite",
    "tolongbuatkanaplikasiwebdenganpython",
]


@pytest.mark.parametrize("goal", SPACELESS_CODING)
def test_spaceless_coding_goal_selects_sandbox_preset(goal):
    preset = _detect_preset(goal.lower(), [], "whatsapp")
    assert preset == "coding_deploy_agent"
    assert AGENT_PRESETS[preset]["tools_config"]["sandbox"] is True


def test_normal_spaced_coding_still_works():
    assert (
        _detect_preset("buat agent coding yang bisa deploy website", [], "whatsapp")
        == "coding_deploy_agent"
    )


def test_spaceless_non_coding_not_misrouted_to_coding():
    # Blob CS tanpa spasi tidak boleh salah jadi coding.
    preset = _detect_preset("agentcustomerservicetokokue", [], "whatsapp")
    assert preset != "coding_deploy_agent"


def test_spaced_input_precision_preserved():
    # "web" di dalam "webhook" tidak boleh memicu coding pada input normal.
    preset = _detect_preset("agent untuk menerima webhook pembayaran", [], "whatsapp")
    assert preset != "coding_deploy_agent"
