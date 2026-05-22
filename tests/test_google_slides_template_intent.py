from app.core.engine.agent_runner import (
    _extract_requested_slide_count,
    _is_google_forms_authoring_intent,
    _is_google_slides_relayout_intent,
    _needs_google_slides_followup,
)
from app.core.engine.google_mcp_support import (
    build_default_form_questions,
    build_google_mcp_usage_notice,
    google_slides_followup_directive,
    _needs_generated_form_questions,
)


def test_slides_relayout_intent_detected_indonesian():
    assert _is_google_slides_relayout_intent("rapihkan kontennya jadikan 3 slide") is True


def test_extract_requested_slide_count_from_message():
    assert _extract_requested_slide_count("rapihkan kontennya jadikan 3 slide") == 3
    assert _extract_requested_slide_count("buatkan Google Slides 2 halaman") == 2
    assert _extract_requested_slide_count("make a 4 page presentation") == 4


def test_slides_authoring_intent_detected_for_new_deck():
    assert _is_google_slides_relayout_intent("tolong buatkan slide di google slides tentang laporan kas") is True


def test_non_slides_message_not_detected():
    assert _is_google_slides_relayout_intent("tolong cek email terbaru") is False
    assert _extract_requested_slide_count("tolong cek email terbaru") is None


def test_slides_followup_needed_when_only_create_presentation_step() -> None:
    steps = [
        {
            "tool": "create_presentation",
            "result": (
                "Presentation Created Successfully for user@example.com:\n"
                "- Title: Laporan Kas\n"
                "- Presentation ID: pres123abc\n"
                "- URL: https://docs.google.com/presentation/d/pres123abc/edit\n"
                "- Slides: 1 slide(s) created"
            ),
        }
    ]
    needed, presentation_id = _needs_google_slides_followup(
        "tolong buatkan slide google slides tentang laporan kas", steps
    )
    assert needed is True
    assert presentation_id == "pres123abc"


def test_slides_followup_not_needed_after_batch_update() -> None:
    steps = [
        {"tool": "create_presentation", "result": "- Presentation ID: pres123abc"},
        {
            "tool": "batch_update_presentation",
            "args": {
                "requests": [
                    {"createShape": {"objectId": "slide1_title"}},
                    {"insertText": {"objectId": "slide1_title", "text": "Laporan Kas"}},
                ]
            },
            "result": (
                "Batch Update Completed for user@example.com:\n"
                "- Presentation ID: pres123abc\n"
                "- Requests Applied: 8"
            ),
        },
    ]
    needed, presentation_id = _needs_google_slides_followup(
        "tolong buatkan slide google slides tentang laporan kas", steps
    )
    assert needed is False
    assert presentation_id == "pres123abc"


def test_slides_followup_needed_when_requested_slide_count_not_reached() -> None:
    steps = [
        {
            "tool": "create_presentation",
            "result": (
                "Presentation Created Successfully for user@example.com:\n"
                "- Presentation ID: pres123abc\n"
                "- Slides: 1 slide(s) created"
            ),
        },
        {
            "tool": "batch_update_presentation",
            "args": {
                "requests": [
                    {"createShape": {"objectId": "slide1_title"}},
                    {"insertText": {"objectId": "slide1_title", "text": "Olahraga Pagi"}},
                ]
            },
            "result": "- Presentation ID: pres123abc\n- Requests Applied: 2",
        },
        {
            "tool": "get_presentation",
            "result": (
                "Presentation Details for user@example.com:\n"
                "- Presentation ID: pres123abc\n"
                "- Total Slides: 1\n"
                "Slides Breakdown:\n"
                "  Slide 1: ID p, 2 element(s), text:\n"
                "    > Olahraga Pagi"
            ),
        },
    ]
    needed, presentation_id = _needs_google_slides_followup(
        "buatkan Google Slides 2 halaman tentang olahraga pagi", steps
    )
    assert needed is True
    assert presentation_id == "pres123abc"


def test_slides_followup_still_needed_after_batch_update_without_text() -> None:
    steps = [
        {"tool": "create_presentation", "result": "- Presentation ID: pres123abc"},
        {
            "tool": "batch_update_presentation",
            "args": {
                "requests": [
                    {"createSlide": {"objectId": "slide2"}},
                    {"createShape": {"objectId": "slide2_box"}},
                ]
            },
            "result": (
                "Batch Update Completed for user@example.com:\n"
                "- Presentation ID: pres123abc\n"
                "- Requests Applied: 2"
            ),
        },
    ]
    needed, presentation_id = _needs_google_slides_followup(
        "tolong buatkan slide google slides tentang laporan kas", steps
    )
    assert needed is True
    assert presentation_id == "pres123abc"


def test_slides_followup_not_needed_when_get_presentation_has_text() -> None:
    steps = [
        {"tool": "create_presentation", "result": "- Presentation ID: pres123abc"},
        {
            "tool": "get_presentation",
            "result": (
                "Presentation Details for user@example.com:\n"
                "- Presentation ID: pres123abc\n"
                "Slides Breakdown:\n"
                "  Slide 1: ID p, 2 element(s), text: \n"
                "    > Laporan Kas"
            ),
        },
    ]
    needed, presentation_id = _needs_google_slides_followup(
        "tolong buatkan slide google slides tentang laporan kas", steps
    )
    assert needed is False
    assert presentation_id == "pres123abc"


def test_slides_notice_requires_content_after_create_presentation() -> None:
    notice = build_google_mcp_usage_notice("tolong buatkan 4 slide presentasi laporan kas").lower()
    assert "create_presentation hanya membuat file kosong" in notice
    assert "batch_update_presentation" in notice
    assert "get_presentation" in notice
    assert "inserttext ke shape" in notice


def test_slides_followup_directive_populates_existing_presentation() -> None:
    directive = google_slides_followup_directive(
        "pres123abc", "tolong buatkan 4 slide presentasi laporan kas"
    ).lower()
    assert "presentation_id=pres123abc" in directive
    assert "get_presentation" in directive
    assert "batch_update_presentation" in directive
    assert "create_presentation" not in directive
    assert "text tidak kosong" in directive


def test_google_forms_authoring_intent_detected() -> None:
    assert _is_google_forms_authoring_intent("tolong bikin google form survei skripsi") is True


def test_google_forms_authoring_intent_for_link_request() -> None:
    assert _is_google_forms_authoring_intent("mana link google formnya? gunakan mcp tool") is True


def test_google_forms_notice_rejects_placeholder_questions() -> None:
    notice = build_google_mcp_usage_notice("tolong buatkan google form survei skripsi")
    lowered = notice.lower()
    assert "create_survey_form" in lowered
    assert "placeholder" in lowered
    assert "pertanyaan 1" in lowered
    assert "options" in lowered


def test_empty_form_question_dicts_are_rejected() -> None:
    assert _needs_generated_form_questions([{}, {}, {}, {}]) is True
    assert _needs_generated_form_questions([
        {"title": "Pertanyaan 1"},
        {"title": "Question 2"},
    ]) is True


def test_default_form_questions_are_meaningful() -> None:
    questions = build_default_form_questions(title="Efektifitas Bakar-bakar saat demo")
    assert len(questions) >= 5
    assert all(q.get("title") for q in questions)
    assert not any(q["title"].lower().startswith("pertanyaan ") for q in questions)
    assert any(q.get("options") for q in questions if q.get("type") == "multiple_choice")
