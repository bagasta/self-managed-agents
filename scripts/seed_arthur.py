"""
seed_arthur.py — Setup/update Arthur (Agent Builder) di database.

Jalankan setelah `make upgrade` untuk memastikan Arthur ada dan terkonfigurasi
dengan system-message-builder.md terbaru.

Usage:
    python scripts/seed_arthur.py
    python scripts/seed_arthur.py --dry-run   # tampilkan config tanpa insert
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys

import yaml

# Add project root to path
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


PROJECT_ROOT = pathlib.Path(__file__).parent.parent
ARTHUR_SKILLS_ROOT = PROJECT_ROOT / "arthur-skills"
RULEBOOK_PATH = ARTHUR_SKILLS_ROOT / "KERNEL.md"
ARTHUR_SKILL_BUNDLE_VERSION = "arthur-skills-2026-07-24-v9"

ARTHUR_SOUL = """\
Kamu adalah Arthur, AI Agent Builder.

Tugasmu adalah membantu user memahami, merancang, membuat, menguji, dan mengelola AI agent di platform ini.
Kamu bekerja seperti konsultan dan arsitek sistem: pahami bisnis serta workflow user lebih dulu, jelaskan eskalasi sejak awal, rangkum kebutuhan faktual, lalu eksekusi hanya setelah user mengonfirmasi bahwa rangkuman itu benar.

PRINSIP KERJAMU:
- Resourceful dulu — gunakan get_platform_capabilities(), get_presets(), dan plan_agent() sebelum create
- Dilarang membuat asumsi untuk create, edit, atau delete; detail yang belum jelas harus ditanyakan, bukan diisi dengan default atau tebakan
- Sebelum membuat agent, selesaikan discovery enam grup: konteks/tujuan, perilaku, eskalasi/batas pengetahuan, data/knowledge, skala/integrasi, dan approver go-live untuk kebutuhan pekerjaan. Tanyakan satu grup per pesan, beri contoh untuk tone serta percakapan ideal/red line, lalu rangkum dan minta konfirmasi akhir
- Jangan menanyakan jam aktif agent, jam operasional, business hours, atau pilihan 24/7 pada discovery pembuatan agent
- Untuk pekerjaan/bisnis, eskalasi wajib berisi kondisi pemicu, nama/role penerima, dan nomor WhatsApp; untuk personal cukup tentukan respons saat agent tidak tahu/fallback, sedangkan nomor eskalasi dan approver boleh dilewati
- Setelah agent dibuat, tawarkan dua jalur WhatsApp yang setara: nomor demo Arthur atau pemasangan ke nomor khusus milik user. Jalankan tool jalur yang dipilih pada turn yang sama
- Jika butuh riset eksternal atau info terbaru, gunakan Tavily browsing tools; jangan gunakan HTTP/ngrok untuk operasi platform internal
- Tolak pembuatan atau update agent untuk buzzer, kampanye politik, propaganda politik, atau manipulasi opini publik
- Setiap agent yang kamu buat WAJIB punya soul yang jelas — lebih efisien kirim soul langsung lewat create_agent(soul=...), atau fallback via set_agent_memory(agent_id, key="soul", value=...)
- Catat agent yang sudah dibuat ke daily memory kamu dengan update_daily("Buat agent X untuk user Y")
- Simpan preferensi arsitektur user ke long-term memory dengan update_longterm("User prefer model X untuk agent tipe Y")

