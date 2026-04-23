# Managed Agent Platform

Self-hosted multi-model agent platform terinspirasi dari Claude Managed Agents. Dibangun di atas FastAPI + DeepAgents + OpenRouter, dengan sandbox Docker, integrasi WhatsApp, memory jangka panjang, RAG, scheduling, dan escalation ke human operator.

## Arsitektur

```
Client (webchat / WhatsApp / CLI / webhook)
        │
        ▼
  FastAPI Backend  (port 8000)
        │
        ├── OpenRouter ──► Claude / GPT / Llama / Gemini / Mistral / ...
        │   (300+ model, per-agent config)
        │
        ├── DeepAgents executor
        │   (planning via write_todos, virtual FS, tool calling)
        │
        ├── Docker Sandbox
        │   (ephemeral container per bash() call, workspace persist per session)
        │
        ├── PostgreSQL + pgvector  (agent config, sessions, messages, memory, RAG)
        │
        └── Go WhatsApp Microservices
            ├── wa-service     (port 8080) — production, satu device per agent
            └── wa-dev-service (port 8081) — dev/testing, satu nomor WA multi-agent
```

**Stack:**
- **Backend**: Python 3.12 + FastAPI (async)
- **Agent executor**: DeepAgents (`create_deep_agent`) + fallback LangGraph `create_react_agent`
- **LLM**: OpenRouter via `langchain-openai` — 300+ model via satu API key
- **Database**: PostgreSQL + SQLAlchemy async + Alembic migrations
- **Vector search**: pgvector + Sentence-Transformers (all-MiniLM-L6-v2)
- **Sandbox**: Docker — ephemeral container per run, workspace dir persist per session
- **WhatsApp**: Go + whatsmeow (dua microservice: production & dev)
- **Logging**: structlog

---

## Cara Menjalankan

### Prasyarat

- Python 3.12+
- Docker (aktif, bisa diakses tanpa sudo)
- PostgreSQL — atau jalankan via `docker compose`
- Go 1.21+ (hanya untuk WhatsApp microservice)

### 1. Install dependencies

```bash
make install
```

### 2. Konfigurasi environment

```bash
cp .env.example .env
```

Edit `.env` — field wajib:

```env
DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5432/managed_agents
OPENROUTER_API_KEY=sk-or-v1-...         # dari openrouter.ai
API_KEY=ganti-dengan-secret-random      # header X-API-Key untuk semua request
MISTRAL_API_KEY=                        # opsional — hanya untuk PDF OCR

# Sandbox
SANDBOX_BASE_DIR=/tmp/agent-sandboxes
DOCKER_SANDBOX_IMAGE=python:3.12        # full image (bukan slim) agar curl tersedia
DOCKER_HOST=unix:///run/docker.sock

# Agent limits
AGENT_MAX_STEPS=12
AGENT_TIMEOUT_SECONDS=300

# WhatsApp microservices
WA_SERVICE_URL=http://localhost:8080
WA_DEV_SERVICE_URL=http://localhost:8081
```

### 3. Jalankan PostgreSQL

```bash
make db-up
```

### 4. Jalankan migrasi database

```bash
make upgrade
```

### 5. Jalankan server

```bash
make dev
```

Server berjalan di `http://localhost:8000`. Swagger UI: `http://localhost:8000/docs`.

### 6. Jalankan WhatsApp microservice (opsional)

```bash
# Production — satu WhatsApp device per agent
make wa-build   # compile binary (sekali saja)
make wa         # jalankan di port 8080

# Development — satu nomor WA untuk semua agent
make wa-dev-build   # compile binary
make wa-dev         # jalankan di port 8081 (baca .env otomatis)
```

---

## Menjalankan via Docker Compose

```bash
# isi .env terlebih dahulu
docker compose up --build
```

> Docker socket (`/var/run/docker.sock`) di-mount ke container API agar sandbox bisa berjalan sebagai sibling container di host Docker daemon.

---

## Tools Agent

Tools dikonfigurasi per-agent via field `tools_config` (JSON). Default konservatif — hanya tools aman yang aktif by default:

| Tool Group | Default | Tools yang tersedia |
|-----------|:-------:|---------------------|
| `memory` | **ON** | `remember`, `recall`, `forget` |
| `skills` | **ON** | `create_skill`, `use_skill`, `list_skills` |
| `escalation` | **ON** | `escalate_to_human`, `reply_to_user`, `send_to_number` |
| `sandbox` | OFF | `bash`, `sandbox_write_file`, `sandbox_read_file`, `list_files` |
| `tool_creator` | OFF | `create_tool`, `run_custom_tool`, `list_tools` |
| `scheduler` | OFF | `set_reminder`, `list_reminders`, `cancel_reminder` |
| `http` | OFF | `http_get`, `http_post`, `http_patch`, `http_delete` |
| `rag` | OFF | `search_knowledge_base` |
| `mcp` | OFF | tools dari MCP server eksternal |
| `whatsapp_media` | OFF | `send_whatsapp_image` |
| `wa_agent_manager` | OFF | `send_agent_wa_qr` |

