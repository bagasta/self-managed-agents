from app.core.engine.agent_runner import _needs_google_forms_followup


def test_forms_followup_needed_when_only_create_form_step() -> None:
    steps = [
        {
            "tool": "create_form",
            "result": "Successfully created form 'Survey'. Form ID: abc123. Edit URL: https://docs.google.com/forms/d/abc123/edit",
        }
    ]
    needed, form_id = _needs_google_forms_followup(
        "bikin google form survei dan kirim link", steps
    )
    assert needed is True
    assert form_id == "abc123"


def test_forms_followup_not_needed_when_batch_and_get_done() -> None:
    steps = [
        {"tool": "create_form", "result": "Form ID: abc123"},
        {"tool": "batch_update_form", "result": "Batch Update Completed"},
        {
            "tool": "get_form",
            "result": "Responder URL: https://docs.google.com/forms/d/abc123/viewform",
        },
    ]
    needed, form_id = _needs_google_forms_followup(
        "bikin google form survei dan kirim link", steps
    )
    assert needed is False
    assert form_id == "abc123"
