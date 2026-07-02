"""User management and plan/quota tools for Arthur builder."""
from __future__ import annotations

import json
from typing import Any

import structlog
from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.tools.builder_identity import (
    best_owner_identifier,
    is_probable_lid,
    owner_filter,
)
from app.core.utils.phone_utils import normalize_phone
from app.models.agent import Agent

logger = structlog.get_logger(__name__)


def build_builder_user_tools(
    db_factory: async_sessionmaker,
    *,
    owner_phone: str | None = None,
    default_target: str = "",
) -> dict[str, Any]:
    """Build user-management and billing/quota helpers used by Arthur."""

    async def preview_agent_creation_entitlement(
        *,
        tools_config: dict[str, Any],
        model: str,
        channel_type: str | None,
    ) -> dict[str, Any]:
        """Check owner tier/slot before Arthur invests in creating the agent."""
        target_phone = best_owner_identifier(owner_phone, default_target)
        if not target_phone:
            return {
                "checked": True,
                "allowed": False,
                "reason": "owner_external_id tidak tersedia.",
                "user_message": (
                    "Saya belum bisa cek paket kamu karena nomor/owner sesi ini belum terbaca. "
                    "Kirim dari session WhatsApp user yang valid dulu."
                ),
            }

        if not hasattr(Agent, "__table__"):
            return {
                "checked": False,
                "allowed": True,
                "reason": "agent_model_unavailable",
            }

        try:
            from app.core.domain.subscription_service import (
                check_can_create_agent,
                get_best_subscription_by_external_ids,
                get_or_create_wa_user,
                get_subscription_by_external_id,
                validate_agent_entitlements,
            )

            async with db_factory() as db:
                owner_candidates = [owner_phone, default_target, target_phone]
                sub_details = await get_best_subscription_by_external_ids(owner_candidates, db)
                if sub_details is not None:
                    user, _sub, _plan = sub_details
                    target_phone = best_owner_identifier(
                        getattr(user, "phone_number", None),
                        getattr(user, "external_id", None),
                        target_phone,
                    )
                elif is_probable_lid(target_phone):
                    sub_details = await get_subscription_by_external_id(target_phone, db)
                    if sub_details is None:
                        return {
                            "checked": True,
                            "allowed": False,
                            "owner": target_phone,
                            "identifier_type": "lid",
                            "reason": "nomor WhatsApp asli belum tersedia.",
                            "user_message": (
                                "Saya belum bisa cek paket kamu karena yang terbaca masih ID WhatsApp internal, "
                                "bukan nomor asli. Kirim pesan dari nomor yang sudah terhubung dulu."
                            ),
                        }
                else:
                    await get_or_create_wa_user(target_phone, db)
                    await db.commit()

                create_check = await check_can_create_agent(target_phone, db)
                if not create_check.get("allowed"):
                    return {
                        "checked": True,
                        "allowed": False,
                        "owner": target_phone,
                        "reason": create_check.get("reason") or "Plan tidak mengizinkan agent baru.",
                        "user_message": create_check.get("reason") or "Paket kamu belum bisa membuat agent baru.",
                        "plan": create_check.get("plan"),
                        "agents_used": create_check.get("agents_used"),
                        "agents_limit": create_check.get("max_agents"),
                    }

                if sub_details is None:
                    sub_details = await get_subscription_by_external_id(target_phone, db)
                if sub_details is None:
                    return {
                        "checked": True,
                        "allowed": False,
                        "owner": target_phone,
                        "reason": "Subscription tidak ditemukan.",
                        "user_message": "Subscription kamu belum ditemukan, jadi agent belum bisa dibuat.",
                    }

                _, sub, plan = sub_details
                entitlement_errors = validate_agent_entitlements(
                    plan,
                    model=model,
                    tools_config=tools_config,
                    channel_type=channel_type or None,
                )
                if entitlement_errors:
                    return {
                        "checked": True,
                        "allowed": False,
                        "owner": target_phone,
                        "reason": "Konfigurasi agent melebihi entitlement plan.",
                        "user_message": "Paket kamu belum mendukung konfigurasi agent ini.",
                        "plan": getattr(plan, "label", None),
                        "plan_code": getattr(plan, "code", None),
                        "violations": entitlement_errors,
                        "agents_used": create_check.get("agents_used"),
                        "agents_limit": create_check.get("max_agents"),
                    }

                return {
                    "checked": True,
                    "allowed": True,
                    "owner": target_phone,
                    "plan": getattr(plan, "label", None),
                    "plan_code": getattr(plan, "code", None),
                    "agents_used": create_check.get("agents_used"),
                    "agents_limit": create_check.get("max_agents"),
                    "tokens_remaining": getattr(sub, "tokens_remaining", None),
                    "expires_at": create_check.get("expires_at"),
                }
        except Exception as exc:
            logger.warning(
                "builder_tools.plan_agent.entitlement_preview_failed",
                owner_phone=target_phone,
                error=str(exc),
            )
            return {
                "checked": False,
                "allowed": True,
                "owner": target_phone,
                "reason": "entitlement_check_unavailable",
                "detail": str(exc),
            }

    @tool
    async def get_user_subscription(phone: str = "") -> str:
        """
        Cek status subscription dan kuota agent owner sesi ini.
        Gunakan ini sebelum/sesudah create_agent atau update_agent untuk info
        akurat tentang plan, sisa slot agent, dan status subscription.

        Tool ini SELALU melaporkan plan owner sesi yang terverifikasi (nomor
        WhatsApp pengirim). Jangan tebak/isi `phone` dari teks chat — nomor yang
        disebut user di percakapan bisa berbeda dari nomor pengirim aslinya.

        Args:
            phone: Opsional. Hanya dipakai sebagai fallback kalau identitas owner
                   sesi tidak terbaca; TIDAK menimpa owner sesi yang terverifikasi.
        """
        try:
            from app.core.domain.subscription_service import (
                get_best_subscription_by_external_ids,
                get_subscription_by_external_id,
            )

            # Owner identity sesi (nomor pengirim terverifikasi) selalu menang.
            # `phone` dari LLM hanya fallback terakhir — mencegah Arthur membaca
            # plan akun lain karena salah nomor dari teks chat.
            target_phone = best_owner_identifier(owner_phone, default_target, phone)
            if not target_phone:
                return json.dumps({"error": "phone tidak tersedia"}, ensure_ascii=False)

            async with db_factory() as db:
                owner_candidates = [owner_phone, default_target, target_phone]
                if not best_owner_identifier(owner_phone, default_target):
                    owner_candidates.append(phone)

                details = await get_best_subscription_by_external_ids(owner_candidates, db)
                if details is None and is_probable_lid(target_phone):
                    details = await get_subscription_by_external_id(target_phone, db)
                if details is None:
                    return json.dumps({
                        "error": "Subscription owner sesi ini tidak ditemukan.",
                        "identifier": target_phone,
                        "identifier_type": "lid" if is_probable_lid(target_phone) else "external_id",
                        "lookup_identifiers": [
                            normalize_phone(str(candidate or ""))
                            for candidate in owner_candidates
                            if normalize_phone(str(candidate or ""))
                        ],
                        "read_only": True,
                    }, ensure_ascii=False)

                user, sub, plan = details
                owner_for_agents = best_owner_identifier(
                    getattr(user, "phone_number", None),
                    getattr(user, "external_id", None),
                    target_phone,
                )

                # Hitung agent aktif
                active_count_result = await db.execute(
                    select(Agent).where(
                        Agent.is_deleted.is_(False),
                        owner_filter(owner_for_agents),
                    )
                )
                active_agents = active_count_result.scalars().all()
                used = len(active_agents)
                limit = plan.max_agents
                remaining = None if limit is None else max(0, limit - used)

                return json.dumps({
                    "phone": owner_for_agents,
                    "user_id": str(getattr(user, "id", "")),
                    "user_external_id": getattr(user, "external_id", None),
                    "user_phone_number": getattr(user, "phone_number", None),
                    "plan_code": plan.code,
                    "plan_label": plan.label,
                    "status": sub.status,
                    "is_active": sub.is_usable,
                    "agents_used": used,
                    "agents_limit": limit,
                    "agents_remaining": remaining,
                    "active_agent_names": [a.name for a in active_agents],
                    "token_quota": sub.token_quota,
                    "tokens_used": getattr(sub, "tokens_used", 0),
                    "tokens_remaining": getattr(sub, "tokens_remaining", max(0, sub.token_quota - getattr(sub, "tokens_used", 0))),
                    "active_until": sub.expires_at.isoformat() if sub.expires_at else None,
                }, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error("builder_tools.get_user_subscription.error", error=str(exc))
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    return {
        "preview_agent_creation_entitlement": preview_agent_creation_entitlement,
        "get_user_subscription": get_user_subscription,
    }
