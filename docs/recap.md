# Recap: Sub-Agent Shared Workspace — Cross-Subagent Collaboration Tanpa Bocor Antar User

**Tanggal**: 2026-05-08
**Status**: ✅ Selesai — verified, 39/39 tests pass

## Konteks

Sebelumnya tiap sub-agent punya workspace terisolasi total:
- Main agent: `{SANDBOX_BASE_DIR}/{session_id}/`
- Sys subagent: `{SANDBOX_BASE_DIR}/{session_id}_sys_{name}/`
- Custom subagent: `{SANDBOX_BASE_DIR}/{session_id}_sub_{agent_id}/`

Akibatnya `sys_analyst` bikin chart, `sys_coder` gak bisa baca file-nya — kolaborasi mustahil tanpa
routing manual via main agent.

## Solusi

Tambah folder `shared/` per **session** (bukan per agent) yang di-mount ke semua sub-sandbox dalam
session yang sama. Karena `parent_session_id` adalah UUID dari tabel `sessions` yang unik per
(agent_id × external_user_id), shared dir otomatis terisolasi per user — mustahil bocor antar user.

## File yang Diubah

| File | Perubahan |
|------|-----------|
| `app/core/infra/sandbox.py` | `_WORKSPACE_SUBDIRS` += `"shared"`; tambah `get_shared_dir()`; `DockerSandbox(session_id, parent_session_id=None)` — sub bikin symlink `workspace/shared → {parent}/shared`; `bash()` tambah bind mount `/workspace/shared` ke parent shared dir |
| `app/core/engine/deep_agent_backend.py` | `__init__` track `_shared_root`; `_resolve()` izinkan path yang resolve ke shared root (bukan cuma `_root`); `_rel()` handle path under shared dir → return `shared/...` |
| `app/core/engine/subagent_builder.py` | Sys + custom sub sandboxes pass `parent_session_id=parent_session_id`; prompt `sys_coder` & `sys_analyst` dapat dokumentasi `/workspace/shared/` |

## Isolasi Multi-tenant

```
session_X (User A) ──┬── main agent workspace
                     ├── sys_coder workspace ──► /workspace/shared → {session_X}/shared
                     └── sys_analyst workspace ──► /workspace/shared → {session_X}/shared

session_Y (User B) ──┬── main agent workspace
                     ├── sys_coder workspace ──► /workspace/shared → {session_Y}/shared
                     └── sys_analyst workspace ──► /workspace/shared → {session_Y}/shared
```

Session_X ≠ session_Y, jadi shared dir-nya direktori berbeda di host. Tidak ada code path yang
bisa cross-mount antar session.

## Verifikasi

```python
s = DockerSandbox('test_main')
s2 = DockerSandbox('test_main_sys_coder', parent_session_id='test_main')
# s.shared_dir == s2.shared_dir  → True
# s2.workspace_dir/'shared' is_symlink → True
# DockerBackend(s2)._resolve('/workspace/shared/foo.txt') → {test_main}/shared/foo.txt ✅
```

Tests: `pytest tests/test_session_lock_and_history.py` → 39 passed.

## Dual-Path Coherence

- **Container path** (bash/execute): `/workspace/shared/*` → bind mount ke `{parent}/shared/`
- **Host path** (DockerBackend write_file/read_file): `sub_workspace/shared/*` → symlink ke `{parent}/shared/*`

Kedua path resolve ke direktori host yang sama. Sub-agent A `write_file('shared/x.png')` →
sub-agent B bisa langsung `read_file('shared/x.png')` atau `execute('cat /workspace/shared/x.png')`.

---

# Recap: Arthur End-to-End Test + Sandbox Image Fix + Test Skill

**Tanggal**: 2026-05-06
**Status**: ✅ Selesai

## Apa yang Ditest

Arthur diuji membuat 3 agent sekaligus via perintah natural language, termasuk 1 request "hard" dari persona orang awam.

## Agent yang Berhasil Dibuat Arthur

| Agent | Perintah Natural | Tools | Hasil |
|-------|-----------------|-------|-------|
| WebBuilder | "Buatkan agent untuk prototype website + deploy Cloudflare" | sandbox, deploy, http, memory, scheduler | Auto-deploy + kasih link tanpa diminta ✅ |
| CS Toko Bagas | "CS toko fashion, eskalasi pembelian ke +6281234567890" | escalation, memory, scheduler | `escalate_to_human` dipanggil + notif terkirim ✅ |
| DocGen | "Bikin agent yang bisa generate PDF, Excel, CSV, Word" | sandbox, whatsapp_media, memory, http | Generate .xlsx + .pdf via openpyxl + reportlab ✅ |
| MarketBot | "Pantau BTC/ETH/BBCA tiap hari, notif turun >5%, laporan mingguan" | http, sandbox, scheduler, memory | Harga live CoinGecko + set_reminder cron + baseline disimpan ✅ |

## Perilaku Arthur yang Terverifikasi

- Tanya klarifikasi (nama + channel) jika kurang info — 1 pertanyaan, jawab natural, langsung buat
- Auto: `plan_agent` → `validate_agent_config` → `create_agent` → `http_post` seed soul → `update_daily`
- Soul agent baru ter-seed via `http_post("/v1/agents/{id}/memory")` — bukan via `remember()` Arthur sendiri
- Jika validasi gagal (misal preset butuh RAG), Arthur adjust dan validasi ulang otomatis

## Bug yang Ditemukan & Diperbaiki

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| `MultipleResultsFound` saat load soul | Arthur punya 3 duplikat soul record di DB | `get_memory()` di `memory_service.py`: `.scalar_one_or_none()` → `.scalars().first()` |
| DocGen tidak bisa import openpyxl/reportlab | `.env` override ke `DOCKER_SANDBOX_IMAGE=python:3.12` (image plain) | Buat `sandbox.Dockerfile` + build `managed-agents-sandbox:latest`, update `.env` |

## File yang Dibuat/Diubah

| File | Perubahan |
|------|-----------|
| `app/core/domain/memory_service.py` | `get_memory()`: `.scalar_one_or_none()` → `.scalars().first()` untuk handle duplikat |
| `sandbox.Dockerfile` | **BARU** — extend `nikolaik/python-nodejs:python3.12-nodejs22` + install openpyxl, reportlab, fpdf2, python-docx, pandas, pillow, dll |
| `.env` | `DOCKER_SANDBOX_IMAGE=managed-agents-sandbox:latest` |
| `app/config.py` | Default `docker_sandbox_image` update ke `managed-agents-sandbox:latest` |
| `docs/test-arthur.md` | **BARU** — dokumentasi langkah test Arthur end-to-end |
| `.claude/commands/test-arthur.md` | **BARU** — slash command `/test-arthur` untuk Claude Code |

## Sandbox Image Baru

```bash
# Build (butuh internet, ~2 menit):
DOCKER_HOST=unix:///run/docker.sock docker build -f sandbox.Dockerfile -t managed-agents-sandbox:latest .

# Packages yang tersedia di sandbox sekarang:
# openpyxl, xlsxwriter, reportlab, fpdf2, python-docx, pandas, pillow,
# requests, beautifulsoup4, lxml, matplotlib, jinja2
# + semua bawaan nikolaik/python-nodejs (Python 3.12 + Node 22)
```

