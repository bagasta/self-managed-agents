# Fase 3 — Scaling Architecture

Arsitektur untuk menangani 500+ user aktif dengan reliability tinggi.
Fase ini relevan setelah Fase 1 dan 2 selesai.

---

## 3.1 Arsitektur Target

```
                    ┌─────────────────────────────────────────┐
                    │              Load Balancer               │
                    │           (Nginx / Caddy / ALB)          │
                    └──────────────────┬──────────────────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
    ┌─────────▼──────┐       ┌─────────▼──────┐       ┌────────▼───────┐
    │   API Worker 1  │       │   API Worker 2  │       │  API Worker 3  │
    │   (FastAPI)     │       │   (FastAPI)     │       │  (FastAPI)     │
    └─────────┬───────┘       └─────────┬───────┘       └────────┬───────┘
              │                         │                         │
              └──────────────┬──────────┘─────────────┘          │
                             │                                    │
                    ┌────────▼────────┐              ┌────────────▼──────┐
                    │   Redis          │              │   Scheduler       │
                    │  (pub/sub +      │              │   Worker          │
                    │   rate limit)    │              │  (1 instance)     │
                    └────────┬────────┘              └────────────┬──────┘
                             │                                    │
                    ┌────────▼────────────────────────────────────▼──────┐
                    │                  PostgreSQL                         │
                    │           (primary + optional read replica)         │
                    └────────────────────────────────────────────────────┘
```

---

## 3.2 Redis — Fondasi untuk Multi-Instance

Redis diperlukan untuk 3 hal sekaligus:

### A. Event Bus (SSE)
Menggantikan `event_bus.py` yang in-memory.

```python
# app/core/event_bus.py — versi Redis
import redis.asyncio as redis
import json, asyncio
from app.config import get_settings

async def publish(session_id: str, event: dict) -> None:
    r = redis.from_url(get_settings().redis_url)
    async with r:
        await r.publish(f"session:{session_id}", json.dumps(event))

async def subscribe_generator(session_id: str):
    """AsyncGenerator untuk SSE endpoint."""
    r = redis.from_url(get_settings().redis_url)
    async with r.pubsub() as pubsub:
        await pubsub.subscribe(f"session:{session_id}")
        async for message in pubsub.listen():
            if message["type"] == "message":
                yield json.loads(message["data"])
```

### B. Rate Limiting
```python
# app/core/rate_limiter.py
from redis.asyncio import Redis
import time

async def check_rate_limit(redis: Redis, key: str, limit: int, window_seconds: int) -> bool:
    """Returns True if request is allowed, False if rate limited."""
    pipe = redis.pipeline()
    now = int(time.time())
    window_start = now - window_seconds
    pipe.zremrangebyscore(key, 0, window_start)
    pipe.zadd(key, {str(now): now})
    pipe.zcard(key)
    pipe.expire(key, window_seconds)
    results = await pipe.execute()
    return results[2] <= limit
```

### C. Scheduler Distributed Lock
Mencegah duplicate job execution saat ada 2+ scheduler worker.

```python
async def acquire_tick_lock(redis: Redis, ttl_seconds: int = 90) -> bool:
    """Returns True jika lock berhasil di-acquire."""
    result = await redis.set("scheduler:tick_lock", "1", nx=True, ex=ttl_seconds)
    return result is True
```

### Setup Redis di docker-compose.prod.yml

```yaml
redis:
  image: redis:7-alpine
  restart: unless-stopped
  command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru
  volumes:
    - redis_data:/data
  healthcheck:
    test: ["CMD", "redis-cli", "ping"]
    interval: 10s
    timeout: 5s
    retries: 3
```

```python
# app/config.py
redis_url: str = "redis://localhost:6379/0"
```

---

## 3.3 Database Connection Pooling

### Masalah Saat Ini
Default SQLAlchemy pool dengan multi-worker akan membuka terlalu banyak koneksi ke PostgreSQL.
500 user aktif × N workers = potentially hundreds of idle connections.

### Solusi: PgBouncer

Tambahkan PgBouncer sebagai connection pooler antara FastAPI dan PostgreSQL:

```yaml
# docker-compose.prod.yml
pgbouncer:
  image: edoburu/pgbouncer:latest
  restart: unless-stopped
  environment:
    DB_USER: ${POSTGRES_USER}
    DB_PASSWORD: ${POSTGRES_PASSWORD}
    DB_HOST: postgres
    DB_NAME: ${POSTGRES_DB}
    POOL_MODE: transaction        # transaction pooling untuk async
    MAX_CLIENT_CONN: 200          # max koneksi dari FastAPI workers
    DEFAULT_POOL_SIZE: 20         # koneksi nyata ke PostgreSQL
  depends_on:
    - postgres
```

