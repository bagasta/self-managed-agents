"""Payment-link primitives owned by Arthur V2, independent of legacy Arthur."""
from __future__ import annotations

from urllib.parse import urlencode
from uuid import uuid4

PAYMENT_BASE_URL = "https://chiefaiofficer.id/pay"
PLAN_ALIASES = {
    "growth": "tier_1", "tier1": "tier_1", "tier_1": "tier_1", "starter": "tier_1",
    "business": "tier_3", "tier3": "tier_3", "tier_3": "tier_3", "enterprise": "tier_3",
    # Legacy names retain a valid upgrade path, but are never offered to users.
    "pro": "tier_1", "tier2": "tier_1", "tier_2": "tier_1",
}
PLAN_LABELS = {"tier_1": "Growth", "tier_3": "Business"}
PLAN_CAPACITY = {"tier_1": 5, "tier_3": None}
PLAN_MONTHLY_PRICES_IDR = {"tier_1": 299_000, "tier_3": 999_000}


def resolve_payment_plan(plan: str) -> str | None:
    return PLAN_ALIASES.get(str(plan or "").strip().lower().replace("-", "_").replace(" ", "_"))


def build_payment_link(plan_code: str, phone: str) -> str:
    return f"{PAYMENT_BASE_URL}?{urlencode({'plan': plan_code, 'wa': phone, 'request': uuid4().hex})}"
