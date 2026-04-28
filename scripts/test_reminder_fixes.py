"""
Test perbaikan reminder:
1. set_multiple_reminders — bisa set 2+ reminder sekaligus
2. scheduler_service._run_job — fallback reply, isolasi event bus error
"""
import asyncio
import sys
import uuid
from datetime import datetime

sys.path.insert(0, "/home/bagas/managed-agents-project")
from dotenv import load_dotenv
load_dotenv("/home/bagas/managed-agents-project/.env")

SEPARATOR = "=" * 60

def ok(msg):   print(f"  \u2705 {msg}")
def fail(msg): print(f"  \u274c {msg}")
def info(msg): print(f"  \u2139\ufe0f  {msg}")
def section(title):
    print(f"\n{SEPARATOR}\n  {title}\n{SEPARATOR}")


async def get_real_agent_and_session(db):
    """Ambil agent nyata dari DB dan buat session test sementara."""
    from app.models.agent import Agent
    from app.models.session import Session
    from sqlalchemy import select

    res = await db.execute(select(Agent).where(Agent.is_deleted.is_(False)).limit(1))
    agent = res.scalar_one_or_none()
    if not agent:
        raise RuntimeError("Tidak ada agent di DB. Buat agent dulu via API.")

    # Buat session test sementara dengan external_user_id unik
    test_uid = f"test_reminder_{uuid.uuid4().hex[:8]}"
    session = Session(
        agent_id=agent.id,
        external_user_id=test_uid,
        channel_type="whatsapp",
        channel_config={"device_id": "wadev_test", "user_phone": "6281234567890"},
    )
    db.add(session)
    await db.flush()
    await db.refresh(session)
    return agent, session


async def test_import_scheduler_tool():
    section("TEST 1: Import scheduler_tool.py")
    try:
        from app.core.tools.scheduler_tool import build_scheduler_tools, _parse_schedule
        ok("Import berhasil")
    except Exception as e:
        fail(f"Import gagal: {e}")
        return False

    cases = [
        ("in 2m", "relative menit"), ("in 1h", "relative jam"),
        ("in 3d", "relative hari"),  ("every 1h", "shorthand"),
        ("0 9 * * 1-5", "cron"),     ("2026-12-31T09:00:00", "ISO datetime"),
    ]
    all_ok = True
    for sched, desc in cases:
        try:
            cron, run_once = _parse_schedule(sched)
            ok(f"_parse_schedule('{sched}') [{desc}] OK")
        except Exception as e:
            fail(f"_parse_schedule('{sched}') [{desc}] ERROR: {e}")
            all_ok = False
    return all_ok


async def test_tool_list():
    section("TEST 2: build_scheduler_tools harus return 4 tools")
    from app.database import AsyncSessionLocal
    from app.core.tools.scheduler_tool import build_scheduler_tools
    try:
        async with AsyncSessionLocal() as db:
            agent, session = await get_real_agent_and_session(db)
            tools = build_scheduler_tools(session.id, agent.id, db)
            tool_names = [t.name for t in tools]
            info(f"Tools: {tool_names}")
            expected = {"set_reminder", "set_multiple_reminders", "list_reminders", "cancel_reminder"}
            missing  = expected - set(tool_names)
            if missing:
                fail(f"Tool tidak ditemukan: {missing}")
                return False
            ok(f"Semua 4 tools tersedia")
            # Rollback session test
            await db.rollback()
            return True
    except Exception as e:
        fail(f"Error: {e}")
        import traceback; traceback.print_exc()
        return False


async def test_set_multiple_reminders():
    section("TEST 3: set_multiple_reminders (2 reminder sekaligus)")
    from app.database import AsyncSessionLocal
    from app.core.tools.scheduler_tool import build_scheduler_tools
    from app.models.scheduled_job import ScheduledJob
    from sqlalchemy import select

    reminders_input = [
        {"label": "test_pagi",  "message": "Selamat pagi!",  "schedule": "in 5m"},
        {"label": "test_malam", "message": "Selamat malam!", "schedule": "in 10m"},
    ]

    try:
        # Buat session test yang di-commit dulu agar FK valid
        async with AsyncSessionLocal() as setup_db:
            agent, session = await get_real_agent_and_session(setup_db)
            session_id = session.id
            agent_id   = agent.id
            await setup_db.commit()

        # Jalankan tool
        async with AsyncSessionLocal() as db:
            tools = build_scheduler_tools(session_id, agent_id, db)
            multi_tool = next(t for t in tools if t.name == "set_multiple_reminders")
            result = await multi_tool.ainvoke({"reminders": reminders_input})
            info(f"Hasil tool:\n{result}")
            if "2 reminder berhasil di-set" in result:
                ok("set_multiple_reminders berhasil membuat 2 reminder")
            else:
                fail(f"Output tidak sesuai: {result}")
                return False

        # Verifikasi di DB
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(ScheduledJob).where(
                    ScheduledJob.session_id == session_id,
                    ScheduledJob.status == "active",
                ).order_by(ScheduledJob.created_at)
            )
            jobs = res.scalars().all()
            info(f"Jobs di DB: {[j.label for j in jobs]}")
            if len(jobs) == 2:
                ok(f"2 ScheduledJob tersimpan di DB: {[j.label for j in jobs]}")
            else:
                fail(f"Jumlah job di DB salah: {len(jobs)} (harusnya 2)")
                return False

            # Cleanup
            for j in jobs:
                j.status = "cancelled"
            # Hapus session test
            from app.models.session import Session
            sess_res = await db.execute(select(Session).where(Session.id == session_id))
            sess_obj = sess_res.scalar_one_or_none()
            if sess_obj:
                await db.delete(sess_obj)
            await db.commit()
            ok("Cleanup selesai")
        return True

    except Exception as e:
        fail(f"Error: {e}")
        import traceback; traceback.print_exc()
        return False


