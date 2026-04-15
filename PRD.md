# PRD: Managed Agent Platform (Self-hosted, Multi-Model)

## 1. Ringkasan

### 1.1. Latar Belakang

Banyak use case internal (Chief AI Officer, ops, support, coding, data analysis) yang butuh **agent AI**:

- Multi-step reasoning (bukan cuma Q&A).
- Bisa manggil **tools internal** (API, DB, Notion, dsb).
- Punya **config per agent** (role, policy, tools, model).
- Bisa dipakai lintas channel (webchat, CLI, OpenClaw, MCP, dsb).
- Punya **sandbox eksekusi kode** — agent bisa nulis dan jalankan kode layaknya punya komputer sendiri.

Produk seperti **Claude Managed Agents** menunjukkan pola yang bagus (session-based, append-only log, built-in tools: bash/read/write/edit/glob/grep), tapi:

- Runtime dan kontrol penuh ada di vendor (Anthropic).
- Model locked ke Claude saja.
- Sulit diintegrasikan dalam-dalam ke ekosistem internal (OpenClaw, MCP, dsb).
- Ada isu data residency / compliance.
- Harganya $0.08/session-hour + token cost.

### 1.2. Tujuan

Membangun **Managed Agent Platform self-hosted** yang terinspirasi dari Claude Managed Agents, dengan stack:

- **LangChain DeepAgents** sebagai orchestration harness (di atas LangGraph).
- **OpenRouter** untuk multi-model support (Claude, GPT, Llama, dsb — 300+ model).
- **Daytona** (atau Docker+gVisor untuk POC) sebagai sandbox eksekusi kode — open-source, self-hosted, gratis.

Target: Platform agent pusat yang bisa dipakai berbagai aplikasi internal, dengan kontrol penuh atas runtime, model, data, dan sandbox.

---

## 2. Sasaran & Non-Sasaran

### 2.1. Sasaran

1. **Runtime Agent Generik**
   - Eksekusi berbagai jenis agent (support, ops, coder, dsb) dengan runtime yang sama.
   - LangChain DeepAgents sebagai core orchestration (planning, filesystem, subagents).

2. **Multi-Model via OpenRouter**
   - Setiap agent bisa dikonfigurasi pakai model berbeda: `anthropic/claude-sonnet-4-6`, `openai/gpt-4.1`, `meta-llama/llama-3.3-70b-instruct`, dsb.
   - Model di-set per-agent config, bukan hardcoded.

3. **Sandbox Eksekusi Kode (Self-Hosted)**
   - Agent bisa nulis file, jalankan bash/Python/script, baca hasil output.
   - Layaknya agent punya komputer sendiri yang terisolasi.
   - Pakai **Daytona** (open-source, self-hosted) sebagai sandbox backend DeepAgents.
   - Fallback POC: Docker container + gVisor runtime.

4. **Manajemen Agent**
   - CRUD agent: nama, deskripsi, instructions (system prompt), model, tools, safety policy.
   - Versioning sederhana.

5. **Session & State**
   - Session per user/task dengan append-only message log (mirip Claude Managed Agents).
   - State di-checkpoint tiap step via LangGraph Store.
   - Session bisa di-resume (persist lintas calls).

