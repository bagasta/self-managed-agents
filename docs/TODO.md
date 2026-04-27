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

## Phase 7 — WhatsApp Integration (whatsmeow)

### Arsitektur
WhatsApp menggunakan **whatsmeow** (Go library) — harus dibangun sebagai **Go microservice** terpisah
(`wa-service/`) yang diekspos via HTTP internal. Python FastAPI berkomunikasi dengan service ini.

```
┌─────────────────────┐       HTTP internal       ┌──────────────────────────┐
│  Python FastAPI     │ ◄────────────────────────► │  wa-service (Go)          │
│  :8000              │                             │  :8080                    │
│                     │                             │  - whatsmeow multi-device │
│  /v1/agents         │   POST /devices             │  - SQLite session store   │
│  /v1/channels/wa/   │   GET  /devices/:id/qr      │  - QR PNG generator       │
│  incoming           │   GET  /devices/:id/status  │  - Webhook forwarder      │
└─────────────────────┘   POST /devices/:id/send    └──────────────────────────┘
                          DELETE /devices/:id

WhatsApp user ──► Go service ──► POST /v1/channels/wa/incoming ──► Python ──► Agent
Agent reply   ──► Python     ──► POST /devices/:id/send         ──► Go ──► WhatsApp user
```

### Sub-tasks

#### 7.1 — Go WhatsApp Service
- [x] **7.1.1** Init Go module `wa-service/` — `go mod init wa-service`
  - Dependencies: `go.mau.fi/whatsmeow`, `github.com/skip2/go-qrcode`, `github.com/gorilla/mux`,
    `go.mau.fi/whatsmeow/store/sqlstore`, `mattn/go-sqlite3`
- [x] **7.1.2** `wa-service/device_manager.go` — multi-device manager:
  - Map `deviceID (string) → *whatsmeow.Client`
  - `CreateDevice(id string) (qrPNG []byte, err error)` — buat client baru, stream QR channel,
    encode QR ke PNG dengan go-qrcode, kembalikan image PNG bytes + base64 string
  - `GetStatus(id string) string` → `"waiting_qr" | "connected" | "disconnected"`
  - `GetFreshQR(id string) ([]byte, error)` → generate QR baru jika belum connected
  - `SendMessage(id, to, text string) error` → send text message via JID
  - `Disconnect(id string)` → logout + hapus dari map
  - Event handler: saat terima pesan masuk → POST ke Python webhook
- [x] **7.1.3** `wa-service/handlers.go` — HTTP handlers:
  - `POST /devices` → `CreateDevice`, return `{device_id, qr_image (base64 PNG), status}`
  - `GET /devices/{id}/qr` → `GetFreshQR`, return `{qr_image, status}`
  - `GET /devices/{id}/status` → `{status, phone_number (jika connected)}`
  - `POST /devices/{id}/send` → body `{to, message}` → `SendMessage`
  - `DELETE /devices/{id}` → `Disconnect`
- [x] **7.1.4** `wa-service/main.go` — HTTP server startup, load existing devices dari SQLite saat boot
- [x] **7.1.5** `wa-service/go.mod` + `wa-service/go.sum`
- [ ] **7.1.6** `wa-service/Dockerfile` (optional, untuk deployment)

#### 7.2 — DB Migration & Model (Python side)
- [x] **7.2.1** `alembic/versions/008_agent_whatsapp.py`
- [x] **7.2.2** Update ORM `app/models/agent.py`
- [x] **7.2.3** Update Pydantic `app/schemas/agent.py`

#### 7.3 — Python API Endpoints
- [x] **7.3.1** `app/core/wa_client.py` — thin HTTP client ke Go service:
  - `async def create_wa_device(device_id: str) → dict` (qr_image, status)
  - `async def get_wa_qr(device_id: str) → dict`
  - `async def get_wa_status(device_id: str) → dict`
  - `async def send_wa_message(device_id: str, to: str, text: str) → None`
  - `async def delete_wa_device(device_id: str) → None`
  - Baca `WA_SERVICE_URL` dari env (default `http://localhost:8080`)
- [x] **7.3.2** Update `app/api/agents.py`:
  - `create_agent`: jika `payload.channel_type == "whatsapp"` → generate `device_id = str(uuid4())`,
    panggil `wa_client.create_wa_device(device_id)`, simpan ke `agent.wa_device_id`,
    sertakan `qr_image` di response
  - Tambah `GET /{agent_id}/whatsapp/qr` → return QR baru dari Go service
  - Tambah `GET /{agent_id}/whatsapp/status` → return status koneksi
  - Tambah `DELETE /{agent_id}/whatsapp` → logout device di Go service, clear `wa_device_id`
- [x] **7.3.3** Update `app/api/channels.py` — `POST /v1/channels/wa/incoming`
- [x] **7.3.4** Update `app/core/channel_service.py` — use wa_client

#### 7.4 — Config & Env
- [x] **7.4.1** Tambah `WA_SERVICE_URL=http://localhost:8080` ke `.env.example`
- [x] **7.4.2** Expose `WA_SERVICE_URL` via pydantic settings

#### 7.5 — Postman Collection
- [x] **7.5.1** Update `managed-agents.postman_collection.json`:
  - Create Agent: tambah contoh dengan `channel_type: "whatsapp"`; response menampilkan `qr_image`
  - Tambah request `GET /v1/agents/:agent_id/whatsapp/qr`
  - Tambah request `GET /v1/agents/:agent_id/whatsapp/status`
  - Tambah request `DELETE /v1/agents/:agent_id/whatsapp`

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
