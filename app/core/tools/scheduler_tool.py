"""
Scheduler tools — agent bisa set/list/cancel reminder/job terjadwal.

Tools yang di-expose ke agent:
  set_reminder(label, message, schedule)              — satu reminder
  set_multiple_reminders(reminders: list[dict])       — beberapa sekaligus (PREFERRED)
  list_reminders()
  cancel_reminder(label)

`schedule` bisa berupa:
  - Relative        : "in 2m", "in 30m", "in 1h", "in 2h", "in 3d"  (dari sekarang)
  - Cron expression : "0 9 * * 1-5"  (setiap hari kerja jam 9 pagi)
  - Shorthand       : "every 1h", "every 30m", "every 1d"
  - ISO datetime    : "2026-04-21T09:00:00"  (sekali jalan, WIB lokal)
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone, tzinfo

# Timezone lokal platform (WIB = UTC+7)
_LOCAL_TZ = timezone(timedelta(hours=7))
_LOCAL_TZ_NAME = "WIB (UTC+7)"

import structlog
from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduled_job import ScheduledJob

logger = structlog.get_logger(__name__)

# Shorthand → cron expression
_SHORTHAND_MAP = {
    "every 1m": "* * * * *",
    "every 5m": "*/5 * * * *",
    "every 15m": "*/15 * * * *",
    "every 30m": "*/30 * * * *",
    "every 1h": "0 * * * *",
    "every 2h": "0 */2 * * *",
    "every 6h": "0 */6 * * *",
    "every 12h": "0 */12 * * *",
    "every 1d": "0 9 * * *",
}

# Regex untuk relative time: "in 2m", "in 30 menit", "in 1h", "in 2 jam", "in 3d", "in 3 hari"
_RELATIVE_RE = re.compile(
    r'^in\s+(\d+)\s*(m|min|menit|h|hour|jam|d|day|hari)s?$',
    re.IGNORECASE,
)


def _parse_schedule(schedule: str) -> tuple[str | None, datetime | None]:
    """
    Return (cron_expr, run_once_at). Tepat satu dari keduanya akan berisi nilai.
    """
    s = schedule.strip()

    # Relative time: "in 2m", "in 30m", "in 1h", "in 2d", dll.
    m = _RELATIVE_RE.match(s)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        now = datetime.now(timezone.utc)
        if unit in ("m", "min", "menit"):
            return None, now + timedelta(minutes=amount)
        elif unit in ("h", "hour", "jam"):
            return None, now + timedelta(hours=amount)
        elif unit in ("d", "day", "hari"):
            return None, now + timedelta(days=amount)

    # Shorthand
    if s.lower() in _SHORTHAND_MAP:
        return _SHORTHAND_MAP[s.lower()], None

    # ISO datetime — dianggap waktu lokal (WIB), dikonversi ke UTC
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=_LOCAL_TZ).astimezone(timezone.utc)
            return None, dt
        except ValueError:
            continue

    # Anggap cron expression (validasi minimal: 5 bagian)
    parts = s.split()
    if len(parts) == 5:
        return s, None

    raise ValueError(
        f"Format schedule tidak dikenali: '{schedule}'. "
        "Gunakan: relative ('in 2m', 'in 1h', 'in 3d'), "
        "cron ('0 9 * * *'), shorthand ('every 1h'), "
        "atau ISO datetime ('2026-04-21T09:00:00' UTC)."
    )


def _compute_next_run(cron_expr: str) -> datetime:
    """Hitung next_run_at dari cron expression (WIB) dan konversi ke UTC."""
    try:
        from croniter import croniter
        now_local = datetime.now(_LOCAL_TZ)
        next_local = croniter(cron_expr, now_local).get_next(datetime)
        return next_local.astimezone(timezone.utc)
    except ImportError:
        return datetime.now(timezone.utc) + timedelta(minutes=1)


def build_scheduler_tools(session_id: uuid.UUID, agent_id: uuid.UUID, db: AsyncSession) -> list:

    _now_local = datetime.now(_LOCAL_TZ).strftime("%Y-%m-%dT%H:%M:%S")

    # --- set_reminder ---
    # Docstring di-inject secara dinamis agar LLM tahu waktu sekarang

    async def _set_reminder(label: str, message: str, schedule: str) -> str:
        from app.database import AsyncSessionLocal

        # Gunakan session terpisah agar tidak conflict dengan DB session agent_runner
        # (LangGraph bisa menjalankan beberapa tool call concurrent via asyncio.gather)
        async with AsyncSessionLocal() as own_db:
            base_label = label
            existing_result = await own_db.execute(
                select(ScheduledJob).where(
                    ScheduledJob.session_id == session_id,
                    ScheduledJob.label == label,
                    ScheduledJob.status == "active",
                )
            )
            existing_job = existing_result.scalar_one_or_none()
            if existing_job:
                suffix = 2
                while True:
                    candidate = f"{base_label}_{suffix}"
                    check = await own_db.execute(
                        select(ScheduledJob).where(
                            ScheduledJob.session_id == session_id,
                            ScheduledJob.label == candidate,
                            ScheduledJob.status == "active",
                        )
                    )
                    if check.scalar_one_or_none() is None:
                        label = candidate
                        break
                    suffix += 1

            try:
                cron_expr, run_once_at = _parse_schedule(schedule)
            except ValueError as exc:
                return f"[error] {exc}"

            next_run = _compute_next_run(cron_expr) if cron_expr else run_once_at

            job = ScheduledJob(
                agent_id=agent_id,
                session_id=session_id,
                label=label,
                cron_expr=cron_expr,
                run_once_at=run_once_at,
                payload=message,
                status="active",
                next_run_at=next_run,
            )
            own_db.add(job)
            await own_db.flush()
            await own_db.commit()

            local_time = next_run.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S") if next_run else "-"
            kind = f"cron ({cron_expr})" if cron_expr else f"sekali pada {local_time} {_LOCAL_TZ_NAME}"
            logger.info("scheduler_tool.set_reminder", label=label, kind=kind)
            return f"Reminder '{label}' berhasil di-set. Jadwal: {kind}. Pesan: \"{message}\""

    _set_reminder.__doc__ = (
        "Set SATU reminder/job terjadwal. "
        "Jika user minta LEBIH DARI SATU reminder sekaligus, GUNAKAN set_multiple_reminders — JANGAN panggil tool ini berkali-kali.\n\n"
        f"Waktu sekarang ({_LOCAL_TZ_NAME}): {_now_local}\n"
        "Gunakan waktu lokal ini sebagai acuan. ISO datetime diinterpretasi sebagai waktu lokal (WIB), "
        "BUKAN UTC.\n\n"
        "Args:\n"
        "    label    : Nama untuk job ini. Jika label sudah ada, suffix _2/_3 ditambah otomatis.\n"
        "               Contoh: 'pagi', 'siang', 'malam', 'daily_report', 'followup'\n"
        "    message  : Pesan yang akan dikirim ke user saat jadwal tiba\n"
        "    schedule : Jadwal pengiriman. Format yang didukung:\n"
        "               - Relative dari sekarang: 'in 2m', 'in 30m', 'in 1h', 'in 2h', 'in 3d'\n"
        "               - Shorthand berulang: 'every 1m', 'every 5m', 'every 30m', 'every 1h', 'every 1d'\n"
        "               - Cron expression WIB: '0 9 * * 1-5' (hari kerja jam 9 pagi WIB)\n"
        f"               - ISO datetime lokal WIB (sekali jalan): '2026-04-24T15:00:00' = jam 15:00 WIB"
    )
    set_reminder = tool(_set_reminder)
    set_reminder.name = "set_reminder"  # override: langchain mengambil nama dari inner func

    # --- set_multiple_reminders ---
    # Tool ini WAJIB dipakai saat user meminta >1 reminder dalam satu permintaan.
    # Lebih andal daripada memanggil set_reminder berkali-kali secara paralel.

    async def _set_multiple_reminders(reminders: list[dict]) -> str:
        """
        Set beberapa reminder/job terjadwal sekaligus dalam satu panggilan.

        GUNAKAN TOOL INI (bukan set_reminder berulang) jika user meminta lebih dari satu reminder.

        Args:
            reminders: List of reminder objects. Setiap object harus punya:
                {
                    "label":    str  — nama unik reminder (contoh: "pagi", "malam"),
                    "message":  str  — pesan yang dikirim saat jadwal tiba,
                    "schedule": str  — format sama dengan set_reminder
                                       ("in 2h", "every 1d", "0 9 * * *", "2026-05-01T09:00:00")
                }
        """
        from app.database import AsyncSessionLocal

        results = []
        async with AsyncSessionLocal() as own_db:
            for item in reminders:
                label = item.get("label", "").strip()
                message = item.get("message", "").strip()
                schedule = item.get("schedule", "").strip()

                if not label or not message or not schedule:
                    results.append(f"[skip] item tidak lengkap: {item}")
                    continue

                # Suffix otomatis jika label sudah ada
                base_label = label
                existing_result = await own_db.execute(
                    select(ScheduledJob).where(
                        ScheduledJob.session_id == session_id,
                        ScheduledJob.label == label,
                        ScheduledJob.status == "active",
                    )
                )
                if existing_result.scalar_one_or_none():
                    suffix = 2
                    while True:
                        candidate = f"{base_label}_{suffix}"
                        check = await own_db.execute(
                            select(ScheduledJob).where(
                                ScheduledJob.session_id == session_id,
                                ScheduledJob.label == candidate,
                                ScheduledJob.status == "active",
                            )
                        )
                        if check.scalar_one_or_none() is None:
                            label = candidate
                            break
                        suffix += 1

                try:
                    cron_expr, run_once_at = _parse_schedule(schedule)
                except ValueError as exc:
                    results.append(f"[error] '{base_label}': {exc}")
                    continue

                next_run = _compute_next_run(cron_expr) if cron_expr else run_once_at

                job = ScheduledJob(
                    agent_id=agent_id,
                    session_id=session_id,
                    label=label,
                    cron_expr=cron_expr,
                    run_once_at=run_once_at,
                    payload=message,
                    status="active",
                    next_run_at=next_run,
                )
                own_db.add(job)

                local_time = next_run.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S") if next_run else "-"
                kind = f"cron ({cron_expr})" if cron_expr else f"sekali pada {local_time} {_LOCAL_TZ_NAME}"
                results.append(f"✅ '{label}': {kind}")
                logger.info("scheduler_tool.set_multiple.item", label=label, kind=kind)

            await own_db.commit()

        summary = "\n".join(results)
        logger.info("scheduler_tool.set_multiple", count=len(reminders))
        return f"{len([r for r in results if r.startswith('✅')])} reminder berhasil di-set:\n{summary}"

    _set_multiple_reminders.__doc__ = (
        "Set BEBERAPA reminder sekaligus dalam SATU panggilan. "
        "SELALU GUNAKAN INI jika user minta lebih dari 1 reminder.\n\n"
        f"Waktu sekarang ({_LOCAL_TZ_NAME}): {_now_local}\n"
        "Gunakan waktu lokal ini sebagai acuan. ISO datetime = waktu lokal WIB, BUKAN UTC.\n\n"
        "Args:\n"
        "    reminders: list of dict, setiap dict harus punya:\n"
        "        label    — nama unik reminder (contoh: 'pagi', 'malam', 'followup')\n"
        "        message  — pesan yang akan dikirim saat jadwal tiba\n"
        "        schedule — 'in 2h' | 'every 1d' | '0 9 * * *' | '2026-05-01T09:00:00'\n\n"
        "Contoh input untuk 2 reminder sekaligus:\n"
        "    [{\"label\": \"pagi\", \"message\": \"Selamat pagi!\", \"schedule\": \"0 7 * * *\"},\n"
        "     {\"label\": \"malam\", \"message\": \"Selamat malam!\", \"schedule\": \"0 21 * * *\"}]"
    )
    set_multiple_reminders = tool(_set_multiple_reminders)
    set_multiple_reminders.name = "set_multiple_reminders"  # override: langchain mengambil nama dari inner func


    # --- list_reminders ---

    @tool
    async def list_reminders() -> str:
        """Tampilkan semua reminder/job terjadwal yang aktif untuk sesi ini."""
        result = await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.session_id == session_id,
                ScheduledJob.status == "active",
            ).order_by(ScheduledJob.created_at)
        )
        jobs = list(result.scalars().all())
        if not jobs:
            return "Tidak ada reminder aktif."

        lines = []
        for j in jobs:
            sched = j.cron_expr or (j.run_once_at.isoformat() if j.run_once_at else "-")
            next_run = j.next_run_at.isoformat() if j.next_run_at else "-"
            lines.append(f"- **{j.label}** | jadwal: {sched} | next: {next_run} | pesan: \"{j.payload}\"")
        return "Reminder aktif:\n" + "\n".join(lines)

    # --- cancel_reminder ---

    @tool
    async def cancel_reminder(label: str) -> str:
        """
        Batalkan sebuah reminder berdasarkan label-nya.

        Args:
            label: Nama reminder yang ingin dibatalkan
        """
        result = await db.execute(
            select(ScheduledJob).where(
                ScheduledJob.session_id == session_id,
                ScheduledJob.label == label,
                ScheduledJob.status == "active",
            )
        )
        job = result.scalar_one_or_none()
        if not job:
            return f"Tidak ada reminder aktif dengan label '{label}'."
        job.status = "cancelled"
        await db.flush()
        logger.info("scheduler_tool.cancelled", label=label)
        return f"Reminder '{label}' berhasil dibatalkan."

    return [set_reminder, set_multiple_reminders, list_reminders, cancel_reminder]
