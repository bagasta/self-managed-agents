# Fase 4 — Observability

Di production, kamu tidak debug dengan print statement — kamu debug dengan data.
Fase ini memastikan kamu tahu masalah SEBELUM user complain.

---

## 4.1 Structured Logging yang Actionable

### Kondisi Saat Ini — Sudah Bagus
`structlog` sudah dipakai dengan JSON output di production mode. Ini fondasi yang solid.

### Yang Kurang

**A. Request ID tidak di-propagate**

Setiap HTTP request harus punya `request_id` unik yang muncul di semua log dalam request tersebut.
Saat ini, `run_id` ada tapi hanya untuk agent run, bukan HTTP request level.

```python
# app/middleware/request_id.py
import uuid
import structlog
from starlette.middleware.base import BaseHTTPMiddleware

class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        structlog.contextvars.unbind_contextvars("request_id")
        return response
```

**B. Log level yang hilang**

`run_agent()` punya banyak `log.debug()` yang berguna saat debugging tapi terlalu noisy di INFO.
Pastikan `LOG_LEVEL=DEBUG` bisa di-toggle tanpa restart (via env var yang di-re-read).

**C. Access log dimatikan di production command**

```yaml
# docker-compose.prod.yml — `--no-access-log` menghilangkan request log
command: uvicorn app.main:app --no-access-log  # ← ini terlalu agresif
```

Ganti dengan custom access log yang lebih berguna:
```python
# app/middleware/access_log.py
class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000
        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        return response
```

---

## 4.2 Metrics — Prometheus + Grafana

### Setup

```python
# requirements.txt tambahkan:
prometheus-fastapi-instrumentator>=6.0.0

# app/main.py
from prometheus_fastapi_instrumentator import Instrumentator

Instrumentator().instrument(app).expose(app, endpoint="/metrics")
```

### Custom Metrics yang Penting untuk Platform Ini

```python
# app/core/metrics.py
from prometheus_client import Counter, Histogram, Gauge

agent_runs_total = Counter(
    "agent_runs_total",
    "Total agent runs",
    ["agent_id", "status"],  # status: success | error | timeout
)

agent_run_duration = Histogram(
    "agent_run_duration_seconds",
    "Duration of agent runs",
    ["agent_id"],
    buckets=[1, 5, 10, 30, 60, 120, 300],
)

llm_tokens_used = Counter(
    "llm_tokens_used_total",
    "Total LLM tokens consumed",
    ["agent_id", "model"],
)

sandbox_containers_active = Gauge(
    "sandbox_containers_active",
    "Number of active Docker sandbox containers",
)

scheduled_jobs_due = Gauge(
    "scheduled_jobs_due_total",
    "Number of scheduled jobs that are due but not yet executed",
)

wa_messages_received = Counter(
    "wa_messages_received_total",
    "WhatsApp messages received",
    ["device_id"],
)
```

### Grafana Dashboard — Panel Kunci

| Panel | Query | Alert jika |
|-------|-------|------------|
| Agent error rate | `rate(agent_runs_total{status="error"}[5m])` | > 5% |
| P95 agent latency | `histogram_quantile(0.95, agent_run_duration_seconds)` | > 60s |
| Token burn rate | `rate(llm_tokens_used_total[1h])` | > budget |
| Active sandboxes | `sandbox_containers_active` | > max |
| Scheduler lag | `scheduled_jobs_due_total` | > 10 |

---

## 4.3 Distributed Tracing

### Masalah
Saat ini tidak ada cara untuk melihat flow lengkap satu request:
HTTP → agent_runner → LLM call → tool call → DB write → WA reply.

### Solusi: OpenTelemetry

```python
# requirements.txt
opentelemetry-sdk>=1.20.0
opentelemetry-instrumentation-fastapi>=0.41b0
opentelemetry-instrumentation-sqlalchemy>=0.41b0
opentelemetry-exporter-otlp>=1.20.0

# app/main.py
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

def setup_tracing(app):
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    SQLAlchemyInstrumentor().instrument()
```

Gunakan **Jaeger** (self-hosted) atau **Tempo** (kalau pakai Grafana stack) sebagai backend.

Minimal: tambahkan `run_id` ke semua log yang relevan (sudah ada, tinggal pastikan konsisten).

---

## 4.4 Alerting

### Alert Kritis (page on-call)

```yaml
# alertmanager rules
groups:
  - name: managed-agents
    rules:
      - alert: AgentHighErrorRate
        expr: rate(agent_runs_total{status="error"}[5m]) > 0.1
        for: 2m
        annotations:
          summary: "Agent error rate > 10% selama 2 menit"

      - alert: DatabaseUnreachable
        expr: up{job="postgres"} == 0
        for: 1m
        annotations:
          summary: "PostgreSQL tidak bisa diakses"

      - alert: SchedulerJobsAccumulating
        expr: scheduled_jobs_due_total > 20
        for: 5m
        annotations:
          summary: "Scheduler lag: >20 job menumpuk tidak dieksekusi"

      - alert: WAServiceDown
        expr: probe_success{job="wa-service"} == 0
        for: 2m
        annotations:
          summary: "WhatsApp service tidak respond"
```

### Alert Warning (Slack notification)

- Token usage > 80% dari daily budget
- P95 latency naik > 2x dari baseline
- Disk usage sandbox > 70%

---

## 4.5 Error Tracking

### Setup Sentry

```python
# requirements.txt
sentry-sdk[fastapi]>=1.40.0

# app/main.py
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
        traces_sample_rate=0.1,   # 10% request di-trace, cukup untuk prod
        environment=settings.environment,
    )
```

```python
# app/config.py
sentry_dsn: str = ""
environment: str = "production"  # atau "staging"
```

Keuntungan vs pure logging:
- Grouping error yang sama otomatis
- Stack trace dengan variable values
- Alert per-error type dengan threshold
- Breadcrumbs: rekonstruksi apa yang terjadi sebelum error

---

## 4.6 Health Checks yang Komprehensif

```python
# app/api/health.py
@router.get("/health/detailed")
async def health_detailed(db: AsyncSession = Depends(get_db)):
    checks = {}

    # DB check
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"

    # Redis check (jika dipakai)
    try:
        r = redis.from_url(settings.redis_url)
        await r.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    # Scheduler check
    from app.core.scheduler_service import is_scheduler_running
    checks["scheduler"] = "ok" if is_scheduler_running() else "stopped"

    # WA service check
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.wa_service_url}/health")
            checks["wa_service"] = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
    except Exception:
        checks["wa_service"] = "unreachable"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ok" if all_ok else "degraded", "checks": checks}
    )
```

---

## Checklist Fase 4

- [x] 4.1 Tambahkan RequestIDMiddleware — `app/middleware/request_id.py`, ditambah ke main.py
- [x] 4.2 Setup Prometheus metrics (instrumentator + custom metrics) — `app/core/metrics.py`, prometheus-fastapi-instrumentator di requirements.txt, Instrumentator().instrument(app) di main.py, endpoint /metrics
- [ ] 4.3 Buat Grafana dashboard dengan 5 panel kunci
- [ ] 4.4 Setup alerting untuk error rate dan scheduler lag
- [x] 4.5 Integrasikan Sentry untuk error tracking — conditional init di main.py berdasarkan `settings.sentry_dsn`, FastApiIntegration + SqlalchemyIntegration
- [x] 4.6 Perluas `/health` endpoint ke `/health/detailed` — endpoint baru di main.py, cek database + scheduler + wa_service, return 503 jika ada yang degraded