## Test Skill

Dibuat slash command `/test-arthur` di `.claude/commands/test-arthur.md` — berisi checklist lengkap, tabel test per agent type, known issues & fix, catatan arsitektur.

---

# Recap: OpenClaw Memory System — Layered Memory + Heartbeat

**Tanggal**: 2026-05-06
**Status**: ✅ Implementasi selesai

## Fitur

Adopsi sistem memory OpenClaw ke platform SaaS. Semua agent (termasuk Arthur) kini punya layered memory yang di-inject otomatis ke setiap sesi — tanpa butuh langkah manual dari agent.

## Arsitektur

```
Setiap sesi agent:
  agent_runner.py
    → load_layered_memory(agent_id, scope=external_user_id)
      → soul          (scope=None, global per agent)
      → user_profile  (scope=user_phone)
      → daily:today   (scope=user_phone)
      → daily:yesterday (scope=user_phone)
    → build_system_prompt(..., layered_memory=...)
      → inject "# Panduan Operasional" ke atas system prompt
        → Identitasmu (soul)
        → User yang Kamu Bantu (user_profile)
        → Konteks Hari Ini (daily today + yesterday)
        → Memory — cara kerja recall/update_daily/update_longterm
        → Heartbeat — protocol HEARTBEAT_OK
        → Keamanan & Batasan
      → dilanjut base_instructions (business logic)
```

## Memory Layers (key di `agent_memories` table)

| Key | Scope | Auto-inject | Deskripsi |
|-----|-------|-------------|-----------|
| `soul` | NULL (global) | ✅ | Identitas agent |
| `user_profile` | external_user_id | ✅ | Profil user |
| `daily:YYYY-MM-DD` | external_user_id | ✅ hari ini + kemarin | Catatan harian |
| `longterm` | external_user_id | ❌ lazy via recall() | Curated memory jangka panjang |
| `heartbeat:config` | external_user_id | ❌ | Heartbeat interval + quiet hours |
| `heartbeat:checklist` | external_user_id | ❌ | Checklist yang dijalankan saat heartbeat |

## File yang Dibuat/Diubah

| File | Perubahan |
|------|-----------|
| `app/core/domain/memory_service.py` | Tambah `load_layered_memory()`, filter layered keys dari `build_memory_context()` |
| `app/core/engine/agent_runner.py` | Load `layered_memory`, pass ke `build_system_prompt`, load `build_heartbeat_tools` |
| `app/core/engine/prompt_builder.py` | `build_system_prompt()` terima `layered_memory`, render `# Panduan Operasional` lengkap |
| `app/core/engine/tool_builder.py` | Tambah `update_daily`, `update_longterm` ke memory tools; tambah `build_heartbeat_tools()` (`enable_heartbeat`, `disable_heartbeat`) |
| `app/core/workers/scheduler_service.py` | Tambah `_run_heartbeat_job()`: detect `[HEARTBEAT]` payload, quiet hours check, find last session per user, HEARTBEAT_OK logic, kirim via channel atau SSE |
| `system-message-builder.md` | Arthur wajib seed soul agent baru via `http_post` ke memory API; wajib `update_daily` + `update_longterm` setelah buat agent |
| `scripts/seed_arthur.py` | Tambah `ARTHUR_SOUL`, seed ke `agent_memories` scope=NULL setelah create/update |

## Isolasi Memory (Multi-tenant Safety)

Memory tidak bocor antar user karena unique constraint DB: `(agent_id, scope, key)`.
- `soul` → `scope=NULL` → shared (intentional, identitas agent sama untuk semua user)
- Semua layer lain → `scope=external_user_id` → terisolasi per user

## Heartbeat System

```
APScheduler _tick() setiap menit
  → deteksi job dengan payload="[HEARTBEAT]"
  → _run_heartbeat_job():
      1. Cek quiet hours dari heartbeat:config (WIB timezone)
      2. Find last active session (agent_id + external_user_id)
      3. Load heartbeat:checklist dari memory
      4. run_agent() dengan "[HEARTBEAT] {checklist}"
      5. Reply "HEARTBEAT_OK" → log only (diam)
         Reply lain → kirim WA (jika channel=whatsapp) atau SSE (webchat/API)
```

Agent aktifkan heartbeat via tool: `enable_heartbeat(interval_minutes=30, quiet_start="23:00", quiet_end="08:00")`

## Arthur Soul Seeding

Setelah `create_agent()`, Arthur wajib:
1. `http_post("/v1/agents/{agent_id}/memory", {"key": "soul", "value": "<identitas ringkas>"})` → seed soul agent baru
2. `update_daily(...)` → catat ke daily log Arthur sendiri
3. `update_longterm(...)` → simpan preferensi arsitektur user

Arthur's own soul di-seed via `scripts/seed_arthur.py` ke `agent_memories` (scope=NULL).

---

# Recap: Bug Fix — Agent Sandbox & Deploy Missing Tools (Agent "Bodoh")

**Tanggal**: 2026-05-05
**Status**: ✅ Fixed

## Gejala

Agen yang dikonfigurasi dengan `sandbox: true` dan `deploy: true` melalui UI (atau API) tetap gagal menjalankan tugas *coding* atau *deployment*. Pengguna mendapati agen merespons "secara bodoh" (generik) atau tidak bisa mengakses *docker sandbox* karena alat (tools) tersebut ternyata **tidak ada** secara *runtime* untuk digunakan oleh agen, padahal agen tersebut secara logika di-_set_ untuk memiliki tools tersebut.

## Root Cause

Terdapat tiga bug berlapis:

### Bug 1 — UI / Config Pydantic Schema Validation 422
- Payload `tools_config` dari antarmuka Web UI secara bawaan (default) menyertakan `mcp: false`.
- Di backend, pydantic schema `ToolsConfig` mengharapkan tipe `dict` untuk `mcp`. Ini menghasilkan HTTP 422 Unprocessable Entity saat menyimpan atau mengupdate agent.

### Bug 2 — Fallback Siluman (Silent Fallback) pada DeepAgents SDK (Kritis)
- Saat menginisialisasi Graph di `agent_runner.py` dan `subagent_builder.py`, sistem menggunakan `create_deep_agent` dari SDK `deepagents` (ini yang me-load `DockerBackend` filesystem dan *execute tools* ke agen).
- Sistem mem-pass `ChatOpenAI(model=...).bind(parallel_tool_calls=False)` yang me-return tipe objek `RunnableBinding`, bukan objek `ChatOpenAI` secara *raw*.
- SDK `deepagents` mencoba mengekstrak nama model dengan memanggil `.count()` pada objek tersebut, menghasilkan `AttributeError`.
- Karena ada *catch clause* generik `except (ImportError, TypeError):` yang tidak sengaja menangkap internal error ini, inisialisasi tersebut melakukan *fallback* diam-diam (silent fallback) ke `create_react_agent` **tanpa backend Docker**. Agen pun akhirnya **kehilangan** semua kapabilitas sandbox (`execute`, `write_file`, `deploy_app`, dll).

## Fix yang Diimplementasikan

### 1. `app/core/config_schema.py`
Menambahkan Pydantic `model_validator` pre-validator untuk field `mcp`, agar mem-parsing tipe *boolean* (seperti `False`) menjadi dictionary kosong `{}`.

