"""Regression tests for the 'lanjut yg pembuatan web' → portfolio hallucination bug.

Root cause (see incident 2026-06-05): the subagent delegation block in the system
prompt contained a fully concrete few-shot example ("Buatkan landing page ... portfolio
sederhana dengan section About dan Projects untuk user bernama Bagas"). When the user
gave an ungrounded instruction ("lanjut yg pembuatan web") with no matching task in the
conversation, the model copied that example verbatim and built a portfolio site instead
of asking what to continue.

These tests pin two fixes:
1. The subagent block must NOT ship a copy-able concrete deliverable example.
2. The block MUST instruct the agent to refuse to invent a task that is not grounded
   in the conversation, and to ask for clarification instead.
"""
from types import SimpleNamespace

from app.core.engine.prompt_builder import build_agent_context_block


def _session():
    return SimpleNamespace(
        id="sess-1",
        agent_id="agent-1",
        channel_config={},
        external_user_id="62895619356936",
        channel_type="whatsapp",
    )


def _agent():
    return SimpleNamespace(
        name="PA Bagas",
        model="openai/gpt-4.1-mini",
        escalation_config={},
        owner_external_id="62895619356936",
        tools_config={},
    )


def _render() -> str:
    return build_agent_context_block(
        agent_model=_agent(),
        session=_session(),
        active_groups=["subagents"],
        custom_tools_db=[],
        subagent_list=[{"name": "sys_coder", "description": "Python sandbox coder"}],
        sender_name="Bagas",
    )


def test_subagent_block_has_no_copyable_portfolio_example():
    block = _render().lower()
    # The exact attractor that got copied during the incident must be gone.
    assert "portfolio" not in block
    assert "portofolio" not in block
    assert "section about dan projects" not in block


def test_subagent_block_requires_grounded_task_before_delegating():
    block = _render()
    lowered = block.lower()
    # Must tell the agent to refuse inventing an ungrounded task and to clarify instead.
    assert "anti-halusinasi task" in lowered
    assert "klarifikasi" in lowered
    # Must explicitly cover the ambiguous "lanjut" / continue case.
    assert "lanjut" in lowered
