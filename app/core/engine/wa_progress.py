from __future__ import annotations

import ast
import json
import re
from typing import Any

_TOOL_PROGRESS_FALLBACK: dict[str, str] = {
    "task": "> 🤖 Mendelegasikan ke subagent...",
    "http_get": "> 🔍 Mengambil data dari web...",
    "http_post": "> 📡 Mengirim request...",
    "deploy_app": "> 🚀 Men-deploy aplikasi...",
    "execute": "> ⚙️ Menjalankan kode...",
    "write_file": "> ✏️ Menulis file...",
    "edit_file": "> ✏️ Mengedit file...",
    "read_file": "> 📖 Membaca file...",
    "search_documents": "> 🔎 Mencari dokumen...",
    "remember": "> 💾 Menyimpan memori...",
    "set_reminder": "> ⏰ Mengatur pengingat...",
    "send_whatsapp_document": "> 📎 Mengirim dokumen...",
    "send_whatsapp_image": "> 🖼️ Mengirim gambar...",
}


def parse_tool_input_payload(input_payload: Any) -> dict[str, Any]:
    if isinstance(input_payload, dict):
        return input_payload
    if not isinstance(input_payload, str):
        return {}
    raw = input_payload.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    try:
        parsed = ast.literal_eval(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def truncate_preview(text: str, max_len: int = 72) -> str:
    """Truncate at word boundary to avoid cut-off mid-word."""
    txt = " ".join((text or "").split())
    if len(txt) <= max_len:
        return txt
    # Cut at last space before max_len
    cut = txt[:max_len].rsplit(" ", 1)[0].rstrip(",:;-")
    return cut + "..."


def build_progress_message(tool_name: str, input_payload: Any) -> str | None:
    payload = parse_tool_input_payload(input_payload)

    if tool_name == "task":
        subagent_name = str(payload.get("name") or "subagent").strip()
        task_text = str(payload.get("task") or payload.get("description") or "").strip()
        if task_text:
            return f"> 🤖 *{subagent_name}* sedang mengerjakan:\n> {truncate_preview(task_text, 80)}"
        return f"> 🤖 Mendelegasikan ke *{subagent_name}*..."

    if tool_name in {"read_file", "write_file", "edit_file"}:
        path = str(payload.get("path") or payload.get("file_path") or "").strip()
        icon = {"read_file": "📖", "write_file": "✏️", "edit_file": "✏️"}.get(tool_name, "📄")
        action = {"read_file": "Membaca", "write_file": "Menulis", "edit_file": "Mengedit"}.get(tool_name, "Memproses")
        if path:
            # Show only filename, not full path — cleaner on WA
            filename = path.split("/")[-1] or path
            return f"> {icon} {action}: `{filename}`"
        return _TOOL_PROGRESS_FALLBACK.get(tool_name)

    if tool_name == "http_get":
        url = str(payload.get("url") or "").strip()
        if url:
            domain = re.sub(r"https?://([^/]+).*", r"\1", url)
            return f"> 🔍 Mengambil data dari `{domain}`..."

    if tool_name == "execute":
        cmd = str(payload.get("command") or payload.get("cmd") or "").strip()
        if cmd:
            return f"> ⚙️ Menjalankan: `{truncate_preview(cmd, 60)}`"

    return _TOOL_PROGRESS_FALLBACK.get(tool_name)


def build_task_done_message(input_payload: Any, output: Any) -> str:
    payload = parse_tool_input_payload(input_payload)
    subagent_name = str(payload.get("name") or "subagent").strip()
    out = output if isinstance(output, str) else str(output)
    url_match = re.search(r"https://[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:/[^\s\"']*)?", out)
    if url_match:
        return f"> ✅ *{subagent_name}* selesai\n> {url_match.group(0).rstrip('.,)')}"
    preview = truncate_preview(out, 80)
    if preview:
        return f"> ✅ *{subagent_name}* selesai: {preview}"
    return f"> ✅ *{subagent_name}* selesai."
