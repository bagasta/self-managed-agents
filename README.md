# Managed Agent Platform

Self-hosted multi-model agent platform terinspirasi dari Claude Managed Agents. Dibangun di atas FastAPI + LangChain + OpenRouter, dengan sandbox eksekusi kode berbasis Docker.

## Arsitektur

```
Client (OpenClaw / webchat / CLI / MCP)
        │
        ▼
  FastAPI Backend
        │
        ├── OpenRouter ──► Claude / GPT / Llama / Gemini / ...
        │   (multi-model, per-agent config)
        │
        └── Docker Sandbox
            (ephemeral container + persistent workspace per session)
            bash · write_file · read_file · list_files
```

**Stack:**
- **Backend**: Python 3.12 + FastAPI (async)
- **Orchestration**: LangChain + LangGraph `create_react_agent`
- **LLM Access**: OpenRouter (`langchain-openrouter`) — 300+ model via satu API key
- **Database**: PostgreSQL + SQLAlchemy async + Alembic
- **Sandbox**: Docker (ephemeral container per run, workspace dir persist per session)
- **Logging**: structlog (JSON di prod, console di dev)

---

## Cara Menjalankan

### Prasyarat

- Python 3.12+
- Docker (aktif, bisa diakses tanpa sudo)
- PostgreSQL — atau jalankan via `docker compose`

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Konfigurasi environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5432/managed_agents
OPENROUTER_API_KEY=sk-or-v1-...        # dari openrouter.ai
API_KEY=ganti-dengan-secret-random     # dipakai di header X-API-Key
LOG_LEVEL=INFO
SANDBOX_BASE_DIR=/tmp/agent-sandboxes
DOCKER_SANDBOX_IMAGE=python:3.12-slim
AGENT_MAX_STEPS=12
AGENT_TIMEOUT_SECONDS=300
```

### 3. Jalankan PostgreSQL

```bash
make db-up
# atau: docker compose up -d postgres
```

### 4. Jalankan migrasi database

```bash
make upgrade
# atau: alembic upgrade head
```

### 5. Jalankan server

```bash
make dev
# atau: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Server berjalan di `http://localhost:8000`. Swagger UI tersedia di `http://localhost:8000/docs`.

---

## Menjalankan via Docker Compose

Untuk menjalankan seluruh stack (PostgreSQL + API) sekaligus:

```bash
# isi .env terlebih dahulu (minimal OPENROUTER_API_KEY dan API_KEY)
docker compose up --build
```

> **Catatan**: Docker socket (`/var/run/docker.sock`) di-mount ke dalam container API agar sandbox containers bisa berjalan sebagai sibling container di host Docker daemon.

---

## API Reference

Semua endpoint memerlukan header `X-API-Key: <nilai API_KEY di .env>`.

### Agent

| Method | Endpoint | Deskripsi |
|--------|----------|-----------|
| `POST` | `/v1/agents` | Buat agent baru |
| `GET` | `/v1/agents` | List semua agent (paging: `?limit=20&offset=0`) |
| `GET` | `/v1/agents/{agent_id}` | Detail satu agent |
| `PATCH` | `/v1/agents/{agent_id}` | Update config agent (increment version otomatis) |
| `DELETE` | `/v1/agents/{agent_id}` | Soft delete agent |

**Contoh buat agent:**

```bash
curl -X POST http://localhost:8000/v1/agents \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Coding Agent",
    "instructions": "Kamu adalah coding assistant. Tulis kode Python yang bersih, jalankan untuk verifikasi, dan jelaskan hasilnya.",
    "model": "anthropic/claude-sonnet-4-6",
    "sandbox_config": {
      "memory": "512m",
      "cpu": "1.0"
    }
  }'
```

Field `model` menggunakan format OpenRouter: `provider/model-name`. Contoh model yang tersedia:

| Model | Identifier |
|-------|-----------|
| Claude Sonnet 4.6 | `anthropic/claude-sonnet-4-6` |
| GPT-4.1 | `openai/gpt-4.1` |
| GPT-4.1 Mini | `openai/gpt-4.1-mini` |
| Llama 3.3 70B | `meta-llama/llama-3.3-70b-instruct` |
| Gemini 2.5 Pro | `google/gemini-2.5-pro` |

