from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

from app.core.domain import workforce_service
from app.core.domain.workforce_jobs import team_tools
from app.core.engine import workforce_runner
from app.core.engine.workforce_runner import _build_workforce_system_prompt
from app.models.agent import Agent
from app.models.run import Run
from arthur_v2.plugin import WorkforceAssignmentInput


class _WorkforceFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def _get_ls_params(self, stop=None, **kwargs):
        # Exercise the exact workforce harness profile used by the production runner.
        return {"ls_provider": "workforce_internal", "ls_model_name": "fake-workforce"}


class _RecordingDb:
    def __init__(self):
        self.rows = []

    def add(self, row):
        if isinstance(row, (Run,)) or row.__class__.__name__ in {"Session", "WorkforceTask", "WorkforceTaskStep", "WorkforceTaskEvent", "Message"}:
            if getattr(row, "id", None) is None:
                row.id = uuid.uuid4()
        self.rows.append(row)

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def execute(self, _statement):
        return _ScalarResult(None)


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return []


@pytest.mark.asyncio
async def test_parallel_team_tools_and_progress_use_independent_db_sessions():
    class GuardedSession:
        def __init__(self):
            self.busy = False
            self.events = []

        async def execute(self, _statement):
            assert not self.busy, "A single AsyncSession was used concurrently"
            self.busy = True
            await asyncio.sleep(0.01)
            self.busy = False
            return _ScalarResult(None)

        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(status="in_progress")

        def add(self, event):
            self.events.append(event)

        async def commit(self):
            await asyncio.sleep(0)

    class SessionFactory:
        def __init__(self):
            self.sessions = []

        def __call__(self):
            session = GuardedSession()
            self.sessions.append(session)

            class Context:
                async def __aenter__(self):
                    return session

                async def __aexit__(self, *_args):
                    return False

            return Context()

    shared_session = GuardedSession()
    factory = SessionFactory()
    task_id, step_id, agent_id, run_id = (uuid.uuid4() for _ in range(4))
    read, post = team_tools(shared_session, task_id, agent_id, db_factory=factory)
    progress = workforce_service._worker_progress_callback(
        shared_session, task_id, step_id, agent_id, factory
    )
    results = await asyncio.gather(
        read.ainvoke({}), read.ainvoke({}),
        post.ainvoke({"message": "Ready"}),
        progress("worker_progress", run_id, {"phase": "working"}),
    )
    assert results[:2] == [[], []]
    assert results[2] == {"ok": True}
    assert len(factory.sessions) == 4
    assert shared_session.events == []


def test_workforce_system_contract_blocks_unsupported_stock_and_safety_classifications():
    worker = SimpleNamespace(
        instructions="Mark ingredients AMAN when the quantity looks wajar.",
    )
    prompt = _build_workforce_system_prompt(
        agent=worker,
        sandbox_enabled=False,
        has_application_tools=False,
    )

    assert "Mark ingredients AMAN" in prompt
    assert prompt.index("Never invent recipes") > prompt.index("Mark ingredients AMAN")
    assert "minimum stock levels" in prompt
    assert "freshness limits" in prompt
    assert "explicit criterion for that exact classification" in prompt
    assert "status 'data belum cukup'" in prompt
    assert "ask one material question" in prompt
    assert "Separate Owner-provided facts" in prompt


@pytest.mark.asyncio
async def test_dependent_caption_waits_for_verified_research(monkeypatch):
    owner_id = uuid.uuid4()
    researcher = Agent(id=uuid.uuid4(), name="Riset Harga", owner_user_id=owner_id, model="openai/test")
    writer = Agent(id=uuid.uuid4(), name="Penulis Konten", owner_user_id=owner_id, model="openai/test")
    db = _RecordingDb()
    calls = []

    async def owner_roster(*_args, **_kwargs):
        return [researcher, writer]

    async def fake_worker(*, agent, prompt, **_kwargs):
        calls.append((agent.name, prompt))
        return uuid.uuid4(), "Produk 1 Rp99.000 https://tokopedia.com/find/pupuk-organik", True

    monkeypatch.setattr(workforce_service, "list_owner_workforce_agents", owner_roster)
    monkeypatch.setattr(workforce_runner, "run_workforce_deep_agent", fake_worker)
    result = await workforce_service.execute_owner_workforce_task(
        db=db, owner_user_id=owner_id, title="Post IG", objective="Riset harga 3 produk lalu caption",
        assignments=[
            {"agent_id": str(researcher.id), "title": "Riset harga", "instructions": "Cari 3 produk."},
            {"agent_id": str(writer.id), "title": "Caption", "instructions": "Tulis dari riset.",
             "depends_on_previous": True},
        ],
        worker_agents={str(researcher.id): researcher, str(writer.id): writer},
    )
    assert result["status"] == "blocked"
    assert [name for name, _ in calls] == ["Riset Harga"]
    assert result["results"][1]["status"] == "blocked"


