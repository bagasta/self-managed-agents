"""Payment-link primitives owned by Arthur V2, independent of legacy Arthur."""
from __future__ import annotations

from urllib.parse import urlencode
from uuid import uuid4

PAYMENT_BASE_URL = "https://chiefaiofficer.id/pay"
PLAN_ALIASES = {
    "starter": "tier_1", "tier1": "tier_1", "tier_1": "tier_1",
    "growth": "tier_2", "pro": "tier_2", "tier2": "tier_2", "tier_2": "tier_2",
    "business": "tier_3", "enterprise": "tier_3", "tier3": "tier_3", "tier_3": "tier_3",
}
PLAN_LABELS = {"tier_1": "Starter", "tier_2": "Pro", "tier_3": "Enterprise"}
PLAN_CAPACITY = {"tier_1": 1, "tier_2": 2, "tier_3": None}


def resolve_payment_plan(plan: str) -> str | None:
    return PLAN_ALIASES.get(str(plan or "").strip().lower().replace("-", "_").replace(" ", "_"))


def build_payment_link(plan_code: str, phone: str) -> str:
    return f"{PAYMENT_BASE_URL}?{urlencode({'plan': plan_code, 'wa': phone, 'request': uuid4().hex})}"
