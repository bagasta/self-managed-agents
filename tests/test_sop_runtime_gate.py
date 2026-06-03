from app.core.engine.sop_runtime_gate import gated_tool_names, is_sop_locked


def test_locked_when_draft():
    assert is_sop_locked({"maturity": "draft"}) is True
    assert is_sop_locked({"maturity": "needs_review"}) is True
    assert is_sop_locked({"maturity": "usable"}) is False
    assert is_sop_locked({"owner_review_required": True, "maturity": "usable"}) is True
    assert is_sop_locked(None) is True


def test_gated_tools_removed_when_locked():
    names = {"recall", "remember", "escalate_to_human", "reply_to_user",
             "send_whatsapp_document", "send_whatsapp_image"}
    kept = gated_tool_names(names, sop={"maturity": "draft"})
    assert "send_whatsapp_document" not in kept
    assert "send_whatsapp_image" not in kept
    assert "escalate_to_human" in kept
    assert "recall" in kept


def test_no_gating_when_usable():
    names = {"send_whatsapp_document", "recall"}
    kept = gated_tool_names(names, sop={"maturity": "usable", "owner_review_required": False})
    assert kept == names


def test_builder_caps_bypass_gating():
    """Agents with builder/system caps must never be gated."""
    from unittest.mock import MagicMock
    from app.core.engine.sop_runtime_gate import filter_tools_by_sop

    mock_tool_doc = MagicMock()
    mock_tool_doc.name = "send_whatsapp_document"
    mock_tool_img = MagicMock()
    mock_tool_img.name = "send_whatsapp_image"
    mock_tool_recall = MagicMock()
    mock_tool_recall.name = "recall"

    all_tools = [mock_tool_doc, mock_tool_img, mock_tool_recall]
    draft_sop = {"maturity": "draft"}

    # Builder cap → no gating
    result = filter_tools_by_sop(all_tools, sop=draft_sop, caps=["builder"])
    assert any(t.name == "send_whatsapp_document" for t in result)

    # System cap → no gating
    result = filter_tools_by_sop(all_tools, sop=draft_sop, caps=["system"])
    assert any(t.name == "send_whatsapp_document" for t in result)

    # Normal agent + draft → gated
    result = filter_tools_by_sop(all_tools, sop=draft_sop, caps=[])
    assert not any(t.name == "send_whatsapp_document" for t in result)
    assert not any(t.name == "send_whatsapp_image" for t in result)
    assert any(t.name == "recall" for t in result)

    # Normal agent + usable → not gated
    result = filter_tools_by_sop(all_tools, sop={"maturity": "usable"}, caps=[])
    assert any(t.name == "send_whatsapp_document" for t in result)
