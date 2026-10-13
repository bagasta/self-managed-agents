from arthur_v2.payments import (
    PLAN_CAPACITY,
    PLAN_LABELS,
    PLAN_MONTHLY_PRICES_IDR,
    resolve_payment_plan,
)


def test_arthur_payment_catalog_uses_growth_and_business_prices():
    assert resolve_payment_plan("Growth") == "tier_1"
    assert resolve_payment_plan("Business") == "tier_3"
    assert PLAN_LABELS == {"tier_1": "Growth", "tier_3": "Business"}
    assert PLAN_CAPACITY == {"tier_1": 5, "tier_3": None}
    assert PLAN_MONTHLY_PRICES_IDR == {"tier_1": 299_000, "tier_3": 999_000}
