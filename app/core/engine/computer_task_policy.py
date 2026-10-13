"""Policy for requests that must be completed through an assigned computer."""
from __future__ import annotations

import re
from typing import Any


_COMPUTER_COMMAND_RE = re.compile(
    r"(?:^|[.!?\n])\s*(?:(?:tolong|coba|silakan|please|arthur)[,\s]+)*"
    r"(?:buka|bukakan|open|akses|access|kunjungi|visit|navigat(?:e|ion)|"
    r"klik|click|ketik|type|isi|cari|carikan|search|telusuri|browse|"
    r"login|log\s*in|periksa|cek|check|buat|buatkan|create|desain|design|"
    r"edit|ubah|update|tambah|tambahkan)\b",
    re.IGNORECASE,
)
_DIRECT_COMPUTER_REQUEST_RE = re.compile(
    r"\b(?:tolong|minta|bisa(?:kah)?|mohon)\s+(?:kamu|anda|arthur|bot)?\s*"
    r"(?:buka|akses|klik|cari|periksa|cek|buat|desain|edit|ubah|tambah)\b",
    re.IGNORECASE,
)
_COMPUTER_TARGET_RE = re.compile(
    r"https?://\S+|\b(?:browser|browsing|komputer|computer|website|situs|google|internet|"
    r"web\s*(?:page|site|browser)?|halaman\s+web|lms|canva|poster|"
    r"kanvas|canvas|form|formulir|page|aplikasi|app)\b",
    re.IGNORECASE,
)
_COMPUTER_ACTIONS = {
    "computer_open_url",
    "computer_click",
    "computer_type_text",
    "computer_press_key",
}
_VISUAL_COMPUTER_TASK_RE = re.compile(
    r"\b(?:canva|poster|desain|design|template|kanvas|canvas|layout|tipografi|typography)\b",
    re.IGNORECASE,
)


def requires_computer_task(user_message: str) -> bool:
    """Return whether the request requires browser/desktop interaction.

    Only a request to operate the UI makes computer evidence mandatory.
    """
    text = user_message or ""
    if re.match(r"\s*(?:buat(?:kan)?|create)\s+(?:bot|asisten|assistant|agent)\b", text, re.IGNORECASE):
        return False
    # A conversational answer may mention actions another bot will perform
    # later. Require a command to this agent and a UI target in this turn.
    return bool(_COMPUTER_TARGET_RE.search(text) and (
        _COMPUTER_COMMAND_RE.search(text) or _DIRECT_COMPUTER_REQUEST_RE.search(text)
    ))


def requires_visual_verification(user_message: str) -> bool:
    """Whether a computer task needs a visible final-state check."""
    return bool(_VISUAL_COMPUTER_TASK_RE.search(user_message or ""))


def computer_task_contract() -> str:
    return (
        "## Computer-required request\n"
        "Permintaan user ini wajib dikerjakan melalui computer yang aktif. Mulai dengan "
        "`computer_screenshot`, lalu gunakan aksi computer yang relevan (misalnya "
        "`computer_open_url`, klik, ketik, atau tombol). Jangan mengganti browser dengan "
        "HTTP/fetch teks, pengetahuan internal, atau tebakan. Setelah setiap aksi penting, "
        "verifikasi screenshot terbaru. Untuk pekerjaan visual seperti poster, pahami brief, "
        "buat komposisi yang jelas (hierarki judul, isi, tipografi, warna, dan ruang), lalu "
        "periksa hasil di kanvas dan revisi bagian yang kurang sebelum menyatakan selesai. "
        "Jangan berhenti pada template atau satu elemen awal. Jika browser, halaman, atau aksi tidak dapat "
        "diverifikasi, jelaskan keterbatasan itu; jangan mengklaim hasil atau aksi berhasil. Jika dua aksi "
        "berturut-turut tidak mengubah screenshot, jangan ulangi klik yang sama: observasi ulang, panggil "
        "`decide_next_action` dengan `inspect_computer=true`, lalu ganti pendekatan. Sebelum menyatakan "
        "pekerjaan visual selesai, panggil `decide_next_action` dengan `inspect_computer=true`; hanya "
        "lanjutkan ke jawaban selesai bila `visual_progress` bernilai `complete` dan `accepted=true`."
    )


def guard_computer_task_reply(
    final_reply: str,
    steps: list[dict[str, Any]],
    user_message: str,
    *,
    computer_enabled: bool,
    task_required: bool | None = None,
    decision_events: list[dict[str, Any]] | None = None,
) -> tuple[str, str | None]:
    """Reject unsupported computer completion claims while preserving planning replies."""
    required = requires_computer_task(user_message) if task_required is None else task_required
    if not computer_enabled or not required:
        return final_reply, None

    # A question, proposal, blocker, or future plan has no browser result to
    # verify. Replacing it would erase a valid assistant build conversation.
    claims_result = bool(re.search(
        r"\b(?:saya|aku|kami|we|i)\s+(?:sudah|telah|berhasil|baru saja)\s+"
        r"(?:membuka|mengakses|mengecek|memeriksa|mencari|mengklik|mengisi|"
        r"mengetik|login|log\s*in|mengedit|mengubah|menambah|"
        r"opened|checked|searched|clicked|typed|created|edited)\b|"
        r"\b(?:saya|aku|kami)\s+(?:sudah|telah|berhasil)\s+membuat\s+"
        r"(?:poster|desain|design|halaman|website|situs)\b|"
        r"^\s*(?:berhasil|selesai|done)\b|"
        r"\b(?:poster|desain|design)\s+(?:sudah|telah)\s+(?:jadi|selesai)\b|"
        r"\b(?:hasil\s+(?:dari|pemeriksaan)\s+(?:web|browser|website|situs|lms)|"
        r"(?:di|pada)\s+(?:web|website|situs|lms)\s+(?:terlihat|menunjukkan|terdapat))\b",
        final_reply or "", re.IGNORECASE,
    ))
    if not claims_result:
        return final_reply, None

    action_steps = [step for step in steps if str(step.get("tool") or "") in _COMPUTER_ACTIONS]
    if not action_steps:
        return (
            "Saya belum menjalankan aksi komputer pada giliran ini, jadi saya tidak akan "
            "mengklaim hasil dari web. Tugas browser ini belum selesai.",
            "missing_computer_action",
        )

    has_observation = any(
        str(step.get("tool") or "") == "computer_screenshot"
        or "observation" in str(step.get("result") or "").lower()
        for step in steps
    )
    if not has_observation:
        return (
            "Aksi komputer sudah dicoba, tetapi belum ada screenshot observasi yang dapat "
            "memverifikasi hasilnya. Saya tidak akan mengklaim tugas browser ini selesai.",
            "missing_computer_observation",
        )
    if requires_visual_verification(user_message):
        reviews = [
            event.get("visual_progress", {})
            for event in (decision_events or [])
            if isinstance(event, dict) and isinstance(event.get("visual_progress"), dict)
        ]
        if not any(review.get("choice") == "complete" and review.get("accepted") is True for review in reviews):
            return (
                "Saya belum dapat memverifikasi hasil visual di kanvas sebagai selesai. Saya tidak akan "
                "mengklaim desain sudah jadi sebelum screenshot terbaru dan pemeriksaan visual menyatakan "
                "semua requirement terpenuhi.",
                "missing_visual_completion_verification",
            )
    return final_reply, None
