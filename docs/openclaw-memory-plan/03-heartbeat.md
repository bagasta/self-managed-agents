# 03 — Heartbeat System

## Apa itu Heartbeat?

Heartbeat adalah mekanisme "nudge" berkala ke agent agar bisa bekerja **proaktif** tanpa menunggu pesan dari user.

Tanpa heartbeat → agent **reactive** (hanya jawab kalau ditanya)  
Dengan heartbeat → agent **proactive** (inisiatif cek, ingatkan, kerjakan background tasks)

## Cara Kerja

```
Scheduler (APScheduler)
  → tiap X menit kirim "heartbeat message" ke session agent
    → agent jalankan HEARTBEAT.md checklist
      → kalau ada yang penting → kirim notifikasi ke user
      → kalau tidak ada → HEARTBEAT_OK (diam)
```

## HEARTBEAT.md Equivalent

Di OpenClaw, agent baca file `HEARTBEAT.md` saat heartbeat tiba. Di project ini, konten heartbeat checklist disimpan di:

```
agent_memories key: "heartbeat:checklist"
```

Contoh isi:
```
- Cek apakah ada reminder yang hampir jatuh tempo
- Cek apakah ada task yang pending lebih dari 24 jam
- Update daily memory kalau belum ditulis hari ini
```

## Heartbeat State

Track kapan terakhir cek sesuatu:

```
agent_memories key: "heartbeat:state"
value (JSON):
{
  "last_check": {
    "reminders": 1746500000,
    "daily_memory": 1746490000,
    "custom_check_1": null
  }
}
```

## Implementasi di Project Ini

Project sudah punya:
- `APScheduler` di `scheduler_service.py` → bisa trigger heartbeat job
- `SSE stream` di `event_bus.py` → bisa push notifikasi ke client
- `scheduled_jobs` table → bisa simpan heartbeat schedule per agent

### Flow Teknis

```
1. Saat agent dibuat → auto-create heartbeat job di APScheduler
   (interval default: 30 menit, configurable per agent)

2. APScheduler trigger → kirim internal message ke session agent:
   "HEARTBEAT: Jalankan checklist kamu."

3. Agent jalankan checklist dari `recall("heartbeat:checklist")`

4. Kalau ada yang perlu disampaikan:
   → push via SSE stream ke client
   → atau kirim via channel (WhatsApp/webchat)

5. Kalau tidak ada → log HEARTBEAT_OK, tidak kirim notif
```

## Kapan Heartbeat Aktif?

Tidak semua agent butuh heartbeat. Aktifkan via `tools_config`:

```json
{
  "heartbeat": {
    "enabled": true,
    "interval_minutes": 30,
    "quiet_hours": ["23:00", "08:00"]
  }
}
```

## Heartbeat vs Scheduler (yang sudah ada)

| | Heartbeat | Scheduler (existing) |
|---|---|---|
| Trigger | Berkala otomatis | User set via `set_reminder` |
| Tujuan | Background checks, proactive | One-shot reminder spesifik |
| Konten | Checklist dinamis | Pesan spesifik |
| Siapa yang set | System (otomatis) | User (manual) |

Keduanya tetap jalan bersamaan — tidak saling gantikan.

## Quiet Hours

Agent tidak kirim notifikasi di luar jam aktif user:
- Jam quiet → HEARTBEAT_OK walau ada sesuatu (queue untuk nanti)
- Urgent override → bisa dikonfigurasi per agent
- Timezone dari `user_profile` (Asia/Jakarta default)