CARA BICARA:
- Bahasa: Indonesia, profesional tapi santai
- Kata lanjut/buat/langsung bukan izin mengarang detail yang belum diberikan; tetap pastikan workflow dan eskalasi sudah jelas
- Berikan penjelasan singkat kenapa kamu memilih konfigurasi tertentu
"""

ARTHUR_CONFIG = {
    "name": "Arthur",
    "description": "AI Agent Builder — bantu user buat dan kelola AI agent via WhatsApp",
    "model": "deepseek/deepseek-v4-flash",
    "temperature": 0.2,
    "max_tokens": 8192,
    "capabilities": ["system", "builder"],
    "allowed_senders": None,  # terbuka untuk siapapun
    "token_quota": 0,            # 0 = unlimited; Arthur adalah control-plane agent
    "quota_period_days": 30,
    "tools_config": {
        "memory": True,
        "skills": True,
        "escalation": True,
        "scheduler": False,
        "sandbox": False,
        "tool_creator": False,
        "rag": False,
        "http": False,          # Arthur pakai builder tools internal, bukan HTTP/ngrok platform
        "tavily": True,         # browsing/search eksternal via Tavily
        "mcp": False,
        "whatsapp_media": True,
        "wa_agent_manager": True,
        "subagents": {"enabled": False},  # disabled — hemat ~250 tokens/request
        "builder": True,        # marker, dimuat via is_system_agent flag
        "arthur_runtime": {
            "enabled": True,
            "progressive_skills": True,
            "build_state": True,
            "image_routing": True,
            "document_routing": True,
            "primary_model": "deepseek/deepseek-v4-flash",
            "document_model": "mistral-ocr-latest",
            "image_model": "openai/gpt-4.1-mini",
            "engine_version": "arthur-progressive-v1",
            "prompt_version": "arthur-kernel-v7",
            "skill_bundle_version": ARTHUR_SKILL_BUNDLE_VERSION,
        },
    },
    "escalation_config": {},
    "operator_ids": [
        p.strip() for p in os.environ.get("ARTHUR_OPERATOR_PHONES", "").split(",")
        if p.strip()
    ],
    "sandbox_config": {},
    "safety_policy": {},
}


async def seed(dry_run: bool = False) -> None:
    if not RULEBOOK_PATH.exists():
        print(f"[ERROR] system-message-builder.md tidak ditemukan di: {RULEBOOK_PATH}")
        sys.exit(1)

    instructions = RULEBOOK_PATH.read_text(encoding="utf-8")
    print(f"[OK] Compact kernel dimuat: {len(instructions)} karakter")
    if len(instructions) > 10_000:
        print("[ERROR] Arthur kernel melebihi batas 10.000 karakter")
        sys.exit(1)

    skill_sources: list[dict] = []
    for skill_path in sorted(ARTHUR_SKILLS_ROOT.glob("*/SKILL.md")):
        runtime_path = skill_path.parent / "runtime.yaml"
        if not runtime_path.exists():
            print(f"[ERROR] runtime.yaml tidak ditemukan untuk {skill_path.parent.name}")
            sys.exit(1)
        content = skill_path.read_text(encoding="utf-8")
        if "[TODO" in content:
            print(f"[ERROR] Skill masih berisi TODO: {skill_path}")
            sys.exit(1)
        if not content.startswith("---\n") or "\n---\n" not in content[4:]:
            print(f"[ERROR] Frontmatter skill invalid: {skill_path}")
            sys.exit(1)
        _frontmatter, body = content[4:].split("\n---\n", 1)
        metadata = yaml.safe_load(_frontmatter) or {}
        runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8")) or {}
        skill_sources.append({
            "name": metadata.get("name"),
            "description": metadata.get("description"),
            "content_md": body.strip(),
            "version": str(runtime.get("version") or "1.0.0"),
            "triggers": list(runtime.get("triggers") or []),
            "supported_states": list(runtime.get("supported_states") or []),
            "allowed_tool_groups": list(runtime.get("allowed_tool_groups") or []),
        })
    if len(skill_sources) != 8 or any(not item["name"] or not item["description"] for item in skill_sources):
        print(f"[ERROR] Bundle skill Arthur tidak lengkap/invalid: {len(skill_sources)} skill")
        sys.exit(1)

    if dry_run:
        print("\n=== DRY RUN — config yang akan di-seed ===")
        print(f"  name          : {ARTHUR_CONFIG['name']}")
        print(f"  model         : {ARTHUR_CONFIG['model']}")
        print(f"  capabilities  : {ARTHUR_CONFIG['capabilities']}")
        print(f"  tools_config  : {ARTHUR_CONFIG['tools_config']}")
        print(f"  operator_ids  : {ARTHUR_CONFIG['operator_ids']}")
        print(f"  instructions  : {instructions[:200]}...")
        print(f"  skill_bundle  : {ARTHUR_SKILL_BUNDLE_VERSION} ({len(skill_sources)} skills)")
        print("\n[DRY RUN] Tidak ada perubahan ke database.")
        return

    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.agent import Agent

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Agent).where(
                Agent.name == "Arthur",
                Agent.capabilities.contains(["system"]),
                Agent.is_deleted.is_(False),
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            print(f"[FOUND] Arthur sudah ada: id={existing.id}")
            existing.instructions = instructions
            existing.model = ARTHUR_CONFIG["model"]
            existing.max_tokens = ARTHUR_CONFIG["max_tokens"]
            existing.capabilities = ARTHUR_CONFIG["capabilities"]
            existing.tools_config = ARTHUR_CONFIG["tools_config"]
            existing.token_quota = ARTHUR_CONFIG["token_quota"]
            existing.tokens_used = 0
            if not existing.created_by_type:
                existing.created_by_type = "system"
                existing.created_by_agent_name = "System"
            # Merge operator_ids — tambahkan yang baru dari env, jangan hapus yang sudah ada
            new_ops = ARTHUR_CONFIG["operator_ids"]
            if new_ops:
                existing_ops = list(existing.operator_ids or [])
                for op in new_ops:
                    if op not in existing_ops:
                        existing_ops.append(op)
                existing.operator_ids = existing_ops
                print(f"  operator_ids: {existing.operator_ids}")
            existing.version = (existing.version or 1) + 1
            await db.commit()
            print(f"[UPDATED] Arthur diupdate ke versi {existing.version}")
            print(f"  id     : {existing.id}")
            print("  api_key: [REDACTED — existing key preserved]")
            arthur_id = existing.id
        else:
            arthur = Agent(
                name=ARTHUR_CONFIG["name"],
                description=ARTHUR_CONFIG["description"],
                instructions=instructions,
                model=ARTHUR_CONFIG["model"],
                temperature=ARTHUR_CONFIG["temperature"],
                max_tokens=ARTHUR_CONFIG["max_tokens"],
                capabilities=ARTHUR_CONFIG["capabilities"],
                allowed_senders=ARTHUR_CONFIG["allowed_senders"],
                token_quota=ARTHUR_CONFIG["token_quota"],
                quota_period_days=ARTHUR_CONFIG["quota_period_days"],
                tools_config=ARTHUR_CONFIG["tools_config"],
                escalation_config=ARTHUR_CONFIG["escalation_config"],
                operator_ids=ARTHUR_CONFIG["operator_ids"],
                sandbox_config=ARTHUR_CONFIG["sandbox_config"],
                safety_policy=ARTHUR_CONFIG["safety_policy"],
                created_by_type="system",
                created_by_agent_name="System",
            )
            db.add(arthur)
            await db.commit()
            await db.refresh(arthur)
            print(f"[CREATED] Arthur berhasil dibuat!")
            print(f"  id     : {arthur.id}")
            print("  api_key: [REDACTED — stored in database]")
            arthur_id = arthur.id

    # Seed Arthur's soul ke agent_memories (scope=None → global per agent)
    from app.core.domain.memory_service import upsert_memory
    async with AsyncSessionLocal() as db:
        await upsert_memory(arthur_id, "soul", ARTHUR_SOUL, db, scope=None)
        await db.commit()
    print(f"[OK] Arthur's soul di-seed ke agent_memories")

    from app.core.domain.skill_service import publish_system_skill
    async with AsyncSessionLocal() as db:
        for source in skill_sources:
            await publish_system_skill(
                agent_id=arthur_id,
                bundle_version=ARTHUR_SKILL_BUNDLE_VERSION,
                publisher="scripts/seed_arthur.py",
                db=db,
                **source,
            )
        await db.commit()
    print(f"[OK] {len(skill_sources)} system skills Arthur dipublish: {ARTHUR_SKILL_BUNDLE_VERSION}")

    print("\n=== Langkah selanjutnya ===")
    print("1. Pastikan Arthur terhubung ke channel WhatsApp yang dipakai user.")
    print("2. Chat dengan Arthur di WA — minta dia buatkan agent CS, asisten, dll.")
    print("3. Validasi Arthur memakai create_agent/update_agent/set_agent_memory, bukan HTTP/ngrok.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed Arthur (Agent Builder) ke database")
    parser.add_argument("--dry-run", action="store_true", help="Tampilkan config tanpa insert")
    args = parser.parse_args()
    asyncio.run(seed(dry_run=args.dry_run))
