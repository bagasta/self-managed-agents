"""
Escalation tools — agent bisa eskalasi ke human operator dan kirim pesan ke channel.

Tools yang di-expose ke agent:
  escalate_to_human(reason, summary)  — aktifkan mode eskalasi
  reply_to_user(message)              — kirim pesan ke user via channel sesi
  send_to_number(phone, message)      — kirim pesan ke nomor/target lain

Saat escalation_active=True:
  - Setiap pesan user akan di-forward ke operator oleh /channels/incoming endpoint
  - Agent tetap jalan dan bisa menerima perintah dari operator
  - Operator bisa memerintahkan agent via pesan (agent bedakan berdasarkan sender)
"""
from __future__ import annotations

import uuid

import structlog
from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.session import Session

logger = structlog.get_logger(__name__)


def build_escalation_tools(
    session_id: uuid.UUID,
    agent_id: uuid.UUID,
    db: AsyncSession,
) -> list:

    @tool
    async def escalate_to_human(reason: str, summary: str = "") -> str:
        """
        Eskalasi percakapan ke human operator.
        Gunakan saat tidak bisa menangani permintaan user, butuh persetujuan manusia,
        atau situasi sensitif yang memerlukan intervensi manusia.

        Args:
            reason  : Alasan eskalasi singkat (contoh: "permintaan refund di luar policy")
            summary : Ringkasan konteks percakapan untuk operator (opsional tapi direkomendasikan)
        """
        # Load session dan agent config
        sess_result = await db.execute(select(Session).where(Session.id == session_id))
        session = sess_result.scalar_one_or_none()
        if not session:
            return "[error] Session tidak ditemukan."

        agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            return "[error] Agent tidak ditemukan."

        escalation_cfg: dict = agent.escalation_config or {}
        if not escalation_cfg:
            return "[error] Agent belum dikonfigurasi escalation_config. Tambahkan operator_phone dan channel_type."

        # Aktifkan mode eskalasi
        session.escalation_active = True
        await db.flush()

        # Kirim notifikasi ke operator
        operator_channel = escalation_cfg.get("channel_type", "")
        operator_phone = escalation_cfg.get("operator_phone", "")
        operator_config = {
            **escalation_cfg,
            "user_phone": operator_phone,
        }

        user_id = session.external_user_id or str(session.id)
        channel_info = session.channel_config or {}
        user_phone = channel_info.get("user_phone", user_id)

        notif_text = (
            f"🚨 *ESKALASI AGENT*\n"
            f"User: {user_phone}\n"
            f"Alasan: {reason}\n"
            + (f"Ringkasan: {summary}\n" if summary else "")
            + f"\nBalas pesan ini untuk mengendalikan agent. Contoh:\n"
            f'- "Kirim ke customer: [pesan balasan]"\n'
            f'- "Kirim ke +62812xxx: [pesan]"\n'
            f'- "Selesai, tangani sendiri"'
        )

        # Selalu simpan notifikasi ke DB (untuk dev UI & fallback jika channel gagal)
        from app.models.message import Message
        db.add(Message(
            session_id=session_id,
            role="escalation",
            content=notif_text,
            step_index=9000,
        ))
        await db.flush()

        # Kirim ke channel operator jika dikonfigurasi
        try:
            from app.core.channel_service import send_message
            await send_message(
                channel_type=operator_channel,
                channel_config=operator_config,
                text=notif_text,
            )
            logger.info("escalation_tool.notified_operator", reason=reason, operator=operator_phone)
        except Exception as exc:
            logger.warning("escalation_tool.channel_send_skipped", error=str(exc))

        return (
            f"Eskalasi berhasil diaktifkan. Operator ({operator_phone}) telah dinotifikasi. "
            f"Pesan user berikutnya akan diteruskan ke operator. "
            f"Tunggu instruksi dari operator."
        )

    @tool
    async def reply_to_user(message: str) -> str:
        """
        Kirim pesan ke user via channel sesi ini.
        Gunakan untuk mengirim jawaban atau informasi ke user atas perintah operator.

        Args:
            message: Pesan yang akan dikirim ke user
        """
        from app.models.message import Message as Msg
        sess_result = await db.execute(select(Session).where(Session.id == session_id))
        session = sess_result.scalar_one_or_none()

        # Simpan ke DB selalu — supaya UI simulator bisa detect & tampilkan di sisi user
        db.add(Msg(
            session_id=session_id,
            role="agent",
            content=f"[TO_USER] {message}",
            step_index=9001,
        ))
        await db.flush()

        # Coba kirim ke channel jika dikonfigurasi
        if session and session.channel_type:
            try:
                from app.core.channel_service import send_message
                await send_message(
                    channel_type=session.channel_type,
                    channel_config=session.channel_config or {},
                    text=message,
                )
                logger.info("escalation_tool.reply_to_user", channel=session.channel_type)
            except Exception as exc:
                logger.warning("escalation_tool.reply_channel_failed", error=str(exc))
        else:
            logger.info("escalation_tool.reply_to_user.dev_mode", message_preview=message[:80])

        # Prefix SENT_TO_USER dipakai UI untuk detect & tampilkan di sisi user
        return f"[SENT_TO_USER] {message}"

    @tool
    async def send_to_number(phone_or_target: str, message: str) -> str:
        """
        Kirim pesan ke nomor telepon atau target lain (berbeda dari user utama).
        Berguna saat operator memerintahkan agent untuk menghubungi pihak lain.

        Args:
            phone_or_target: Nomor tujuan (contoh: "+62812xxx") atau chat_id
            message        : Pesan yang akan dikirim
        """
        from app.models.message import Message as Msg
        sess_result = await db.execute(select(Session).where(Session.id == session_id))
        session = sess_result.scalar_one_or_none()

        # Simpan ke DB
        db.add(Msg(
            session_id=session_id,
            role="agent",
            content=f"[TO_NUMBER:{phone_or_target}] {message}",
            step_index=9002,
        ))
        await db.flush()

        # Coba kirim via channel jika dikonfigurasi
        if session and session.channel_type:
            try:
                from app.core.channel_service import send_message
                await send_message(
                    channel_type=session.channel_type,
                    channel_config=session.channel_config or {},
                    text=message,
                    to_override=phone_or_target,
                )
                logger.info("escalation_tool.send_to_number", target=phone_or_target)
            except Exception as exc:
                logger.warning("escalation_tool.send_to_number_channel_failed", error=str(exc))
        else:
            logger.info("escalation_tool.send_to_number.dev_mode", target=phone_or_target, message_preview=message[:80])

        return f"[SENT_TO_NUMBER:{phone_or_target}] {message}"

    return [escalate_to_human, reply_to_user, send_to_number]
