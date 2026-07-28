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
import hashlib
import os
import pathlib
import re
import sys

import yaml

# Add project root to path
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


PROJECT_ROOT = pathlib.Path(__file__).parent.parent
ARTHUR_SKILLS_ROOT = PROJECT_ROOT / "arthur-skills"
RULEBOOK_PATH = ARTHUR_SKILLS_ROOT / "KERNEL.md"
ARTHUR_SKILL_BUNDLE_VERSION = "arthur-skills-2026-07-28-v20"

ARTHUR_SOUL = """\
Kamu adalah Arthur, AI Agent Builder.

Tugasmu adalah membantu user memahami, merancang, membuat, menguji, dan mengelola AI agent di platform ini.
Kamu bekerja seperti konsultan dan arsitek sistem: pahami bisnis serta workflow user lebih dulu, jelaskan eskalasi sejak awal, rangkum kebutuhan faktual, lalu eksekusi hanya setelah user mengonfirmasi bahwa rangkuman itu benar.

PRINSIP KERJAMU:
- Resourceful dulu — gunakan get_platform_capabilities(), get_presets(), dan plan_agent() sebelum create
- Dilarang membuat asumsi untuk create, edit, atau delete; detail material yang belum jelas harus ditanyakan. Detail presentasi yang user delegasikan boleh memakai default aman dan harus muncul pada rangkuman akhir
- Sebelum membuat agent, selesaikan brief inti: konteks/tujuan, pengguna, tugas konkret, batas penting, eskalasi, dan integrasi. Tanyakan tepat satu hal yang paling berdampak per pesan, lalu rangkum dan minta konfirmasi akhir. Jangan menahan pembuatan hanya karena tone, contoh percakapan, volume chat, atau approver belum dibahas
- Jangan menanyakan jam aktif agent, jam operasional, business hours, atau pilihan 24/7 pada discovery pembuatan agent
- Untuk pekerjaan/bisnis, eskalasi wajib berisi kondisi pemicu, nama/role penerima, dan nomor WhatsApp; untuk personal cukup tentukan respons saat agent tidak tahu/fallback, sedangkan nomor eskalasi dan approver boleh dilewati
- Setelah agent dibuat, tawarkan dua jalur WhatsApp yang setara: nomor demo Arthur atau pemasangan ke nomor khusus milik user. Jalankan tool jalur yang dipilih pada turn yang sama
- Jika butuh riset eksternal atau info terbaru, gunakan Tavily browsing tools; jangan gunakan HTTP/ngrok untuk operasi platform internal
- Tolak pembuatan atau update agent untuk buzzer, kampanye politik, propaganda politik, atau manipulasi opini publik
- Setiap agent yang kamu buat WAJIB punya soul yang jelas — lebih efisien kirim soul langsung lewat create_agent(soul=...), atau fallback via set_agent_memory(agent_id, key="soul", value=...)
- Catat agent yang sudah dibuat ke daily memory kamu dengan update_daily("Buat agent X untuk user Y")
- Simpan preferensi arsitektur user ke long-term memory dengan update_longterm("User prefer model X untuk agent tipe Y")

CARA BICARA:
- Bahasa: Indonesia, hangat, profesional, dan terasa seperti rekan kerja yang sigap—bukan formulir atau bot
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
            "engine_version": "arthur-progressive-v7",
            "prompt_version": "arthur-kernel-v18",
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


def load_arthur_source_bundle() -> tuple[str, list[dict]]:
    """Validate every local Arthur source before any database write."""
    if not RULEBOOK_PATH.exists():
        raise ValueError(f"Arthur kernel tidak ditemukan di: {RULEBOOK_PATH}")

    instructions = RULEBOOK_PATH.read_text(encoding="utf-8")
    if len(instructions) > 10_000:
        raise ValueError("Arthur kernel melebihi batas 10.000 karakter")

    skill_sources: list[dict] = []
    seen_names: set[str] = set()
    for skill_path in sorted(ARTHUR_SKILLS_ROOT.glob("*/SKILL.md")):
        runtime_path = skill_path.parent / "runtime.yaml"
        if not runtime_path.exists():
            raise ValueError(f"runtime.yaml tidak ditemukan untuk {skill_path.parent.name}")
        content = skill_path.read_text(encoding="utf-8")
        if "[TODO" in content:
            raise ValueError(f"Skill masih berisi TODO: {skill_path}")
        if not content.startswith("---\n") or "\n---\n" not in content[4:]:
            raise ValueError(f"Frontmatter skill invalid: {skill_path}")
        _frontmatter, body = content[4:].split("\n---\n", 1)
        metadata = yaml.safe_load(_frontmatter) or {}
        runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8")) or {}
        name = str(metadata.get("name") or "").strip()
        description = str(metadata.get("description") or "").strip()
        version = str(runtime.get("version") or "").strip()
        if not name or not description:
            raise ValueError(f"Metadata name/description tidak lengkap: {skill_path}")
        if name in seen_names:
            raise ValueError(f"Nama skill duplikat dalam bundle: {name}")
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError(f"Versi skill harus semver x.y.z: {name}@{version or '(empty)'}")
        if not body.strip():
            raise ValueError(f"Body skill kosong: {skill_path}")
        seen_names.add(name)
        skill_sources.append({
            "name": name,
            "description": description,
            "content_md": body.strip(),
            "version": version,
            "triggers": list(runtime.get("triggers") or []),
            "supported_states": list(runtime.get("supported_states") or []),
            "allowed_tool_groups": list(runtime.get("allowed_tool_groups") or []),
        })
    if len(skill_sources) != 8:
        raise ValueError(f"Bundle skill Arthur tidak lengkap/invalid: {len(skill_sources)} skill")
    return instructions, skill_sources


async def seed(dry_run: bool = False) -> None:
    try:
        instructions, skill_sources = load_arthur_source_bundle()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"[ERROR] Preflight source Arthur gagal: {exc}")
        raise SystemExit(1) from exc

    print(f"[OK] Compact kernel dimuat: {len(instructions)} karakter")
    print(f"[OK] Preflight source lulus: {len(skill_sources)} skill valid")

    if dry_run:
        print("\n=== DRY RUN — config yang akan di-seed ===")
        print(f"  name          : {ARTHUR_CONFIG['name']}")
        print(f"  model         : {ARTHUR_CONFIG['model']}")
        print(f"  capabilities  : {ARTHUR_CONFIG['capabilities']}")
        print(f"  tools_config  : {ARTHUR_CONFIG['tools_config']}")
        print(f"  operator_ids  : {ARTHUR_CONFIG['operator_ids']}")
        print(f"  instructions  : {instructions[:200]}...")
        print(f"  skill_bundle  : {ARTHUR_SKILL_BUNDLE_VERSION} ({len(skill_sources)} skills)")
        for source in skill_sources:
            checksum = hashlib.sha256(source["content_md"].encode("utf-8")).hexdigest()
            print(f"    - {source['name']}@{source['version']} sha256:{checksum[:12]}")
        print("\n[DRY RUN] Tidak ada perubahan ke database.")
        return

    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.core.domain.memory_service import upsert_memory
    from app.core.domain.skill_service import publish_system_skill
    from app.models.agent import Agent
    from app.models.skill import Skill

    created = False
    arthur_version = 1
    arthur_id = None
    async with AsyncSessionLocal() as db:
        async with db.begin():
            result = await db.execute(
                select(Agent).where(
                    Agent.name == "Arthur",
                    Agent.capabilities.contains(["system"]),
                    Agent.is_deleted.is_(False),
                )
            )
            existing = result.scalar_one_or_none()

            # Immutable checksum validation happens before config, soul, or
            # activation state is mutated. The surrounding transaction is the
            # second line of defense and rolls every write back on any failure.
            if existing is not None:
                for source in skill_sources:
                    existing_skill = (
                        await db.execute(
                            select(Skill).where(
                                Skill.agent_id == existing.id,
                                Skill.name == source["name"],
                                Skill.version == source["version"],
                            )
                        )
                    ).scalar_one_or_none()
                    expected_checksum = hashlib.sha256(
                        source["content_md"].encode("utf-8")
                    ).hexdigest()
                    if (
                        existing_skill is not None
                        and existing_skill.checksum
                        and existing_skill.checksum != expected_checksum
                    ):
                        raise ValueError(
                            "Immutable system skill "
                            f"{source['name']}@{source['version']} has a different checksum"
                        )

            if existing:
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
                new_ops = ARTHUR_CONFIG["operator_ids"]
                if new_ops:
                    existing_ops = list(existing.operator_ids or [])
                    for op in new_ops:
                        if op not in existing_ops:
                            existing_ops.append(op)
                    existing.operator_ids = existing_ops
                existing.version = (existing.version or 1) + 1
                arthur = existing
            else:
                created = True
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

            await db.flush()
            arthur_id = arthur.id
            arthur_version = arthur.version or 1
            await upsert_memory(arthur_id, "soul", ARTHUR_SOUL, db, scope=None)
            for source in skill_sources:
                await publish_system_skill(
                    agent_id=arthur_id,
                    bundle_version=ARTHUR_SKILL_BUNDLE_VERSION,
                    publisher="scripts/seed_arthur.py",
                    db=db,
                    **source,
                )

    action = "CREATED" if created else "UPDATED"
    print(f"[{action}] Arthur tersimpan atomically ke versi {arthur_version}")
    print(f"  id     : {arthur_id}")
    print("  api_key: [REDACTED — existing key preserved]" if not created else "  api_key: [REDACTED — stored in database]")
    print("[OK] Arthur's soul di-seed ke agent_memories")
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
