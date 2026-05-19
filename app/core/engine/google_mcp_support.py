"""Google Workspace MCP helpers used by agent_runner.

This module keeps Google-specific intent detection, auth-link generation, and
user-facing fallback replies out of the main orchestration flow.
"""
from __future__ import annotations

import copy
import re
import os
import uuid
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool, tool
from langchain_openai import ChatOpenAI


@dataclass
class GoogleMcpRuntime:
    enabled: bool
    workspace_server: dict[str, Any] | None
    connected_user_id: str | None
    auth_url: str | None
    preflight_error: str | None
    integration_url: str
    candidate_user_ids: list[str]
    system_prompt: Any


def _is_google_mcp_intent(message: str) -> bool:
    if not message:
        return False
    m = message.lower()
    keywords = (
        "google sheet", "spreadsheet", "gmail", "calendar", "drive", "docs", "sheets",
        "slides", "presentation", "google slides", "forms", "tasks", "contacts", "chat",
        "edit sheet", "update sheet", "buka sheet", "ubah sheet", "google workspace",
    )
    return any(k in m for k in keywords)


def _is_google_auth_or_scope_error(error_text: str) -> bool:
    if not error_text:
        return False
    e = error_text.lower()
    markers = (
        "401 unauthorized",
        "invalid_token",
        "token expired",
        "oauth credentials lack required scopes",
        "required scopes",
        "insufficient scope",
        "insufficient authentication scopes",
        "request had insufficient authentication scopes",
        "permission_denied",
        "insufficientpermissions",
        "access_denied",
        "googleapis.com/auth/",
    )
    return any(m in e for m in markers)


def _extract_google_mcp_step_error(steps: list[dict[str, Any]]) -> str | None:
    for step in steps:
        tool_name = str((step or {}).get("tool", "")).lower()
        result = str((step or {}).get("result", "") or "")
        if not tool_name or not result:
            continue
        if not (
            tool_name.startswith("search_gmail")
            or tool_name.startswith("get_calendar")
            or tool_name.startswith("create_calendar")
            or tool_name.startswith("drive_")
            or tool_name.startswith("docs_")
            or tool_name.startswith("sheets_")
            or tool_name.startswith("slides_")
            or tool_name.startswith("forms_")
            or "google" in tool_name
            or "sheet" in tool_name
            or "gmail" in tool_name
            or "calendar" in tool_name
            or "drive" in tool_name
            or "slides" in tool_name
            or "presentation" in tool_name
            or "docs" in tool_name
        ):
            continue
        if _is_google_auth_or_scope_error(result):
            return result
    return None


def _looks_like_progress_claim(reply_text: str) -> bool:
    if not reply_text:
        return False
    t = reply_text.lower()
    markers = (
        "lagi proses",
        "sedang proses",
        "on progress",
        "sebentar lagi",
        "akan saya kirim",
        "akan gue kirim",
        "begitu selesai",
        "processing",
        "working on",
    )
    return any(m in t for m in markers)


def _extract_requested_slide_count(message: str) -> int | None:
    if not message:
        return None
    m = re.search(r"\b(\d{1,2})\s*slide\b", message.lower())
    if not m:
        return None
    try:
        n = int(m.group(1))
    except Exception:
        return None
    if 1 <= n <= 12:
        return n
    return None


def _is_google_slides_relayout_intent(message: str) -> bool:
    if not message:
        return False
    m = message.lower()
    slides_markers = (
        "slide",
        "slides",
        "presentasi",
        "presentation",
    )
    relayout_markers = (
        "rapih",
        "rapikan",
        "rapihkan",
        "jadikan",
        "layout",
        "restructure",
        "susun ulang",
        "bikin",
        "buat",
    )
    return any(k in m for k in slides_markers) and any(k in m for k in relayout_markers)


def _is_google_forms_authoring_intent(message: str) -> bool:
    if not message:
        return False
    m = message.lower()
    forms_markers = (
        "google form",
        "google forms",
        "form",
        "survei",
        "survey",
        "kuesioner",
        "kuisioner",
        "questionnaire",
    )
    authoring_markers = (
        "bikin",
        "buat",
        "isi",
        "pertanyaan",
        "question",
        "kirim link",
        "link",
        "mcp",
    )
    return any(k in m for k in forms_markers) and any(k in m for k in authoring_markers)


def _is_google_sheets_authoring_intent(message: str) -> bool:
    if not message:
        return False
    m = message.lower()
    sheet_markers = (
        "google sheet",
        "google sheets",
        "spreadsheet",
        "sheet",
        "sheets",
        "excel",
        "xlsx",
        "tabel",
        "table",
        "rumus",
        "formula",
    )
    authoring_markers = (
        "bikin",
        "buat",
        "generate",
        "isi",
        "edit",
        "ubah",
        "update",
        "tambah",
        "masukkan",
        "laporan",
        "rekap",
        "budget",
        "anggaran",
        "jadwal",
        "tracker",
        "invoice",
        "rumus",
        "formula",
        "tabel",
        "table",
    )
    if not any(k in m for k in sheet_markers):
        return False
    if _is_blank_spreadsheet_only_intent(m):
        return False
    return any(k in m for k in authoring_markers)


def _is_blank_spreadsheet_only_intent(message_lower: str) -> bool:
    blank_markers = (
        "kosong",
        "blank",
        "empty",
        "file aja",
        "file saja",
        "spreadsheet aja",
        "spreadsheet saja",
        "sheet aja",
        "sheet saja",
        "tanpa isi",
        "tanpa tabel",
    )
    return any(k in message_lower for k in blank_markers)


