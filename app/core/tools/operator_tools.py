"""
operator_tools.py — Tools eksklusif untuk session operator.

Tools ini memungkinkan operator mematikan/mengaktifkan kembali
balasan AI untuk satu pengguna tertentu (via nomor WhatsApp).

Hanya di-inject ke agent ketika session yang aktif adalah session operator
(dideteksi di agent_runner.py berdasarkan is_op_msg flag).

Tools:
  disable_ai_for_user(phone)  — set session.ai_disabled = True
  enable_ai_for_user(phone)   — set session.ai_disabled = False
"""
from __future__ import annotations

import uuid

import structlog
from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.phone_utils import normalize_phone
from app.models.session import Session

logger = structlog.get_logger(__name__)


def build_operator_tools(agent_id: uuid.UUID, db: AsyncSession) -> list:
    """
    Bangun tools operator untuk mematikan/mengaktifkan AI per user.
    Hanya dipanggil saat session yang aktif adalah session operator.
    """

    # ------------------------------------------------------------------
    # disable_ai_for_user
    # ------------------------------------------------------------------
    async def _disable_ai_for_user(phone: str) -> str:
        """
        Matikan balasan AI untuk nomor WhatsApp pengguna tertentu.
        AI akan diam (tidak membalas) sampai operator mengaktifkan kembali.

        Args:
            phone: Nomor WhatsApp pengguna (contoh: 628123456789)
        """
        log = logger.bind(agent_id=str(agent_id), target_phone=phone)
        normalized = normalize_phone(phone)

        result = await db.execute(
            select(Session).where(
                Session.agent_id == agent_id,
                Session.external_user_id == normalized,
            )
        )
        session = result.scalar_one_or_none()

        if not session:
            # Buat session baru dengan ai_disabled=True (pre-block)
            # Ini memungkinkan operator mem-block nomor yang belum pernah chat
            from app.models.session import Session as SessionModel
            session = SessionModel(
                agent_id=agent_id,
                external_user_id=normalized,
                channel_type="whatsapp",
                channel_config={"user_phone": normalized},
                ai_disabled=True,
            )
            db.add(session)
            await db.commit()
            log.info("operator_tools.disable_ai.session_created_blocked", phone=normalized)
            return (
                f"✅ Nomor {phone} telah di-blokir. "
                "AI tidak akan membalas pesan dari nomor ini (bahkan jika belum pernah chat sebelumnya)."
            )

        if session.ai_disabled:
            return f"ℹ️ AI untuk nomor {phone} sudah dalam keadaan nonaktif."

        session.ai_disabled = True
        await db.commit()
        log.info("operator_tools.disable_ai.done", session_id=str(session.id))
        return (
            f"✅ AI dinonaktifkan untuk nomor {phone}. "
            "Agent tidak akan membalas pesan dari pengguna ini sampai Anda mengaktifkannya kembali."
        )

    # ------------------------------------------------------------------
    # enable_ai_for_user
    # ------------------------------------------------------------------
    async def _enable_ai_for_user(phone: str) -> str:
        """
        Aktifkan kembali balasan AI untuk nomor WhatsApp pengguna tertentu.

        Args:
            phone: Nomor WhatsApp pengguna (contoh: 628123456789)
        """
        log = logger.bind(agent_id=str(agent_id), target_phone=phone)
        normalized = normalize_phone(phone)

        result = await db.execute(
            select(Session).where(
                Session.agent_id == agent_id,
                Session.external_user_id == normalized,
            )
        )
        session = result.scalar_one_or_none()

        if not session:
            log.info("operator_tools.enable_ai.session_not_found")
            return (
                f"❌ Tidak ditemukan sesi untuk nomor {phone}. "
                "Pengguna ini belum pernah mengirim pesan ke agent."
            )

        if not session.ai_disabled:
            return f"ℹ️ AI untuk nomor {phone} sudah dalam keadaan aktif."

        session.ai_disabled = False
        await db.commit()
        log.info("operator_tools.enable_ai.done", session_id=str(session.id))
        return (
            f"✅ AI diaktifkan kembali untuk nomor {phone}. "
            "Agent akan kembali membalas pesan dari pengguna ini."
        )

    disable_ai = tool(_disable_ai_for_user)
    disable_ai.name = "disable_ai_for_user"

    enable_ai = tool(_enable_ai_for_user)
    enable_ai.name = "enable_ai_for_user"

    return [disable_ai, enable_ai]