6. **Tools Integrasi**
   - Built-in via sandbox: `bash`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`.
   - Tambahan modular: HTTP tool, database query, RAG retrieval, GitHub API.

7. **Observability**
   - Logging per step: tool calls (nama, argumen, hasil ringkas).
   - Request/response input-output per session.
   - Optional: LangSmith integration.

8. **Integrasi Eksternal**
   - Satu endpoint generik yang bisa dipanggil dari OpenClaw, webchat, CLI, MCP.

### 2.2. Non-Sasaran (Out of Scope Fase 1)

- UI admin yang kompleks.
- Multi-tenant multi-organization (cukup single-tenant + user scoping sederhana).
- Full RBAC & audit trail enterprise.
- GPU workloads (fase 1 cukup CPU sandbox).
- Integrasi semua tool internal sekaligus (fokus 2–3 tool dulu).

---

## 3. User Persona & Use Case

### 3.1. Persona

1. **Platform Engineer / AI Infra (Primary)** — setup & maintain platform, butuh API & config yang clear.
2. **Internal Product Owner / Ops Lead** — minta bikin agent untuk use case tertentu (finance ops, CS).
3. **End User (Internal Staff / Dev)** — interaksi via webchat, CLI/MCP di VSCode, OpenClaw.

### 3.2. Use Case Utama (Fase 1)

1. **Coding / Dev Helper Agent**
   - Role: nulis kode, jalankan, debug, baca repo.
   - Tools: sandbox (bash, file ops), GitHub API wrapper.
   - Sandbox: Daytona — agent bisa install package, jalankan script Python, baca output.

2. **Internal Ops Agent**
   - Role: multi-step reasoning dengan internal API calls.
   - Tools: HTTP GET/POST ke endpoint internal, sandbox untuk data processing.

3. **Documentation / Knowledge Agent**
   - Role: jawab pertanyaan dari dokumen (RAG sederhana).
   - Tools: RAG retrieval tool.

---

## 4. Kebutuhan Produk

### 4.1. Model Data

#### Agent

| Field | Type | Keterangan |
|---|---|---|
| `id` | UUID | Primary key |
| `name` | string | Nama agent |
| `description` | string | Deskripsi singkat |
| `instructions` | text | System prompt / role |
| `model` | string | Format OpenRouter: `anthropic/claude-sonnet-4-6` |
| `tools_config` | JSON | List tools yang di-enable + config per tool |
| `sandbox_config` | JSON | Sandbox backend (daytona/docker) + resource limits |
| `safety_policy` | JSON/text | Do/don't per agent |
| `version` | int | Revisi config |
| `created_at`, `updated_at` | timestamp | |

#### Session

| Field | Type | Keterangan |
|---|---|---|
| `id` | UUID | |
| `agent_id` | UUID | FK ke Agent |
| `external_user_id` | string | Optional, mapping ke user actual |
| `metadata` | JSON | Context tambahan |
| `sandbox_id` | string | ID sandbox instance yang dialokasikan |
| `created_at`, `updated_at` | timestamp | |

#### Message / Run Log

| Field | Type | Keterangan |
|---|---|---|
| `id` | UUID | |
| `session_id` | UUID | |
| `role` | enum | `user` / `agent` / `tool` |
| `content` | text/JSON | Isi pesan atau tool output |
| `tool_name` | string | Optional, nama tool yang dipanggil |
| `step_index` | int | Urutan langkah dalam satu run |
| `timestamp` | timestamp | |

---

### 4.2. API Surface

#### Agent Management

```
POST   /v1/agents                        Buat agent baru
GET    /v1/agents                        List agents (paging)
GET    /v1/agents/{agent_id}             Detail satu agent
PATCH  /v1/agents/{agent_id}             Update config
DELETE /v1/agents/{agent_id}             Soft delete
```

#### Session & Messaging

```
POST   /v1/agents/{agent_id}/sessions                           Buat session baru
POST   /v1/agents/{agent_id}/sessions/{session_id}/messages     Kirim pesan & jalankan agent
GET    /v1/sessions/{session_id}/history                        Riwayat percakapan
DELETE /v1/sessions/{session_id}                                Hapus session + cleanup sandbox
```

#### Observability

```
GET    /v1/runs/{run_id}                 Detail satu eksekusi agent
```

**Request body `POST .../messages`:**
```json
{
  "message": "Tulis script Python untuk parse CSV ini dan tampilkan statistiknya",
  "metadata": {}
}
```

**Response:**
```json
{
  "reply": "...",
  "steps": [
    {"step": 1, "tool": "write_file", "args": {...}, "result": "ok"},
    {"step": 2, "tool": "bash", "args": {"cmd": "python script.py"}, "result": "...output..."}
  ],
  "run_id": "uuid"
}
```

---

### 4.3. Runtime & Orchestration (DeepAgents)

#### Flow per Request

1. Load `AgentConfig` dari DB.
2. Resolve model string via OpenRouter (`ChatOpenRouter(model=agent.model)`).
3. Build `DeepAgent` instance:
   - `instructions` → system prompt.
   - Attach tools berdasarkan `tools_config` + sandbox tools (bash, file ops).
   - Set sandbox backend ke Daytona instance milik session ini.
   - Inject safety guidelines.
4. Load session history sebagai context (dari LangGraph Store / DB).
5. Jalankan DeepAgent dengan input message dari user.
6. Enforce batas step (8–12) dan timeout.
7. Simpan semua langkah & tool calls ke Message Log.
8. Return reply + steps summary.

#### Middleware Stack (DeepAgents)

```
TodoListMiddleware      — externalizes planning ke to-do list, cegah agent drift
FilesystemMiddleware    — routing file ops ke Daytona sandbox
SubAgentMiddleware      — spawn child agents (opsional, fase 2)
```

#### Step & Tool Constraints

- Max steps per run: 12 (configurable per agent).
- Timeout per run: 5 menit default (configurable).
- Log semua tool calls: nama, argumen, status, ringkasan hasil.

---

### 4.4. Sandbox (Daytona — Self-Hosted)

#### Arsitektur

```
FastAPI Backend
    │
    ├── POST /v1/agents/{id}/sessions
    │       → Allocate Daytona sandbox instance
    │       → Store sandbox_id di session
    │
    └── POST .../messages
            → DeepAgent runs
            → FilesystemMiddleware routes file ops to Daytona
            → bash tool executes in isolated Daytona container