### 2. UI Frontend (`UI-DEV/app.js`, `UI-DEV/index.html`)
- Mengubah fungsi set state form default untuk memastikan `mcp` diinisialisasi sebagai `{}`.
- Menambahkan dokumentasi hint di UI yang menyatakan `mcp` menolak nilai boolean.

### 3. `app/core/engine/agent_runner.py` & `app/core/engine/subagent_builder.py`
- **Fix Utama**: Memisahkan inisialisasi *raw LLM* dengan yang sudah di-*bind*. Kini, fungsi `create_deep_agent` diberikan `llm_raw` (tanpa `.bind()`) sehingga error SDK terhindarkan dan backend Docker berhasil diinjeksi. Varian LLM dengan binding tetap disisakan untuk proses *fallback*.
- Memperbaiki `except` clause dengan menambahkan `AttributeError` dan `log.warning` yang mencatat detail pengecualian secara eksplisit agar kejadian silent fallback dapat diobservasi/dilacak pada server log di masa mendatang.

---

# Recap: Bug Fix — Dangling Tool Call / "No tool output found for function call"

**Tanggal**: 2026-05-05
**Status**: ✅ Fixed — 12/12 regression tests pass

## Gejala

Log `agent_run.dangling_tool_call_retry` muncul saat run agent dengan model `openai/gpt-5.1`.
Provider reject turn dengan error:
```
No tool output found for function call ...
```

## Root Cause

**Dua bug berlapis:**

### Bug 1 — `prior_messages` tidak di-sanitize sebelum first `ainvoke`

`_sanitize_input_messages()` hanya dipanggil di retry path, bukan sebelum first invoke.
Jika `prior_messages` dari DB history mengandung AIMessage dengan dangling `tool_calls`
(AIMessage tanpa ToolMessage pasangan), first invoke langsung gagal karena provider reject.

### Bug 2 — Retry pakai graph yang sama (`create_deep_agent`)

Retry path menggunakan graph yang sama tanpa rebuild. Jika Deep Agents SDK
yang menyebabkan tool result hilang mid-graph (misalnya tool exception sebelum
ToolMessage ditulis ke state), retry dengan graph yang sama akan gagal lagi.

## Fix yang Diimplementasikan

### `app/core/agent_runner.py`

**1. Sanitize `prior_messages` sebelum first invoke:**
```python
# SEBELUM
input_messages = prior_messages + [HumanMessage(content=human_content)]

# SESUDAH
sanitized_prior = _sanitize_input_messages(prior_messages)
input_messages = sanitized_prior + [HumanMessage(content=human_content)]
```

**2. Retry dengan fallback ke `create_react_agent` (LangGraph built-in):**
```python
# SEBELUM: retry with same graph
result = await graph.ainvoke({"messages": clean_input}, ...)

# SESUDAH: rebuild graph with create_react_agent as safer executor
from langgraph.prebuilt import create_react_agent as _cra
_fallback_graph = _cra(llm, tools=tools, prompt=system_prompt)
result = await _fallback_graph.ainvoke({"messages": clean_input}, ...)
```

**3. Tool call integrity audit log (observability):**
- Post-graph: log semua `tool_call_id` yang di-request vs yang di-answer
- Log `agent_run.tool_call_integrity_check` jika ada dangling
- Log `agent_run.tool_call_integrity_ok` jika semua selesai

## Behavior: Before vs After

| Scenario | Before | After |
|----------|--------|-------|
| History dengan dangling AIMessage tool_calls | First invoke gagal → retry → mungkin gagal lagi | Sanitized sebelum invoke → tidak pernah gagal karena history |
| Deep Agents SDK drops tool result mid-graph | Retry dengan graph sama → provider reject lagi | Retry dengan `create_react_agent` → LangGraph ToolNode lebih reliable |
| Clean history, normal execution | OK | OK (unchanged) |

## `openai/gpt-5.1` — Sequential Tool Calls

Model ini di-bind dengan `parallel_tool_calls=False` di semua path (sudah ada sebelumnya di line 215).
Retry path juga menggunakan LLM yang sama dengan binding ini.
Semua agent path (normal + retry) sekarang berjalan sequential.

## Files Changed

| File | Perubahan |
|------|-----------|
| `app/core/agent_runner.py` | Sanitize prior_messages sebelum first ainvoke; retry pakai fallback `create_react_agent`; tool call integrity audit log |
| `tests/test_tool_call_orchestration.py` | +8 test cases baru: `TestSanitizeInputMessages` extended, `TestPreInvokeSanitizationRegression` |

## Test Plan

```bash
.venv/bin/python -m pytest tests/test_tool_call_orchestration.py -v
# 12/12 PASSED
```

Regression tests cover:
- Dangling tool_calls di prior_messages distrip sebelum first invoke
- Partial dangling (mixed answered+orphaned): hanya yang orphaned distrip
- Clean history tetap untouched (no regression)
- `_ensure_tool_messages_complete` inject synthetic untuk orphan mid-graph
- Idempotent: double-run tidak double-inject

---

# Recap: Deploy Feature — Sandbox Agent → Public URL via Cloudflare Tunnel

**Tanggal**: 2026-05-04
**Status**: ✅ Implementasi selesai — ⏳ End-to-end test pending (Docker sock issue di local)

## Fitur

Agent dengan `sandbox: true` kini bisa deploy hasil koding ke public URL secara otomatis, tanpa konfigurasi tambahan.

## Arsitektur

```
Agent (sandbox=true)
  → deploy_app(command, port)        # LangChain tool
    → deployment_service.deploy_app()
      → Docker: start app container (python:3.11-slim, mount workspace)
      → Wait: container stable (running, not restarting)
      → Docker: start cloudflared container (network_mode=container:{id})
      → Wait: capture trycloudflare.com URL dari logs
      → Wait: poll /dev/tcp/localhost/{port} sampai app benar-benar listen
      → Return: {"url": "https://xxx.trycloudflare.com", "status": "running"}
```

## File yang Dibuat/Diubah

| File | Perubahan |
|------|-----------|
| `app/core/deployment_service.py` | **BARU** — lifecycle manager: deploy_app, stop_deployment, get_deployment_status, get_app_logs |
| `app/core/tools/deployment_tools.py` | **BARU** — 4 LangChain tools: deploy_app, stop_deployment, get_deployment_status, get_deployment_logs |
| `app/core/tool_builder.py` | Tambah `build_deployment_tools(sandbox)` factory; import deployment_tools |
| `app/core/agent_runner.py` | Deploy tools auto-aktif saat sandbox aktif (no extra flag needed) |
| `app/core/tools/builder_tools.py` | `_TOOLS_CONFIG_DOCS` — tambah deploy entry; `update_agent` merge tools_config (bukan replace) |
| `system-message-builder.md` | Arthur tahu: `sandbox:true` = deploy otomatis aktif; tidak perlu key `deploy` terpisah |
| `UI-DEV/app.js` | `_toolBadges()` — tampilkan 🐳sandbox, 🚀deploy di tabel agents; `linkify()` — URL di chat jadi clickable |
| `UI-DEV/style.css` | `.tool-badge`, `.chat-link` styles |
| `UI-DEV/index.html` | tools-config-hint updated |

## Key Fixes yang Diimplementasikan

