"""Exercise real PostgreSQL locking and recovery with deterministic specialist outputs."""
import asyncio
import json
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.schema import CreateTable

from app.models.agent import Agent
from app.models.run import Run
from app.models.session import Session
from app.models.team_chat import TeamChatMember, TeamChatMessage, TeamChatRecovery, TeamChatRoom
from app.models.workforce_task import WorkforceTask, WorkforceTaskStep, WorkforceTaskEvent
from app.core.domain.workforce_service import execute_owner_workforce_task
from app.core.domain.workforce_jobs import run_persisted_task, cancel_task, retry_task, workforce_queue_loop
from app.api.team_chat import _sync_workforce_replies


@pytest_asyncio.fixture
async def database():
    binaries = Path('/usr/lib/postgresql/16/bin')
    if not (binaries / 'initdb').exists():
        pytest.skip('Local PostgreSQL test binaries unavailable')
    directory = Path(tempfile.mkdtemp(prefix='workforce-test-'))
    data = directory / 'data'
    subprocess.run([str(binaries / 'initdb'), '-D', str(data), '-A', 'trust', '--no-locale'],
                   check=True, capture_output=True)
    try:
        subprocess.run([str(binaries / 'pg_ctl'), '-D', str(data), '-l', str(directory / 'server.log'),
                        '-o', f"-k {directory} -h '' -p 55439", '-w', 'start'], check=True, capture_output=True)
        engine = create_async_engine(f'postgresql+asyncpg:///postgres?host={directory}&port=55439', pool_size=15)
        try:
            async with engine.begin() as connection:
                for model in (
                    Agent,
                    Run,
                    Session,
                    TeamChatRoom,
                    TeamChatMember,
                    TeamChatMessage,
                    TeamChatRecovery,
                    WorkforceTask,
                    WorkforceTaskStep,
                    WorkforceTaskEvent,
                ):
                    await connection.execute(CreateTable(model.__table__, include_foreign_key_constraints=[]))
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
    finally:
        subprocess.run([str(binaries / 'pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                       capture_output=True)
        shutil.rmtree(directory)


async def queue(factory, count=2):
    owner = uuid.uuid4()
    async with factory() as db:
        agents = [Agent(name=f'Specialist {i}', owner_user_id=owner) for i in range(count)]
        db.add_all(agents)
        await db.commit()
        result = await execute_owner_workforce_task(
            db=db, owner_user_id=owner, title='Synthetic sales', objective='Sum supplied data.',
            assignments=[{'agent_id': str(a.id), 'title': a.name, 'instructions': 'Calculate 2+3.'} for a in agents],
            worker_agents={str(a.id): a for a in agents}, db_factory=factory, background=True,
        )
        return uuid.UUID(result['task_id'])


@pytest.mark.asyncio
async def test_workforce_group_progress_is_append_only(database):
    owner = uuid.uuid4()
    async with database() as db:
        agent = Agent(name='Web Builder', owner_user_id=owner)
        db.add(agent)
        await db.flush()
        room = TeamChatRoom(workspace_id=str(owner), owner_user_id=owner, kind='group',
                            title='Website', manager_agent_id=agent.id)
        db.add(room)
        await db.flush()
        task = WorkforceTask(workspace_id=str(owner), owner_user_id=owner,
                             title='Draft HTML', description='Create a page', status='in_progress')
        db.add(task)
        await db.flush()
        step = WorkforceTaskStep(task_id=task.id, sequence=0, title='Build page',
                                instructions='Create HTML', assigned_agent_id=agent.id, status='in_progress')
        db.add(step)
        await db.flush()
        placeholder = TeamChatMessage(room_id=room.id, sender_type='agent', sender_agent_id=agent.id,
                                      workforce_step_id=step.id, content='Starting draft', status='working')
        db.add(placeholder)
        db.add(WorkforceTaskEvent(task_id=task.id, step_id=step.id, event_type='team_message',
                                  actor_agent_id=agent.id, metadata_={'message': 'Drafting section one'}))
        await db.commit()
        await _sync_workforce_replies(db, room)
        await _sync_workforce_replies(db, room)
        rows = await db.execute(select(TeamChatMessage).where(
            TeamChatMessage.room_id == room.id).order_by(TeamChatMessage.created_at))
        messages = list(rows.scalars())
        assert [message.content for message in messages] == ['Starting draft', 'Drafting section one']
        step.status, step.output_summary = 'completed', '<html><body>Complete</body></html>'
        await db.commit()
        await _sync_workforce_replies(db, room)
        rows = await db.execute(select(TeamChatMessage).where(
            TeamChatMessage.room_id == room.id).order_by(TeamChatMessage.created_at))
        messages = list(rows.scalars())
        assert [message.content for message in messages] == [
            'Starting draft', 'Drafting section one', '<html><body>Complete</body></html>',
        ]
        assert messages[0].status == 'sent'


@pytest.mark.asyncio
async def test_long_html_worker_result_reaches_next_specialist(database, monkeypatch):
    owner = uuid.uuid4()
    html = '<html><body>' + ('<section>Complete copy</section>' * 500) + '</body></html>'
    received = []

    async def specialist(*, agent, prompt, **_kwargs):
        if agent.name == 'Copywriter':
            return None, html, True
        received.append(prompt)
        return None, 'QA checked the complete draft.', True

    monkeypatch.setattr('app.core.engine.workforce_runner.run_workforce_deep_agent', specialist)
    async with database() as db:
        agents = [Agent(name='Copywriter', owner_user_id=owner), Agent(name='QA Tester', owner_user_id=owner)]
        db.add_all(agents)
        await db.commit()
        result = await execute_owner_workforce_task(
            db=db, owner_user_id=owner, title='Website draft', objective='Produce complete HTML.',
            assignments=[
                {'agent_id': str(agents[0].id), 'title': 'Write HTML', 'instructions': 'Write complete HTML.'},
                {'agent_id': str(agents[1].id), 'title': 'QA HTML', 'instructions': 'Review full HTML.',
                 'depends_on_previous': True},
            ],
            worker_agents={str(agent.id): agent for agent in agents}, db_factory=database,
        )
        assert result['status'] == 'completed'
        assert html in received[0]


@pytest.mark.asyncio
async def test_group_peer_scope_survives_background_queue(database, monkeypatch):
    owner = uuid.uuid4()
    calls = []

    async def specialist(**_kwargs):
        calls.append(True)
        return None, 'Should not run without an eligible reviewer', True

    monkeypatch.setattr('app.core.engine.workforce_runner.run_workforce_deep_agent', specialist)
    async with database() as db:
        worker = Agent(name='Web Builder', owner_user_id=owner)
        unrelated = Agent(name='Penulis Konten', owner_user_id=owner)
        db.add_all([worker, unrelated])
        await db.commit()
        result = await execute_owner_workforce_task(
            db=db, owner_user_id=owner, title='Review website', objective='Review HTML.',
            assignments=[{'agent_id': str(worker.id), 'title': 'Build and review',
                          'instructions': 'Ask for a peer review.', 'requires_peer_help': True}],
            worker_agents={str(worker.id): worker}, db_factory=database, background=True,
            allowed_peer_agent_ids={worker.id},
        )
        task_id = uuid.UUID(result['task_id'])
    await run_persisted_task(database, task_id)
    async with database() as db:
        step = (await db.execute(select(WorkforceTaskStep).where(
            WorkforceTaskStep.task_id == task_id))).scalar_one()
        assert step.status == 'blocked'
    assert not calls


@pytest.mark.asyncio
async def test_queue_parallel_single_claim_and_team_context(database, monkeypatch):
    task_id = await queue(database)
    started = set()
    both = asyncio.Event()

    async def specialist(*, agent, additional_tools, **kwargs):
        started.add(agent.id)
        if len(started) == 2:
            both.set()
        tools = {t.name: t for t in additional_tools}
        await tools['post_team_message'].ainvoke({'message': f'{agent.name}: 2+3=5'})
        await asyncio.wait_for(both.wait(), 5)
        messages = await tools['read_team_messages'].ainvoke({})
        assert messages
        return None, '5', True

    monkeypatch.setattr('app.core.engine.workforce_runner.run_workforce_deep_agent', specialist)
    await asyncio.wait_for(asyncio.gather(run_persisted_task(database, task_id),
                                           run_persisted_task(database, task_id)), 15)
    assert len(started) == 2
    async with database() as db:
        task = await db.get(WorkforceTask, task_id)
        assert task.status == 'completed'
        assert '5' in task.result_summary
        rows = list((await db.execute(select(WorkforceTaskEvent).where(
            WorkforceTaskEvent.task_id == task_id, WorkforceTaskEvent.event_type == 'team_message'
        ))).scalars())
        assert len(rows) == 2


@pytest.mark.asyncio
async def test_recovery_preserves_completed_and_blocks_uncertain_step(database, monkeypatch):
    task_id = await queue(database, 3)
    async with database() as db:
        steps = list((await db.execute(select(WorkforceTaskStep).where(
            WorkforceTaskStep.task_id == task_id).order_by(WorkforceTaskStep.sequence))).scalars())
        steps[0].status, steps[0].output_summary = 'completed', 'previous result'
        steps[1].status = 'in_progress'
        await db.commit()
    calls = []

    async def specialist(**kwargs):
        calls.append(kwargs['agent'].id)
        return None, 'recovered queued work', True

    monkeypatch.setattr('app.core.engine.workforce_runner.run_workforce_deep_agent', specialist)
    await run_persisted_task(database, task_id)
    assert calls == [steps[2].assigned_agent_id]
    async with database() as db:
        task = await db.get(WorkforceTask, task_id)
        assert task.status == 'blocked'
        assert 'previous result' in task.result_summary
        assert 'interrupted' in task.result_summary
        retried = await retry_task(db, task)
        assert retried['ok']
    await run_persisted_task(database, task_id)
    assert calls == [steps[2].assigned_agent_id, steps[1].assigned_agent_id]
    async with database() as db:
        assert (await db.get(WorkforceTask, task_id)).status == 'completed'


@pytest.mark.asyncio
async def test_cancellation_stops_running_specialist(database, monkeypatch):
    task_id = await queue(database, 1)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def specialist(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr('app.core.engine.workforce_runner.run_workforce_deep_agent', specialist)
    running = asyncio.create_task(run_persisted_task(database, task_id))
    await asyncio.wait_for(started.wait(), 5)
    async with database() as db:
        await cancel_task(db, await db.get(WorkforceTask, task_id))
    await asyncio.wait_for(running, 5)
    assert stopped.is_set()
    async with database() as db:
        assert (await db.get(WorkforceTask, task_id)).status == 'cancelled'
        step = (await db.execute(select(WorkforceTaskStep).where(WorkforceTaskStep.task_id == task_id))).scalar_one()
        assert step.status == 'cancelled'


@pytest.mark.asyncio
async def test_fresh_consumer_discovers_committed_work(database, monkeypatch):
    task_id = await queue(database, 1)
    completed = asyncio.Event()

    async def specialist(**kwargs):
        completed.set()
        return None, 'Persisted queue consumed', True

    monkeypatch.setattr('app.core.engine.workforce_runner.run_workforce_deep_agent', specialist)
    consumer = asyncio.create_task(workforce_queue_loop(database))
    try:
        await asyncio.wait_for(completed.wait(), 5)
        for _ in range(30):
            async with database() as db:
                if (await db.get(WorkforceTask, task_id)).status == 'completed':
                    break
            await asyncio.sleep(0.1)
        else:
            pytest.fail('Queue did not persist completion')
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


async def prepare_group_failure(database, count=1, *, blocked=True, recovery_eligible=True):
    task_id = await queue(database, count)
    async with database() as db:
        task = await db.get(WorkforceTask, task_id)
        steps = list((await db.execute(select(WorkforceTaskStep).where(
            WorkforceTaskStep.task_id == task_id,
        ).order_by(WorkforceTaskStep.sequence))).scalars().all())
        manager = Agent(name='Arthur', owner_user_id=task.owner_user_id)
        db.add(manager)
        await db.flush()
        manager_session = Session(agent_id=manager.id, external_user_id=str(task.owner_user_id), metadata_={})
        db.add(manager_session)
        await db.flush()
        room = TeamChatRoom(
            workspace_id=task.workspace_id,
            owner_user_id=task.owner_user_id,
            kind='group',
            title='Recovery test',
            manager_agent_id=manager.id,
        )
        db.add(room)
        await db.flush()
        db.add(TeamChatMember(room_id=room.id, agent_id=manager.id, session_id=manager_session.id))
        for step in steps:
            db.add(TeamChatMember(room_id=room.id, agent_id=step.assigned_agent_id, session_id=None))
            if blocked:
                step.status = 'blocked'
                step.output_summary = f'{step.title}: source not verified'
            db.add(TeamChatMessage(
                room_id=room.id,
                sender_type='agent',
                sender_agent_id=step.assigned_agent_id,
                workforce_step_id=step.id,
                content='',
                status='working',
            ))
        if blocked:
            task.status = 'blocked'
            db.add(WorkforceTaskEvent(
                task_id=task.id,
                event_type='task_blocked',
                metadata_={'manager_recovery_eligible': recovery_eligible},
            ))
        await db.commit()
        return task_id, room.id, manager.id, [step.id for step in steps]


@pytest.mark.asyncio
async def test_failed_group_worker_gets_one_manager_recovery_turn(database, monkeypatch):
    from app.api import team_chat
    from app.core.domain import workforce_recovery

    task_id, room_id, manager_id, _ = await prepare_group_failure(database)

    calls = []

    async def manager_run(**kwargs):
        calls.append(kwargs['user_message'])
        return {'reply': 'Saya akan meminta scope produk yang lebih spesifik sebelum menugaskan riset ulang.',
                'steps': [], 'tokens_used': 0}

    monkeypatch.setattr(team_chat, 'run_agent', manager_run)
    assert await workforce_recovery.recover_group_workforce_failures(database) == 1
    assert await workforce_recovery.recover_group_workforce_failures(database) == 0
    assert len(calls) == 1
    assert 'worker_blocked' in calls[0]

    async with database() as db:
        messages = list((await db.execute(select(TeamChatMessage).where(
            TeamChatMessage.room_id == room_id,
            TeamChatMessage.sender_agent_id == manager_id,
        ))).scalars().all())
        assert len(messages) == 1
        assert messages[0].status == 'sent'
        assert 'scope produk' in messages[0].content
        recoveries = list((await db.execute(select(TeamChatRecovery).where(
            TeamChatRecovery.task_id == task_id,
        ))).scalars().all())
        assert len(recoveries) == 1
        assert recoveries[0].status == 'completed'


@pytest.mark.asyncio
async def test_parallel_consumers_group_two_failures_into_one_arthur_turn(database, monkeypatch):
    from app.api import team_chat
    from app.core.domain import workforce_recovery

    task_id, room_id, manager_id, step_ids = await prepare_group_failure(database, count=2)
    calls = []

    async def manager_run(**kwargs):
        calls.append(kwargs['user_message'])
        await asyncio.sleep(0.1)
        return {'reply': 'Kedua laporan sudah saya terima. Mohon dua tautan produk resmi.',
                'steps': [], 'tokens_used': 0}

    monkeypatch.setattr(team_chat, 'run_agent', manager_run)
    results = await asyncio.gather(
        workforce_recovery.recover_group_workforce_failures(database),
        workforce_recovery.recover_group_workforce_failures(database),
    )
    assert sum(results) == 1
    assert len(calls) == 1
    assert all(str(step_id) not in calls[0] for step_id in step_ids)
    assert 'Specialist 0' in calls[0] and 'Specialist 1' in calls[0]

    async with database() as db:
        recoveries = list((await db.execute(select(TeamChatRecovery).where(
            TeamChatRecovery.task_id == task_id,
        ))).scalars().all())
        assert len(recoveries) == 1
        messages = list((await db.execute(select(TeamChatMessage).where(
            TeamChatMessage.room_id == room_id,
            TeamChatMessage.sender_agent_id == manager_id,
        ))).scalars().all())
        assert len(messages) == 1
        assert messages[0].status == 'sent'


@pytest.mark.asyncio
async def test_distinct_failed_tasks_in_one_group_serialize_manager_turns(database, monkeypatch):
    from app.api import team_chat
    from app.core.domain import workforce_recovery

    first_task_id, room_id, _, first_steps = await prepare_group_failure(database)
    async with database() as db:
        first_task = await db.get(WorkforceTask, first_task_id)
        first_step = await db.get(WorkforceTaskStep, first_steps[0])
        second_task = WorkforceTask(
            workspace_id=first_task.workspace_id,
            owner_user_id=first_task.owner_user_id,
            title='Second blocked assignment',
            description='Verify a different product.',
            status='blocked',
        )
        db.add(second_task)
        await db.flush()
        second_step = WorkforceTaskStep(
            task_id=second_task.id, sequence=0, title='Second specialist',
            assigned_agent_id=first_step.assigned_agent_id,
            status='blocked', output_summary='Second source not verified',
        )
        db.add(second_step)
        await db.flush()
        db.add(TeamChatMessage(
            room_id=room_id, sender_type='agent',
            sender_agent_id=second_step.assigned_agent_id,
            workforce_step_id=second_step.id, content='', status='working',
        ))
        db.add(WorkforceTaskEvent(
            task_id=second_task.id, event_type='task_blocked',
            metadata_={'manager_recovery_eligible': True},
        ))
        await db.commit()

    active = 0
    peak_active = 0

    async def manager_run(**_kwargs):
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.1)
        active -= 1
        return {'reply': 'Saya perlu dua sumber resmi untuk melanjutkan.',
                'steps': [], 'tokens_used': 0}

    monkeypatch.setattr(team_chat, 'run_agent', manager_run)
    results = await asyncio.gather(
        workforce_recovery.recover_group_workforce_failures(database),
        workforce_recovery.recover_group_workforce_failures(database),
    )
    assert sum(results) == 2
    assert peak_active == 1
    async with database() as db:
        recoveries = list((await db.execute(select(TeamChatRecovery).where(
            TeamChatRecovery.room_id == room_id,
        ))).scalars().all())
        assert len(recoveries) == 2
        assert all(recovery.status == 'completed' for recovery in recoveries)


@pytest.mark.asyncio
async def test_interrupted_manager_turn_requires_review_before_replay(database, monkeypatch):
    from app.api import team_chat
    from app.core.domain import workforce_recovery

    task_id, room_id, manager_id, _ = await prepare_group_failure(database)
    await workforce_recovery._enqueue_blocked_group_tasks(database)
    claim = await workforce_recovery._claim_one(database)
    assert claim is not None
    async with database() as db:
        recovery = await db.get(TeamChatRecovery, claim[0])
        recovery.claimed_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await db.commit()

    async def unexpected_manager_run(**_kwargs):
        pytest.fail('An uncertain manager turn must not replay automatically')

    monkeypatch.setattr(team_chat, 'run_agent', unexpected_manager_run)
    assert await workforce_recovery.recover_group_workforce_failures(database) == 0
    async with database() as db:
        recovery = await db.get(TeamChatRecovery, claim[0])
        assert recovery.status == 'needs_review'
        message = await db.get(TeamChatMessage, recovery.manager_message_id)
        assert message.status == 'failed'
        assert 'Status penugasan baru belum pasti' in message.content


@pytest.mark.asyncio
async def test_old_blocked_task_is_not_redispatched_on_upgrade(database):
    from app.core.domain import workforce_recovery

    task_id, _, _, _ = await prepare_group_failure(database, recovery_eligible=False)
    assert await workforce_recovery.recover_group_workforce_failures(database) == 0
    async with database() as db:
        recoveries = list((await db.execute(select(TeamChatRecovery).where(
            TeamChatRecovery.task_id == task_id,
        ))).scalars().all())
        assert recoveries == []


@pytest.mark.asyncio
async def test_recovery_chain_stops_at_bounded_depth(database):
    from app.core.domain import workforce_recovery

    task_id, _, _, _ = await prepare_group_failure(database)
    async with database() as db:
        task = await db.get(WorkforceTask, task_id)
        task.context = {**(task.context or {}), 'manager_recovery_depth': 2}
        await db.commit()
    assert await workforce_recovery.recover_group_workforce_failures(database) == 0
    async with database() as db:
        recovery = (await db.execute(select(TeamChatRecovery).where(
            TeamChatRecovery.task_id == task_id,
        ))).scalar_one()
        assert recovery.status == 'needs_review'
        message = await db.get(TeamChatMessage, recovery.manager_message_id)
        assert 'keputusan Owner' in message.content


@pytest.mark.asyncio
async def test_worker_queue_delivers_failed_assignment_to_arthur(database, monkeypatch):
    from app.api import team_chat

    task_id, room_id, manager_id, _ = await prepare_group_failure(database, blocked=False)
    manager_called = asyncio.Event()

    async def failed_specialist(**_kwargs):
        return None, '[BLOCKED: source verification failed]', False

    async def manager_run(**kwargs):
        assert 'source verification failed' in kwargs['user_message']
        manager_called.set()
        return {'reply': 'Mohon nama produk dan kota target agar riset bisa diulang.',
                'steps': [], 'tokens_used': 0}

    monkeypatch.setattr('app.core.engine.workforce_runner.run_workforce_deep_agent', failed_specialist)
    monkeypatch.setattr(team_chat, 'run_agent', manager_run)
    consumer = asyncio.create_task(workforce_queue_loop(database))
    try:
        await asyncio.wait_for(manager_called.wait(), 12)
        for _ in range(20):
            async with database() as db:
                recoveries = list((await db.execute(select(TeamChatRecovery).where(
                    TeamChatRecovery.task_id == task_id,
                ))).scalars().all())
                if recoveries and recoveries[0].status == 'completed':
                    break
            await asyncio.sleep(0.1)
        else:
            pytest.fail('Manager recovery did not persist completion')
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

    async with database() as db:
        task = await db.get(WorkforceTask, task_id)
        assert task.status == 'blocked'
        replies = list((await db.execute(select(TeamChatMessage).where(
            TeamChatMessage.room_id == room_id,
            TeamChatMessage.sender_agent_id == manager_id,
        ))).scalars().all())
        assert len(replies) == 1
        assert replies[0].status == 'sent'
        assert 'nama produk dan kota' in replies[0].content


@pytest.mark.asyncio
async def test_manager_recovery_dispatches_followup_visible_in_group(database, monkeypatch):
    from app.api import team_chat
    from app.core.domain import workforce_recovery

    task_id, room_id, manager_id, original_steps = await prepare_group_failure(database)
    async with database() as db:
        task = await db.get(WorkforceTask, task_id)
        original = await db.get(WorkforceTaskStep, original_steps[0])
        worker = await db.get(Agent, original.assigned_agent_id)
        owner_id = task.owner_user_id

    async def manager_run(**_kwargs):
        async with database() as dispatch_db:
            result = await execute_owner_workforce_task(
                db=dispatch_db,
                owner_user_id=owner_id,
                title='Verifikasi sumber produk',
                objective='Temukan tautan halaman produk resmi untuk dua item yang diminta.',
                assignments=[{
                    'agent_id': str(worker.id),
                    'title': 'Riset sumber resmi',
                    'instructions': 'Cari dua halaman produk dan laporkan URL spesifik.',
                }],
                worker_agents={str(worker.id): worker},
                db_factory=database,
                background=True,
            )
        assert result['ok']
        return {
            'reply': 'Saya menugaskan ulang Riset Harga untuk sumber resmi.',
            'steps': [{
                'tool': 'orchestrate_owner_workforce_task',
                'result': json.dumps({'ok': True, 'task_id': result['task_id']}),
            }],
            'tokens_used': 0,
        }

    monkeypatch.setattr(team_chat, 'run_agent', manager_run)
    assert await workforce_recovery.recover_group_workforce_failures(database) == 1
    async with database() as db:
        manager_message = (await db.execute(select(TeamChatMessage).where(
            TeamChatMessage.room_id == room_id,
            TeamChatMessage.sender_agent_id == manager_id,
        ))).scalar_one()
        assert manager_message.status == 'sent'
        assert 'Verifikasi sumber produk' in manager_message.content
        followups = list((await db.execute(select(TeamChatMessage).where(
            TeamChatMessage.room_id == room_id,
            TeamChatMessage.sender_agent_id == worker.id,
            TeamChatMessage.workforce_step_id != original.id,
        ))).scalars().all())
        assert len(followups) == 1
        assert followups[0].status == 'working'
        followup_task = await db.get(WorkforceTask, (await db.get(
            WorkforceTaskStep, followups[0].workforce_step_id
        )).task_id)
        assert followup_task.parent_task_id == task_id
        assert followup_task.context['manager_recovery_depth'] == 1
