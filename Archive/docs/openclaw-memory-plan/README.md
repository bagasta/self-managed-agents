# OpenClaw Memory System — Implementation Plan

Adopting OpenClaw's layered memory + heartbeat system into the managed-agents-project SaaS platform.

## Files

| File | Isi |
|------|-----|
| [01-memory-layers.md](./01-memory-layers.md) | Layer memory: soul, user profile, daily, longterm |
| [02-system-message.md](./02-system-message.md) | AGENTS.md equivalent di system prompt |
| [03-heartbeat.md](./03-heartbeat.md) | Heartbeat mechanism & proactive agent |
| [04-implementation.md](./04-implementation.md) | Rencana implementasi teknis & urutan kerja |

## Latar Belakang

OpenClaw menggunakan file-file Markdown (`SOUL.md`, `USER.md`, `MEMORY.md`, `memory/YYYY-MM-DD.md`) sebagai sistem memory agent. Karena project ini adalah SaaS multi-tenant, file MD tidak bisa disimpan di filesystem — semua memory harus tinggal di database, dengan akses via tools.

## Prinsip Desain

- **System message = ringkas** — hanya operasional inti + identity agent
- **Memory = DB-backed** — disimpan di `agent_memories` table dengan typed keys
- **Baca = auto-inject** — `agent_runner.py` inject memory ke system prompt (0 steps)
- **Tulis = tool call** — agent panggil `remember()` / `update_daily()` saat sesi
- **Proaktif = heartbeat** — scheduler nudge agent secara berkala untuk background tasks