```

#### Kenapa Daytona

- **Open-source** (Apache 2.0), self-hosted sepenuhnya — tidak ada biaya layanan.
- **Native integration** di LangChain DeepAgents sebagai built-in sandbox backend.
- **Startup < 60ms** — cukup cepat untuk per-session allocation.
- **Stateful** — sandbox persist selama session, agent bisa baca file yang ditulis di step sebelumnya.
- **Full OS environment** — agent bisa install package, jalankan script apapun.
- **Deployment**: Docker, Kubernetes, atau bare-metal on-prem.

#### Fallback: Docker + gVisor (POC/Dev)

Untuk development lokal atau jika Daytona belum di-setup:

- Jalankan Docker container per session dengan `--runtime=runsc` (gVisor).
- gVisor adalah open-source container sandbox dari Google — isolasi lebih kuat dari Docker biasa (user-space kernel), tanpa perlu full VM.
- Trade-off: startup ~1-3s (lebih lambat dari Daytona), tapi setup jauh lebih mudah.

```yaml
# docker-compose untuk dev sandbox
services:
  sandbox:
    image: python:3.12-slim
    runtime: runsc  # gVisor
    network_mode: none  # no network by default
    read_only: false
    tmpfs:
      - /workspace
```

#### Resource Limits per Sandbox

```json
{
  "cpu": "1.0",
  "memory": "512m",
  "disk": "1g",
  "network": false,
  "timeout": 300
}
```

---

## 5. Kebutuhan Teknis

### 5.1. Stack

| Layer | Teknologi |
|---|---|
| Backend | Python 3.12+, FastAPI |
| AI Orchestration | LangChain DeepAgents (di atas LangGraph) |
| LLM Access | OpenRouter (`langchain-openrouter`, `ChatOpenRouter`) |
| State / Checkpointing | LangGraph Store (PostgreSQL backend) |
| Database | PostgreSQL |
| Sandbox (produksi) | Daytona (self-hosted, open-source) |
| Sandbox (dev/POC) | Docker + gVisor (`runtime: runsc`) |
| Observability | Structured logging (structlog), opsional LangSmith |

### 5.2. OpenRouter Integration

```python
from langchain_openrouter import ChatOpenRouter

