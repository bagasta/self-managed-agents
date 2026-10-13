from arthur_v2.plugin import (
    ASSISTANT_MODEL_LABELS,
    ASSISTANT_MODELS,
    ARTHUR_V2_ASSISTANT_MODEL,
    _assistant_model_level,
    build_arthur_v2_system_prompt,
    build_arthur_v2_tools,
)


def test_arthur_v2_exposes_only_normal_and_advance_model_levels():
    assert ARTHUR_V2_ASSISTANT_MODEL == "anthropic/claude-haiku-5.5"
    assert ASSISTANT_MODELS == {
        "normal": "deepseek/deepseek-v4-flash",
        "advance": "anthropic/claude-haiku-5.5",
    }
    assert ASSISTANT_MODEL_LABELS == {
        "normal": "kepintaran normal",
        "advance": "kepintaran advance",
    }
    assert _assistant_model_level("anthropic/claude-haiku-5.5") == "advance"
    assert _assistant_model_level("unknown/provider-model") == "normal"


def test_arthur_v2_can_change_an_owned_assistant_model_by_level():
    runtime_tool = next(
        tool
        for tool in build_arthur_v2_tools(
            db_factory=None,
            owner_phone="6281234567890",
            self_agent_id="00000000-0000-0000-0000-000000000000",
        )
        if tool.name == "configure_assistant_runtime"
    )

    model_level = runtime_tool.args_schema.model_json_schema()["properties"]["model_level"]
    assert set(model_level["anyOf"][0]["enum"]) == {"normal", "advance"}
    assert "kepintaran advance" in build_arthur_v2_system_prompt()
