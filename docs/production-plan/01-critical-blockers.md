# Fase 1 — Critical Blockers (Wajib Sebelum Go-Live)

Ini adalah masalah yang akan menyebabkan **data loss, downtime, atau silent failure**
bahkan dengan traffic ringan. Harus diselesaikan sebelum ada user nyata.

---

## 1.1 APScheduler In-Process → External Scheduler

### Masalah
`scheduler_service.py` menjalankan APScheduler di dalam proses FastAPI yang sama.

**Konsekuensi nyata:**
- Setiap kali server restart (deploy, crash, OOM kill), **semua reminder yang sedang berjalan
  hilang mid-execution** tanpa recovery.
- Kalau nanti pakai Gunicorn multi-worker atau 2 instance, reminder akan **dijalankan duplikat**
  oleh setiap worker, atau tidak sama sekali karena race condition.
- `_tick()` spawn `asyncio.create_task()` per job — task ini tidak punya error boundary.
  Kalau satu task crash, tidak ada yang tahu.

### Solusi

**Option A (Rekomendasi — cepat):** Pisahkan scheduler jadi proses terpisah.

```
# docker-compose.yml tambahkan:
scheduler:
  build: .
  command: python -m app.scheduler_worker
  env_file: .env
  depends_on:
    postgres:
      condition: service_healthy
```

```python
# app/scheduler_worker.py — entry point baru
import asyncio
from app.core.scheduler_service import run_scheduler_loop

if __name__ == "__main__":
    asyncio.run(run_scheduler_loop())
```

Dengan ini, scheduler jalan di proses sendiri, tidak terpengaruh restart API,
dan hanya ada satu instance yang jalan.

**Option B (Jangka panjang):** Migrasi ke `pg-boss` (PostgreSQL-native job queue)
atau `Celery + Redis`. Lebih robust, tapi effort lebih besar.

**Immediate fix sementara** (sebelum opsi di atas siap): Tambahkan distributed lock
di `_tick()` menggunakan PostgreSQL advisory lock, agar kalau ada 2 instance scheduler,
hanya satu yang jalan per tick.

```python
async def _tick_with_lock() -> None:
    async with AsyncSessionLocal() as db:
        # Coba acquire advisory lock (non-blocking)
        result = await db.execute(text("SELECT pg_try_advisory_lock(12345)"))
        acquired = result.scalar()
        if not acquired:
            return  # instance lain sedang tick
    await _tick()
    async with AsyncSessionLocal() as db:
        await db.execute(text("SELECT pg_advisory_unlock(12345)"))
        await db.commit()
```

---

## 1.2 In-Memory Event Bus → Tidak Bisa Multi-Process

### Masalah
`event_bus.py` menyimpan subscriber dict di memory Python:
```python
_subscribers: dict[str, list[asyncio.Queue]] = {}
```

**Konsekuensi nyata:**
- Scheduler publish event ke process A, tapi SSE client connect ke process B → **event tidak sampai**.
- Bahkan single-process: kalau server restart saat user sedang subscribe SSE,
  koneksi hilang dan tidak ada reconnect logic di sisi server.

### Solusi

**Jangka pendek:** Pastikan hanya 1 worker (`--workers 1`) dan dokumentasikan keterbatasan ini.
Ini acceptable untuk early production dengan traffic rendah.

**Jangka menengah:** Ganti `event_bus.py` dengan Redis pub/sub.

```python
# app/core/event_bus_redis.py
import redis.asyncio as redis
import json

_redis: redis.Redis | None = None

async def get_redis():
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.redis_url)
    return _redis

async def publish(session_id: str, event: dict) -> None:
    r = await get_redis()
    await r.publish(f"session:{session_id}", json.dumps(event))

async def subscribe(session_id: str):
    r = await get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(f"session:{session_id}")
    return pubsub
```

Dengan Redis, event bus bisa multi-process dan multi-instance.

---

## 1.3 Tidak Ada Rate Limiting