async def test_duplicate_label_suffix():
    section("TEST 4: set_multiple_reminders — label duplikat -> auto-suffix")
    from app.database import AsyncSessionLocal
    from app.core.tools.scheduler_tool import build_scheduler_tools
    from app.models.scheduled_job import ScheduledJob
    from sqlalchemy import select

    reminders_input = [
        {"label": "follow_up", "message": "First",  "schedule": "in 5m"},
        {"label": "follow_up", "message": "Second", "schedule": "in 10m"},
    ]

    try:
        async with AsyncSessionLocal() as setup_db:
            agent, session = await get_real_agent_and_session(setup_db)
            session_id = session.id
            agent_id   = agent.id
            await setup_db.commit()

        async with AsyncSessionLocal() as db:
            tools = build_scheduler_tools(session_id, agent_id, db)
            multi_tool = next(t for t in tools if t.name == "set_multiple_reminders")
            result = await multi_tool.ainvoke({"reminders": reminders_input})
            info(f"Hasil tool:\n{result}")

        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(ScheduledJob).where(
                    ScheduledJob.session_id == session_id,
                    ScheduledJob.status == "active",
                )
            )
            jobs = res.scalars().all()
            labels = [j.label for j in jobs]
            info(f"Labels di DB: {labels}")
            if "follow_up" in labels and "follow_up_2" in labels:
                ok(f"Auto-suffix bekerja benar: {labels}")
            else:
                fail(f"Auto-suffix tidak bekerja: {labels}")
                return False

            # Cleanup
            for j in jobs:
                j.status = "cancelled"
            from app.models.session import Session
            sess_res = await db.execute(select(Session).where(Session.id == session_id))
            sess_obj = sess_res.scalar_one_or_none()
            if sess_obj:
                await db.delete(sess_obj)
            await db.commit()
            ok("Cleanup selesai")
        return True

    except Exception as e:
        fail(f"Error: {e}")
        import traceback; traceback.print_exc()
        return False


async def test_fallback_reply_logic():
    section("TEST 5: scheduler_service — source code checks")
    import pathlib
    src = pathlib.Path("/home/bagas/managed-agents-project/app/core/scheduler_service.py").read_text()

    checks = [
        ("empty_reply_fallback", "Fallback ketika reply kosong"),
        ("event_bus_failed",     "Isolasi error event bus"),
        ("reply_send_failed",    "Isolasi error kirim channel"),
        ("is_wadev",             "Log device_id wa-dev"),
        ("exc_info=True",        "Full traceback di job_error"),
    ]
    all_ok = True
    for pattern, desc in checks:
        if pattern in src:
            ok(f"{desc} ('{pattern}' ditemukan)")
        else:
            fail(f"{desc} TIDAK DITEMUKAN ('{pattern}')")
            all_ok = False
    return all_ok


async def main():
    print(f"\n{'=' * 60}")
    print("  TEST SUITE: Reminder Fixes")
    print(f"  Waktu: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 60}")

    results = {}
    results["1. Import & parse_schedule"]         = await test_import_scheduler_tool()
    results["2. build_scheduler_tools (4 tools)"] = await test_tool_list()
    results["3. set_multiple_reminders (2x)"]     = await test_set_multiple_reminders()
    results["4. Auto-suffix label duplikat"]       = await test_duplicate_label_suffix()
    results["5. scheduler_service hardening"]      = await test_fallback_reply_logic()

    section("HASIL AKHIR")
    passed = sum(1 for v in results.values() if v)
    total  = len(results)
    for name, result in results.items():
        status = "\u2705 PASS" if result else "\u274c FAIL"
        print(f"  {status}  {name}")
    print(f"\n  Total: {passed}/{total} passed")
    if passed == total:
        print("\n  \U0001f389 Semua test LULUS!\n")
    else:
        print("\n  \u26a0\ufe0f  Ada test yang GAGAL.\n")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