### Fix 1 — 502 Bad Gateway (cloudflared tidak bisa reach Flask)
**Root cause**: Cloudflared di container terpisah tidak bisa reach `localhost:8080` milik app container (Flask bind ke `127.0.0.1`).
**Fix**: `network_mode=f"container:{app_container.id}"` — cloudflared share network namespace app container, sehingga `localhost` adalah sama.

### Fix 2 — 409 Conflict (cloudflared gagal join namespace)
**Root cause**: Cloudflared dimulai sebelum app container stably running → gagal join namespace.
**Fix**: Wait loop 20s untuk konfirmasi container running stabil (double-check), baru start cloudflared. Gunakan container ID (bukan name) untuk network_mode, retry 3x.

### Fix 3 — 502 Bad Gateway (URL ready tapi app belum listen)
**Root cause**: Cloudflared URL muncul dalam ~3s, tapi pip install + Flask startup butuh 30-60s. URL dikembalikan ke user sebelum app siap.
**Fix**: Poll `/dev/tcp/localhost/{port}` via `exec_run` hingga 120s sebelum return URL ke agent.

### Fix 4 — Deep Agents SDK double-nested workspace
**Root cause**: SDK menulis file ke `workspace_dir/workspace/` (nested), bukan `workspace_dir/`.
**Fix**: `actual_workspace = workspace_dir / "workspace"` jika exists, fallback ke `workspace_dir`.

### Fix 5 — Arthur tidak tahu deploy capability
**Root cause**: `_TOOLS_CONFIG_DOCS` tidak punya entry untuk `deploy`.
**Fix**: Tambah entry di dict; update `system-message-builder.md`.

### Fix 6 — Arthur replace tools_config saat update_agent
**Root cause**: `update_agent` tool melakukan `agent.tools_config = json.loads(tools_config)` — overwrite penuh.
**Fix**: Merge: `existing.update(new_tc)` — preserve keys yang tidak disebutkan.

## Naming Convention Containers

```
madeploy-app-{session_id[:12]}  — app container (Flask/Python)
madeploy-cf-{session_id[:12]}   — cloudflared container
```

Containers bersifat persistent (`restart_policy: unless-stopped`). `deploy_app()` selalu kill existing sebelum create baru.

## Cara Pakai (dari agent)

```
User: "Buat Flask app hello world dan deploy, kasih linknya"
Agent: [write_file app.py] → [execute pip install flask] → [deploy_app("python app.py", 8080)]
Agent: "App sudah live di https://xxx.trycloudflare.com"
```

## Status Production (Remote Server)

Arthur di remote (`managed-agent.chiefaiofficer.id`, agent `f72ed473`) **tidak punya `is_system_agent=true`** karena:
- Remote adalah server terpisah dengan DB berbeda
- PATCH API `/v1/agents/{id}` dengan `is_system_agent:true` tidak berhasil (field kemungkinan tidak ada di deployed schema)
- Perlu akses DB remote (via `scripts/seed_arthur.py`) atau SSH ke server

**Workaround untuk production**: Run `python scripts/seed_arthur.py` di remote server, atau update Arthur's `is_system_agent` langsung via psql.

---

# Recap: Arthur End-to-End Test & Agent Quality Fixes

**Tanggal**: 2026-04-30
**Status**: ✅ Terselesaikan

## Test Round 1 — Glow Studio (2026-04-30, sebelumnya)

Arthur diuji dengan use case tersulit: klinik kecantikan "Glow Studio" — 10 requirement kompleks.

| Aspek | Skor | Catatan |
|-------|------|---------|
| Pemahaman brief | 9/10 | Tangkap 10 req sekaligus, hanya minta nama agent |
| Kualitas instructions | 9/10 | Lengkap, terstruktur, no-markdown, contoh percakapan konkret |
| Flow kerja (validate → create) | 9/10 | Otomatis panggil `validate_agent_config` dulu, baru `create_agent` |
| Handling escalation | 9/10 | Tool call eksplisit + kondisi spesifik masuk ke instructions |
| Reliability (empty reply bug) | 5/10 | Reply kosong saat session panjang + Arthur panggil tools |
| max_tokens awareness | 4/10 | Tidak set field ini meski tersedia di schema |
| **Overall** | **7.5/10** | Production-ready untuk use case standar, 2 bug kritis perlu fix |

Bug dari round ini sudah diselesaikan (lihat section di bawah).

---

## Test Round 2 — Warung Bu Sari, Non-IT User (2026-04-30)

Arthur diuji dengan persona user non-IT pemilik warung makan, full conversation dari discovery sampai create agent + revisi.

### Alur Arthur

1. **Init**: Panggil `get_platform_capabilities()` sebelum menyapa — tidak terlihat user, di background
2. **Discovery**: 1 pertanyaan per giliran, menggali bisnis → kebutuhan → operator eskalasi → menu & info
3. **Konfirmasi**: Rangkum rencana sebelum create — tunggu persetujuan user
4. **Create**: `validate_agent_config` → `create_agent` — agent "Sari" jadi dalam satu turn
5. **Revisi post-create**: Update nama, tambah promo, tambah aturan libur nasional — semua via `update_agent`

### Hasil

| Aspek | Hasil |
|-------|-------|
| Bahasa non-teknis | ✅ Tidak ada istilah API/UUID/token ke user |
| Discovery flow | ✅ Natural, 1 pertanyaan per giliran |
| Instructions quality | ✅ max_tokens=700, menu lengkap, escalation eksplisit |
| Revisi post-create | ✅ Arthur update langsung via `update_agent` |

---

## Test Round 3 — Stress Test Agent "Sari Bu Warung" (2026-04-30)

Agent yang dibuat Arthur diuji dengan 8 edge case. Ditemukan 5 bug, semua dilaporkan ke Arthur dan diperbaiki via conversation.

### Bug yang Ditemukan & Diperbaiki

| # | Bug | Sebelum | Sesudah | Fix |
|---|-----|---------|---------|-----|
| 1 | **Hallucination pembayaran** | "hanya tunai, belum bisa QRIS" (ngarang) | "Untuk info cara pembayaran, nanti dibantu Bu Sari" | Arthur update instructions via `update_agent` |
| 2 | **Hallucination delivery** | "bisa delivery, pemilik bantu proses pengantaran" (ngarang) | "Belum ada layanan delivery ya kak" | Arthur update instructions via `update_agent` |
| 3 | **Out of topic** | Jawab soal matematika (360) | "Aku fokus bantu info Warung Bu Sari aja ya kak" | Arthur tambah guardrail topik |
| 4 | **Tawari pesanan sendiri** | "Mau sekalian dipesankan?" lalu harus eskalasi | Langsung arahkan ke Bu Sari tanpa nawarin | Arthur perketat deteksi pesanan |
| 5 | **Asumsi hari Jumat** | "Hari ini ada promo kak..." (tidak tahu hari apa) | "Promo soto ayam khusus setiap hari Jumat ya kak" | Arthur hapus pola "hari ini ada promo" |

Root cause semua bug: Arthur tidak menyertakan aturan eksplisit untuk skenario yang tidak pernah dibahas saat discovery (pembayaran, delivery, off-topic, ambiguitas pesanan). Agent WA mengisi kekosongan dengan asumsi umum yang seringkali salah.

---