@pytest.mark.asyncio
async def test_dependent_caption_receives_research_result(monkeypatch):
    owner_id = uuid.uuid4()
    researcher = Agent(id=uuid.uuid4(), name="Riset Harga", owner_user_id=owner_id, model="openai/test")
    writer = Agent(id=uuid.uuid4(), name="Penulis Konten", owner_user_id=owner_id, model="openai/test")
    db = _RecordingDb()
    research = "\n".join(
        f"Produk {n} | Rp{n}0.000 | https://tokopedia.com/toko/produk-{n}"
        for n in range(1, 4)
    )
    calls = []

    async def owner_roster(*_args, **_kwargs):
        return [researcher, writer]

    async def fake_worker(*, agent, prompt, **_kwargs):
        calls.append((agent.name, prompt))
        return uuid.uuid4(), research if agent.id == researcher.id else "Caption IG: Tiga pilihan pupuk organik.", True

    monkeypatch.setattr(workforce_service, "list_owner_workforce_agents", owner_roster)
    monkeypatch.setattr(workforce_runner, "run_workforce_deep_agent", fake_worker)
    result = await workforce_service.execute_owner_workforce_task(
        db=db, owner_user_id=owner_id, title="Post IG", objective="Riset harga 3 produk lalu caption",
        assignments=[
            {"agent_id": str(researcher.id), "title": "Riset harga", "instructions": "Cari 3 produk."},
            {"agent_id": str(writer.id), "title": "Caption", "instructions": "Tulis dari riset.",
             "depends_on_previous": True},
        ],
        worker_agents={str(researcher.id): researcher, str(writer.id): writer},
    )
    assert result["status"] == "completed"
    assert [name for name, _ in calls] == ["Riset Harga", "Penulis Konten"]
    assert research in calls[1][1]


def test_real_chat_model_accepts_expected_sandbox_filesystem_tools(tmp_path):
    from deepagents import create_deep_agent
    from app.core.engine.agent_llm import _WorkforceChatOpenAI
    from app.core.engine.deep_agent_backend import DockerBackend

    class FakeSandbox:
        session_id = uuid.uuid4()
        workspace_dir = tmp_path
        parent_session_id = None

    @tool
    def read_team_messages() -> str:
        """Read bounded team messages."""
        return ""

    workforce_runner._register_workforce_harness_profile()
    model = _WorkforceChatOpenAI(
        model="test-model", api_key="test", base_url="https://example.invalid/v1"
    )
    model._workforce_harness_profile = "workforce_internal_sandbox"
    graph = create_deep_agent(
        model=model, tools=[read_team_messages], subagents=[],
        backend=DockerBackend(FakeSandbox()), system_prompt="Internal task",
    )
    workforce_runner._assert_no_model_callable_tools(
        graph, model, allowed_tool_names=frozenset({"read_team_messages"}),
        sandbox_backend_enabled=True,
    )