def _extract_form_id_from_text(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"Form ID:\s*([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"/forms/d/([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    return None


def _extract_spreadsheet_id_from_text(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"\bID:\s*([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"\bspreadsheet\s+([A-Za-z0-9_-]{12,})\b", text, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _extract_presentation_id_from_text(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"\bPresentation ID:\s*([A-Za-z0-9_-]+)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"/presentation/d/([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"\bpresentation\s+([A-Za-z0-9_-]{12,})\b", text, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _fallback_unqualified_sheet_range(range_name: str) -> str | None:
    if not range_name or "!" not in range_name:
        return None

    sheet_name, cell_range = range_name.split("!", 1)
    sheet_name = sheet_name.strip().strip("'").strip('"').lower().replace(" ", "")
    cell_range = cell_range.strip()
    if not cell_range:
        return None
    if sheet_name in {"sheet1", "lembar1"}:
        return cell_range
    return None


_SLIDES_ELEMENT_PROPERTY_REQUESTS = {
    "createShape",
    "createImage",
    "createVideo",
    "createLine",
    "createTable",
    "createSheetsChart",
}

_SLIDES_VALID_SHAPE_TYPES = {
    "TEXT_BOX",
    "RECTANGLE",
    "ROUND_RECTANGLE",
    "ELLIPSE",
    "ARC",
    "BENT_ARROW",
    "BENT_UP_ARROW",
    "BEVEL",
    "BLOCK_ARC",
    "BRACE_PAIR",
    "BRACKET_PAIR",
    "CAN",
    "CHART",
    "CHEVRON",
    "CLOUD",
    "CORNER",
    "CUBE",
    "CURVED_DOWN_ARROW",
    "CURVED_LEFT_ARROW",
    "CURVED_RIGHT_ARROW",
    "CURVED_UP_ARROW",
    "DECAGON",
    "DIAMOND",
    "DOWN_ARROW",
    "ELLIPSE",
    "FOLDED_CORNER",
    "FRAME",
    "HEART",
    "HEXAGON",
    "HOME_PLATE",
    "HORIZONTAL_SCROLL",
    "LEFT_ARROW",
    "LEFT_BRACE",
    "LEFT_BRACKET",
    "LEFT_RIGHT_ARROW",
    "LEFT_CIRCULAR_ARROW",
    "LEFT_RIGHT_UP_ARROW",
    "LEFT_UP_ARROW",
    "LIGHTNING_BOLT",
    "LINE",
    "MOON",
    "NO_SMOKING",
    "NOTCHED_RIGHT_ARROW",
    "OCTAGON",
    "PARALLELOGRAM",
    "PENTAGON",
    "PIE",
    "PLAQUE",
    "PLUS",
    "QUAD_ARROW",
    "QUAD_ARROW_CALLOUT",
    "RIBBON",
    "RIBBON_2",
    "RIGHT_ARROW",
    "RIGHT_BRACE",
    "RIGHT_BRACKET",
    "ROUND_1_RECTANGLE",
    "ROUND_2_DIAGONAL_RECTANGLE",
    "ROUND_2_SAME_RECTANGLE",
    "RT_TRIANGLE",
    "SMILEY_FACE",
    "SNIP_1_RECTANGLE",
    "SNIP_2_DIAGONAL_RECTANGLE",
    "SNIP_2_SAME_RECTANGLE",
    "SNIP_ROUND_RECTANGLE",
    "STAR_10",
    "STAR_12",
    "STAR_16",
    "STAR_24",
    "STAR_32",
    "STAR_4",
    "STAR_5",
    "STAR_6",
    "STAR_7",
    "STAR_8",
    "STRIPED_RIGHT_ARROW",
    "SUN",
    "TRAPEZOID",
    "TRIANGLE",
    "UP_ARROW",
    "UP_DOWN_ARROW",
    "UTURN_ARROW",
    "WAVE",
    "WEDGE_ELLIPSE_CALLOUT",
    "WEDGE_RECTANGLE_CALLOUT",
    "WEDGE_ROUND_RECTANGLE_CALLOUT",
}


def _normalize_slides_batch_requests(requests: Any) -> Any:
    if not isinstance(requests, list):
        return requests

    normalized = copy.deepcopy(requests)
    for request in normalized:
        _normalize_slides_request(request)

    return normalized


def _normalize_slides_request(request: Any) -> None:
    if not isinstance(request, dict):
        return

    _normalize_slides_structure(request)

    for request_type in _SLIDES_ELEMENT_PROPERTY_REQUESTS:
        payload = request.get(request_type)
        if not isinstance(payload, dict):
            continue
        element_properties = payload.get("elementProperties")
        if isinstance(element_properties, dict):
            _normalize_slides_element_properties(element_properties)

    payload = request.get("updatePageElementTransform")
    if isinstance(payload, dict):
        transform = payload.get("transform")
        if isinstance(transform, dict):
            _ensure_slides_transform_unit(transform)


def _normalize_slides_structure(value: Any) -> None:
    if isinstance(value, dict):
        _ensure_slides_dimension_unit(value)
        _ensure_slides_transform_unit(value)
        _normalize_slides_shape_type(value)
        for nested_value in value.values():
            _normalize_slides_structure(nested_value)
    elif isinstance(value, list):
        for nested_value in value:
            _normalize_slides_structure(nested_value)


def _normalize_slides_element_properties(element_properties: dict[str, Any]) -> None:
    size = element_properties.get("size")
    if isinstance(size, dict):
        for key in ("width", "height"):
            _ensure_slides_dimension_unit(size.get(key))

    transform = element_properties.get("transform")
    if isinstance(transform, dict):
        _ensure_slides_transform_unit(transform)


def _ensure_slides_dimension_unit(dimension: Any) -> None:
    if not isinstance(dimension, dict):
        return
    if "magnitude" not in dimension:
        return
    if not dimension.get("unit") or str(dimension.get("unit")).upper() == "UNIT_UNSPECIFIED":
        dimension["unit"] = "PT"


def _ensure_slides_transform_unit(transform: dict[str, Any]) -> None:
    transform_keys = {"scaleX", "scaleY", "shearX", "shearY", "translateX", "translateY"}
    if not any(key in transform for key in transform_keys):
        return
    if not transform.get("unit") or str(transform.get("unit")).upper() == "UNIT_UNSPECIFIED":
        transform["unit"] = "PT"


def _normalize_slides_shape_type(payload: dict[str, Any]) -> None:
    for key in ("shape_type", "shapeType"):
        if key not in payload:
            continue
        shape_type = payload.get(key)
        if not isinstance(shape_type, str):
            continue
        normalized = shape_type.strip().upper()
        if normalized in _SLIDES_VALID_SHAPE_TYPES:
            payload[key] = normalized
            return
        if any(marker in normalized for marker in ("TITLE", "BODY", "SUBTITLE", "PLACEHOLDER", "TEXT")):
            payload[key] = "TEXT_BOX"
            return
        payload[key] = normalized
        return


def _slides_batch_args_have_text_write(args: Any) -> bool:
    if not isinstance(args, dict):
        return False

    requests = args.get("requests")
    if not isinstance(requests, list):
        return False

    for request in requests:
        if not isinstance(request, dict):
            continue
        insert_text = request.get("insertText")
        if isinstance(insert_text, dict) and str(insert_text.get("text") or "").strip():
            return True

        replace_text = request.get("replaceAllText")
        if isinstance(replace_text, dict) and str(replace_text.get("replaceText") or "").strip():
            return True

    return False


def _presentation_result_has_non_empty_text(result: str) -> bool:
    if not result:
        return False
    lowered = result.lower()
    if "text:" not in lowered:
        return False
    non_empty_lines = [
        line
        for line in lowered.splitlines()
        if "text:" in line and "text: empty" not in line
    ]
    return bool(non_empty_lines) or "\n    > " in result


def _needs_google_forms_followup(user_message: str, steps: list[dict[str, Any]]) -> tuple[bool, str | None]:
    if not _is_google_forms_authoring_intent(user_message):
        return False, None
    saw_create = False
    saw_batch = False
    saw_get = False
    form_id: str | None = None
    for step in steps or []:
        tool_name = str((step or {}).get("tool", "") or "").lower()
        result = str((step or {}).get("result", "") or "")
        if tool_name == "create_form":
            saw_create = True
            form_id = form_id or _extract_form_id_from_text(result)
        elif tool_name == "batch_update_form":
            saw_batch = True
        elif tool_name == "get_form":
            saw_get = True
            form_id = form_id or _extract_form_id_from_text(result)
    return (saw_create and (not saw_batch or not saw_get)), form_id


def _needs_google_sheets_followup(user_message: str, steps: list[dict[str, Any]]) -> tuple[bool, str | None]:
    if not _is_google_sheets_authoring_intent(user_message):
        return False, None

    saw_create_spreadsheet = False
    saw_content_write = False
    spreadsheet_id: str | None = None
    for step in steps or []:
        tool_name = str((step or {}).get("tool", "") or "").lower()
        result = str((step or {}).get("result", "") or "")
        if tool_name == "create_spreadsheet":
            saw_create_spreadsheet = True
            spreadsheet_id = spreadsheet_id or _extract_spreadsheet_id_from_text(result)
        elif tool_name in {"modify_sheet_values", "append_table_rows"}:
            saw_content_write = True
            spreadsheet_id = spreadsheet_id or _extract_spreadsheet_id_from_text(result)
        elif (
            "sheet" in tool_name
            and any(marker in tool_name for marker in ("write", "update_values", "append"))
        ):
            saw_content_write = True
            spreadsheet_id = spreadsheet_id or _extract_spreadsheet_id_from_text(result)

    return (saw_create_spreadsheet and not saw_content_write), spreadsheet_id


def _needs_google_slides_followup(user_message: str, steps: list[dict[str, Any]]) -> tuple[bool, str | None]:
    if not _is_google_slides_relayout_intent(user_message):
        return False, None

    saw_create_presentation = False
    saw_content_update = False
    presentation_id: str | None = None
    for step in steps or []:
        tool_name = str((step or {}).get("tool", "") or "").lower()
        result = str((step or {}).get("result", "") or "")
        if tool_name == "create_presentation":
            saw_create_presentation = True
            presentation_id = presentation_id or _extract_presentation_id_from_text(result)
        elif tool_name == "batch_update_presentation":
            if _slides_batch_args_have_text_write((step or {}).get("args")):
                saw_content_update = True
            presentation_id = presentation_id or _extract_presentation_id_from_text(result)
        elif tool_name in {"get_presentation", "get_page"}:
            presentation_id = presentation_id or _extract_presentation_id_from_text(result)
            if _presentation_result_has_non_empty_text(result):
                saw_content_update = True

    return (saw_create_presentation and not saw_content_update), presentation_id


def _build_google_mcp_validation_reply(error_text: str) -> str:
    e = (error_text or "").lower()
    if "batch_update_presentation" in e and "requests" in e and "missing required argument" in e:
        return (
            "Maaf, edit Google Slides belum berhasil dijalankan karena format perintah editnya belum lengkap. "
            "Saya harus ambil struktur presentasinya dulu lalu kirim perubahan slide dalam format edit yang benar. "
            "Silakan coba lagi, sekarang agent sudah diarahkan untuk ambil struktur slide dulu sebelum mengedit."
        )
    if (
        "batch_update_presentation" in e
        and "invalid slides batch update request" in e
        and "inserttext.objectid" in e
    ):
        return (
            "Maaf, edit Google Slides belum berhasil karena teks diarahkan ke ID slide halaman, "
            "padahal insertText harus ke shape/text box. Saya perlu buat shape dulu lalu isi teks ke shape tersebut. "
            "Silakan coba lagi."
        )
    if (
        "batch_update_presentation" in e
        and ("invalid value" in e or "unknown dimension unit" in e or "unit_unspecified" in e)
        and "dimension" in e
        and ("create_shape" in e or "createshape" in e)
    ):
        return (
            "Maaf, edit Google Slides belum berhasil karena ukuran elemen slide tidak valid. "
            "Saya harus pakai size/transform dengan unit yang benar (PT) dan dimensi yang wajar, lalu kirim ulang editnya. "
            "Silakan coba lagi."
        )
    if (
        "batch_update_presentation" in e
        and "create_shape.shape_type" in e
        and "title" in e
    ):
        return (
            "Maaf, edit Google Slides belum berhasil karena tipe shape yang dipakai bukan tipe yang valid untuk createShape. "
            "Untuk judul dan isi teks, saya harus pakai shape text box dulu, bukan TITLE placeholder. "
            "Silakan coba lagi."
        )
    if (
        "error calling tool 'create_form'" in e
        and "only info.title can be set when creating a form" in e
    ):
        return (
            "Maaf, pembuatan Google Form belum berhasil karena saat create_form hanya field title yang boleh diisi. "
            "Saya harus buat form dulu dengan title saja, lalu isi deskripsi/pertanyaan lewat update lanjutan (batchUpdate). "
            "Silakan coba lagi."
        )
    if (
        "validation error for call[batch_update_form]" in e
        and "missing required argument" in e
        and "requests" in e
    ):
        return (
            "Maaf, pengisian Google Form belum berhasil karena format update belum menyertakan daftar requests. "
            "Saya harus kirim batch_update_form dengan requests yang berisi updateFormInfo dan createItem pertanyaan. "
            "Silakan coba lagi."
        )
    if (
        "error calling tool 'batch_update_form'" in e
        and "request kind was not provided" in e
    ):
        return (
            "Maaf, pengisian Google Form belum berhasil karena requests batch_update_form berisi objek kosong. "
            "Setiap request harus punya jenis operasi seperti updateFormInfo atau createItem. "
            "Untuk pembuatan form baru, lebih aman gunakan create_survey_form agar form dibuat dan diisi dalam satu langkah."
        )
    return (
        "Maaf, aksi Google Workspace belum berhasil dijalankan karena format input tool belum lengkap. "
        "Silakan coba lagi."
    )


def build_google_mcp_usage_notice(user_message: str) -> str:
    notice = "\n\n[SYSTEM NOTICE - GOOGLE WORKSPACE MCP USAGE]\n"
    notice += (
        "Saat memakai tool Google Workspace MCP, WAJIB ikuti schema tool secara persis. "
        "Jangan mengira-ngira nama argumen. Contoh penting: "
        "modify_sheet_values memakai argumen range_name (bukan range); "
        "draft_gmail_message.to/cc/bcc berupa string tunggal; "
        "manage_contact.emails/phones berupa list of objects; "
        "manage_event untuk update butuh event_id, dan jika mengubah waktu sertakan start_time serta end_time; "
        "UNTUK GOOGLE SLIDES: jangan pernah panggil batch_update_presentation tanpa requests; "
        "jika user minta edit slide, WAJIB panggil get_presentation dulu untuk ambil struktur slide/object; "
        "jangan insertText ke page/slide objectId (mis. 'p'), karena insertText hanya valid untuk shape atau table cell; "
        "buat shape/text box dulu (createShape) lalu insertText ke objectId shape tersebut, baru panggil batch_update_presentation; "
        "untuk title/body gunakan createShape.shape_type='TEXT_BOX', bukan 'TITLE' atau placeholder shape lain; "
        "untuk slide yang masih template/placeholder, bersihkan teks placeholder seperti 'Klik - tambahkan judul' dan 'Klik untuk menambahkan subjudul'; "
        "hindari menumpuk banyak teks di koordinat yang sama; gunakan maksimal 2-3 shape utama per slide (title, body kiri, body kanan atau subtitle), "
        "set ukuran dan posisi yang masuk akal, dan jika mengedit semua isi slide lebih aman membuat shape baru yang rapi daripada menulis ke elemen yang tidak jelas; "
        "untuk createShape WAJIB sertakan unit='PT' di elementProperties.size.width, elementProperties.size.height, dan elementProperties.transform; "
        "jangan biarkan unit kosong/UNIT_UNSPECIFIED. Gunakan ukuran konservatif yang valid (contoh title width 300-500 PT, body width 250-350 PT), "
        "hindari width/height ekstrem yang berisiko invalid dimension; "
        "UNTUK GOOGLE FORMS: jika tool create_survey_form tersedia dan user meminta membuat Google Form baru/survei, "
        "GUNAKAN create_survey_form sebagai pilihan utama karena tool itu membuat form, mengisi pertanyaan, dan mengambil link secara aman dalam satu langkah. "
        "Saat memakai create_survey_form, questions WAJIB berisi pertanyaan final yang spesifik dan relevan dengan topik user; "
        "JANGAN gunakan placeholder seperti 'Pertanyaan 1', 'Pertanyaan 2', 'Question 1', atau judul generik serupa. "
        "Setiap question minimal punya title yang bermakna, type (short_answer, paragraph, multiple_choice), required, dan options jika multiple_choice. "
        "Jika harus memakai create_form, create_form hanya boleh mengirim title. "
        "Jangan kirim description/document_title/items saat create_form. Setelah form jadi, lanjutkan isi deskripsi dan pertanyaan via batch_update_form. "
        "Untuk batch_update_form, JANGAN PERNAH kirim requests berupa [{}], objek kosong, atau list berisi request tanpa kind. "
        "Setiap item requests WAJIB punya tepat satu kind valid: updateFormInfo, createItem, updateItem, deleteItem, moveItem, atau updateSettings.\n"
    )
    notice += "[/SYSTEM NOTICE]\n"

    if _is_google_sheets_authoring_intent(user_message):
        notice += "\n\n[SYSTEM NOTICE - SHEETS WORKFLOW MODE]\n"
        notice += (
            "User meminta pembuatan atau pengeditan Google Sheets. create_spreadsheet hanya membuat file kosong; "
            "JANGAN berhenti setelah create_spreadsheet jika user meminta tabel, data, laporan, tracker, edit, rumus, atau formula. "
            "Workflow wajib untuk spreadsheet baru: "
            "(1) create_spreadsheet dengan title dan sheet_names bila perlu; "
            "(2) modify_sheet_values untuk mengisi header, baris data, dan formula dengan argumen spreadsheet_id, range_name, values, value_input_option='USER_ENTERED'; "
            "(3) format_sheet_range untuk header/angka bila tool tersedia; "
            "(4) resize_sheet_dimensions untuk freeze header dan auto-resize kolom bila tool tersedia; "
            "(5) read_sheet_values dengan include_formulas=True untuk verifikasi. "
            "modify_sheet_values memakai range_name, bukan range. "
            "Untuk spreadsheet baru tanpa sheet_names eksplisit, jangan hardcode Sheet1!A1:F10 karena nama tab default bisa berbeda per locale; pakai range tanpa nama sheet seperti A1:F10, atau ambil nama tab dari get_spreadsheet_info dulu. "
            "Untuk rumus, tulis formula sebagai string diawali '=' dan gunakan value_input_option='USER_ENTERED', contoh '=SUM(B2:B10)', '=AVERAGE(C2:C10)', '=IF(D2>=80,\"OK\",\"Review\")'. "
            "Jika user tidak memberi data lengkap, buat tabel template yang relevan dengan konteks user, berisi header siap pakai, beberapa baris contoh wajar, dan kolom formula yang menghitung total/rata-rata/status. "
            "Balasan final harus menyebut sheet sudah diisi dan formula apa yang dibuat, bukan hanya mengirim link file kosong."
        )
        notice += "\n[/SYSTEM NOTICE]\n"

    if _is_google_slides_relayout_intent(user_message):
        requested_slides = _extract_requested_slide_count(user_message) or 3
        notice += "\n\n[SYSTEM NOTICE - SLIDES TEMPLATE MODE]\n"
        notice += (
            f"User meminta pembuatan/perapihan Google Slides. Targetkan {requested_slides} slide yang rapi, ringkas, dan mudah dibaca. "
            "create_presentation hanya membuat file kosong; JANGAN berhenti setelah create_presentation jika user meminta dibuatkan slide/presentasi. "
            "Workflow wajib untuk presentasi baru: (1) create_presentation; (2) get_presentation untuk ambil slide ID awal; "
            "(3) batch_update_presentation untuk mengisi konten dengan createShape + insertText; (4) get_presentation lagi untuk verifikasi teks sudah ada. "
            "WAJIB gunakan pola: createSlide (jika perlu) -> createShape title/body -> insertText ke SHAPE saja. "
            "DILARANG insertText ke page/slide objectId. "
            "Untuk createShape title/body, shape_type harus TEXT_BOX; jangan gunakan TITLE/BODY placeholder type. "
            "Tiap slide maksimal 2-3 shape utama dan hindari overlap. "
            "Ringkas konten panjang menjadi poin inti; jangan dump semua paragraf mentah. "
            "Jika user hanya memberi topik, buat outline presentasi sendiri yang relevan: cover, 1-2 slide isi utama, dan penutup/rekomendasi. "
            "Jika elemen lama tidak jelas, buat shape baru dengan objectId unik yang eksplisit. "
            "Untuk createShape, selalu pakai size.unit='PT' dan transform.unit='PT' dengan nilai konservatif (hindari dimensi ekstrem). "
            "Saat user minta 'buatkan slide', 'rapihkan', atau 'jadikan N slide', task belum selesai sebelum batch_update_presentation berhasil membuat teks nyata di slide."
        )
        notice += "\n[/SYSTEM NOTICE]\n"

    if _is_google_forms_authoring_intent(user_message):
        notice += "\n\n[SYSTEM NOTICE - FORMS WORKFLOW MODE]\n"
        notice += (
            "User meminta pembuatan/pengisian Google Form. Jika tool create_survey_form tersedia, WAJIB prioritaskan create_survey_form untuk form baru. "
            "Isi argumen title, description, topic_hint, dan questions bila user memberi pertanyaan spesifik. "
            "Jika user tidak memberi daftar pertanyaan rinci, buatkan draft questions relevan minimal 5-8 pertanyaan sesuai konteks user, dengan tipe campuran seperlunya (short_answer, paragraph, multiple_choice). "
            "JANGAN buat title pertanyaan berupa placeholder seperti 'Pertanyaan 1', 'Pertanyaan 2', 'Question 1', atau sekadar nomor. "
            "Tulis title pertanyaan yang siap dibaca responden dan terkait langsung dengan topik. "
            "Untuk multiple_choice, isi options minimal 2-5 opsi bermakna. "
            "Contoh questions valid untuk create_survey_form: "
            "[{title:'Seberapa sering Anda mengikuti demonstrasi?',type:'multiple_choice',required:true,options:['Tidak pernah','Kadang-kadang','Sering']},"
            "{title:'Menurut Anda, apa dampak utama kegiatan tersebut?',type:'paragraph',required:false}]. "
            "Jika create_survey_form tidak tersedia, jalankan workflow manual end-to-end: "
            "(1) create_form dengan title saja; "
            "(2) batch_update_form dengan requests valid berisi updateFormInfo dan createItem pertanyaan; "
            "(3) get_form untuk verifikasi hasil dan ambil responder URL/edit URL; "
            "(4) balas user dengan link final. "
            "Saat user minta link, pastikan URL form dikirim di jawaban final dan jangan jawab normatif tanpa eksekusi tool. "
            "Untuk batch_update_form, requests tidak boleh kosong secara semantik: jangan pernah kirim [{}]. "
            "Contoh request valid: {updateFormInfo:{info:{description:'...'},updateMask:'description'}} atau "
            "{createItem:{item:{title:'Pertanyaan',questionItem:{question:{required:true,textQuestion:{}}}},location:{index:0}}}."
        )
        notice += "\n[/SYSTEM NOTICE]\n"

    return notice


def build_mcp_unavailable_notice(mcp_errors: dict[str, str], google_mcp_auth_url: str | None) -> str:
    notice = "\n\n[SYSTEM NOTICE - MCP TOOL UNAVAILABLE]\n"
    notice += (
        "HARD RULE: Jika tool MCP yang dibutuhkan user sedang unavailable, "
        "JANGAN pernah mengklaim pekerjaan sudah diproses, diupdate, sedang berjalan, atau selesai. "
        "JANGAN membuat janji seperti 'lagi diproses', 'sebentar lagi selesai', atau 'nanti saya kirim link'. "
        "Jawab secara jujur bahwa aksi belum dieksekusi.\n"
    )
    for server_name, error in mcp_errors.items():
        if "401" in error or "Unauthorized" in error:
            fallback = ""
            if server_name == "google_workspace" and google_mcp_auth_url:
                fallback = f"Jika tool gagal, fallback link ini: {google_mcp_auth_url}. "
            notice += (
                f"- {server_name}: Akun Google belum terhubung atau token tidak valid. "
                f"Panggil tool get_google_workspace_auth_link untuk mengambil link re-auth terbaru, "
                f"lalu jelaskan ke user dalam bahasa user secara natural. "
                f"{fallback}"
                f"JANGAN coba mencari file credential, token, "
                f"atau mengakses email/kalender dengan cara lain.\n"
            )
        else:
            notice += (
                f"- {server_name}: Koneksi gagal ({error[:100]}). "
                f"Beritahu user bahwa layanan ini sedang tidak tersedia.\n"
            )
    notice += "[/SYSTEM NOTICE]\n"
    return notice


def google_slides_dimension_retry_directive() -> str:
    return (
        "[SYSTEM RETRY DIRECTIVE - GOOGLE SLIDES DIMENSION]\n"
        "Perbaiki payload createShape sekarang juga.\n"
        "WAJIB: setiap createShape.elementProperties.size.width/height punya field magnitude + unit='PT'.\n"
        "WAJIB: createShape.elementProperties.transform.unit='PT'. Jangan biarkan unit kosong atau UNIT_UNSPECIFIED.\n"
        "WAJIB: createShape.shape_type untuk title/body harus TEXT_BOX, bukan TITLE atau placeholder lain.\n"
        "Gunakan dimensi konservatif valid (mis: title width 420PT height 60PT; body width 420PT height 220PT), hindari angka ekstrem.\n"
        "Jangan hapus semua slide sekaligus jika tidak perlu; fokus relayout aman.\n"
        "[/SYSTEM RETRY DIRECTIVE]"
    )


def google_slides_shape_retry_directive() -> str:
    return (
        "[SYSTEM RETRY DIRECTIVE - GOOGLE SLIDES]\n"
        "Perbaiki langkah edit Google Slides sekarang juga.\n"
        "WAJIB: jangan insertText ke page/slide objectId (contoh: 'p', 'slide2').\n"
        "Langkah benar: get_presentation/get_page -> identifikasi slide target -> createShape pada pageObjectId slide -> "
        "insertText ke objectId shape yang baru dibuat.\n"
        "Untuk shape title/body, gunakan createShape.shape_type='TEXT_BOX'.\n"
        "Untuk rapihkan konten jadi beberapa slide, buat shape title/body per slide dengan posisi tidak overlap.\n"
        "[/SYSTEM RETRY DIRECTIVE]"
    )


def google_slides_followup_directive(presentation_id: str, user_message: str) -> str:
    requested_slides = _extract_requested_slide_count(user_message) or 3
    return (
        "[SYSTEM FOLLOW-UP DIRECTIVE - GOOGLE SLIDES]\n"
        f"Presentation sudah dibuat dengan presentation_id={presentation_id}, tetapi kontennya belum dibuat. "
        "Lanjutkan SEKARANG juga sampai slide berisi teks nyata, bukan file kosong. "
        "WAJIB panggil get_presentation terlebih dahulu untuk mengambil slide ID yang ada. "
        "Lalu panggil batch_update_presentation dengan requests non-kosong untuk membuat konten. "
        f"Targetkan {requested_slides} slide total yang relevan dengan request user berikut: "
        f"{user_message[:500]}. "
        "Gunakan slide pertama yang sudah ada untuk cover atau pembuka; bila butuh slide tambahan, buat dengan createSlide. "
        "Untuk setiap slide, buat shape title/body dengan createShape, lalu insertText ke objectId shape tersebut. "
        "JANGAN insertText ke objectId slide/page. "
        "Untuk title/body shape, gunakan createShape.shape_type='TEXT_BOX'. "
        "Setiap createShape.elementProperties.size.width/height harus punya magnitude + unit='PT', dan transform.unit='PT'. "
        "Gunakan objectId unik yang eksplisit seperti slide1_title, slide1_body, slide2_title. "
        "Jika user tidak memberi materi rinci, buat outline presentasi yang wajar dari topik user: cover, poin utama, detail/analisis, dan penutup/rekomendasi sesuai jumlah slide. "
        "Setelah batch_update_presentation berhasil, WAJIB panggil get_presentation lagi untuk verifikasi bahwa setiap slide punya text tidak kosong. "
        "Balasan final HARUS berisi link Google Slides serta ringkasan isi tiap slide yang dibuat. "
        "[/SYSTEM FOLLOW-UP DIRECTIVE]"
    )


def google_forms_create_retry_directive() -> str:
    return (
        "[SYSTEM RETRY DIRECTIVE - GOOGLE FORMS]\n"
        "Perbaiki langkah pembuatan Google Form sekarang juga.\n"
        "WAJIB: saat create_form hanya kirim title saja.\n"
        "JANGAN kirim description/document_title/items/settings saat create_form.\n"
        "Setelah create_form berhasil, lanjutkan update deskripsi/pertanyaan dengan tool update/batchUpdate forms.\n"
        "[/SYSTEM RETRY DIRECTIVE]"
    )


def google_forms_request_kind_retry_directive() -> str:
    return (
        "[SYSTEM RETRY DIRECTIVE - GOOGLE FORMS REQUEST KIND]\n"
        "Perbaiki sekarang: batch_update_form gagal karena ada request kosong atau request tanpa kind.\n"
        "JANGAN panggil batch_update_form dengan requests=[{}] atau list berisi objek kosong.\n"
        "Jika tool create_survey_form tersedia dan task adalah membuat form baru, gunakan create_survey_form sekarang.\n"
        "Saat memakai create_survey_form, questions harus berisi pertanyaan spesifik sesuai topik user, bukan placeholder seperti 'Pertanyaan 1'.\n"
        "Jika harus batch_update_form, setiap request WAJIB punya satu kind valid: updateFormInfo atau createItem.\n"
        "Contoh updateFormInfo valid: {updateFormInfo:{info:{description:'...'},updateMask:'description'}}.\n"
        "Contoh createItem valid: {createItem:{item:{title:'Pertanyaan',questionItem:{question:{required:true,textQuestion:{}}}},location:{index:0}}}.\n"
        "Lanjutkan sampai get_form/link final berhasil.\n"
        "[/SYSTEM RETRY DIRECTIVE]"
    )


def google_forms_followup_directive(form_id: str) -> str:
    return (
        "[SYSTEM FOLLOW-UP DIRECTIVE - GOOGLE FORMS]\n"
        f"Form sudah dibuat dengan form_id={form_id}. "
        "Lanjutkan SEKARANG juga workflow yang belum selesai. "
        "WAJIB panggil batch_update_form DENGAN ARGUMEN requests (list non-kosong). "
        "JANGAN kirim requests=[{}] atau request kosong tanpa kind. "
        "JANGAN gunakan judul placeholder seperti 'Pertanyaan 1'; setiap createItem.item.title harus berupa pertanyaan final yang relevan dengan topik user. "
        "Contoh struktur minimal yang VALID untuk requests: "
        "[{updateFormInfo:{info:{description:'...'},updateMask:'description'}}, "
        "{createItem:{item:{title:'Pertanyaan 1',questionItem:{question:{required:true,textQuestion:{}}}},location:{index:0}}}] . "
        "Tambah minimal 5 createItem pertanyaan relevan jika user belum kasih daftar rinci. "
        "Setelah batch_update_form berhasil, WAJIB panggil get_form agar responder URL/edit URL terambil. "
        "Balasan final HARUS berisi link Google Form dan ringkasan pertanyaan yang ditambahkan. "
        "[/SYSTEM FOLLOW-UP DIRECTIVE]"
    )


def google_forms_followup_retry_directive() -> str:
    return (
        "[SYSTEM FOLLOW-UP RETRY DIRECTIVE - GOOGLE FORMS REQUESTS]\n"
        "Perbaiki sekarang: batch_update_form WAJIB menyertakan requests sebagai list non-kosong.\n"
        "JANGAN kirim [{}]; setiap request harus punya kind valid seperti updateFormInfo atau createItem.\n"
        "JANGAN gunakan title placeholder seperti 'Pertanyaan 1'. Tulis pertanyaan final yang bermakna dan relevan.\n"
        "Gunakan urutan: updateFormInfo(description) + minimal 5 createItem pertanyaan + get_form.\n"
        "Jangan panggil batch_update_form tanpa requests.\n"
        "[/SYSTEM FOLLOW-UP RETRY DIRECTIVE]"
    )


def google_sheets_followup_directive(spreadsheet_id: str, user_message: str) -> str:
    return (
        "[SYSTEM FOLLOW-UP DIRECTIVE - GOOGLE SHEETS]\n"
        f"Spreadsheet sudah dibuat dengan spreadsheet_id={spreadsheet_id}, tetapi isinya belum dibuat. "
        "Lanjutkan SEKARANG juga workflow spreadsheet sampai ada tabel/data/rumus yang benar. "
        "WAJIB panggil modify_sheet_values dengan argumen spreadsheet_id, range_name, values, dan value_input_option='USER_ENTERED'. "
        "JANGAN gunakan argumen bernama range; tool ini memakai range_name. "
        "Untuk file baru, pakai range_name tanpa nama sheet seperti A1:F10 kecuali kamu sudah tahu nama tab sebenarnya dari get_spreadsheet_info. "
        "JANGAN hardcode Sheet1!A1:F10 karena tab default bisa bernama berbeda dan memicu Unable to parse range. "
        "Buat tabel yang relevan dengan request user berikut: "
        f"{user_message[:500]}. "
        "Jika user tidak memberi data rinci, buat template praktis dengan header siap pakai, beberapa baris contoh, dan minimal satu kolom formula. "
        "Formula harus ditulis sebagai string diawali '=' agar Google Sheets menghitungnya, contoh '=SUM(B2:B10)', '=AVERAGE(C2:C10)', '=IF(D2>=80,\"OK\",\"Review\")'. "
        "Setelah values berhasil ditulis, rapikan dengan format_sheet_range untuk header dan resize_sheet_dimensions untuk freeze header/auto-resize kolom bila tool tersedia. "
        "Terakhir, panggil read_sheet_values dengan include_formulas=True untuk verifikasi. "
        "Balasan final HARUS berisi link spreadsheet serta ringkasan tabel dan formula yang dibuat. "
        "[/SYSTEM FOLLOW-UP DIRECTIVE]"
    )


async def _fetch_google_auth_link(
    *, integration_url: str, api_key: str, agent_id: uuid.UUID, candidate_user_ids: list[str]
) -> str | None:
    try:
        import httpx as _httpx

        async with _httpx.AsyncClient(timeout=8.0) as _hc:
            for candidate in candidate_user_ids:
                resp = await _hc.post(
                    f"{integration_url}/v1/integrations/google/connect",
                    json={"external_user_id": candidate, "agent_id": str(agent_id)},
                    headers={"X-API-Key": api_key},
                )
                if resp.status_code == 200:
                    data = resp.json() if resp.text else {}
                    auth_url = data.get("auth_url") or data.get("authorization_url")
                    if auth_url:
                        auth_url = str(auth_url)
                        if "/authorize" in auth_url:
                            return auth_url
    except Exception:
        return None
    return None


def _has_google_mcp_step(steps: list[dict[str, Any]]) -> bool:
    for step in steps:
        tool_name = str((step or {}).get("tool", "")).lower()
        if not tool_name:
            continue
        if (
            tool_name.startswith("search_gmail")
            or tool_name.startswith("get_calendar")
            or tool_name.startswith("create_calendar")
            or tool_name.startswith("drive_")
            or tool_name.startswith("docs_")
            or tool_name.startswith("sheets_")
            or "google" in tool_name
            or "sheet" in tool_name
            or "gmail" in tool_name
        ):
            return True
    return False


def _candidate_external_user_ids(primary: str | None, channel_user_phone: str | None) -> list[str]:
    vals: list[str] = []
    for raw in (primary, channel_user_phone):
        if not raw:
            continue
        s = str(raw).strip()
        if s:
            vals.append(s)

    candidates: list[str] = []
    seen: set[str] = set()
    for value in vals:
        variants = [value]
        if value.startswith("+"):
            variants.append(value[1:])
        if value.isdigit() and not value.startswith("+"):
            variants.append(f"+{value}")
            if value.startswith("62"):
                variants.append("0" + value[2:])
        if value.startswith("0") and value[1:].isdigit():
            variants.append("62" + value[1:])
            variants.append("+62" + value[1:])
        if "@" in value:
            variants.append(value.split("@", 1)[0])

        for variant in variants:
            key = variant.strip()
            if key and key not in seen:
                seen.add(key)
                candidates.append(key)
    return candidates


def _build_google_reauth_tool(
    *,
    integration_url: str,
    api_key: str,
    agent_id: uuid.UUID,
    candidate_user_ids: list[str],
) -> list:
    @tool
    async def get_google_workspace_auth_link() -> str:
        """Generate and return Google Workspace re-auth link for current user."""
        auth_url = await _fetch_google_auth_link(
            integration_url=integration_url,
            api_key=api_key,
            agent_id=agent_id,
            candidate_user_ids=candidate_user_ids,
        )
        if not auth_url:
            return "AUTH_LINK_UNAVAILABLE"
        return auth_url

    return [get_google_workspace_auth_link]


def sanitize_google_forms_tools(mcp_tools: list, log: Any) -> list:
    """Wrap Google Workspace tools to repair weak LLM payloads before MCP execution."""
    wrapped_tools: list = []
    for mcp_tool in mcp_tools:
        tool_name = getattr(mcp_tool, "name", "")
        if tool_name == "batch_update_presentation":
            async def _batch_update_presentation_guarded(_tool=mcp_tool, **kwargs):
                original_requests = kwargs.get("requests")
                normalized_requests = _normalize_slides_batch_requests(original_requests)
                if normalized_requests is not original_requests:
                    kwargs["requests"] = normalized_requests
                try:
                    return await _tool.ainvoke(kwargs)
                except Exception as exc:
                    err = str(exc).lower()
                    if (
                        "batch_update_presentation" in err
                        and (
                            "unknown dimension unit" in err
                            or "unit_unspecified" in err
                            or "invalid value" in err
                        )
                        and "dimension" in err
                    ):
                        retry_kwargs = dict(kwargs)
                        retry_kwargs["requests"] = _normalize_slides_batch_requests(
                            retry_kwargs.get("requests")
                        )
                        log.warning(
                            "agent_run.slides_dimension_retry_guard",
                            error=str(exc)[:300],
                        )
                        return await _tool.ainvoke(retry_kwargs)
                    raise

            wrapped_tools.append(
                StructuredTool.from_function(
                    coroutine=_batch_update_presentation_guarded,
                    name=mcp_tool.name,
                    description=getattr(mcp_tool, "description", None),
                    args_schema=getattr(mcp_tool, "args_schema", None),
                )
            )
            continue

        if tool_name == "create_shape":
            async def _create_shape_guarded(_tool=mcp_tool, **kwargs):
                normalized_kwargs = _normalize_create_shape_kwargs(kwargs)
                return await _tool.ainvoke(normalized_kwargs)

            wrapped_tools.append(
                StructuredTool.from_function(
                    coroutine=_create_shape_guarded,
                    name=mcp_tool.name,
                    description=getattr(mcp_tool, "description", None),
                    args_schema=getattr(mcp_tool, "args_schema", None),
                )
            )
            continue

        if tool_name == "modify_sheet_values":
            async def _modify_sheet_values_guarded(_tool=mcp_tool, **kwargs):
                range_name = str(kwargs.get("range_name") or "")
                try:
                    return await _tool.ainvoke(kwargs)
                except Exception as exc:
                    err = str(exc)
                    fallback_range = _fallback_unqualified_sheet_range(range_name)
                    if fallback_range and "unable to parse range" in err.lower():
                        retry_kwargs = dict(kwargs)
                        retry_kwargs["range_name"] = fallback_range
                        log.warning(
                            "agent_run.sheets_range_retry_unqualified",
                            original_range=range_name,
                            retry_range=fallback_range,
                        )
                        return await _tool.ainvoke(retry_kwargs)
                    raise

            wrapped_tools.append(
                StructuredTool.from_function(
                    coroutine=_modify_sheet_values_guarded,
                    name=mcp_tool.name,
                    description=getattr(mcp_tool, "description", None),
                    args_schema=getattr(mcp_tool, "args_schema", None),
                )
            )
            continue

        if tool_name != "create_survey_form":
            wrapped_tools.append(mcp_tool)
            continue

        async def _create_survey_form_guarded(_tool=mcp_tool, **kwargs):
            original_questions = kwargs.get("questions")
            if _needs_generated_form_questions(original_questions):
                kwargs["questions"] = build_default_form_questions(
                    title=str(kwargs.get("title") or ""),
                    description=str(kwargs.get("description") or ""),
                    topic_hint=str(kwargs.get("topic_hint") or ""),
                )
                log.warning(
                    "agent_run.forms_questions_autofilled",
                    tool="create_survey_form",
                    original_questions=original_questions,
                    generated=len(kwargs["questions"]),
                )
            return await _tool.ainvoke(kwargs)

        wrapped_tools.append(
            StructuredTool.from_function(
                coroutine=_create_survey_form_guarded,
                name=mcp_tool.name,
                description=getattr(mcp_tool, "description", None),
                args_schema=getattr(mcp_tool, "args_schema", None),
            )
        )
    return wrapped_tools


def _normalize_create_shape_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(kwargs)
    payload = normalized.get("shape_type")
    if isinstance(payload, str):
        normalized["shape_type"] = _normalize_slides_shape_type_value(payload)

    payload = normalized.get("shapeType")
    if isinstance(payload, str):
        normalized["shapeType"] = _normalize_slides_shape_type_value(payload)

    element_properties = normalized.get("elementProperties")
    if isinstance(element_properties, dict):
        _normalize_slides_element_properties(element_properties)

    return normalized


def _normalize_slides_shape_type_value(shape_type: str) -> str:
    normalized = shape_type.strip().upper()
    if normalized in _SLIDES_VALID_SHAPE_TYPES:
        return normalized
    if any(marker in normalized for marker in ("TITLE", "BODY", "SUBTITLE", "PLACEHOLDER", "TEXT")):
        return "TEXT_BOX"
    return normalized


def _needs_generated_form_questions(questions: Any) -> bool:
    if not isinstance(questions, list) or not questions:
        return False

    meaningful = 0
    blank_or_placeholder = 0
    for question in questions:
        if not isinstance(question, dict) or not question:
            blank_or_placeholder += 1
            continue
        title = str(question.get("title") or "").strip()
        if not title or _is_placeholder_question_title(title):
            blank_or_placeholder += 1
            continue
        meaningful += 1

    return meaningful < 3 or blank_or_placeholder > 0


def _is_placeholder_question_title(title: str) -> bool:
    normalized = title.strip().lower()
    return bool(re.fullmatch(r"(pertanyaan|question)\s*\d+", normalized))


def build_default_form_questions(
    *,
    title: str,
    description: str = "",
    topic_hint: str = "",
) -> list[dict[str, Any]]:
    topic = _derive_form_topic(title=title, description=description, topic_hint=topic_hint)
    return [
        {
            "title": "Nama atau inisial responden",
            "type": "short_answer",
            "required": False,
        },
        {
            "title": f"Apakah Anda pernah melihat atau terlibat langsung dalam {topic}?",
            "type": "multiple_choice",
            "required": True,
            "options": ["Ya, pernah langsung", "Pernah melihat dari jauh/media", "Tidak pernah"],
        },
        {
            "title": f"Seberapa sering {topic} terjadi dalam pengalaman atau pengamatan Anda?",
            "type": "multiple_choice",
            "required": True,
            "options": ["Sangat sering", "Cukup sering", "Jarang", "Tidak pernah"],
        },
        {
            "title": f"Menurut Anda, seberapa efektif {topic} dalam menarik perhatian publik?",
            "type": "multiple_choice",
            "required": True,
            "options": ["Sangat efektif", "Cukup efektif", "Kurang efektif", "Tidak efektif"],
        },
        {
            "title": f"Apa dampak positif yang Anda lihat dari {topic}?",
            "type": "paragraph",
            "required": False,
        },
        {
            "title": f"Apa risiko atau dampak negatif yang muncul dari {topic}?",
            "type": "paragraph",
            "required": False,
        },
        {
            "title": f"Bagaimana tanggapan masyarakat sekitar terhadap {topic}?",
            "type": "multiple_choice",
            "required": True,
            "options": ["Mendukung", "Netral", "Kurang mendukung", "Menolak", "Tidak tahu"],
        },
        {
            "title": f"Apa saran Anda agar kegiatan terkait {topic} lebih aman dan tetap efektif?",
            "type": "paragraph",
            "required": False,
        },
    ]


def _derive_form_topic(*, title: str, description: str, topic_hint: str) -> str:
    for raw in (topic_hint, title, description):
        text = str(raw or "").strip()
        if text:
            return text
    return "topik survei ini"


async def prepare_google_mcp_runtime(
    *,
    tools_config: dict[str, Any],
    tools: list,
    active_groups: list[str],
    session: Any,
    agent_id: uuid.UUID,
    memory_scope: str | None,
    api_key: str,
    user_message: str,
    system_prompt: Any,
    log: Any,
) -> GoogleMcpRuntime:
    mcp_cfg = tools_config.get("mcp", {})
    mcp_enabled = False
    workspace_server = None
    if isinstance(mcp_cfg, dict):
        has_wrapper = "enabled" in mcp_cfg or "servers" in mcp_cfg
        if has_wrapper:
            mcp_enabled = bool(mcp_cfg.get("enabled", bool(mcp_cfg.get("servers"))))
            servers = mcp_cfg.get("servers", {}) if isinstance(mcp_cfg.get("servers", {}), dict) else {}
            workspace_server = servers.get("google_workspace")
        else:
            workspace_server = mcp_cfg.get("google_workspace") if isinstance(mcp_cfg.get("google_workspace"), dict) else None
            mcp_enabled = bool(workspace_server)

    integration_url = os.environ.get("GOOGLE_INTEGRATION_SERVICE_URL", "http://localhost:8003")
    channel_cfg = session.channel_config if isinstance(session.channel_config, dict) else {}
    candidate_ids = _candidate_external_user_ids(memory_scope, channel_cfg.get("user_phone"))

    if mcp_enabled and workspace_server and candidate_ids:
        tools.extend(
            _build_google_reauth_tool(
                integration_url=integration_url,
                api_key=api_key,
                agent_id=agent_id,
                candidate_user_ids=candidate_ids,
            )
        )
        active_groups.append("google_reauth")

    connected_user_id: str | None = None
    auth_url: str | None = None
    preflight_error: str | None = None

    if mcp_enabled and workspace_server:
        try:
            import httpx as _httpx

            jwt = None
            jwt_external_user_id = None
            async with _httpx.AsyncClient(timeout=5.0) as http_client:
                for candidate in candidate_ids:
                    status_payload: dict[str, Any] | None = None
                    for agent_param in (str(agent_id), None):
                        params = {"external_user_id": candidate}
                        if agent_param:
                            params["agent_id"] = agent_param

                        status_resp = await http_client.get(
                            f"{integration_url}/v1/integrations/google/status",
                            params=params,
                            headers={"X-API-Key": api_key},
                        )
                        if status_resp.status_code == 200:
                            status_payload = status_resp.json() if status_resp.text else {}
                            if bool(status_payload.get("connected")):
                                connected_user_id = candidate
                                break

                    if not status_payload or not bool(status_payload.get("connected")):
                        connect_resp = await http_client.post(
                            f"{integration_url}/v1/integrations/google/connect",
                            json={"external_user_id": candidate, "agent_id": str(agent_id)},
                            headers={"X-API-Key": api_key},
                        )
                        if connect_resp.status_code == 200:
                            connect_data = connect_resp.json() if connect_resp.text else {}
                            auth_url = connect_data.get("auth_url") or connect_data.get("authorization_url")
                        preflight_error = "Google Workspace belum terhubung atau token sudah expired"
                        if connected_user_id is None:
                            connected_user_id = candidate
                        continue

                    for agent_param in (str(agent_id), None):
                        params = {"external_user_id": candidate}
                        if agent_param:
                            params["agent_id"] = agent_param
                        resp = await http_client.get(
                            f"{integration_url}/v1/integrations/google/token",
                            params=params,
                            headers={"X-API-Key": api_key},
                        )
                        if resp.status_code == 200:
                            jwt = resp.json().get("bearer_token")
                            jwt_external_user_id = candidate
                            break
                    if jwt:
                        break

                    connect_resp = await http_client.post(
                        f"{integration_url}/v1/integrations/google/connect",
                        json={"external_user_id": candidate, "agent_id": str(agent_id)},
                        headers={"X-API-Key": api_key},
                    )
                    if connect_resp.status_code == 200:
                        connect_data = connect_resp.json() if connect_resp.text else {}
                        auth_url = connect_data.get("auth_url") or connect_data.get("authorization_url")
                    preflight_error = "Google Workspace belum terhubung atau token sudah expired"

            if jwt:
                workspace_server.setdefault("headers", {})["Authorization"] = f"Bearer {jwt}"
                connected_user_id = jwt_external_user_id
                log.info("agent_run.google_mcp_token_injected", external_user_id=jwt_external_user_id)
            elif candidate_ids:
                log.info("agent_run.google_mcp_not_connected", external_user_ids=candidate_ids)
            else:
                log.info("agent_run.google_mcp_missing_external_user_id")
        except Exception as err:
            log.warning("agent_run.google_mcp_token_error", error=str(err))

    if mcp_enabled and workspace_server and isinstance(system_prompt, str):
        system_prompt += build_google_mcp_usage_notice(user_message)

    return GoogleMcpRuntime(
        enabled=mcp_enabled,
        workspace_server=workspace_server,
        connected_user_id=connected_user_id,
        auth_url=auth_url,
        preflight_error=preflight_error,
        integration_url=integration_url,
        candidate_user_ids=candidate_ids,
        system_prompt=system_prompt,
    )


async def apply_mcp_error_notice(
    *,
    mcp_errors: dict[str, str],
    runtime: GoogleMcpRuntime,
    agent_id: uuid.UUID,
    memory_scope: str | None,
    api_key: str,
    system_prompt: Any,
    log: Any,
) -> tuple[str | None, Any]:
    auth_url = runtime.auth_url
    google_mcp_err = str(mcp_errors.get("google_workspace", ""))
    if google_mcp_err and ("401" in google_mcp_err or "Unauthorized" in google_mcp_err):
        reauth_user = runtime.connected_user_id or memory_scope
        if reauth_user:
            try:
                import httpx as _httpx

                async with _httpx.AsyncClient(timeout=5.0) as http_client:
                    resp = await http_client.post(
                        f"{runtime.integration_url}/v1/integrations/google/connect",
                        json={"external_user_id": reauth_user, "agent_id": str(agent_id)},
                        headers={"X-API-Key": api_key},
                    )
                if resp.status_code == 200:
                    data = resp.json() if resp.text else {}
                    auth_url = data.get("auth_url") or data.get("authorization_url")
            except Exception as err:
                log.warning("agent_run.google_mcp_reauth_link_error", error=str(err))

    if isinstance(system_prompt, str):
        system_prompt += build_mcp_unavailable_notice(mcp_errors, auth_url)
    return auth_url, system_prompt


async def apply_google_mcp_reply_overrides(
    *,
    final_reply: str,
    steps: list,
    mcp_errors: dict[str, str],
    runtime: GoogleMcpRuntime,
    auth_url: str | None,
    llm_raw: ChatOpenAI,
    user_message: str,
    agent_id: uuid.UUID,
    api_key: str,
    log: Any,
) -> tuple[str, list, str | None]:
    google_mcp_err = mcp_errors.get("google_workspace") if isinstance(mcp_errors, dict) else None
    google_mcp_step_err = _extract_google_mcp_step_error(steps)
    google_mcp_auth_err = google_mcp_err or google_mcp_step_err
    must_override_google_auth = bool(google_mcp_auth_err) and _is_google_auth_or_scope_error(str(google_mcp_auth_err))

    if must_override_google_auth:
        if not auth_url:
            auth_url = await _fetch_google_auth_link(
                integration_url=runtime.integration_url,
                api_key=api_key,
                agent_id=agent_id,
                candidate_user_ids=runtime.candidate_user_ids,
            )
        final_reply = await _build_google_mcp_auth_failure_reply(
            llm=llm_raw,
            user_message=user_message,
            error_text=str(google_mcp_auth_err),
            auth_url=auth_url,
        )
        steps = []
        log.warning("agent_run.reply_overridden_mcp_auth_failed", error=str(google_mcp_auth_err)[:200])

    must_override_google_unavailable = (
        bool(google_mcp_err)
        and not must_override_google_auth
        and _is_google_mcp_intent(user_message)
        and (not final_reply or _looks_like_progress_claim(final_reply))
    )
    if must_override_google_unavailable:
        previous_reply = final_reply or ""
        final_reply = _build_google_mcp_unavailable_reply(str(google_mcp_err))
        steps = []
        log.warning(
            "agent_run.reply_overridden_mcp_unavailable",
            error=str(google_mcp_err)[:200],
            previous_reply=previous_reply[:200],
        )

    return final_reply, steps, auth_url


async def _build_google_mcp_auth_failure_reply(
    *,
    llm: ChatOpenAI,
    user_message: str,
    error_text: str,
    auth_url: str | None,
) -> str:
    guidance = (
        "You are assisting a user whose Google Workspace MCP access failed. "
        "Reply in the same language style as the user's last message. "
        "Be transparent that no Google action was executed yet. "
        "Ask the user to reconnect Google, then retry the original request. "
        "Do not include any URL in your reply. "
    )
    if not auth_url:
        guidance += "If no link is available, ask user to reconnect from integration settings."

    prompt = (
        f"User message: {user_message}\n"
        f"MCP error: {error_text}\n"
        f"Auth URL: {auth_url or 'N/A'}\n"
        "Write one concise user-facing reply."
    )
    try:
        resp = await llm.ainvoke([HumanMessage(content=guidance + "\n\n" + prompt)])
        text = getattr(resp, "content", "")
        if isinstance(text, str) and text.strip():
            base_reply = text.strip()
            if auth_url:
                return f"{base_reply}\n\nReconnect link:\n{auth_url}"
            return base_reply
    except Exception:
        pass
    if auth_url:
        return (
            "Google Workspace auth failed, so I could not execute your request yet. "
            "Please reconnect first, then retry.\n\n"
            f"Reconnect link:\n{auth_url}"
        )
    return "Google Workspace auth failed. Please reconnect Google first, then retry."


def _build_google_mcp_unavailable_reply(error_text: str) -> str:
    e = (error_text or "").lower()
    if "504" in e or "timeout" in e or "gateway timeout" in e:
        return (
            "Maaf, aksi Google Workspace belum berhasil dijalankan karena koneksi ke layanan Google sedang timeout. "
            "Jadi presentasi/link belum berhasil dibuat atau diambil. Coba kirim lagi sebentar lagi."
        )
    return (
        "Maaf, aksi Google Workspace belum berhasil dijalankan karena layanan sedang tidak tersedia. "
        "Jadi perubahan atau link belum berhasil dibuat. Coba kirim lagi beberapa saat lagi."
    )
