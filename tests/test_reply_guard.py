from app.core.engine.reply_guard import ensure_non_empty_reply


def test_non_empty_model_reply_is_never_rewritten_for_arthur_v2():
    reply = "Rangkuman kebutuhan Minsel sudah siap. Konfirmasi jika ingin saya buat."
    trace: dict[str, str] = {}

    output = ensure_non_empty_reply(
        reply,
        [{"tool": "get_current_plan", "result": '{"ok": true}'}],
        active_groups=["builder"],
        system_plugin="arthur_v2",
        decision_trace=trace,
    )

    assert output == reply
    assert trace["reason"] == "pass_through"


def test_non_empty_model_reply_is_never_rewritten_for_any_agent():
    reply = "Saya sudah mencatat kebutuhanmu dan menunggu konfirmasi."
    assert ensure_non_empty_reply(reply, []) == reply


def test_empty_reply_uses_url_from_a_tool_result():
    output = ensure_non_empty_reply(
        "",
        [{"tool": "create_document", "result": "URL: https://docs.example.test/file-1"}],
    )
    assert "https://docs.example.test/file-1" in output


def test_empty_reply_has_a_user_facing_fallback():
    trace: dict[str, str] = {}
    output = ensure_non_empty_reply("", [], decision_trace=trace)
    assert output
    assert trace["reason"] == "fallback_empty_reply"