### Masalah
Endpoint `/v1/agents/{id}/sessions/{session_id}/messages` tidak ada rate limiting.
Setiap request bisa trigger LLM call (mahal) dan Docker container spawn (resource-intensive).

**Skenario bahaya:**
- 1 user kirim 100 pesan dalam 10 detik → 100 LLM calls bersamaan → bill OpenRouter meledak.
- WhatsApp delivery retry jika server lambat → duplicate message → agent dipanggil dua kali.

### Solusi

Gunakan `slowapi` (rate limiting untuk FastAPI):

```python
# requirements.txt
slowapi>=0.1.9

# app/main.py
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# app/api/messages.py
@router.post("/{session_id}/messages")
@limiter.limit("10/minute")  # per IP
async def send_message(request: Request, ...):
    ...
```

Untuk WhatsApp channel (bot-to-bot), gunakan key dari `X-API-Key` bukan IP.

---

## 1.4 Hardcoded Developer Phone di Source Code

### Masalah
```python
# app/api/channels.py:453
_DEVELOPER_PHONE = "62895619356936"
```

Nomor HP hardcoded di source code. Ini akan:
- Bocor di git history selamanya
- Tidak bisa diganti tanpa redeploy
- Mempersulit deployment ke environment berbeda (staging vs production)

### Solusi

```python
# app/config.py — tambahkan:
developer_phone: str = ""  # nomor untuk notifikasi error ke developer

# app/api/channels.py
from app.config import get_settings
_DEVELOPER_PHONE = get_settings().developer_phone
```

```bash
# .env
DEVELOPER_PHONE=62895619356936
```

---

## 1.5 CORS Allow All Origins

### Masalah
```python
# app/main.py:62
allow_origins=["*"],  # lock down in production  ← komentar sendiri bilang ini salah
```

Bahkan ada komentar "lock down in production" tapi tidak pernah dikerjakan.

### Solusi

```python
# app/config.py
allowed_origins: list[str] = ["http://localhost:3000"]

# app/main.py
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    ...
)
```

```bash
# .env production
ALLOWED_ORIGINS=["https://your-frontend-domain.com"]
```

---

## 1.6 docker-compose.yml Production — Tidak Ada Health Restart

### Masalah
Tidak ada `restart: unless-stopped` di service API. Kalau crash, tidak auto-restart.
Command masih `--reload` (development mode):

```yaml
command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload  # SALAH untuk prod
```

### Solusi

Buat `docker-compose.prod.yml` terpisah:

```yaml
version: "3.9"

services:
  api:
    build: .
    restart: unless-stopped
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log
    ports:
      - "8000:8000"
    env_file: .env.prod
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
    depends_on:
      postgres:
        condition: service_healthy

  scheduler:
    build: .
    restart: unless-stopped
    command: python -m app.scheduler_worker
    env_file: .env.prod
    depends_on:
      postgres:
        condition: service_healthy

  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  postgres_data:
  sandbox_data:
```

---

## 1.7 `/health` Endpoint Tidak Cek Dependencies

### Masalah
```python
@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.1.0"}  # selalu OK, bahkan saat DB mati
```

Load balancer atau monitoring tidak bisa detect kalau DB mati.

### Solusi

```python
@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict:
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    status = "ok" if db_ok else "degraded"
    if not db_ok:
        return JSONResponse(
            status_code=503,
            content={"status": status, "db": "unreachable"}
        )
    return {"status": status, "version": "0.2.0", "db": "ok"}
```

---

## Checklist Fase 1

- [ ] 1.1 Pisahkan scheduler jadi proses terpisah (atau minimal tambahkan advisory lock)
- [ ] 1.2 Dokumentasikan single-worker constraint; roadmap Redis pub/sub
- [ ] 1.3 Tambahkan rate limiting di endpoint message
- [ ] 1.4 Pindahkan `DEVELOPER_PHONE` ke env var
- [ ] 1.5 Lock down CORS origins via config
- [ ] 1.6 Buat `docker-compose.prod.yml` dengan restart policy + production command
- [ ] 1.7 Perbaiki `/health` endpoint agar cek DB