# Model di-set dari agent config — bisa ganti tanpa ubah kode
llm = ChatOpenRouter(
    model=agent.model,  # "anthropic/claude-sonnet-4-6" / "openai/gpt-4.1" / dll
    api_key=settings.OPENROUTER_API_KEY,
)
```

Model yang didukung (contoh):
- `anthropic/claude-sonnet-4-6` — default untuk coding agent
- `openai/gpt-4.1-mini` — cost-efficient untuk ops agent
- `meta-llama/llama-3.3-70b-instruct` — open-weight, privacy-sensitive use case
- `google/gemini-2.5-pro` — long context tasks

### 5.3. Security & Auth

**Fase 1:**
- API key per environment (`X-API-Key` header).
- Sandbox: network disabled by default, resource limits enforced.
- Secrets tidak pernah dimasukkan ke sandbox environment.

**Fase 2:**
- JWT / OAuth2 integration.
- Per-user credential vault untuk MCP/external services.
- Mapping `external_user_id` ke identity asli.

---

## 6. Milestone & Scope

### Milestone 1 — POC Backend ✅ SELESAI

- [x] Skeleton FastAPI + PostgreSQL + Alembic migrations.
- [x] Agent CRUD (`POST/GET/PATCH /v1/agents`).
- [x] Session creation endpoint.
- [x] Message endpoint dengan ReAct agent:
  - OpenRouter integration via `ChatOpenAI` + `base_url` (multi-model).
  - Docker sandbox (dev mode) — agent bisa jalankan bash dan baca/tulis file, internet aktif.
  - 4 built-in tools: `bash`, `write_file`, `read_file`, `list_files`.
- [x] Basic logging (step + tool calls ke DB).
- [x] Session history endpoint.

### Milestone 2 — Internal Alpha

- [ ] Daytona self-hosted setup sebagai sandbox backend produksi.
- [ ] Multi-model: agent config bisa pakai model OpenRouter berbeda.
- [ ] Tools tambahan: HTTP internal, basic RAG retrieval.
- [ ] Session history endpoint.
- [ ] Integrasi ke 1 channel (OpenClaw atau webchat).
- [ ] Structured logging (structlog) + step trace di response.

#### Memory System

- [ ] **Short-term memory** — auto-summarize history ketika jumlah token mendekati batas context window LLM, sehingga session panjang tidak overflow.
- [ ] **Long-term memory** — tabel `agent_memories` (per-agent, key-value + timestamp). Tools yang dikasih ke agent:
  - `remember(key, value)` — simpan fakta/preferensi lintas session.
  - `recall(query)` — ambil memory yang relevan (exact match dulu, semantic search fase berikutnya).
  - `forget(key)` — hapus memory entry.
  - Memory di-inject ke system prompt saat agent run.

#### Self-Extending Agent (Skill & Tool Creator)

- [ ] **Skill Creator** — agent bisa membuat dan menyimpan "skill" (instruksi/prompt reusable) ke DB. Tabel `skills` (per-agent: `id`, `agent_id`, `name`, `description`, `content_md`, `created_at`). Tools:
  - `create_skill(name, description, content_md)` — tulis skill baru.
  - `list_skills()` — lihat semua skill milik agent ini.
  - `use_skill(name)` — load isi skill ke dalam context aktif.

- [ ] **Tool Creator** — agent bisa menulis kode Python untuk tool baru, disimpan ke DB, dan otomatis tersedia di session berikutnya. Tabel `custom_tools` (per-agent: `id`, `agent_id`, `name`, `description`, `code`, `created_at`). Tools:
  - `create_tool(name, description, python_code)` — simpan tool baru (kode divalidasi syntax dulu).
  - `list_tools()` — lihat semua custom tool yang tersedia.
  - Saat agent run, custom tools di-load dinamis dan diregistrasi sebagai LangChain tool di samping built-in tools.
  - Eksekusi custom tool berjalan di Docker sandbox (terisolasi).

### Milestone 3 — Hardening & UX

- [ ] API key auth + CORS.
- [ ] Basic web admin (list agents, edit config).
- [ ] LangSmith integration (opsional).
- [ ] Developer docs: cara bikin agent baru, cara nambah tool, cara setup sandbox.
- [ ] SubAgentMiddleware untuk multi-agent coordination (fase ini atau berikutnya).

---

## 7. Risiko & Pertimbangan

| Risiko | Mitigasi |
|---|---|
| **Sandbox security** | Daytona hardware isolation; jalankan dengan network=none; jangan taruh secrets di sandbox |
| **Biaya LLM** | Pakai OpenRouter dengan model hemat untuk agent sederhana; enforce step limit; monitor token usage |
| **Agent drift / loop** | TodoListMiddleware externalize planning; max steps 12; hard timeout 5 menit |
| **Daytona setup complexity** | Fallback ke Docker+gVisor untuk dev; Daytona punya Docker Compose deployment |
| **LangGraph state size** | Checkpoint compression; purge old sessions setelah N hari |

---

## 8. Success Criteria (Fase 1)

Platform dianggap sukses di fase 1 jika:

1. Minimal 1 agent coding (Dev Helper) bisa:
   - Menerima task via API.
   - Nulis file ke sandbox, jalankan Python script, return output — dalam satu session.
   - Multi-step reasoning dengan 3+ tool calls terlog.

2. Minimal 1 agent non-coding (Ops atau Docs Agent) bisa:
   - Di-call via API dengan model berbeda dari coding agent.
   - Mengunakan tool HTTP atau RAG.

3. Platform engineer bisa:
   - Buat agent baru / update model dan instructions via API — **tanpa ubah kode Python**.

4. Ada log cukup untuk trace kenapa agent ambil langkah tertentu (tool apa dipakai, hasil apa).