@pytest.mark.asyncio
async def test_workforce_graph_exposes_and_executes_only_bounded_peer_tool(monkeypatch):
    peer_id = uuid.uuid4()
    calls = []
    persisted_progress = []

    async def persist_progress(event_type, run_id, metadata):
        run_record = next(row for row in db.rows if isinstance(row, Run) and row.id == run_id)
        persisted_progress.append((event_type, run_id, metadata, run_record.status))

    async def request_peer_help(peer_agent_id: str, question: str) -> str:
        calls.append((peer_agent_id, question))
        return "Verified synthetic revenue is 112000."

    peer_tool = tool("request_peer_help", description="Ask one same-owner task-scoped peer.")(request_peer_help)
    fake_model = _WorkforceFakeModel(responses=[
        AIMessage(content="", tool_calls=[{
            "name": "request_peer_help",
            "args": {"peer_agent_id": str(peer_id), "question": "Recompute synthetic revenue."},
            "id": "peer-call-1",
        }]),
        AIMessage(content="Peer cross-check: synthetic revenue is 112000."),
    ])
    monkeypatch.setattr(workforce_runner, "get_settings", lambda: SimpleNamespace(workforce_llm_max_tokens=1800))
    monkeypatch.setattr(workforce_runner, "build_agent_llms", lambda *args, **kwargs: (fake_model, fake_model))
    db = _RecordingDb()
    worker = SimpleNamespace(
        id=uuid.uuid4(), model="openai/test", instructions="Internal worker", temperature=0.1,
        max_tokens=None, tools_config={}, name="Sales Analyst",
    )

    run_id, output, ok = await workforce_runner.run_workforce_deep_agent(
        agent=worker,
        workspace_id=str(uuid.uuid4()),
        prompt="Call request_peer_help before finalizing; the allowed peer is listed in this test.",
        role="specialist",
        task_id=uuid.uuid4(),
        db=db,
        additional_tools=[peer_tool],
        progress_callback=persist_progress,
    )

    run_record = next(row for row in db.rows if isinstance(row, Run))
    assert ok is True, run_record.error_message
    assert run_id
    assert "112000" in output
    assert calls == [(str(peer_id), "Recompute synthetic revenue.")]
    assert run_record.runtime_metadata["tools_exposed"] == ["request_peer_help"]
    assert run_record.runtime_metadata["max_completion_tokens"] == 1800
    event_types = [event[0] for event in persisted_progress]
    assert event_types[0] == "worker_started"
    assert "worker_progress" in event_types
    assert event_types[-1] == "worker_completed"
    assert event_types.index("worker_started") < event_types.index("worker_progress") < event_types.index("worker_completed")
    assert all(event[1] == run_id for event in persisted_progress)
    assert all(event[2]["role"] == "specialist" for event in persisted_progress)
    assert all(event[3] == "running" for event in persisted_progress[:-1])
    assert persisted_progress[-1][3] == "completed"


@pytest.mark.asyncio
async def test_workforce_specialist_exposes_personal_web_search_when_enabled(monkeypatch):
    @tool("tavily_search", description="Search public web sources.")
    async def search(query: str) -> str:
        return "https://tokopedia.com/toko/produk-1"

    fake_model = _WorkforceFakeModel(responses=[
        AIMessage(content="", tool_calls=[{
            "name": "tavily_search", "args": {"query": "product prices"}, "id": "search-1",
        }]),
        AIMessage(content="Produk 1 Rp10.000 https://tokopedia.com/toko/produk-1"),
    ])
    monkeypatch.setattr(workforce_runner, "get_settings", lambda: SimpleNamespace(
        workforce_llm_max_tokens=1800, tavily_api_key="test-key",
    ))
    monkeypatch.setattr(workforce_runner, "build_agent_llms", lambda *args, **kwargs: (fake_model, fake_model))
    monkeypatch.setattr("app.core.engine.tool_builder.build_tavily_tools", lambda _config: [search])
    db = _RecordingDb()
    worker = SimpleNamespace(
        id=uuid.uuid4(), model="openai/test", instructions="Research public prices", temperature=0.1,
        max_tokens=None, tools_config={}, name="Riset Harga",
    )
    _run_id, _output, ok = await workforce_runner.run_workforce_deep_agent(
        agent=worker, workspace_id=str(uuid.uuid4()), prompt="Find price for 1 produk.", role="specialist",
        task_id=uuid.uuid4(), db=db,
    )
    run = next(row for row in db.rows if isinstance(row, Run))
    assert ok is True, run.error_message
    assert "tavily_search" in run.runtime_metadata["tools_exposed"]


@pytest.mark.asyncio
async def test_workforce_research_cannot_complete_with_unseen_product_link(monkeypatch):
    fake_model = _WorkforceFakeModel(responses=[
        AIMessage(content="Produk 1 Rp10.000 https://tokopedia.com/toko/produk-1"),
    ])
    monkeypatch.setattr(workforce_runner, "get_settings", lambda: SimpleNamespace(
        workforce_llm_max_tokens=1800, tavily_api_key=None,
    ))
    monkeypatch.setattr(workforce_runner, "build_agent_llms", lambda *args, **kwargs: (fake_model, fake_model))
    db = _RecordingDb()
    worker = SimpleNamespace(
        id=uuid.uuid4(), model="openai/test", instructions="Research public prices", temperature=0.1,
        max_tokens=None, tools_config={}, name="Riset Harga",
    )
    _run_id, _output, ok = await workforce_runner.run_workforce_deep_agent(
        agent=worker, workspace_id=str(uuid.uuid4()), prompt="Find price for 1 produk.", role="specialist",
        task_id=uuid.uuid4(), db=db,
    )
    run = next(row for row in db.rows if isinstance(row, Run))
    assert ok is False
    assert run.runtime_metadata["failure_code"] == "unverified_product_sources"