## Bug Fixes yang Diimplementasikan

### Fix 1 — 3 Bug dari Round 1 ✅

**Bug 1 (High): Empty Reply saat Arthur Panggil Tools**
- `system-message-builder.md` — hapus instruksi "via subagent WAJIB"
- `app/core/agent_runner.py` — robust list content extraction untuk handle `[{"type":"text","text":"..."}]`

**Bug 2 (Medium): `create_agent` tidak support `max_tokens`**
- `app/core/tools/builder_tools.py` — tambah `max_tokens` di `create_agent` + dokumentasi di `get_platform_capabilities`
- `system-message-builder.md` — panduan: WA CS agent gunakan `max_tokens=512-800`

**Bug 3 (Low): Subagent `task` dipanggil meski disabled**
- Sama dengan Bug 1 fix #1

### Fix 2 — Arthur Self-Update dengan Operator Gate ✅

- `app/core/tools/builder_tools.py` — `update_agent` cek `operator_ids` sebelum izinkan self-update; tambah `add_operator` / `remove_operator` params; `get_self_config` expose `operator_ids`
- `system-message-builder.md` — tambah section "Kelola Diri Sendiri" dengan verifikasi operator sebelum eksekusi
- `scripts/seed_arthur.py` — support env var `ARTHUR_OPERATOR_PHONES` untuk bootstrap operator

### Fix 3 — Aturan Konfirmasi Sebelum Update ✅

**Bug**: Arthur kadang memanggil `update_agent` sebelum user memberikan konfirmasi eksplisit.

**Fix** di `system-message-builder.md`:
- Fase 3: `JANGAN buat agent sebelum user menjawab konfirmasi ini`
- Section "Kelola Agent": aturan **Propose → Tunggu → Execute**
- Jika banyak perubahan: satu proposal, satu konfirmasi, satu `update_agent` call

### Fix 4 — Aturan Edit vs Create Baru ✅

**Bug**: Setelah agent dibuat, user minta perubahan → Arthur buat agent baru alih-alih update.

**Fix** di `system-message-builder.md`:
- Fase 5: wajib simpan `agent_id` ke memory setelah create; perubahan apapun pasca-create → `update_agent`, bukan `create_agent` lagi
- Section "Kelola Agent": tambah blok "Aturan Edit vs Create Baru" — `create_agent` hanya jika user eksplisit minta agent berbeda/baru

### Fix 5 — Stress Test Round 4: 3 Agent (Kece, Sehati, Bersih) ✅

**Temuan dari stress test adversarial:**

| # | Bug Arthur | Deskripsi | Fix |
|---|-----------|-----------|-----|
| 1 | **Terlalu perfeksionis** | User sudah bilang "buat sekarang" 2x masih diblokir pertanyaan | Fase 3: jika user sudah request create ≥2x, proceed dengan default — jangan blokir lagi |
| 2 | **"Buat + tambah fitur" dalam 1 kalimat** | "oke buat, tapi tambahkan scheduler" → Arthur langsung create tanpa proses fitur tambahan | Fase 4: jika ada kata "tapi/dan/tambahkan" setelah konfirmasi, proses dulu sebelum create |
| 3 | **"Update palsu" tanpa tool call** | Arthur tulis "sudah diupdate" tanpa memanggil `update_agent` | Tambah LARANGAN KERAS: jangan bilang "sudah diupdate" tanpa benar-benar memanggil tool |

**Temuan dari stress test kualitas agent yang dihasilkan:**

| Agent | Bug | Before | After |
|-------|-----|--------|-------|
| Kece | Hallucinate harga | Ngarang "kisaran 50-70rb" | "Belum punya data, tanya admin" ✅ |
| Kece | Off-topic | Antusias rekomendasikan restoran | Tolak halus, redirect ke toko ✅ |
| Kece | Saran medis | Suruh duduk, minum air putih | "Bukan tenaga medis, hubungi faskes" ✅ |
| Sehati | Eskalasi tanpa tool | Bilang "diteruskan" tanpa action | Escalate via `escalate_to_human` tool ✅ |

Root cause semua bug agent: **gap instruksi = LLM isi sendiri dengan asumsi umum**. Arthur tidak menyertakan aturan default untuk topik yang tidak dibahas saat discovery (pembayaran, delivery, off-topic, medis). Semua diperbaiki via conversation dengan Arthur menggunakan `update_agent`.

**Kesimpulan pola:** Hampir semua masalah — baik di Arthur maupun di agent yang dibuatnya — berakar di system message, bukan kode. LLM butuh instruksi *negatif* ("jangan lakukan X") sama pentingnya dengan instruksi *positif* ("lakukan Y"). Satu kalimat ambigu di rulebook menghasilkan perilaku salah yang konsisten di ratusan sesi.

---

## File yang Diubah

| File | Perubahan |
|------|-----------|
| `system-message-builder.md` | Hapus subagent wajib; panduan max_tokens; self-update section; aturan konfirmasi + edit vs create baru; fix perfeksionis; fix "buat+fitur"; larangan update palsu — Arthur v35 |
| `app/core/agent_runner.py` | Robust list content extraction |
| `app/core/tools/builder_tools.py` | max_tokens di create_agent; operator gate di update_agent; add/remove_operator; operator_ids di get_self_config |
| `scripts/seed_arthur.py` | ARTHUR_OPERATOR_PHONES env var + merge logic |

---

# Recap: Token Efficiency — Per-Agent max_tokens & Prompt Trimming

**Tanggal**: 2026-04-30
**Status**: ✅ Terselesaikan

## Konteks
OpenRouter memblokir kredit sebesar `input_tokens + max_tokens` di awal setiap request. Default `max_tokens=4096` menyebabkan over-reservation — rata-rata reply WA hanya 50-200 tokens, tapi 4096 selalu dicadangkan.

## Perubahan

### 1. Per-agent `max_tokens` kolom di DB
- `app/models/agent.py` — tambah `max_tokens: Mapped[int | None]` (nullable, override per-agent)
- `app/schemas/agent.py` — tambah di `AgentCreate`, `AgentUpdate`, `AgentResponse`
- `app/api/agents.py` — pass `max_tokens` di create handler
- Migration: `alembic/versions/24aaaa8cc724_add_max_tokens_to_agents.py`

### 2. agent_runner.py pakai per-agent value
```python
_max_tokens = getattr(agent_model, "max_tokens", None) or settings.llm_max_tokens
```
Fallback ke global default jika kolom null.

### 3. Global default turun: 4096 → 1024
- `app/config.py`: `llm_max_tokens = 1024`, `default_subagent_max_tokens = 512`
- `app/core/subagent_builder.py`: ganti hardcode `4096` → `settings.default_subagent_max_tokens`

### 4. Arthur di-set max_tokens=2048
- `scripts/seed_arthur.py`: `max_tokens: 2048` (Arthur perlu ruang untuk nulis instructions)

### 5. Trim system-message-builder.md
- 10,494 chars → 5,547 chars (hemat ~1,236 tokens input/request)
- Hapus Self-Identity block dari system prompt (pakai `get_self_config` tool on-demand)
- Disable subagents di Arthur config (hemat ~250 tokens/request)

