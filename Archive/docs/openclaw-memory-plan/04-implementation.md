# 04 — Rencana Implementasi

## Urutan Pengerjaan

### Phase 1: Memory Layers (Fondasi)

**1a. Extend `memory_service.py`**
- Tambah support typed keys (`soul`, `user_profile`, `daily:*`, `longterm`, `heartbeat:*`)
- Tambah method `get_layer(agent_id, external_user_id, layer_key)`
- Tambah method `set_layer(agent_id, external_user_id, layer_key, content)`

**1b. Update `agent_runner.py`**
- Setelah load agent config, load memory layers dari DB
- Inject ke system prompt sebelum agent dijalankan:
  - `soul` (fallback ke `agent.instructions` kalau kosong)
  - `user_profile`
  - `daily:hari-ini` + `daily:kemarin`
- Longterm TIDAK auto-inject

**1c. Update Memory Tools (`app/core/tools/`)**
- `remember(key, value)` → sudah ada, pastikan support typed keys
- `recall(key)` → sudah ada, pastikan support typed keys
- Tambah `update_daily(content)` → shortcut untuk tulis ke `daily:YYYY-MM-DD`
- Tambah `update_longterm(content)` → append/update `longterm`

**1d. System Message Template**
- Buat template string di `agent_runner.py` (lihat `02-system-message.md`)
- Template render dengan memory yang sudah di-load

---

### Phase 2: Arthur Integration

**2a. Arthur Soul Template**
- Seed `soul` untuk Arthur di DB saat startup / seed script
- Arthur wajib generate `soul` untuk setiap agent yang dia buat

**2b. Arthur Memory**
- Arthur simpan log pembuatan agent ke `daily:*`
- Arthur simpan preferensi arsitektur user ke `longterm`

---

### Phase 3: Heartbeat

**3a. `tools_config` Schema**
- Tambah field `heartbeat: { enabled, interval_minutes, quiet_hours }`

**3b. Heartbeat Job di `scheduler_service.py`**
- Saat agent dibuat dengan `heartbeat.enabled: true` → auto-register job APScheduler
- Job trigger internal heartbeat message ke agent session

**3c. Heartbeat Handler di `agent_runner.py`**
- Deteksi heartbeat message
- Agent load `recall("heartbeat:checklist")` → jalankan
- Kalau hasil perlu notif → push via SSE / channel
- Kalau tidak → log OK, tidak kirim

**3d. Quiet Hours**
- Cek timezone dari `user_profile`
- Suppress notifikasi di luar jam aktif

---

## File yang Diubah

| File | Perubahan |
|------|-----------|
| `app/core/memory_service.py` | Typed key support, layer methods |
| `app/core/agent_runner.py` | Auto-inject memory layers, system message template, heartbeat handler |
| `app/core/tools/builder_tools.py` | Update/Daily/Longterm memory tools |
| `app/core/scheduler_service.py` | Heartbeat job registration |
| `app/models/agents.py` | Tambah heartbeat config ke tools_config schema |
| `scripts/seed_arthur.py` | Seed soul Arthur ke DB |

## File Baru

| File | Isi |
|------|-----|
| `app/core/tools/memory_tools.py` | `update_daily`, `update_longterm` tools |
| `app/core/heartbeat_service.py` | Logic heartbeat: schedule, quiet hours, checklist runner |

---

## Estimasi Kompleksitas

| Phase | Estimasi |
|-------|----------|
| Phase 1: Memory Layers | Medium — extend yang sudah ada |
| Phase 2: Arthur Integration | Small — mostly seed data + prompt |
| Phase 3: Heartbeat | Medium-Large — service baru, integrasi scheduler + SSE |

---

## Pertanyaan yang Perlu Dijawab Sebelum Implementasi

1. Apakah `soul` per agent bisa di-edit via API? Atau hanya Arthur yang bisa set?
2. `user_profile` diisi siapa? Arthur saat onboarding, atau user sendiri via chat?
3. Heartbeat notifikasi dikirim ke mana defaultnya? SSE? WhatsApp? Tergantung channel aktif?
4. Apakah longterm memory punya size limit? Perlu auto-summarize kalau terlalu panjang?
