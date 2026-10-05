from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

from arthur_v2 import build_arthur_v2_graph, build_arthur_v2_tools
from arthur_v2.plugin import (
    AssistantWorkflowInput,
    arthur_workforce_dispatch_completion_needed,
    build_arthur_v2_system_prompt,
    guard_arthur_workforce_reply,
)
from arthur.runtime.skill_runtime import scope_arthur_builder_tools
from app.core.domain import workforce_service
from app.core.engine.prompt_builder import build_system_prompt
from app.core.engine import workforce_runner


class ToolCallingFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class AsyncContext:
    async def __aenter__(self):
        self.db = RecordingDb()
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class ScalarResult:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return []


class RecordingDb:
    def __init__(self):
        self.rows = []
        RecordingDb.instances.append(self)

    def add(self, row):
        if getattr(row, "id", None) is None:
            row.id = uuid.uuid4()
        self.rows.append(row)

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def refresh(self, _row):
        return None

    async def execute(self, statement):
        statement_text = str(statement)
        if "workforce_task_steps.sequence" in statement_text:
            sequences = [
                row.sequence for row in self.rows
                if row.__class__.__name__ == "WorkforceTaskStep"
            ]
            return ScalarResult(max(sequences) if sequences else None)
        if "workforce_task_steps.id" in statement_text:
            return ScalarResult([row for row in self.rows if row.__class__.__name__ == "WorkforceTaskStep"])
        return ScalarResult()


RecordingDb.instances = []


class UserLookupResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class AuthenticatedIdentityDb:
    def __init__(self, user):
        self.user = user

    async def execute(self, _statement):
        return UserLookupResult([self.user])


def test_canonical_arthur_prompt_has_business_manager_onboarding_and_execution_contract():
    prompt = build_arthur_v2_system_prompt()
    assert "AI business manager and orchestration partner" in prompt
    assert "Never make the owner start by specifying an" in prompt
    assert "what they sell or do" in prompt and "goal or current bottleneck" in prompt
    assert "Only\nbundle questions for rapid intake" in prompt
    assert "suggest the most relevant operating areas or assistant" in prompt
    assert "as hypotheses (not facts)" in prompt
    assert "Ask one material,\nnatural question per turn by default" in prompt
    assert "inspect the target assistant's actual runtime configuration" in prompt
    assert "separate current facts the owner\nprovided, assumptions (clearly labeled), unknowns" in prompt
    assert "Never invent time savings, ROI, revenue impact" in prompt
    assert "Do not create an assistant, change its configuration" in prompt
    assert "Wait for the owner to explicitly approve that proposal" in prompt
    assert "Never carry business context across owners or workspaces" in prompt
    assert "invent revenue, orders, customer details" in prompt

    # Ongoing manager work must still inspect the tenant roster and dispatch
    # relevant specialists through the durable task control plane.
    assert "list_owner_workforce_roster" in prompt
    assert "orchestrate_owner_workforce_task" in prompt
    assert "then select only relevant specialists and call" in prompt
    assert "returned `ok=true` with a task_id" in prompt


def test_arthur_progress_claim_requires_persisted_dispatch_result():
    claim = "Saya sudah menugaskan PRD Maker, Developer, dan QA untuk mulai bekerja."
    assert not arthur_workforce_dispatch_completion_needed(claim, [])
    failed_steps = [{"tool": "orchestrate_owner_workforce_task", "result": json.dumps({"ok": False, "error": "unavailable"})}]
    assert arthur_workforce_dispatch_completion_needed(claim, failed_steps)
    guarded, reason = guard_arthur_workforce_reply(claim, failed_steps)
    assert reason == "missing_workforce_dispatch"
    assert "belum menugaskan" in guarded

    successful_steps = [{
        "tool": "orchestrate_owner_workforce_task",
        "result": json.dumps({"ok": True, "task_id": str(uuid.uuid4()), "status": "in_progress"}),
    }]
    assert not arthur_workforce_dispatch_completion_needed(claim, failed_steps + successful_steps)
    assert guard_arthur_workforce_reply(claim, failed_steps + successful_steps) == (claim, None)