```bash
# .env.prod — arahkan ke PgBouncer, bukan langsung ke PostgreSQL
DATABASE_URL=postgresql+asyncpg://postgres:password@pgbouncer:5432/managed_agents
```

### SQLAlchemy Pool Settings

```python
# app/database.py
engine = create_async_engine(
    settings.database_url,
    pool_size=5,           # per worker — kalau 4 worker = 20 koneksi total ke PgBouncer
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,     # recycle koneksi tiap 30 menit
    pool_pre_ping=True,    # cek koneksi sebelum pakai (deteksi koneksi mati)
)
```

---

## 3.4 Agent Execution — Async Queue

### Masalah Saat Ini
`run_agent()` dipanggil secara synchronous dalam request-response cycle.
Satu agent run bisa 5-300 detik → Uvicorn worker tertahan → throughput turun drastis.

Request flow saat ini:
```
User → POST /messages → run_agent() (blocking 30s) → response
```

Dengan 10 user concurrent, 10 workers langsung penuh.

### Solusi: Background Task + Polling / WebSocket

**Short-term:** Gunakan FastAPI `BackgroundTasks` untuk non-WA channels.

```python
@router.post("/{session_id}/messages")
async def send_message(
    session_id: uuid.UUID,
    body: MessageRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    run_id = uuid.uuid4()
    # Simpan status "pending" ke DB
    background_tasks.add_task(run_agent_async, run_id, session_id, body.message)
    return {"run_id": str(run_id), "status": "pending"}

# Client polling:
# GET /v1/runs/{run_id} → {"status": "running"} atau {"status": "done", "reply": "..."}
```

**Long-term:** Celery task queue dengan Redis broker:

```python
# app/tasks.py
from celery import Celery

celery_app = Celery("agents", broker=settings.redis_url, backend=settings.redis_url)

@celery_app.task(bind=True, max_retries=2, time_limit=300)
def run_agent_task(self, run_id: str, session_id: str, message: str):
    ...
```

---

## 3.5 Docker Sandbox — Resource Limits

### Masalah Saat Ini
`DockerSandbox` tidak ada CPU/memory limit. Kalau agent menulis loop Python infinite,
container akan eat semua CPU host.

### Solusi

```python
# app/core/sandbox.py — tambahkan resource constraints
container = client.containers.run(
    image=settings.docker_sandbox_image,
    command="sleep 300",
    detach=True,
    mem_limit="256m",         # max 256MB RAM per sandbox
    memswap_limit="256m",     # tidak boleh swap
    cpu_period=100000,
    cpu_quota=25000,          # 25% CPU (0.25 core)
    network_disabled=True,    # isolasi network (kecuali yang butuh http)
    read_only=False,
    volumes={str(workspace): {"bind": "/workspace", "mode": "rw"}},
    remove=False,
)
```

Juga tambahkan limit total container yang bisa jalan bersamaan:
```python
# Cek jumlah sandbox container sebelum spawn baru
running = client.containers.list(filters={"label": "managed-agent-sandbox"})
if len(running) >= settings.max_concurrent_sandboxes:
    raise RuntimeError("Too many concurrent sandbox executions")
```

---

## 3.6 WhatsApp Idempotency

### Masalah
WA delivery bisa retry (kalau server lambat). Pesan yang sama bisa masuk 2x,
menyebabkan agent run 2x → 2 replies ke user.

### Solusi: Message deduplication

```python
# WAIncomingMessage sudah ada timestamp — gunakan sebagai idempotency key

async def _is_duplicate_message(device_id: str, from_phone: str, timestamp: int, db) -> bool:
    # Cek apakah message dengan timestamp+from sudah pernah diproses
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    result = await db.execute(
        select(Message).where(
            Message.role == "user",
            Message.created_at >= cutoff,
            # Store message_id di metadata
            Message.metadata_["wa_timestamp"].astext == str(timestamp),
            Message.metadata_["wa_from"].astext == from_phone,
        )
    )
    return result.scalar_one_or_none() is not None
```

---

## Checklist Fase 3

- [ ] 3.1 Tambahkan Redis ke docker-compose.prod.yml
- [ ] 3.2 Migrasi event_bus.py ke Redis pub/sub
- [ ] 3.3 Implementasi rate limiter berbasis Redis
- [ ] 3.4 Setup PgBouncer dan tuning SQLAlchemy pool
- [ ] 3.5 Background task untuk agent execution (non-WA channel)
- [ ] 3.6 Tambahkan resource limits ke DockerSandbox
- [ ] 3.7 Implementasi WA message deduplication
