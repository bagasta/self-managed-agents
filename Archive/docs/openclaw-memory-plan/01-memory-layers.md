# 01 — Memory Layers

## Mapping: OpenClaw → SaaS DB

| OpenClaw File | Key di DB | Scope | Kapan Di-load |
|---|---|---|---|
| `SOUL.md` | `soul` | per `agent_id` | Auto-inject, selalu |
| `USER.md` | `user_profile` | per `agent_id` + `external_user_id` | Auto-inject, selalu |
| `memory/YYYY-MM-DD.md` | `daily:YYYY-MM-DD` | per `agent_id` + `external_user_id` | Auto-inject hari ini + kemarin |
| `MEMORY.md` | `longterm` | per `agent_id` + `external_user_id` | Lazy — agent `recall()` saat butuh |

## Tabel DB: `agent_memories` (sudah ada)

Tabel ini sudah ada dan dipakai untuk memory KV. Kita extend dengan menambahkan konsep **layer type** via key prefix.

### Key Conventions

```
soul                    → identity & persona agent (global per agent)
user_profile            → profil user (per external_user_id)
daily:2026-05-06        → daily notes hari ini
daily:2026-05-05        → daily notes kemarin
longterm                → curated long-term memory
heartbeat:state         → last check timestamps (email, calendar, dll)
```

## Cara Kerja

### Auto-inject (di `agent_runner.py`)

Sebelum agent dijalankan, `agent_runner.py` load dan inject ke system prompt:

```python
# Selalu inject
soul = await memory_service.get(agent_id, "soul")
user_profile = await memory_service.get(agent_id, external_user_id, "user_profile")

# Inject daily (hari ini + kemarin)
today = await memory_service.get(agent_id, external_user_id, f"daily:{today_date}")
yesterday = await memory_service.get(agent_id, external_user_id, f"daily:{yesterday_date}")

# Longterm TIDAK auto-inject — terlalu panjang
# Agent recall() sendiri kalau butuh
```

### Lazy Load via Tool

Agent bisa akses longterm memory kapan saja:

```
User: "Inget ga waktu kita bahas deploy bulan lalu?"
Agent: [recall("longterm")] → baca curated memory → jawab
```

### Tulis Memory via Tool

Agent tulis selama sesi:

```
Agent: [remember("daily:2026-05-06", "User minta fitur X, sudah dikerjakan")]
Agent: [remember("longterm", "User prefer model gpt-5.1 untuk semua agent baru")]
```

## Memory untuk Arthur (Agent Builder)

Arthur punya layer memory tambahan:

```
arthur:soul             → identitas Arthur sebagai builder
arthur:daily:YYYY-MM-DD → log agent apa yang dibuat hari ini
arthur:longterm         → keputusan arsitektur, preferensi user soal agent config
```

Setiap agent yang Arthur buat otomatis mendapat template `soul` dari Arthur berdasarkan tipe agent yang diminta.
