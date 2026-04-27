# Fase 6 — Roadmap & Prioritas Pengerjaan

Urutan pengerjaan berdasarkan **risk × effort**. Item dengan risk tinggi dan effort rendah
dikerjakan pertama.

---

## Matriks Prioritas

| Item | Risk | Effort | Prioritas |
|------|------|--------|-----------|
| 1.4 Hardcoded dev phone → env var | Tinggi | 15 menit | **P0** |
| 1.5 CORS lock down | Tinggi | 15 menit | **P0** |
| 1.7 Health check cek DB | Medium | 30 menit | **P0** |
| 1.3 Rate limiting | Tinggi | 2 jam | **P1** |
| 1.1 Scheduler jadi proses terpisah | Tinggi | 4 jam | **P1** |
| 1.6 docker-compose.prod.yml | Tinggi | 2 jam | **P1** |
| 5.5 PostgreSQL password | Tinggi | 30 menit | **P1** |
| 5.7 Input size validation | Medium | 1 jam | **P1** |
| 2.2 Dedup normalize_phone | Low | 1 jam | **P2** |
| 2.4 Magic numbers → config | Low | 2 jam | **P2** |
| 2.5 Fix except Exception: pass | Medium | 1 jam | **P2** |
| 4.1 Request ID middleware | Medium | 1 jam | **P2** |
| 4.5 Sentry integration | Medium | 2 jam | **P2** |
| 5.8 Docker socket hardening | Tinggi | 1 hari | **P2** |
| 3.1 Redis setup | Tinggi | 1 hari | **P3** |
| 3.2 Event bus → Redis | Tinggi | 1 hari | **P3** |
| 3.4 PgBouncer | Medium | 4 jam | **P3** |
| 2.1 Pecah agent_runner.py | Medium | 2 hari | **P3** |
| 2.3 Pecah wa_incoming() | Medium | 4 jam | **P3** |
| 4.2 Prometheus metrics | Medium | 1 hari | **P3** |
| 3.6 Sandbox resource limits | Medium | 2 jam | **P3** |
| 5.1 Multi-key API system | Medium | 1 hari | **P4** |
| 3.5 Background task queue | Medium | 2 hari | **P4** |
| 4.3 Grafana dashboard | Low | 1 hari | **P4** |
| 4.4 Alerting rules | Medium | 4 jam | **P4** |
| 3.7 WA deduplication | Medium | 4 jam | **P4** |

---

## Sprint Plan

### Sprint 0 — Quick Wins (1 hari kerja)
**Target: Eliminasi semua masalah yang bisa dikerjakan dalam < 1 jam**

```
Pagi (2 jam):
  ✓ 1.4 Pindahkan DEVELOPER_PHONE ke env var
  ✓ 1.5 Lock down CORS via config
  ✓ 1.7 Perbaiki /health endpoint
  ✓ 5.5 Ganti PostgreSQL default password
  ✓ 5.7 Tambah input size validation di schemas

Siang (3 jam):
  ✓ 1.6 Buat docker-compose.prod.yml dengan restart policy
  ✓ 1.3 Tambah rate limiting (slowapi)

Sore (2 jam):
  ✓ 2.2 Buat phone_utils.py, dedup normalize_phone
  ✓ 2.5 Fix except Exception: pass yang tersisa
  ✓ 4.1 Tambah RequestIDMiddleware
```

**Hasil Sprint 0:** Platform aman untuk soft launch dengan 1 server, single process.

---

### Sprint 1 — Reliability (3-5 hari kerja)
**Target: Platform tidak akan kehilangan data atau silent fail**

```
Hari 1:
  ✓ 1.1 Pisahkan scheduler jadi proses terpisah
      - Buat app/scheduler_worker.py
      - Tambah service 'scheduler' di docker-compose.prod.yml
      - Test: pastikan reminder tetap jalan setelah API restart

Hari 2:
  ✓ 2.4 Pindahkan magic numbers ke config
  ✓ 2.6 Hilangkan import di dalam fungsi (yang tidak circular)
  ✓ 4.5 Integrasikan Sentry

Hari 3:
  ✓ 5.8 Hardening Docker sandbox (drop caps, resource limits, network disable)
  ✓ 3.6 Resource limits untuk sandbox containers

Hari 4-5:
  ✓ 2.3 Pecah wa_incoming() handler menjadi helper functions
  ✓ 2.7 Perbaiki type hints di run_agent()
```