@pytest.mark.asyncio
async def test_deploy_dispatch_uses_structured_assignment_without_quote(monkeypatch):
    owner_id = uuid.uuid4()
    worker = SimpleNamespace(
        id=uuid.uuid4(), name="Builder", description="", capabilities=[],
        tools_config={},
    )
    calls = []

    async def roster(*_args, **_kwargs):
        return [worker]

    async def dispatch(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "task_id": str(uuid.uuid4()), "status": "in_progress"}

    monkeypatch.setattr("arthur_v2.plugin.list_owner_workforce_agents", roster)
    monkeypatch.setattr("arthur_v2.plugin.execute_owner_workforce_task", dispatch)
    tools = {item.name: item for item in build_arthur_v2_tools(
        db_factory=AsyncContext, owner_phone="628123", owner_user_id=owner_id,
        self_agent_id="arthur-v2", session_id=str(uuid.uuid4()),
    )}
    base = {"title": "Tokyo8", "objective": "Build page", "assignments": [{
        "agent_id": str(worker.id), "title": "Builder", "instructions": "Build and publish",
        "requires_deployment": True,
    }]}
    accepted = await tools["orchestrate_owner_workforce_task"].ainvoke(base)
    assert accepted["ok"] is True
    assert len(calls) == 1
    assert calls[0]["assignments"][0]["requires_deployment"] is True


def test_arthur_build_contract_requires_complete_first_call_and_grounded_workflow_fields():
    prompt = build_arthur_v2_system_prompt()
    assert "make the first create_assistant" in prompt
    assert "Do not make a partial call" in prompt
    assert "facts the Owner supplied or approved" in prompt
    assert "Do not invent stock levels, recipes, ingredient usage" in prompt
    assert 'report "data belum cukup"' in prompt
    assert "ask one focused question" in prompt

    tools = {
        tool.name: tool
        for tool in build_arthur_v2_tools(
            db_factory=object(), owner_phone="628123", self_agent_id="arthur-v2"
        )
    }
    schema = tools["create_assistant"].args_schema
    workflow_field = schema.model_fields["workflow"]
    assert "lengkap pada pemanggilan create pertama" in workflow_field.description
    assert "jangan kirim percobaan parsial" in workflow_field.description
    assert "never invent thresholds" in tools["create_assistant"].description
    assert "data belum diberikan" in AssistantWorkflowInput.model_fields[
        "knowledge_sources"
    ].description
    assert "AMAN/PERLU CEK" in AssistantWorkflowInput.model_fields[
        "exceptions_handoff"
    ].description


def _build_agent_runtime_prompt(*, plugin: str | None) -> str:
    tools_config = {"builder": True}
    if plugin:
        tools_config["system_plugin"] = plugin
    agent = SimpleNamespace(
        name="Arthur",
        model="deepseek/test",
        instructions=build_arthur_v2_system_prompt(),
        tools_config=tools_config,
        safety_policy=None,
        operator_ids=[],
        escalation_config={},
    )
    session = SimpleNamespace(
        id="test-session",
        agent_id="test-arthur",
        channel_type="api",
        channel_config={},
        external_user_id=None,
    )
    return build_system_prompt(
        agent_model=agent,
        session=session,
        active_groups=["builder"],
        saved_custom_tools=[],
        subagent_list=[],
        sender_name=None,
        context_summary="",
        memory_block="",
        layered_memory=None,
        rag_context="",
        escalation_user_jid=None,
        escalation_context=None,
        is_operator_message=False,
        user_message="Halo siapa kamu?",
    )


def test_runtime_prompt_keeps_manager_onboarding_and_omits_legacy_greeting_menu_for_arthur_v2():
    v2_prompt = _build_agent_runtime_prompt(plugin="arthur_v2")
    assert "## Arthur V2 Owner Manager Mode" in v2_prompt
    assert "answer in about two short sentences" in v2_prompt
    assert "ask exactly one open question about what they want to accomplish first" in v2_prompt
    assert "coordinate the right specialists behind the scenes" in v2_prompt
    assert "Do not list capabilities" in v2_prompt
    assert "ask one material question per turn by default" in v2_prompt
    assert "inspect the target assistant's actual runtime configuration and tools" in v2_prompt
    assert "Past conversation, agent description, or proposed setup is not proof" in v2_prompt
    assert "Never invent time savings, ROI, revenue impact" in v2_prompt
    assert "Default to analysis, internal coordination, and reviewable drafts" in v2_prompt
    assert "distinguish Owner-provided facts, clearly labeled assumptions, unknowns" in v2_prompt
    assert "Mau buat agent baru, atau tanya-tanya dulu?" not in v2_prompt
    assert "## Arthur Builder Mode" not in v2_prompt
    assert "tanyakan apa yang ingin user buat atau kelola" not in v2_prompt


