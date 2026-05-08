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

# Add project root to path
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


RULEBOOK_PATH = pathlib.Path(__file__).parent.parent / "system-message-builder.md"

ARTHUR_SOUL = """\
Kamu adalah Arthur, AI Agent Builder.

Tugasmu adalah membantu user merancang, membuat, dan mengelola AI agent di platform ini.
Kamu bekerja seperti seorang arsitek sistem — memahami kebutuhan user, merekomendasikan konfigurasi yang tepat, dan mengeksekusi pembuatan agent secara langsung via tools.

PRINSIP KERJAMU:
- Resourceful dulu — gunakan get_platform_capabilities(), get_presets(), dan plan_agent() sebelum create
- Setiap agent yang kamu buat WAJIB punya soul yang jelas — tulis soul ke memory agent baru dengan remember("soul", "...")
- Catat agent yang sudah dibuat ke daily memory kamu dengan update_daily("Buat agent X untuk user Y")
- Simpan preferensi arsitektur user ke long-term memory dengan update_longterm("User prefer model X untuk agent tipe Y")

CARA BICARA:
- Bahasa: Indonesia, profesional tapi santai
- Konfirmasi plan sebelum eksekusi jika agent yang diminta kompleks
- Berikan penjelasan singkat kenapa kamu memilih konfigurasi tertentu
"""

ARTHUR_CONFIG = {
    "name": "Arthur",
    "description": "AI Agent Builder — bantu user buat dan kelola AI agent via WhatsApp",
    "model": "deepseek/deepseek-v4-flash",
    "temperature": 0.7,
    "max_tokens": 2048,         # Arthur butuh ruang lebih untuk nulis instructions agent
    "capabilities": ["system", "builder"],
    "allowed_senders": None,  # terbuka untuk siapapun
    "token_quota": 10_000_000,
    "quota_period_days": 30,
    "tools_config": {
        "memory": True,
        "skills": True,
        "escalation": True,
        "scheduler": False,
        "sandbox": False,
        "tool_creator": False,
        "rag": False,
        "http": True,           # Arthur butuh http untuk call platform API
        "mcp": False,
        "whatsapp_media": True,
        "wa_agent_manager": True,
        "subagents": {"enabled": False},  # disabled — hemat ~250 tokens/request
        "builder": True,        # marker, dimuat via is_system_agent flag
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
    print(f"[OK] Rulebook dimuat: {len(instructions)} karakter")

    if dry_run:
        print("\n=== DRY RUN — config yang akan di-seed ===")
        print(f"  name          : {ARTHUR_CONFIG['name']}")
        print(f"  model         : {ARTHUR_CONFIG['model']}")
        print(f"  capabilities  : {ARTHUR_CONFIG['capabilities']}")
        print(f"  tools_config  : {ARTHUR_CONFIG['tools_config']}")
        print(f"  operator_ids  : {ARTHUR_CONFIG['operator_ids']}")
        print(f"  instructions  : {instructions[:200]}...")
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
            print(f"  api_key: {existing.api_key}")
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
            )
            db.add(arthur)
            await db.commit()
            await db.refresh(arthur)
            print(f"[CREATED] Arthur berhasil dibuat!")
            print(f"  id     : {arthur.id}")
            print(f"  api_key: {arthur.api_key}")
            arthur_id = arthur.id

    # Seed Arthur's soul ke agent_memories (scope=None → global per agent)
    from app.core.domain.memory_service import upsert_memory
    async with AsyncSessionLocal() as db:
        await upsert_memory(arthur_id, "soul", ARTHUR_SOUL, db, scope=None)
        await db.commit()
    print(f"[OK] Arthur's soul di-seed ke agent_memories")

    print("\n=== Langkah selanjutnya ===")
    print("1. Set Arthur's WA device (hubungkan ke wa-dev-service untuk testing):")
    print("   PATCH /v1/agents/{arthur_id} dengan { 'channel_type': 'whatsapp' }")
    print("2. POST /v1/agents/{arthur_id}/whatsapp/connect  → scan QR")
    print("3. Chat dengan Arthur di WA — minta dia buatkan agent CS, asisten, dll")
    print("4. Validasi kualitas system prompt yang dihasilkan Arthur")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed Arthur (Agent Builder) ke database")
    parser.add_argument("--dry-run", action="store_true", help="Tampilkan config tanpa insert")
    args = parser.parse_args()
    asyncio.run(seed(dry_run=args.dry_run))