@pytest.mark.asyncio
async def test_deploy_specialist_compiles_with_sandbox_and_deployment_tools(monkeypatch, tmp_path):
    fake_model = _WorkforceFakeModel(responses=[AIMessage(content="Deployment task prepared.")])

    class FakeSandbox:
        def __init__(self, session_id):
            self.session_id = str(session_id)
            self.workspace_dir = tmp_path

        def _get_client(self):
            return SimpleNamespace(ping=lambda: True)

        async def aclose(self):
            return None

    monkeypatch.setattr(workforce_runner, "get_settings", lambda: SimpleNamespace(
        workforce_llm_max_tokens=1800, sandbox_subagents_enabled=True,
        docker_sandbox_image="python:3.12-slim",
    ))
    monkeypatch.setattr(workforce_runner, "build_agent_llms", lambda *args, **kwargs: (fake_model, fake_model))
    monkeypatch.setattr("app.core.infra.sandbox.DockerSandbox", FakeSandbox)
    db = _RecordingDb()
    worker = SimpleNamespace(
        id=uuid.uuid4(), model="openai/test", instructions="Internal deploy worker", temperature=0.1,
        max_tokens=None, tools_config={}, name="Website Builder",
    )

    _run_id, output, ok = await workforce_runner.run_workforce_deep_agent(
        agent=worker, workspace_id=str(uuid.uuid4()), prompt="Build and deploy the approved page.",
        role="specialist", task_id=uuid.uuid4(), db=db,
        assignment_text="Coding & Deploy\nBuild and deploy the approved page.",
        requires_deployment=True,
    )

    run = next(row for row in db.rows if isinstance(row, Run))
    assert ok is True, run.error_message
    assert output == "Deployment task prepared."
    assert run.runtime_metadata["workforce_sandbox_enabled"] is True
    assert run.runtime_metadata["workforce_deploy_enabled"] is True
    assert run.runtime_metadata["max_completion_tokens"] == 8192
    assert {"deploy_app", "get_deployment_status", "get_deployment_logs", "stop_deployment"}.issubset(
        run.runtime_metadata["tools_exposed"]
    )


@pytest.mark.asyncio
async def test_explicit_peer_requirement_blocks_parent_step_if_worker_does_not_call_tool(monkeypatch):
    owner_id, worker_id, helper_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    worker = Agent(id=worker_id, name="Sales Analyst", owner_user_id=owner_id, model="openai/test")
    helper = Agent(id=helper_id, name="Finance Checker", owner_user_id=owner_id, model="openai/test")
    db = _RecordingDb()

    async def owner_roster(*_args, **_kwargs):
        return [worker, helper]

    async def omit_peer_call(**_kwargs):
        return uuid.uuid4(), "I will ask the peer, but this output contains no tool result.", True

    monkeypatch.setattr(workforce_service, "list_owner_workforce_agents", owner_roster)
    monkeypatch.setattr(workforce_runner, "run_workforce_deep_agent", omit_peer_call)

    result = await workforce_service.execute_owner_workforce_task(
        db=db,
        owner_user_id=owner_id,
        title="Synthetic dimsum verification",
        objective="Check the provided sample arithmetic.",
        assignments=[{
            "agent_id": str(worker_id),
            "title": "Revenue calculation",
            "instructions": "Calculate the revenue and compare with a peer.",
            "requires_peer_help": True,
        }],
        worker_agents={str(worker_id): worker},
    )

    assert result["ok"] is False
    assert result["status"] == "blocked"
    assert result["results"][0]["status"] == "blocked"
    assert "not completed" in result["results"][0]["summary"]
    task = next(row for row in db.rows if row.__class__.__name__ == "WorkforceTask")
    step = next(row for row in db.rows if row.__class__.__name__ == "WorkforceTaskStep")
    assert task.status == "blocked"
    assert step.status == "blocked"
    assert step.input_context["requires_peer_help"] is True