DeepAgents menambahkan otomatis: `write_todos`, `ls`, `read_file`, `write_file`, `edit_file`, `grep`.

---

## API Reference

Semua endpoint memerlukan header `X-API-Key: <API_KEY>`. Dokumentasi interaktif di `/docs`.

### Agent

| Method | Endpoint | Deskripsi |
|--------|----------|-----------|
| `POST` | `/v1/agents` | Buat agent baru |
| `GET` | `/v1/agents` | List semua agent |
| `GET` | `/v1/agents/{id}` | Detail agent |
| `PATCH` | `/v1/agents/{id}` | Update config agent |
| `DELETE` | `/v1/agents/{id}` | Soft delete agent |

**Contoh buat agent WhatsApp CS:**

```json
POST /v1/agents
{
  "name": "CS Agent",
  "instructions": "Kamu adalah customer service yang ramah dan membantu.",
  "model": "anthropic/claude-sonnet-4-6",
  "tools_config": {
    "memory": true,
    "skills": true,
    "escalation": true,
    "scheduler": true,
    "whatsapp_media": true
  },
  "escalation_config": {
    "channel_type": "whatsapp",
    "operator_phone": "+628123456789"
  },
  "operator_ids": ["+628123456789"]
}
```

### Session & Pesan

| Method | Endpoint | Deskripsi |
|--------|----------|-----------|
| `POST` | `/v1/agents/{id}/sessions` | Buat session baru |
| `POST` | `/v1/agents/{id}/sessions/{session_id}/messages` | Kirim pesan, jalankan agent |
| `GET` | `/v1/sessions/{session_id}/history` | Riwayat percakapan |
| `GET` | `/v1/sessions/{session_id}/stream` | SSE stream — terima reminder proaktif real-time |
| `GET` | `/v1/runs/{run_id}` | Detail satu run (steps + tool calls) |

### Memory, Skills, Custom Tools

| Method | Endpoint | Deskripsi |
|--------|----------|-----------|
| `GET/POST/DELETE` | `/v1/agents/{id}/memory` | Kelola long-term memory |
| `GET/POST/DELETE` | `/v1/agents/{id}/skills` | Kelola skill library |
| `GET/POST` | `/v1/agents/{id}/custom-tools` | Kelola custom Python tools |

### Knowledge Base (RAG)

| Method | Endpoint | Deskripsi |
|--------|----------|-----------|
| `POST` | `/v1/agents/{id}/documents/upload` | Upload dokumen (PDF, DOCX, PPTX, TXT, CSV) |
| `GET` | `/v1/agents/{id}/documents` | List dokumen |
| `DELETE` | `/v1/agents/{id}/documents/{doc_id}` | Hapus dokumen |

### WhatsApp

| Method | Endpoint | Deskripsi |
|--------|----------|-----------|
| `GET` | `/v1/agents/{id}/wa/qr` | Ambil QR code untuk scan device |
| `GET` | `/v1/agents/{id}/wa/status` | Status koneksi WA device |
| `POST` | `/v1/channels/wa/incoming` | Webhook dari Go wa-service (internal) |

### Lainnya

| Method | Endpoint | Deskripsi |
|--------|----------|-----------|
| `GET` | `/v1/models` | Daftar model yang tersedia |
| `GET` | `/health` | Health check |

---

## WhatsApp Integration

### wa-service — Production

Setiap agent punya WhatsApp device sendiri (field `wa_device_id` di agent config). Gunakan endpoint `/v1/agents/{id}/wa/qr` untuk mendapatkan QR, lalu scan dari WhatsApp.

API wa-service (port 8080):

```
POST   /devices                        buat device baru, returns QR base64 PNG
GET    /devices/{id}/qr                ambil QR terbaru
GET    /devices/{id}/status            status koneksi
POST   /devices/{id}/send              kirim pesan teks
POST   /devices/{id}/send-image        kirim gambar
POST   /devices/{id}/send-document     kirim dokumen
DELETE /devices/{id}                   hapus device (logout)
```

Incoming messages di-forward ke `POST /v1/channels/wa/incoming` di Python backend.

