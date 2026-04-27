# Production Readiness Plan

Dokumen ini berisi rencana bertahap untuk membawa platform ke kondisi production-grade
yang mampu melayani 500+ user aktif secara andal.

## Dokumen dalam Folder Ini

| File | Isi |
|------|-----|
| [01-critical-blockers.md](01-critical-blockers.md) | Blocker yang harus diselesaikan SEBELUM go-live |
| [02-code-quality.md](02-code-quality.md) | Refactor kode untuk maintainability jangka panjang |
| [03-scaling.md](03-scaling.md) | Arsitektur untuk horizontal scaling & high traffic |
| [04-observability.md](04-observability.md) | Monitoring, alerting, dan debugging di production |
| [05-security.md](05-security.md) | Hardening keamanan sebelum expose ke public |
| [06-roadmap.md](06-roadmap.md) | Urutan pengerjaan dan estimasi effort |
| [07-ai-staff-features.md](07-ai-staff-features.md) | Fitur baru: allowlist, on/off chat, voice note |

## Prioritas Singkat

```
FASE 1 (Wajib sebelum launch)  → 01-critical-blockers.md
FASE 2 (Sebelum scale-out)     → 02-code-quality.md + 05-security.md
FASE 3 (Saat traffic naik)     → 03-scaling.md + 04-observability.md
```