## Total Saving per Request (Arthur)
| Komponen | Saved |
|---|---|
| Instructions trim | ~1,236 tokens |
| Self-identity block | ~70 tokens |
| Subagents disabled | ~250 tokens |
| max_tokens reservation | ~3,072 tokens (4096→1024 default; Arthur 4096→2048) |
| **Total** | **~4,628 tokens per request** |

## Panduan per Use Case
| Agent Type | Rekomendasi max_tokens |
|---|---|
| WA CS / asisten sederhana | 512–800 |
| Arthur (agent builder) | 2048 |
| Agent dengan sandbox/coding | 1024–2048 |
| Default global | 1024 |

---

# Recap: Bug Fixes — Escalation, max_tokens Warning, LID Phone Display

**Tanggal**: 2026-04-29  
**Status**: ✅ Terselesaikan

## Bug 1: `max_tokens` UserWarning di langchain-openai

**Gejala**: Log `UserWarning: Parameters {'max_tokens'} should be specified explicitly. Instead they were passed in as part of model_kwargs parameter.`

**Root Cause**: Versi langchain-openai terbaru sekarang mendukung `max_tokens` sebagai parameter langsung di `ChatOpenAI`. Menggunakan `model_kwargs={"max_tokens": N}` sudah deprecated.

**Fix**:
- `app/core/agent_runner.py` — ganti `model_kwargs={"max_tokens": settings.llm_max_tokens}` → `max_tokens=settings.llm_max_tokens`
- `app/core/subagent_builder.py` — sama di dua tempat (system subagents + custom agent subagents)

## Bug 2: Agent Eskalasi via Teks, Tidak Panggil Tool

**Gejala**: Agent menulis "Saya akan teruskan ke tim CS" di reply teks tapi tidak memanggil tool `escalate_to_human`. Operator tidak dapat notifikasi.

**Root Cause**: Template system prompt di `system-message-builder.md` hanya menyebut "eskalasikan jika kondisi X" tapi tidak secara eksplisit instruksikan untuk *memanggil tool*.

**Fix**: Tambah instruksi di seksi `ESKALASI KE OPERATOR` template:
```
- Cara eskalasi WAJIB: panggil tool escalate_to_human(reason, summary) terlebih dahulu — baru balas user
- JANGAN hanya bilang "diteruskan ke tim" tanpa memanggil tool escalate_to_human
```
Arthur direseeed dengan template baru.

## Bug 3: Notifikasi Eskalasi Tampilkan LID, Bukan Phone Number

**Gejala**: Notifikasi eskalasi ke operator menampilkan `278593120796757@lid` di field "Chat ID/no wa" — bukan nomor telepon yang bisa dibaca.

**Root Cause**: `escalation_tool.py` menggunakan `user_wa_jid = channel_cfg.get("user_phone")` untuk display. `channel_config.user_phone` berisi `effective_reply_target` (LID JID dari `body.chat_id`). Padahal `user_phone_display = session.external_user_id` sudah ada (berisi phone number yang di-resolve) tapi tidak dipakai di `notif_text`.

**Fix** (`app/core/tools/escalation_tool.py`):
```python
# Sebelum
f"Chat ID/no wa: {user_wa_jid}\n"
# Sesudah
f"Chat ID/no wa: {user_phone_display}\n"
```
`user_phone_display = session.external_user_id or user_wa_jid` — mengambil phone number yang sudah di-normalize dari `body.phone_from` (di-resolve Go service dari LID).

---

# Recap: Phase 4 Agent Builder (Integration Testing)

**Tanggal**: 2026-04-28  
**Status**: ✅ Terselesaikan (automated) — 🔲 Manual WA testing pending

## Deskripsi
Integration tests + seed script untuk Agent Builder. Memvalidasi seluruh pipeline end-to-end secara programatik.

## Perubahan Utama
1. **scripts/seed_arthur.py** — Script untuk create/update Arthur di DB. Baca `system-message-builder.md` sebagai instructions, set `is_system_agent=True`, `allowed_senders=None`. Supports `--dry-run`.
2. **tests/test_agent_builder_phase4.py** — 26 integration tests:
   - `TestSeedScript` — dry-run works, config valid
   - `TestArthurConfig` — is_system_agent, http, wa_agent_manager, allowed_senders=None
   - `TestBuilderPipelineFlow` — 5 steps: validate→create→list→get_detail→update
   - `TestTenantIsolation` — User A tidak bisa akses agent User B (list/update/read)
   - `TestAgentRunnerIntegration` — builder tools dimuat hanya untuk system agent
   - `TestWABestPracticesValidation` — markdown warn, escalation suggest, quality score

## Total TDD
- Phase 1: 19 tests | Phase 2: 28 tests | Phase 3: 30 tests | Phase 4: 26 tests | Existing: 13 tests
- **Total: 116/116 PASSED**

## Cara Deploy Arthur
```bash
# 1. Apply migration (sudah dilakukan di Phase 1)
make upgrade

# 2. Seed Arthur
python scripts/seed_arthur.py

# 3. Hubungkan ke WA dev untuk testing
# POST /v1/agents/{arthur_id}/whatsapp/connect
# Scan QR → chat untuk testing manual
```

## Next Steps (manual)
- Chat dengan Arthur via wa-dev-service
- Minta buatkan 2-3 agent (CS toko, asisten pribadi, bot FAQ)
- Validasi kualitas instructions yang dihasilkan
- Iterasi rulebook jika ada gap

---

# Recap: Phase 3 Agent Builder (Builder Tools)

**Tanggal**: 2026-04-28  
**Status**: ✅ Terselesaikan

## Deskripsi
Implementasi `builder_tools.py` — 7 tools eksklusif untuk system agent (Arthur/Agent Builder), integrasi ke `tool_builder.py` dan `agent_runner.py`.

## Perubahan Utama
1. **app/core/tools/builder_tools.py** — 7 tools baru:
   - `get_platform_capabilities` — ringkasan kapabilitas platform (tools, channels, models, limitations, WA best practices)
   - `list_available_wa_devices` — daftar WA device yang sudah di-assign
   - `validate_agent_config` — validasi config + quality score sebelum create/update
   - `create_agent` — buat agent baru di DB, owner_phone otomatis masuk `operator_ids`
   - `update_agent` — update agent dengan ownership check via `operator_ids`
   - `get_agent_detail` — baca config agent dengan ownership check
   - `list_my_agents` — list agent milik user (filter via `operator_ids`)
2. **app/core/tool_builder.py** — tambah `build_builder_tools(db, owner_phone)` wrapper
3. **app/core/agent_runner.py** — tambah blok: `if agent_model.is_system_agent: tools.extend(build_builder_tools(...))`
4. **tests/test_builder_tools.py** — 30 TDD tests, 30/30 passed

## Keamanan
- Setiap tool ownership-scoped via `operator_ids` — user hanya bisa lihat/edit agent miliknya
- `is_system_agent=False` hardcoded di `create_agent` — agent yang dibuat tidak bisa jadi meta-agent
- Builder tools tidak dimuat untuk agent biasa (`is_system_agent=False`)

## Next Steps
- Phase 4: Testing & Iterasi — chat langsung dengan Arthur via wa-dev-service

---

# Recap: Phase 2 Agent Builder (Platform Rulebook)

**Tanggal**: 2026-04-28  
**Status**: ✅ Terselesaikan

