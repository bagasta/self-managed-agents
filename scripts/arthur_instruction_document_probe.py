"""Synthetic, read-only model smoke; never reads or modifies customer agents."""
import asyncio
import json
import re

from arthur_v2.instruction_writer import compose_assistant_instructions, writer_model


async def main():
    content = await compose_assistant_instructions(
        name="QA Markdown Probe", purpose="Membantu menyusun daftar belanja pribadi",
        instructions="Gunakan sapaan Kak. Catat daftar belanja dalam balasan. Jangan melakukan pembelian. Jangan menghubungi siapa pun.",
        workflow={"trigger": "Owner mengirim daftar", "steps": "Susun daftar", "outputs": "Daftar dalam balasan"},
        capabilities={"google_services": [], "scheduler": False, "deploy": False},
        tool_policy="Tidak ada integrasi eksternal. Jangan mengklaim pembelian atau pengiriman berhasil.",
    )
    assert content.startswith("# SYSTEM MESSAGE — QA Markdown Probe")
    assert "Kak" in content
    print(json.dumps({"synthetic_only": True, "model": writer_model(), "markdown": True,
        "numbered_sections": len(re.findall(r"^## \d+\.", content, re.M)),
        "output_chars": len(content), "database_writes": 0, "whatsapp_sends": 0}))


if __name__ == "__main__":
    asyncio.run(main())
