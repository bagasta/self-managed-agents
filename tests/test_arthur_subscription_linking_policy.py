from app.core.engine.prompt_builder import _build_arthur_tool_category_guide


def test_arthur_keeps_subscription_and_agent_management_inside_whatsapp() -> None:
    guide = _build_arthur_tool_category_guide()

    assert "Semua penjelasan dan tindakan dilakukan lewat chat WhatsApp ini" in guide
    assert "Jangan pernah mengarahkan user membuka dashboard" in guide
    assert "link_dashboard_account" not in guide


def test_subscription_blocker_uses_verified_tool_result_and_payment_link() -> None:
    guide = _build_arthur_tool_category_guide()

    assert "blocker yang benar-benar dibuktikan tool" in guide
    assert "payment link bila tersedia" in guide
