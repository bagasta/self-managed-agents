"""Payment-link tools for Arthur builder."""
from __future__ import annotations

import json
from uuid import uuid4
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import structlog
from langchain_core.tools import tool

from app.core.tools.builder_identity import best_owner_identifier
from app.core.utils.phone_utils import normalize_phone

logger = structlog.get_logger(__name__)

PAYMENT_BASE_URL = "https://chiefaiofficer.id/pay"
PLAN_ALIASES = {
    "starter": "tier_1",
    "tier1": "tier_1",
    "tier_1": "tier_1",
    "growth": "tier_2",
    "pro": "tier_2",
    "tier2": "tier_2",
    "tier_2": "tier_2",
    "business": "tier_3",
    "enterprise": "tier_3",
    "tier3": "tier_3",
    "tier_3": "tier_3",
}
PLAN_LABELS = {
    "tier_1": "Starter",
    "tier_2": "Pro",
    "tier_3": "Enterprise",
}
PLAN_CAPACITY = {
    "tier_1": 1,
    "tier_2": 2,
    "tier_3": None,
}


def resolve_payment_plan(plan: str | None) -> str | None:
    normalized = str(plan or "").strip().lower().replace("-", "_").replace(" ", "_")
    return PLAN_ALIASES.get(normalized)


def build_payment_link(plan_code: str, phone: str) -> str:
    query = urlencode({"plan": plan_code, "wa": phone, "request": uuid4().hex})
    return f"{PAYMENT_BASE_URL}?{query}"


def verified_payment_payload(
    raw_result: Any,
    *,
    expected_plan: str,
) -> dict[str, Any] | None:
    """Accept only a payment URL emitted for the requested plan and host."""
    try:
        payload = (
            raw_result
            if isinstance(raw_result, dict)
            else json.loads(str(raw_result or ""))
        )
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    if str(payload.get("plan_code") or "") != expected_plan:
        return None
    payment_link = str(payload.get("payment_link") or "").strip()
    parsed = urlparse(payment_link)
    query = parse_qs(parsed.query)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "chiefaiofficer.id"
        or parsed.path != "/pay"
        or query.get("plan") != [expected_plan]
        or not query.get("wa")
        or not query.get("request")
    ):
        return None
    return payload


def payment_user_reply(payload: dict[str, Any]) -> str:
    plan_label = str(payload.get("plan_label") or "paket pilihanmu")
    payment_link = str(payload.get("payment_link") or "")
    max_agents = payload.get("max_agents")
    capacity = (
        "agent tanpa batas"
        if max_agents is None
        else f"maksimal {int(max_agents)} agent"
    )
    return (
        f"Berikut link pembayaran paket {plan_label} ({capacity}):\n"
        f"{payment_link}\n\n"
        "Harga dan periode yang berlaku ditampilkan di halaman checkout. "
        "Setelah pembayaran berhasil dan notifikasinya diterima, paket akan aktif otomatis."
    )


def build_builder_payment_tools(
    *,
    owner_phone: str | None = None,
    default_target: str = "",
) -> dict[str, Any]:
    """Build plan/payment helpers used by Arthur."""

    @tool
    async def get_payment_link(plan: str = "", phone: str = "") -> str:
        """
        Buat link pembayaran Clevio untuk paket tertentu.

        Gunakan saat user meminta beli/upgrade paket, minta link pembayaran,
        atau ingin melihat link plan tertentu. Tool ini hanya membuat link
        publik Clevio; pembayaran dan aktivasi tetap diproses oleh sistem
        setelah notifikasi DOKU masuk.

        Jangan tolak hanya karena plan user saat ini sudah lebih tinggi.
        Kalau user meminta link tier tertentu untuk testing/lihat link,
        tetap panggil tool ini dan berikan link hasilnya.

        Args:
            plan: Paket yang diminta: tier_1/Starter, tier_2/Pro, atau tier_3/Enterprise.
            phone: Opsional. Fallback jika nomor pengirim sesi tidak terbaca.
        """
        try:
            plan_code = resolve_payment_plan(plan)
            if plan_code is None:
                return json.dumps({
                    "error": "plan_tidak_valid",
                    "message": "Pilih paket Starter/tier_1, Pro/tier_2, atau Enterprise/tier_3.",
                    "available_plans": PLAN_LABELS,
                }, ensure_ascii=False)

            target_phone = best_owner_identifier(owner_phone, default_target, phone)
            normalized_phone = normalize_phone(str(target_phone or ""))
            if not normalized_phone:
                return json.dumps({
                    "error": "phone_tidak_tersedia",
                    "message": "Nomor WhatsApp user belum terbaca.",
                }, ensure_ascii=False)

            payment_link = build_payment_link(plan_code, normalized_phone)
            return json.dumps({
                "plan_code": plan_code,
                "plan_label": PLAN_LABELS[plan_code],
                "max_agents": PLAN_CAPACITY[plan_code],
                "phone": normalized_phone,
                "payment_link": payment_link,
                "message": (
                    "Kirim payment_link ini ke user. Setelah pembayaran sukses, "
                    "paket akan aktif otomatis setelah notifikasi pembayaran masuk."
                ),
            }, ensure_ascii=False)
        except Exception as exc:
            logger.error("builder_tools.get_payment_link.error", error=str(exc))
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    return {"get_payment_link": get_payment_link}