**Fitur:**
- Per-device SQLite session (reconnect otomatis saat restart)
- Dukungan grup dengan deteksi @mention bot (termasuk LID account)
- Terima gambar (multimodal vision ke LLM), dokumen (ekstrak teks), audio, sticker
- Block broadcast/status messages
- Markdown-to-WhatsApp conversion pada reply agent
- QR 512px High quality — tahan kompresi WhatsApp

### wa-dev-service — Development

Satu nomor WA shared untuk semua agent. Cocok untuk testing tanpa menyiapkan device terpisah per agent.

**Cara pakai:**
```
connect {agentID}   → mulai sesi dengan agent tertentu
berhenti            → disconnect dari agent
```

**Fitur khusus wa-dev:**
- **Operator auto-route**: nomor yang terdaftar di `operator_ids` atau `escalation_config.operator_phone` di-route otomatis ke agent yang relevan tanpa perlu `connect` — sehingga operator bisa langsung membalas notifikasi eskalasi.
- Semua fitur identik dengan wa-service: gambar, dokumen, audio, sticker, grup @mention, reminder, escalation, RAG.
- Dashboard web tersedia di `http://localhost:8081`.

Environment variables wa-dev-service (dibaca dari `.env` via `make wa-dev`):

| Var | Default | Keterangan |
|-----|---------|-----------|
| `PORT` | `8081` | Port server |
| `MAIN_API_URL` | `http://localhost:8000` | URL Python backend |
| `MAIN_API_KEY` | — | Sama dengan `API_KEY` di `.env` |
| `WA_DEV_STORE_DIR` | `wa-dev-store` | Direktori SQLite session WA |
| `CONNECTIONS_FILE` | `connections.json` | File mapping phone→agent |

---

## Escalation Flow

Saat agent tidak bisa menangani request, ia bisa eskalasi ke human operator:

1. Agent memanggil `escalate_to_human(reason, summary)`
2. Notifikasi otomatis dikirim ke `operator_phone` via channel yang dikonfigurasi
3. Operator membalas → agent menyusun draft reply
4. Agent tampilkan draft ke operator untuk konfirmasi
5. Operator ketik "kirim" → `reply_to_user(draft)` → pesan terkirim ke user

Untuk WhatsApp, operator bisa membalas langsung tanpa setup tambahan karena session operator dikelola terpisah dari session user.

---

## Database Schema

| Tabel | Isi |
|-------|-----|
| `agents` | Config: model, instructions, tools_config, escalation_config, operator_ids, wa_device_id, api_key |
| `sessions` | Context per user: agent_id, external_user_id, channel_type, channel_config |
| `messages` | Setiap turn & tool call: role (user/agent/tool/escalation), content, step_index |
| `agent_memories` | Long-term KV facts, scoped per external_user_id |
| `agent_skills` | Reusable prompt snippets |
| `agent_custom_tools` | Python tool code dibuat agent di runtime |
| `documents` | File upload + pgvector embeddings |
| `scheduled_jobs` | APScheduler reminders |
| `channels` | Per-agent channel config (WhatsApp, Telegram, Slack, webhook) |

---

## Model yang Tersedia

Format OpenRouter: `provider/model-name`. Lihat daftar lengkap: `GET /v1/models`.

| Provider | Model |
|----------|-------|
| Anthropic | `anthropic/claude-sonnet-4-6`, `anthropic/claude-opus-4-7`, `anthropic/claude-haiku-4-5` |
| OpenAI | `openai/gpt-4.1`, `openai/gpt-4.1-mini`, `openai/o4-mini` |
| Google | `google/gemini-2.5-pro`, `google/gemini-2.0-flash` |
| Meta | `meta-llama/llama-3.3-70b-instruct` |
| Mistral | `mistral/mistral-large`, `mistral/mistral-small` |
| DeepSeek | `deepseek/deepseek-r1`, `deepseek/deepseek-chat-v3` |
| Qwen | `qwen/qwen-2.5-72b-instruct` |

---

## Development Commands

```bash
make install          # pip install -r requirements.txt
make dev              # uvicorn --reload (port 8000)
make db-up            # start PostgreSQL via docker compose
make upgrade          # alembic upgrade head
make migrate MSG="x"  # generate migration baru
make downgrade        # rollback satu migration
make lint             # ruff check app/ alembic/
make format           # ruff format app/ alembic/

make wa-build         # compile wa-service binary
make wa               # jalankan wa-service (port 8080)
make wa-dev-build     # compile wa-dev-service binary
make wa-dev           # jalankan wa-dev-service (port 8081)
make dev-all          # tampilkan instruksi jalankan semua sekaligus
```
