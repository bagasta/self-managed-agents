from app.core.engine.agent_runner import _build_google_mcp_validation_reply
from app.core.engine.google_mcp_support import _normalize_slides_batch_requests


def test_slides_invalid_page_target_reply_is_user_friendly() -> None:
    err = (
        "Error calling tool 'batch_update_presentation': Invalid Slides batch update request: "
        "requests[3].insertText.objectId='p' targets a slide/page object."
    )
    reply = _build_google_mcp_validation_reply(err)
    lowered = reply.lower()
    assert "google slides" in lowered
    assert "shape" in lowered or "text box" in lowered
    assert "belum berhasil" in lowered


def test_slides_invalid_dimension_reply_is_user_friendly() -> None:
    err = (
        "Error calling tool 'batch_update_presentation': API error in batch_update_presentation: "
        "Invalid value at 'requests[4].create_shape.element_properties.size.width' "
        "(type.googleapis.com/google.apps.slides.v1.Dimension), 6000000"
    )
    reply = _build_google_mcp_validation_reply(err)
    lowered = reply.lower()
    assert "google slides" in lowered
    assert "ukuran" in lowered or "dimensi" in lowered
    assert "pt" in lowered
    assert "belum berhasil" in lowered


def test_slides_unit_unspecified_dimension_reply_is_user_friendly() -> None:
    err = (
        "Error calling tool 'batch_update_presentation': API error in batch_update_presentation: "
        "Invalid requests[2].createShape: Unknown dimension unit UNIT_UNSPECIFIED"
    )
    reply = _build_google_mcp_validation_reply(err)
    lowered = reply.lower()
    assert "google slides" in lowered
    assert "ukuran" in lowered or "dimensi" in lowered
    assert "pt" in lowered


def test_slides_batch_requests_add_pt_units_to_create_shape() -> None:
    requests = [
        {
            "createShape": {
                "objectId": "title_box",
                "shapeType": "TEXT_BOX",
                "elementProperties": {
                    "pageObjectId": "slide_1",
                    "size": {
                        "width": {"magnitude": 420},
                        "height": {"magnitude": 60, "unit": "UNIT_UNSPECIFIED"},
                    },
                    "transform": {
                        "scaleX": 1,
                        "scaleY": 1,
                        "translateX": 40,
                        "translateY": 30,
                    },
                },
            }
        }
    ]
    normalized = _normalize_slides_batch_requests(requests)
    props = normalized[0]["createShape"]["elementProperties"]
    assert props["size"]["width"]["unit"] == "PT"
    assert props["size"]["height"]["unit"] == "PT"
    assert props["transform"]["unit"] == "PT"
    assert "unit" not in requests[0]["createShape"]["elementProperties"]["size"]["width"]


def test_slides_batch_requests_recursively_normalize_nested_dimensions() -> None:
    requests = [
        {
            "createShape": {
                "objectId": "title_box",
                "shapeType": "TEXT_BOX",
                "elementProperties": {
                    "pageObjectId": "slide_1",
                    "size": {
                        "width": {"magnitude": 420, "unit": "UNIT_UNSPECIFIED"},
                        "height": {"magnitude": 60},
                    },
                },
                "customWrapper": {
                    "nestedSize": {"magnitude": 88},
                    "nestedList": [{"magnitude": 12, "unit": ""}],
                },
            }
        }
    ]
    normalized = _normalize_slides_batch_requests(requests)
    props = normalized[0]["createShape"]["elementProperties"]
    assert props["size"]["width"]["unit"] == "PT"
    assert props["size"]["height"]["unit"] == "PT"
    assert normalized[0]["createShape"]["customWrapper"]["nestedSize"]["unit"] == "PT"
    assert normalized[0]["createShape"]["customWrapper"]["nestedList"][0]["unit"] == "PT"


def test_slides_batch_requests_normalize_placeholder_shape_types() -> None:
    requests = [
        {
            "createShape": {
                "objectId": "slide1_title",
                "shapeType": "TITLE",
                "elementProperties": {
                    "pageObjectId": "slide_1",
                    "size": {
                        "width": {"magnitude": 420},
                        "height": {"magnitude": 60},
                    },
                    "transform": {
                        "scaleX": 1,
                        "scaleY": 1,
                        "translateX": 40,
                        "translateY": 30,
                    },
                },
            }
        }
    ]

    normalized = _normalize_slides_batch_requests(requests)
    create_shape = normalized[0]["createShape"]
    assert create_shape["shapeType"] == "TEXT_BOX"
    assert create_shape["objectId"].startswith("slide1_title_")
    assert create_shape["elementProperties"]["size"]["width"]["unit"] == "PT"
    assert create_shape["elementProperties"]["transform"]["unit"] == "PT"
    assert requests[0]["createShape"]["shapeType"] == "TITLE"


def test_slides_batch_requests_make_created_ids_unique_and_rewrite_refs() -> None:
    requests = [
        {"createSlide": {"objectId": "slide2"}},
        {
            "createShape": {
                "objectId": "slide2_title",
                "shapeType": "TEXT_BOX",
                "elementProperties": {
                    "pageObjectId": "slide2",
                    "size": {"width": {"magnitude": 420}, "height": {"magnitude": 60}},
                },
            }
        },
        {"insertText": {"objectId": "slide2_title", "text": "Judul"}},
    ]

    normalized = _normalize_slides_batch_requests(requests)

    slide_id = normalized[0]["createSlide"]["objectId"]
    title_id = normalized[1]["createShape"]["objectId"]
    assert slide_id.startswith("slide2_")
    assert title_id.startswith("slide2_title_")
    assert normalized[1]["createShape"]["elementProperties"]["pageObjectId"] == slide_id
    assert normalized[2]["insertText"]["objectId"] == title_id
    assert requests[0]["createSlide"]["objectId"] == "slide2"


def test_forms_create_form_title_only_reply_is_user_friendly() -> None:
    err = (
        "Error calling tool 'create_form': API error in create_form: "
        "Only info.title can be set when creating a form. To add items and change settings, use batchUpdate."
    )
    reply = _build_google_mcp_validation_reply(err)
    lowered = reply.lower()
    assert "google form" in lowered or "google forms" in lowered
    assert "title" in lowered
    assert "batchupdate" in lowered or "update" in lowered
    assert "belum berhasil" in lowered


def test_forms_batch_update_missing_requests_reply_is_user_friendly() -> None:
    err = (
        "1 validation error for call[batch_update_form] requests "
        "Missing required argument [type=missing_argument]"
    )
    reply = _build_google_mcp_validation_reply(err)
    lowered = reply.lower()
    assert "google form" in lowered or "google forms" in lowered
    assert "requests" in lowered
    assert "updateforminfo" in lowered or "createitem" in lowered


def test_forms_batch_update_empty_request_kind_reply_is_user_friendly() -> None:
    err = (
        "Error calling tool 'batch_update_form': API error in batch_update_form: "
        "<HttpError 400 returned \"request kind was not provided\". "
        "Details: \"request kind was not provided\">"
    )
    reply = _build_google_mcp_validation_reply(err)
    lowered = reply.lower()
    assert "google form" in lowered
    assert "objek kosong" in lowered or "request kosong" in lowered
    assert "create_survey_form" in lowered
