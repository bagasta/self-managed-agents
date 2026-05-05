"""
Utilitas konversi teks untuk berbagai channel output.
"""
from __future__ import annotations

import re


def markdown_to_wa(text: str) -> str:
    """
    Konversi markdown ke format teks yang rapi untuk WhatsApp.

    WhatsApp mendukung:
      *teks*   → bold
      _teks_   → italic
      ~teks~   → strikethrough
      `teks`   → monospace

    Konversi yang dilakukan:
      **teks** / __teks__  → *teks* (bold)
      *teks* / _teks_      → _teks_ (italic, karena WA * adalah bold)
      # Judul              → *Judul* (bold)
      [teks](url)          → teks (url)
      ```blok kode```      → `blok kode`
      ---                  → ─────────────────────
      Bullet - / * / +    → • (bullet WA-friendly)
    """
    # 1. Blok kode triple backtick → pertahankan isi, bungkus dengan backtick tunggal per baris
    def _replace_code_block(m: re.Match) -> str:
        content = m.group(2).strip()
        lines = content.splitlines()
        formatted = "\n".join(f"`{line}`" if line.strip() else "" for line in lines)
        return formatted

    # Wajib ada newline setelah opening backticks agar language identifier tidak salah tangkap
    text = re.sub(r"```(\w*)\n(.*?)```", _replace_code_block, text, flags=re.DOTALL)

    # 2. Inline code `teks` → tetap (WhatsApp mendukung)

    # 3. Bold: **teks** atau __teks__ → tandai dulu dengan placeholder agar tidak bentrok
    #    dengan konversi italic di bawah
    _B = "\x00B\x00"
    _BE = "\x00BE\x00"
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: f"{_B}{m.group(1)}{_BE}", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", lambda m: f"{_B}{m.group(1)}{_BE}", text, flags=re.DOTALL)

    # 4. Italic: *teks* (single) → _teks_ (WA italic)
    #    Placeholder di atas sudah tidak pakai *, jadi tidak akan ikut terkonversi
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"_\1_", text)

    # 5. Italic: _teks_ (single underscore) → sudah benar untuk WA, biarkan saja

    # 3b. Kembalikan placeholder bold → *teks* (WA bold)
    text = text.replace(_B, "*").replace(_BE, "*")

    # 6. Strikethrough: ~~teks~~ → ~teks~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text, flags=re.DOTALL)

    # 7. Heading: # / ## / ### → *Judul*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # 8. Link: [teks](url) → teks (url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)

    # 9. Horizontal rule: --- atau *** atau ___ → separator sederhana
    text = re.sub(r"^(\s*[-*_]{3,}\s*)$", "─────────────────────", text, flags=re.MULTILINE)

    # 10. Bullet list: baris diawali - / * / + (bukan dalam kode) → •
    text = re.sub(r"^[ \t]*[-*+]\s+", "• ", text, flags=re.MULTILINE)

    # 11. Blockquote: > teks → teks (WA tidak punya blockquote markdown style)
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)

    # 12. Hapus HTML tag jika ada
    text = re.sub(r"<[^>]+>", "", text)

    # 13. Normalisasi blank lines berlebih (maks 2 baris kosong berturut-turut)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()
