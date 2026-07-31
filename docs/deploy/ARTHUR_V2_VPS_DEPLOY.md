# Arthur V2 — VPS Production Runbook

Arthur V2 adalah satu-satunya system agent builder yang boleh aktif di VPS.
Arthur V1/legacy tidak dihapus dari database, tetapi harus tetap retired.
Dokumen ini menggantikan instruksi deploy Arthur lama yang memanggil
`scripts/seed_arthur.py`.

## Kontrak release

- Seed yang benar: `python -m arthur_v2.seed`.
- Model Arthur V2: `deepseek/deepseek-v4-flash`.
- Penanda record: `tools_config.system_plugin=arthur_v2`.
- Legacy wajib nonaktif: `ARTHUR_LEGACY_ENABLED=false` pada `deploy/.env.prod`.
- Jangan menjalankan `scripts/seed_arthur.py`, `preflight_arthur_release.py`,
  atau runbook progressive lama untuk mengaktifkan Arthur di release ini.

## Sebelum deploy

1. Pastikan branch/commit release memuat `arthur_v2/`,
   `app/core/system_agents/`, dan perubahan Google MCP yang diperlukan.
2. Pastikan `deploy/.env.prod` menyatakan:

   ```env
   ARTHUR_LEGACY_ENABLED=false
   ```

3. Validasi compose dan test V2 dari checkout release:

   ```bash
   docker compose -f deploy/docker-compose.prod.yml config --quiet
   python -m pytest -q tests/system_agents/test_arthur_v2.py
   ```

4. Jika Google Workspace dipakai, pastikan dua service terpisah tersedia:
   Google integration API (OAuth/token) dan Google Workspace MCP di mode
   `streamable-http` pada endpoint `/mcp`. Mode `stdio` tidak bisa dipakai oleh
   runtime agent HTTP.

## Deploy dan seed

Jalankan dari direktori project di VPS:

```bash
PROD_COMPOSE=(docker compose -f deploy/docker-compose.prod.yml)
"${PROD_COMPOSE[@]}" build api
"${PROD_COMPOSE[@]}" run --rm --no-deps api alembic upgrade head
"${PROD_COMPOSE[@]}" up -d --no-deps api scheduler
"${PROD_COMPOSE[@]}" exec api python -m arthur_v2.seed
```

`arthur_v2.seed` idempoten. Ia hanya create/update record V2 dan tidak
menyentuh Arthur legacy atau agent milik user.

## Verifikasi wajib

```bash
"${PROD_COMPOSE[@]}" exec api python - <<'PY'
import asyncio
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.agent import Agent

async def check():
    async with AsyncSessionLocal() as db:
        v2 = (await db.execute(
            select(Agent).where(Agent.tools_config["system_plugin"].astext == "arthur_v2")
        )).scalar_one()
        print({"id": str(v2.id), "model": v2.model, "plugin": v2.tools_config["system_plugin"]})

asyncio.run(check())
PY
"${PROD_COMPOSE[@]}" ps api scheduler wa-service wa-dev-service
```

Expected: satu record V2 dengan model DeepSeek V4 Flash dan plugin `arthur_v2`.
Lalu lakukan canary dari nomor WhatsApp Arthur: cek plan, buat agent kecil, dan
jika Google dipakai, verifikasi tool Sheet benar-benar ter-load setelah OAuth.

## Rollback

Rollback code/image tidak mengharuskan menghapus data Arthur V2. Jangan
mengaktifkan legacy sebagai rollback otomatis. Bila benar-benar perlu rollback
darurat ke V1, lakukan perubahan `ARTHUR_LEGACY_ENABLED=true` secara eksplisit,
redeploy API/scheduler, lalu seed legacy secara sadar dalam incident record.