## Deskripsi
Audit seluruh kapabilitas platform dan penulisan `system-message-builder.md` — Platform Rulebook komprehensif yang menjadi system prompt Arthur (Agent Builder).

## Perubahan Utama
1. **system-message-builder.md** — Platform Rulebook baru di root project berisi: identitas Arthur, konfigurasi platform, kapabilitas teknis (12 tools_config keys, semua channel, input types), best practices prompting WhatsApp (no-markdown, panjang pesan, few-shot, eskalasi), batasan platform, template system prompt, alur 5 fase, endpoint reference lengkap.
2. **tests/test_agent_builder_phase2.py** — 28 TDD tests memvalidasi semua seksi wajib rulebook. 28/28 passed.

## Next Steps
- Phase 3: Builder Tools (`builder_tools.py`, `build_builder_tools()`, integrasi `agent_runner.py`)

---

# Recap: Phase 1 Agent Builder (Multi-tenancy Foundation)

**Tanggal**: 2026-04-28  
**Status**: ✅ Terselesaikan

## Deskripsi
Implementasi fondasi untuk Agent Builder yang bertugas membantu user onboard/create agent via WhatsApp.

## Perubahan Utama
1. **Model & Schema:** Menambahkan `is_system_agent` (Boolean, default False) di `Agent` model dan Pydantic schemas. Digunakan untuk membedakan meta-agent (system) yang boleh mengakses builder tools dengan agent biasa.
2. **Database:** Generate dan apply Alembic migration `add_is_system_agent_to_agents`.
3. **Tests:** Membuat 19 unit test untuk verifikasi behavior DB default, schema, dan integrasi migration (TDD). Semua test berjalan hijau.
4. **Fix Pre-existing Regression:** Menemukan dan memperbaiki 2 test lama yang gagal di `test_transcription_service.py` akibat format label yang sudah berubah pada fix context poisoning sebelumnya.

## Next Steps
- Lanjut ke Phase 2 (Platform Rulebook) dan Phase 3 (Builder Tools).

---

# Recap: Bug Allowlist & WhatsApp LID Accounts

**Tanggal**: 2026-04-28  
**Status**: ✅ Terselesaikan — Verified working in production

---

# Recap: Bug Voice Note Transcription — OGG Format Not Supported

**Tanggal**: 2026-04-28  
**Status**: ✅ Terselesaikan — Verified working in production

## Gejala

Kirim voice note (PTT) di WhatsApp → transcription gagal dengan error 400 dari OpenRouter:
```
"Invalid value: 'ogg'. Supported values are: 'wav' and 'mp3'."
```
Agent menerima fallback `[Voice note: tidak dapat ditranskripsi]` dan tidak bisa baca isi VN.

## Root Cause

Model `openai/gpt-audio-mini` via OpenRouter hanya support format `wav` dan `mp3`. WhatsApp PTT dikirim sebagai `.ogg`. Kode sebelumnya langsung kirim format `ogg` ke API tanpa konversi.

## Fix

### 1. `app/core/transcription_service.py`
- Tambah `_OPENAI_SUPPORTED_FORMATS = {"mp3", "wav"}`
- Tambah `_convert_to_mp3(audio_b64)` — konversi via `ffmpeg` menggunakan `asyncio.create_subprocess_exec`
- Pakai `shutil.which("ffmpeg")` untuk resolve path binary
- Di `transcribe_audio()`: auto-konversi jika format bukan mp3/wav sebelum kirim ke API

### 2. `Dockerfile`
- Tambah `ffmpeg` ke `apt-get install` block

### 3. `tests/test_transcription_service.py`
- 2 test baru: `test_ogg_converted_to_mp3`, `test_ogg_conversion_failure_returns_fallback`
- Existing tests ganti ke format `"mp3"`
- `TestProcessWaMediaAudio` tests: monkeypatch `_convert_to_mp3`
- **13/13 tests passed**

## Deploy
```bash
cd deploy && docker compose -f docker-compose.prod.yml up --build -d api
```

---

# Recap: Bug Agent Tidak Mengerti Transkrip VN (Context Poisoning)

**Tanggal**: 2026-04-28  
**Status**: ✅ Terselesaikan

## Gejala

Transkripsi berhasil (log `transcription_service.success`) tapi agent tetap bilang "tidak bisa membaca/mendengar audio". Terjadi terutama di session lama (30+ pesan).

## Root Cause

Dua masalah berlapis:
1. **Duplicate label** — Go set `text = "[Voice note]"`, Python append `\n[Voice note: transcript]` → agent terima dua label membingungkan, tidak tahu mana yang isi asli
2. **Summary poisoning** — Session lama di-summarize dengan riwayat agent bilang "tidak bisa transkripsi" berkali-kali → polusi permanen di context, override pesan baru

## Fix

### 1. `app/api/channels.py`
Jika `media_type` adalah `ptt`/`audio` dan ada `media_context`, pakai `media_context.strip()` langsung sebagai `user_message` — buang placeholder `[Voice note]` dari Go yang redundan.

### 2. `app/api/wa_helpers.py`
Format label lebih eksplisit agar agent tidak ambigu:
```python
media_context = (
    f"\n[Sistem: Pengguna mengirim {label}. "
    f"Berikut hasil transkripsi otomatis — balas berdasarkan isi ini]\n"
    f"Transkripsi: {transcript}"
)
```

### 3. `app/core/prompt_builder.py` ← Fix Root Cause
Tambah instruksi permanen di WhatsApp system prompt (dibangun fresh tiap request, tidak hilang saat context di-summarize):
> Jika pesan mengandung `[Sistem: Pengguna mengirim pesan suara...]` + `Transkripsi: <teks>`, agent sudah menerima isi VN. Balas langsung — JANGAN bilang tidak bisa membaca audio.

## Kenapa `prompt_builder.py` adalah Fix yang Tepat
System prompt dibangun ulang setiap request → tidak terpengaruh session history/summary yang kotor. Conversation history bisa corrupt, system prompt selalu fresh dan benar.

## Deploy
```bash
cd deploy && docker compose -f docker-compose.prod.yml up -d api
```

---

---

## Deskripsi Bug

Fitur `allowed_senders` pada agent tidak berfungsi dengan benar untuk akun WhatsApp modern yang menggunakan sistem **LID (Linked ID)**.

### Gejala

- User isi `allowed_senders` dengan format nomor biasa: `+6282299312107`
- Agent **tetap memblokir** nomor tersebut (seharusnya diizinkan)
- Agent juga **memblokir nomor lain** yang tidak ada di allowlist (seharusnya benar)
- Kedua nomor diblokir dengan log yang sama

### Log Error di Server

```json
{"device_id": "wadev_b76b7e02-...", "from_phone": "+236116347228384", "chat_id": "236116347228384@lid", "event": "wa_incoming.blocked_sender"}
{"device_id": "wadev_b76b7e02-...", "from_phone": "+151414827434073", "chat_id": "151414827434073@lid", "event": "wa_incoming.blocked_sender"}
```

---

## Root Cause

WhatsApp modern menggunakan dua sistem identifikasi berbeda:

| Format | Contoh | Keterangan |
|--------|--------|-----------|
| **Phone JID** | `6282299312107@s.whatsapp.net` | Akun WA lama |
| **LID** | `236116347228384@lid` | Akun WA baru (Linked ID) |