**Hasil Sprint 1:** Bisa handle 50-100 concurrent user dengan konfidence.

---

### Sprint 2 — Scale Foundation (1 minggu)
**Target: Bisa scale ke multi-instance**

```
Hari 1-2:
  ✓ 3.1 Tambahkan Redis ke stack
  ✓ 3.2 Migrasi event_bus.py ke Redis pub/sub
  ✓ 3.3 Rate limiting berbasis Redis (gantikan slowapi sederhana)

Hari 3:
  ✓ 3.4 Setup PgBouncer
  ✓ Tuning SQLAlchemy pool settings

Hari 4-5:
  ✓ 4.2 Setup Prometheus + Grafana
  ✓ 4.3 Buat dashboard dengan panel kunci
  ✓ 4.4 Setup alerting rules
```

**Hasil Sprint 2:** Bisa di-scale ke 2+ API workers. Monitoring aktif.

---

### Sprint 3 — Maintainability (1-2 minggu)
**Target: Codebase bisa di-maintain dan dikembangkan tanpa takut breaking**

```
  ✓ 2.1 Pecah agent_runner.py → tool_builder, prompt_builder, subagent_builder, context_service
  ✓ 5.1 Multi-key API key system
  ✓ 3.5 Background task untuk agent execution
  ✓ 3.7 WA message deduplication
  ✓ 4.3 OpenTelemetry tracing (jika budget ada)
```

---

## Kapasitas Perkiraan per Sprint

| Kondisi | Max Concurrent Users | Reliability |
|---------|---------------------|-------------|
| Sekarang (before Sprint 0) | ~20 | Fragile — bisa crash silent |
| Setelah Sprint 0 | ~50 | Stable single-server |
| Setelah Sprint 1 | ~100 | Reliable single-server |
| Setelah Sprint 2 | ~300-500 | Scalable multi-worker |
| Setelah Sprint 3 | ~500+ | Production-grade |

---

## Dependencies Kritis

```
Sprint 0 tidak ada dependency → bisa langsung mulai

Sprint 1 butuh Sprint 0 selesai

Sprint 2 butuh:
  - Sprint 0 ✓
  - Redis instance tersedia (1 jam setup)
  - Grafana/Prometheus instance tersedia

Sprint 3 butuh Sprint 1 dan 2 selesai
```

---

## Yang Tidak Perlu Dikerjakan (Sekarang)

Beberapa hal yang mungkin terasa penting tapi bukan prioritas untuk 500 user:

- **Testing suite** — tidak ada test saat ini dan menambah test untuk codebase yang berubah
  cepat lebih mahal daripada benefitnya saat ini. Fokus ke integration test untuk critical path
  saja (scheduler delivery, WA reply routing).

- **Kubernetes** — Docker Compose sudah cukup untuk 500 user di satu VM yang proper
  (8 core, 32GB RAM). K8s menambah kompleksitas operasional yang tidak sebanding benefitnya.

- **Microservices split** — agent_runner sebagai service terpisah, dll. Premature optimization.
  Monolith yang di-tuned lebih baik dari microservices yang under-resourced.

- **Multi-region** — tidak relevan sampai ada requirement spesifik latency atau compliance.

---

## Sizing Rekomendasi Server untuk 500 User

Asumsi: 500 registered user, ~50 concurrent active, agent runs 10-30 detik rata-rata.

| Component | Spec | Alasan |
|-----------|------|--------|
| API server | 4 vCPU, 8GB RAM | 1 worker (async), RAM untuk embedding model (~500MB) |
| PostgreSQL | 2 vCPU, 4GB RAM, 50GB SSD | Shared sessions + pgvector |
| Redis | 1 vCPU, 1GB RAM | Pub/sub + rate limiting, mostly in-memory |
| Docker sandbox host | 4 vCPU, 8GB RAM | Max 10 concurrent sandbox containers |

**Total biaya perkiraan:**
- Self-hosted VPS: ~$80-150/bulan (Hetzner, DigitalOcean, Vultr)
- OpenRouter LLM: tergantung usage (gpt-4o-mini ~$0.15/1M tokens)