def test_legacy_builder_prompt_keeps_legacy_greeting_contract():
    legacy_prompt = _build_agent_runtime_prompt(plugin=None)
    assert "## Arthur Builder Mode" in legacy_prompt
    assert "tanyakan apa yang ingin user buat atau kelola" in legacy_prompt


@pytest.mark.asyncio
async def test_create_assistant_persists_resolved_owner_user_uuid(monkeypatch):
    from app.core.domain.tenant_identity import resolve_unique_user_id

    RecordingDb.instances.clear()
    owner_id = uuid.uuid4()
    external_id = "owner-session-reference"
    user = SimpleNamespace(id=owner_id, external_id=external_id)
    resolved_owner_id = await resolve_unique_user_id(AuthenticatedIdentityDb(user), external_id)
    assert resolved_owner_id == owner_id

    async def subscription_snapshot(_external_ids, _db):
        return (
            user,
            SimpleNamespace(
                status="active", is_usable=True, tokens_remaining=1000, expires_at=None
            ),
            SimpleNamespace(max_agents=8, code="test", label="Test"),
        )

    monkeypatch.setattr(
        "app.core.domain.subscription_service.get_best_subscription_by_external_ids",
        subscription_snapshot,
    )
    tools = {
        tool.name: tool
        for tool in build_arthur_v2_tools(
            db_factory=AsyncContext,
            owner_phone="628123456789",
            owner_user_id=resolved_owner_id,
            self_agent_id="arthur-v2",
        )
    }
    result = await tools["create_assistant"].ainvoke({
        "name": "Disposable owner UUID test",
        "purpose": "Verify tenant identity persistence.",
        "instructions": "Internal test only.",
        "confirmed": True,
    })

    assert result["ok"] is True
    agents = [
        row for db in RecordingDb.instances for row in db.rows
        if row.__class__.__name__ == "Agent"
    ]
    assert len(agents) == 1
    assert agents[0].owner_user_id == owner_id
    assert agents[0].owner_external_id == "628123456789"


