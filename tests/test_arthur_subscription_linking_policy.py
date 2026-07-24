from app.core.engine.prompt_builder import _build_arthur_tool_category_guide


def test_dashboard_linking_is_not_a_create_agent_prerequisite() -> None:
    guide = _build_arthur_tool_category_guide()

    assert "linking dashboard bukan prasyarat pembuatan agent" in guide
    assert "intent membuat agent bukan alasan untuk meminta linking" in guide
    assert "atau status identity_unlinked: minta user buka Dashboard" not in guide


def test_dashboard_linking_requires_an_explicit_paid_plan_mismatch() -> None:
    guide = _build_arthur_tool_category_guide()

    assert "user secara eksplisit menyatakan plan dashboard sudah berbayar/di-upgrade" in guide
    assert "hasil tool pada turn yang sama" in guide
