from __future__ import annotations

import re
from typing import Any


def ensure_non_empty_reply(reply: str, steps: list[dict[str, Any]]) -> str:
    text = (reply or "").strip()
    if text:
        return text

    url_pat = re.compile(r"https://[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:/[^\s\"']*)?")
    for step in steps or []:
        result = str(step.get("result", ""))
        match = url_pat.search(result)
        if match:
            return f"Proses selesai. Cek hasilnya di sini: {match.group(0).rstrip('.,)')}"

    if steps:
        tool_names = [str(s.get("tool", "")).strip() for s in steps if s.get("tool")]
        tool_names = [name for name in tool_names if name]
        if tool_names:
            uniq = []
            for name in tool_names:
                if name not in uniq:
                    uniq.append(name)
            preview = ", ".join(uniq[:3])
            return f"Proses sudah dijalankan ({preview}). Kalau belum sesuai, coba kirim ulang instruksinya ya."

    return "Maaf, proses lagi gangguan. Coba kirim ulang pesanmu ya."
