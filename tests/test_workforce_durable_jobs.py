"""Exercise real PostgreSQL locking and recovery with deterministic specialist outputs."""
import asyncio
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.schema import CreateTable

from app.models.agent import Agent
from app.models.workforce_task import WorkforceTask, WorkforceTaskStep, WorkforceTaskEvent
from app.core.domain.workforce_service import execute_owner_workforce_task
from app.core.domain.workforce_jobs import run_persisted_task, cancel_task, retry_task, workforce_queue_loop


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
                for model in (Agent, WorkforceTask, WorkforceTaskStep, WorkforceTaskEvent):
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
