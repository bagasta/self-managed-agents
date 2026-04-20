# Implementation TODO

## Status Legend
- [ ] Todo
- [~] In Progress
- [x] Done

---

## Phase 1 — DB & Models (fondasi)

- [x] **1.1** Migration: tambah `escalation_config` (JSONB) ke tabel `agents`
- [x] **1.2** Migration: tambah kolom ke tabel `sessions`:
  - `channel_type` (VARCHAR)
  - `channel_config` (JSONB, encrypted api_key)
  - `escalation_active` (BOOLEAN, default false)
- [x] **1.3** Migration: buat tabel `scheduled_jobs`
- [x] **1.4** Update ORM model `Agent` — tambah `escalation_config`
- [x] **1.5** Update ORM model `Session` — tambah channel fields + `escalation_active`
- [x] **1.6** Buat ORM model `ScheduledJob`
- [x] **1.7** Update Pydantic schemas `AgentCreate` / `AgentUpdate` / `AgentResponse`
- [x] **1.8** Update Pydantic schemas `SessionCreate` / `SessionResponse`
- [x] **1.9** Buat Pydantic schemas `ScheduledJobCreate` / `ScheduledJobResponse`

---

## Phase 2 — Channel Service

- [x] **2.1** Buat `app/core/channel_service.py` — adapter whatsapp/telegram/slack/webhook/in-app
- [x] **2.2** Utility enkripsi/dekripsi credential (Fernet) — `encrypt_channel_config` / `decrypt_channel_config`

---

## Phase 3 — Default Tools Update

- [x] **3.1** Update defaults di agent_runner — ON: memory/tool_creator/rag/sandbox/skills/scheduler/escalation
- [x] **3.2** Buat `app/core/tools/scheduler_tool.py` — set_reminder / list_reminders / cancel_reminder
- [x] **3.3** Buat `app/core/tools/escalation_tool.py` — escalate_to_human / reply_to_user / send_to_number
- [x] **3.4** Wire scheduler + escalation tools ke `agent_runner.py`

---

## Phase 4 — APScheduler (Proactive Engine)

- [x] **4.1** Tambah `apscheduler` + `cryptography` ke `requirements.txt`
- [x] **4.2** Buat `app/core/scheduler_service.py` — AsyncIOScheduler tick setiap menit
- [x] **4.3** Daftarkan start/stop scheduler di `app/main.py` lifespan

---

## Phase 5 — Incoming Channel Webhook

- [x] **5.1** Buat `app/api/channels.py` — POST /v1/channels/incoming/{session_id}
  - Detect operator vs user biasa
  - Forward ke operator jika escalation_active
  - Kirim reply ke channel yang sesuai
- [x] **5.2** Daftarkan router di `main.py`

---

## Phase 6 — Update Agent Runner Logic

- [x] **6.1** Session API: enkripsi channel_config sebelum simpan ke DB
- [x] **6.2** Inject konteks eskalasi ke system prompt agent_runner
  - Prefix `[OPERATOR]` → MODE: OPERATOR COMMAND
  - Prefix `[USER_IN_ESCALATION]` atau `session.escalation_active` → MODE: ESKALASI AKTIF
- [x] **6.3** Setelah agent reply di `/messages` endpoint: otomatis kirim via channel_service
  jika session punya `channel_type` (untuk non-incoming flow)

---

## Catatan Arsitektur

### Flow Eskalasi
```
Agent call escalate_to_human()
  → session.escalation_active = True
  → kirim summary ke operator (via escalation_config channel)

Pesan user berikutnya masuk via /channels/incoming
  → agent forward ke operator
  → "User +62812xxx: [pesan]"

Operator balas:
  → "Kirim ke customer: [jawaban]"  → agent eksekusi reply_to_user()
  → "Kirim ke +62899: [pesan]"      → agent eksekusi send_to_number()
  → "Selesai tangani sendiri"        → session.escalation_active = False
```

### Default tools_config saat agent dibuat
```json
{
  "memory": true,
  "tool_creator": true,
  "rag": true,
  "sandbox": true,
  "skills": true,
  "scheduler": true,
  "escalation": true,
  "http": false,
  "mcp": false
}
```

### Tabel scheduled_jobs — status lifecycle
```
active → paused (manual) → active
active → cancelled (manual / agent)
active → done (one-time job setelah dijalankan)
```