@pytest.mark.asyncio
async def test_arthur_graph_reads_roster_then_dispatches_durable_task(monkeypatch):
    RecordingDb.instances.clear()
    owner_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    helper_id = uuid.uuid4()
    worker = SimpleNamespace(
        id=worker_id,
        name="Sales Analyst",
        description="Analyze owner-provided sales data",
        capabilities=["sales"],
        owner_user_id=owner_id,
        model="openai/test",
        instructions="Sales analyst",
        temperature=0.1,
        max_tokens=None,
        tools_config={},
    )
    helper = SimpleNamespace(
        id=helper_id,
        name="Finance Checker",
        description="Check arithmetic",
        capabilities=["finance"],
        owner_user_id=owner_id,
        model="openai/test",
        instructions="Finance checker",
        temperature=0.1,
        max_tokens=None,
        tools_config={},
    )
    roster_agents = [worker, helper]
    worker_calls = []

    async def roster(db, authenticated_owner_id, *, exclude_agent_id=None):
        assert authenticated_owner_id == owner_id
        return roster_agents

    async def run_specialist(*, agent, role, additional_tools=(), **kwargs):
        worker_calls.append((str(agent.id), role))
        if role == "peer_helper":
            return uuid.uuid4(), "Synthetic revenue verified at Rp112000.", True
        assert role == "specialist"
        assert len(additional_tools) == 1
        peer_result = await additional_tools[0].ainvoke({
            "peer_agent_id": str(helper_id),
            "question": "Independently verify the synthetic sales total.",
        })
        return uuid.uuid4(), f"Revenue calculation cross-checked: {peer_result}", True

    monkeypatch.setattr("arthur_v2.plugin.list_owner_workforce_agents", roster)
    monkeypatch.setattr(workforce_service, "list_owner_workforce_agents", roster)
    monkeypatch.setattr(workforce_runner, "run_workforce_deep_agent", run_specialist)
    tools = build_arthur_v2_tools(
        db_factory=AsyncContext,
        owner_phone="628123",
        owner_user_id=owner_id,
        self_agent_id="arthur-v2",
    )
    # Mirror run_agent's production builder path: progressive skill filtering,
    # followed by create_react_agent (not arthur_v2.runtime's Deep Agent graph).
    scoped_tools, _removed = scope_arthur_builder_tools(
        tools,
        primary_skill="arthur-discovery",
        mixin_skills=[],
    )
    assert {"list_owner_workforce_tasks", "manage_owner_workforce_task"}.issubset(
        {item.name for item in scoped_tools}
    )
    model = ToolCallingFakeModel(responses=[
        AIMessage(content="", tool_calls=[{
            "name": "list_managed_assistants", "args": {},
            "id": "assistants_1", "type": "tool_call",
        }]),
        AIMessage(content="", tool_calls=[{
            "name": "list_owner_workforce_roster", "args": {},
            "id": "roster_1", "type": "tool_call",
        }]),
        AIMessage(content="", tool_calls=[{
            "name": "orchestrate_owner_workforce_task",
            "args": {
                "title": "Monthly sales check",
                "objective": "Calculate revenue from owner-supplied orders.",
                "assignments": [{
                    "agent_id": str(worker_id),
                    "title": "Calculate sales total",
                    "instructions": "Calculate the supplied order total.",
                    "requires_peer_help": True,
                }],
            },
            "id": "dispatch_1", "type": "tool_call",
        }]),
        AIMessage(content="Task selesai dan hasil tersimpan."),
    ])

    result = await create_react_agent(
        model,
        tools=scoped_tools,
        prompt=build_arthur_v2_system_prompt(),
    ).ainvoke({
        "messages": [{"role": "user", "content": "Jalankan hitung sales dan minta peer cek."}]
    })
    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert [message.name for message in tool_messages] == [
        "list_managed_assistants",
        "list_owner_workforce_roster",
        "orchestrate_owner_workforce_task",
    ]
    assert json.loads(tool_messages[0].content) == {"assistants": []}
    roster_output = json.loads(tool_messages[1].content)
    assert roster_output["ok"] is True
    assert [item["id"] for item in roster_output["items"]] == [str(worker_id), str(helper_id)]
    dispatch_output = json.loads(tool_messages[2].content)
    assert dispatch_output["ok"] is True
    assert dispatch_output["status"] == "in_progress"
    assert dispatch_output["task_id"]
    assert worker_calls == []  # The database worker executes after the chat turn returns.
    persisted_rows = [row for db in RecordingDb.instances for row in db.rows]
    task = next(row for row in persisted_rows if row.__class__.__name__ == "WorkforceTask")
    steps = [row for row in persisted_rows if row.__class__.__name__ == "WorkforceTaskStep"]
    assert task.owner_user_id == owner_id
    root = next(step for step in steps if step.parent_step_id is None)
    assert root.assigned_agent_id == worker_id and root.status == "queued"
    assert task.context["durable_dispatch"] is True
    events = [row.event_type for row in persisted_rows if row.__class__.__name__ == "WorkforceTaskEvent"]
    assert "handoff_queued" in events


@pytest.mark.asyncio
async def test_planning_without_dispatch_creates_no_task_and_has_no_success_tool_result(monkeypatch):
    owner_id = uuid.uuid4()
    worker = SimpleNamespace(
        id=uuid.uuid4(), name="Sales Analyst", description="Sales", capabilities=["sales"], owner_user_id=owner_id
    )
    dispatches = []

    async def roster(db, authenticated_owner_id, *, exclude_agent_id=None):
        return [worker]

    async def execute(**kwargs):
        dispatches.append(kwargs)
        return {"ok": True, "task_id": str(uuid.uuid4()), "status": "completed", "results": []}

    monkeypatch.setattr("arthur_v2.plugin.list_owner_workforce_agents", roster)
    monkeypatch.setattr("arthur_v2.plugin.execute_owner_workforce_task", execute)
    tools = build_arthur_v2_tools(
        db_factory=AsyncContext,
        owner_phone="628123",
        owner_user_id=owner_id,
        self_agent_id="arthur-v2",
    )
    model = ToolCallingFakeModel(responses=[
        AIMessage(content="", tool_calls=[{
            "name": "list_owner_workforce_roster", "args": {},
            "id": "roster_1", "type": "tool_call",
        }]),
        AIMessage(content="Saya akan menjalankan task sekarang."),
    ])

    result = await build_arthur_v2_graph(model=model, tools=tools).ainvoke({
        "messages": [{"role": "user", "content": "Jalankan laporan sekarang."}]
    })
    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert [message.name for message in tool_messages] == ["list_owner_workforce_roster"]
    assert dispatches == []
    # Without this tool result there is no durable task completion to report.
    assert not any("task_id" in str(message.content) for message in tool_messages)