@pytest.mark.asyncio
async def test_failed_worker_progress_is_persisted_on_owning_task_and_blocks_summary(monkeypatch):
    owner_id, worker_id = uuid.uuid4(), uuid.uuid4()
    worker = Agent(id=worker_id, name="Dimsum Analyst", owner_user_id=owner_id, model="openai/test")
    db = _RecordingDb()

    async def owner_roster(*_args, **_kwargs):
        return [worker]

    async def fail_with_real_boundaries(*, progress_callback, **_kwargs):
        run_id = uuid.uuid4()
        await progress_callback("worker_started", run_id, {"role": "specialist", "phase": "execution_started"})
        await progress_callback("worker_progress", run_id, {"role": "specialist", "phase": "using_internal_tools"})
        await progress_callback("worker_failed", run_id, {"role": "specialist", "phase": "execution_failed", "failure_type": "RuntimeError"})
        return run_id, "Internal agent run failed; platform logs contain the failure category.", False

    monkeypatch.setattr(workforce_service, "list_owner_workforce_agents", owner_roster)
    monkeypatch.setattr(workforce_runner, "run_workforce_deep_agent", fail_with_real_boundaries)

    result = await workforce_service.execute_owner_workforce_task(
        db=db,
        owner_user_id=owner_id,
        title="Review synthetic dimsum orders",
        objective="Check the owner-provided sample only.",
        assignments=[{
            "agent_id": str(worker_id),
            "title": "Check order totals",
            "instructions": "Calculate the supplied order total.",
        }],
        worker_agents={str(worker_id): worker},
    )

    assert result["status"] == "blocked"
    task = next(row for row in db.rows if row.__class__.__name__ == "WorkforceTask")
    events = [row for row in db.rows if row.__class__.__name__ == "WorkforceTaskEvent"]
    progress = [row for row in events if row.event_type.startswith("worker_")]
    assert [row.event_type for row in progress] == [
        "worker_started", "worker_progress", "worker_failed"
    ]
    assert all(row.task_id == task.id for row in progress)
    assert all(row.actor_agent_id == worker_id for row in progress)
    assert all(row.step_id is not None and row.run_id is not None for row in progress)
    assert task.owner_user_id == owner_id


def test_arthur_assignment_schema_has_typed_peer_requirement():
    assignment = WorkforceAssignmentInput.model_validate({
        "agent_id": str(uuid.uuid4()),
        "title": "Cross-check",
        "instructions": "Recompute total revenue.",
        "requires_peer_help": True,
    })
    assert assignment.requires_peer_help is True
    assert WorkforceAssignmentInput(agent_id=str(uuid.uuid4()), title="Task", instructions="Work").requires_peer_help is False


def test_workforce_completion_tokens_are_configurable_and_hard_bounded():
    agent = SimpleNamespace(max_tokens=None)
    assert workforce_runner._workforce_max_completion_tokens(SimpleNamespace(workforce_llm_max_tokens=1800), agent) == 1800
    assert workforce_runner._workforce_max_completion_tokens(SimpleNamespace(workforce_llm_max_tokens=9000), agent) == 3072
    assert workforce_runner._workforce_max_completion_tokens(SimpleNamespace(workforce_llm_max_tokens=2048), SimpleNamespace(max_tokens=768)) == 768


@pytest.mark.asyncio
async def test_only_deploy_specialist_receives_owner_deployment_authorization(monkeypatch):
    owner_id = uuid.uuid4()
    prd = Agent(id=uuid.uuid4(), name="PRD", owner_user_id=owner_id, model="openai/test")
    builder = Agent(id=uuid.uuid4(), name="Builder", owner_user_id=owner_id, model="openai/test")
    qa = Agent(id=uuid.uuid4(), name="QA", owner_user_id=owner_id, model="openai/test")
    db = _RecordingDb()
    received = []

    async def owner_roster(*_args, **_kwargs):
        return [prd, builder, qa]

    async def fake_worker(*, agent, requires_deployment, **_kwargs):
        received.append((agent.name, requires_deployment))
        return uuid.uuid4(), "done", True

    monkeypatch.setattr(workforce_service, "list_owner_workforce_agents", owner_roster)
    monkeypatch.setattr(workforce_runner, "run_workforce_deep_agent", fake_worker)
    result = await workforce_service.execute_owner_workforce_task(
        db=db, owner_user_id=owner_id, title="Tokyo8", objective="Create and publish a landing page.",
        assignments=[
            {"agent_id": str(prd.id), "title": "Bikin PRD", "instructions": "Write PRD for page that will be deployed."},
            {"agent_id": str(builder.id), "title": "Coding & Deploy", "instructions": "Build and deploy the landing page.", "requires_deployment": True},
            {"agent_id": str(qa.id), "title": "QA", "instructions": "Test after deployment."},
        ],
        worker_agents={str(agent.id): agent for agent in [prd, builder, qa]},
    )
    assert result["status"] == "completed"
    assert [name for name, requires_deployment in received
            if requires_deployment] == ["Builder"]