Lihat daftar lengkap di [openrouter.ai/models](https://openrouter.ai/models).

---

### Session

| Method | Endpoint | Deskripsi |
|--------|----------|-----------|
| `POST` | `/v1/agents/{agent_id}/sessions` | Buat session baru untuk agent |

Session akan membuat workspace directory di `SANDBOX_BASE_DIR/{session_id}/`. File yang ditulis agent dalam workspace ini persist selama session ada.

**Contoh buat session:**

```bash
curl -X POST http://localhost:8000/v1/agents/{agent_id}/sessions \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "external_user_id": "user-123",
    "metadata": {"channel": "webchat"}
  }'
```

---

### Pesan & Eksekusi Agent

| Method | Endpoint | Deskripsi |
|--------|----------|-----------|
| `POST` | `/v1/agents/{agent_id}/sessions/{session_id}/messages` | Kirim pesan dan jalankan agent |
| `GET` | `/v1/sessions/{session_id}/history` | Ambil riwayat percakapan |

**Contoh kirim pesan:**

```bash
curl -X POST http://localhost:8000/v1/agents/{agent_id}/sessions/{session_id}/messages \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Buat script Python yang membaca file CSV dan tampilkan 5 baris pertama beserta statistik kolomnya."
  }'
```

**Contoh response:**

```json
{
  "reply": "Sudah saya buat dan jalankan script-nya. Hasilnya:\n...",
  "steps": [
    { "step": 1, "tool": "write_file", "args": {"path": "analyze.py", "content": "..."}, "result": "Written 342 chars to analyze.py" },
    { "step": 2, "tool": "bash", "args": {"cmd": "python analyze.py"}, "result": "   col1  col2\n0  ..." }
  ],
  "run_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

---

## Sandbox

Agent memiliki akses ke 4 built-in tools yang berjalan di dalam sandbox Docker:

| Tool | Deskripsi |
|------|-----------|
| `bash(cmd)` | Jalankan perintah bash di dalam container terisolasi |
| `write_file(path, content)` | Tulis file ke workspace |
| `read_file(path)` | Baca file dari workspace |
| `list_files(directory)` | List file di workspace |

**Desain sandbox:**
- Setiap message spin up **container baru** (ephemeral) dari image `DOCKER_SANDBOX_IMAGE`
- Workspace directory `SANDBOX_BASE_DIR/{session_id}/` di-mount sebagai `/workspace` di dalam container
- Container di-remove otomatis setelah selesai (`remove=True`)
- File yang ditulis **persist antar messages** karena ada di host directory, bukan di dalam container
- Network di-disable by default (`network_disabled=True`)
- Memory limit: 512m, CPU limit: 1 core

**Mengaktifkan gVisor (isolasi lebih kuat):**

Install gVisor dan tambahkan `runtime: runsc` ke Docker daemon, lalu set di `.env`:

```env
DOCKER_RUNTIME=runsc
```

---

## Migrations

```bash
# Generate migration baru dari perubahan model
make migrate MSG="tambah kolom baru"
# atau: alembic revision --autogenerate -m "tambah kolom baru"

# Apply semua migration pending
make upgrade

# Rollback satu migration
make downgrade
```

---

## Menambah Agent Baru

Tidak perlu ubah kode. Cukup `POST /v1/agents` dengan config yang berbeda:

```bash
# Ops agent pakai model yang lebih hemat
curl -X POST http://localhost:8000/v1/agents \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Ops Agent",
    "instructions": "Kamu adalah ops assistant. Bantu monitoring dan troubleshooting sistem internal.",
    "model": "openai/gpt-4.1-mini",
    "safety_policy": {
      "forbidden": ["hapus data produksi", "modifikasi konfigurasi tanpa konfirmasi"]
    }
  }'
```

Update model agent tanpa restart server:

```bash
curl -X PATCH http://localhost:8000/v1/agents/{agent_id} \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "anthropic/claude-sonnet-4-6"}'
```

---

## Upgrade ke DeepAgents

`app/core/agent_runner.py` saat ini menggunakan `create_react_agent` dari LangGraph. Saat LangChain DeepAgents siap dipakai, swap bagian build agent di `run_agent()`:

```python
# Ganti ini:
graph = create_react_agent(llm, tools=tools, prompt=system_prompt)

# Dengan ini:
from deepagents import create_deep_agent
from deepagents.middleware import TodoListMiddleware, FilesystemMiddleware

graph = create_deep_agent(
    llm=llm,
    tools=tools,
    system_prompt=system_prompt,
    middlewares=[
        TodoListMiddleware(),          # externalize planning ke to-do list
        FilesystemMiddleware(sandbox=sandbox),
    ],
)
```

Sisa `run_agent()` tidak perlu diubah — sama-sama `ainvoke` + parsing messages.

---

## Upgrade ke Daytona Sandbox

Untuk produksi, ganti `DockerSandbox` dengan Daytona (open-source, startup <60ms, hardware isolation):

1. Deploy Daytona self-hosted: [github.com/daytonaio/daytona](https://github.com/daytonaio/daytona)
2. Buat class `DaytonaSandbox` di `app/core/sandbox.py` yang implement method `bash`, `write_file`, `read_file`, `list_files` menggunakan Daytona Python SDK
3. Update `agent_runner.py` untuk instantiate `DaytonaSandbox` jika `SANDBOX_BACKEND=daytona`

---

## Milestone

- [x] **Milestone 1** — FastAPI skeleton, Agent CRUD, session, message endpoint dengan Docker sandbox + LangGraph
- [ ] **Milestone 2** — Daytona sandbox, tools tambahan (HTTP internal, RAG), LangSmith, integrasi OpenClaw/webchat
- [ ] **Milestone 3** — Web admin UI, JWT auth, developer docs, SubAgent support
