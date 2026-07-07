from app.core.tools.builder_payment_tools import build_payment_link, resolve_payment_plan


def test_resolve_payment_plan_aliases():
    assert resolve_payment_plan("tier_1") == "tier_1"
    assert resolve_payment_plan("Starter") == "tier_1"
    assert resolve_payment_plan("tier 2") == "tier_2"
    assert resolve_payment_plan("Pro") == "tier_2"
    assert resolve_payment_plan("Enterprise") == "tier_3"
    assert resolve_payment_plan("unknown") is None


def test_build_payment_link_uses_clevio_bridge():
    assert (
        build_payment_link("tier_2", "6281234567890")
        == "https://chiefaiofficer.id/pay?plan=tier_2&wa=6281234567890"
    )