Untuk akun LID:
- `evt.Info.Sender.User` di Go berisi **LID number** (`236116347228384`), **bukan phone number**
- `evt.Info.Chat.String()` juga berisi LID format (`236116347228384@lid`)
- **Tidak ada field yang berisi phone number asli** dalam message event

Sehingga:
- `allowed_senders = ["+6282299312107"]` → normalized: `6282299312107`
- Incoming `from_phone = "+236116347228384"` → normalized: `236116347228384`
- **Tidak pernah match** karena keduanya adalah identifier berbeda

---

## Yang Sudah Dicoba (Gagal)

### Attempt 1: Dual-check from_phone + chat_id
Cek allowlist terhadap `from_phone` DAN `chat_id`. Gagal karena keduanya sama-sama LID format.

### Attempt 2: `GetPNForLID` dari local store
Gunakan `client.Store.LIDs.GetPNForLID()` di Go untuk resolve LID → phone dari local SQLite cache.  
Gagal karena: mapping LID↔phone hanya ada di cache jika kontak sudah pernah di-sync WA (akun yang baru pertama kali pesan tidak ada di cache).

### Attempt 3: `IsOnWhatsApp` endpoint
Tambah endpoint `POST /devices/{id}/resolve-phones` di Go yang query WA server via `IsOnWhatsApp()`.  
Python panggil endpoint ini saat allowlist check, resolve `+6282299312107` → actual WA JID.  
**Status**: Kode sudah di-push tapi hasil di server masih sama — kemungkinan:
- Docker belum rebuild dengan kode terbaru
- `IsOnWhatsApp` rate-limited atau belum dipanggil dengan benar
- Masih ada issue di alur resolve

---

## File yang Dimodifikasi

| File | Perubahan |
|------|-----------|
| `wa-service/handlers.go` | Tambah handler `resolvePhones` — endpoint `POST /devices/{id}/resolve-phones` |
| `wa-service/main.go` | Register route `resolve-phones` |
| `wa-service/device_manager.go` | Tambah field `phone_from` di payload, coba `GetPNForLID` |
| `app/core/wa_client.py` | Tambah fungsi `resolve_wa_phones()` |
| `app/api/channels.py` | Allowlist check pakai `resolve_wa_phones` + JID comparison |
| `app/models/agent.py` | Tambah kolom `allowed_senders` (JSONB) |
| `app/models/session.py` | Tambah kolom `ai_disabled` (Boolean) |

---

## Root Cause Sebenarnya (Ditemukan)

`wa-dev-service` menghasilkan device ID dengan prefix `wadev_` (format: `wadev_{agentID}`). Di `app/core/wa_client.py`, fungsi `resolve_wa_phones` langsung return `{}` untuk device ID dengan prefix `wadev_`:

```python
if device_id.startswith("wadev_"):
    return {}  # ← bypass total, allowed_set hanya berisi phone asli
```

Akibatnya:
- `allowed_set = {"6282299312107"}` (phone biasa saja)
- `candidates = {"236116347228384"}` (LID number dari incoming)
- **Tidak pernah intersect → selalu blocked**

## Fix yang Diimplementasikan

### 1. `wa-dev-service/whatsapp.go`
Tambah method `ResolvePhones()` ke `WhatsAppClient` yang memanggil `client.IsOnWhatsApp()` — sama persis dengan wa-service.

### 2. `wa-dev-service/api.go`
Tambah handler `POST /resolve-phones` yang memanggil method di atas.

### 3. `wa-dev-service/main.go`
Register route `POST /resolve-phones`.

### 4. `app/core/wa_client.py`
Ganti early-return `wadev_` dengan routing ke `_wa_dev_base_url()/resolve-phones`:
```python
if device_id.startswith("wadev_"):
    url = f"{_wa_dev_base_url()}/resolve-phones"
else:
    url = f"{_base_url()}/devices/{device_id}/resolve-phones"
```

### Setelah fix, flow yang benar:
1. Incoming: `from_phone = "236116347228384"` (LID)
2. `resolve_wa_phones(device_id, ["+6282299312107"])` → wa-dev `/resolve-phones` → `IsOnWhatsApp(["6282299312107"])` → `{"6282299312107": "236116347228384@lid"}`
3. `allowed_set = {"6282299312107", "236116347228384"}` (phone + LID part)
4. `candidates = {"236116347228384"}` → **intersect! → allowed**

### Deploy
Rebuild wa-dev-service binary: `make wa-dev-build`

### Hasil
✅ **Verified working** — nomor phone biasa (`+6282299312107`) di `allowed_senders` berhasil dikenali meskipun incoming adalah LID account.

---

## Yang Perlu Diinvestigasi Lebih Lanjut

1. **Verifikasi endpoint resolve-phones berjalan**: Test manual dengan curl dari VPS:
   ```bash
   curl -X POST http://localhost:8080/devices/{device_id}/resolve-phones \
     -H "Content-Type: application/json" \
     -d '{"phones": ["+6282299312107"]}'
   ```
   Lihat apakah response-nya mengandung JID yang benar (LID atau phone).

2. **Cek apakah `IsOnWhatsApp` return LID JID**: Untuk akun LID, `res.JID` dari `IsOnWhatsApp` seharusnya berisi `236116347228384@lid`. Kalau tidak, berarti WA API tidak mengekspos mapping ini.

3. **Alternatif: Simpan mapping LID↔phone di DB**: Saat session pertama dibuat untuk akun LID, simpan `external_user_id = LID` tapi juga simpan `phone_number` jika tersedia. Operator kemudian bisa query session untuk tahu LID dari phone number.

4. **Alternatif: Ubah cara input allowed_senders**: Buat operator bisa input dalam format LID juga, atau buat sistem dimana operator bisa "learn" nomor yang masuk sebelum di-allowlist.

5. **Cek whatsmeow versi terbaru**: Ada kemungkinan versi terbaru whatsmeow punya API lain untuk resolve LID yang lebih reliable.

---

## Context Code Penting

### Allowlist check saat ini (`app/api/channels.py` ~line 248)
```python
if not _is_operator:
    allowed = getattr(agent, "allowed_senders", None)
    if allowed:
        resolved = await resolve_wa_phones(body.device_id, [p for p in allowed if p])
        allowed_set: set[str] = set()
        for p in allowed:
            normalized = normalize_phone(p)
            allowed_set.add(normalized)
            jid = resolved.get(normalized)
            if jid:
                allowed_set.add(normalize_phone(jid))
        candidates = {normalize_phone(from_phone)}
        if reply_target:
            candidates.add(normalize_phone(reply_target))
        if not candidates.intersection(allowed_set):
            return {"status": "ignored", "reason": "sender not in allowlist"}
```

### Go resolve-phones handler (`wa-service/handlers.go`)
```go
results, err := info.Client.IsOnWhatsApp(r.Context(), stripped)
// stripped = ["6282299312107"]
// results[0].JID.String() seharusnya = "236116347228384@lid" untuk akun LID
```

### Dari mana `from_phone` di Python
```
Go wa-service → body.from_ = "+" + evt.Info.Sender.User  (LID number untuk akun LID)
Go wa-service → body.phone_from = hasil GetPNForLID (fallback ke from_ jika gagal)
Python → from_phone = body.phone_from or body.from_
```