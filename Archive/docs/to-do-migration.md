# Migration TODO — Deep Agents SDK (Phase 1, 2 & 3)

**Last updated:** 2026-04-24  
**Goal:** Migrasi penuh platform managed agent ke arsitektur Deep Agents SDK tanpa mengubah tujuan project.

---

## Status Phase 1 (SELESAI ✅)

| Item | Status |
|------|--------|
| Buat `app/core/deep_agent_backend.py` (`DockerBackend`) | ✅ Done |
| Hapus `build_sandbox_tools()` dari `agent_runner.py` | ✅ Done |
| Pertahankan `sandbox_write_binary_file` sebagai custom tool | ✅ Done |
| Pass `backend=DockerBackend(sandbox)` ke `create_deep_agent()` | ✅ Done |
| Smoke test `DockerBackend` | ✅ Done |

Agent sekarang otomatis punya:
- `write_todos` — planning / task decomposition
- `read_file`, `write_file`, `edit_file`, `ls`, `glob`, `grep` — dari Deep Agents via backend
- `execute` — shell di Docker container
- `sandbox_write_binary_file` — custom tool (base64 binary write, tidak ada di BackendProtocol)

---

## Phase 2 — Sub-agent Spawning

### Arsitektur Sub-agent (Gabungan insight internal + Gemini research)

Deep Agent pattern yang sebenarnya bukan sekadar "delegate task ke agent lain" — ada **feedback loop** yang membuat output lebih berkualitas:

```
Main Agent (Orchestrator)
  → Planner: breakdown task → DAG of steps          [write_todos sudah handle ini]
  → Executor: jalankan step (Researcher/Coder/etc)
  → Grader: review hasil Executor
      → reject? → Executor retry dengan feedback spesifik
      → approve? → Main Agent lanjut ke step berikutnya
  → Memory Summarizer: compress context di background [otomatis, bukan explicit call]
```

#### Katalog Sub-agent

**Tier 1 — Executor (prioritas Phase 2, implementasi straightforward):**

| Sub-agent | Fokus | Tools utama |
|-----------|-------|-------------|
| **Researcher** | Cari info, HTTP, summarize sumber | `http_get`, `http_post`, RAG |
| **Coder** | Tulis & jalankan kode, debug, testing | Sandbox penuh, `execute`, file tools |
| **Writer** | Draft, edit, format dokumen | Memory, skills |
| **Analyst** | Olah data, hitung, buat laporan | Sandbox (pandas/numpy), `execute` |

**Tier 2 — Quality Gate (implementasi lebih kompleks, butuh desain khusus):**

| Sub-agent | Fokus | Catatan desain |
|-----------|-------|----------------|
| **Critic/Reviewer** | Review output Executor, beri feedback spesifik, approve/reject | Dipanggil *setelah* Executor selesai — bukan dipilih bebas oleh main agent. Butuh middleware pattern |
| **Planner** | Breakdown task kompleks ke DAG, monitor relevansi rencana | `write_todos` SDK sudah cover basic planning. Sub-agent Planner untuk kasus yang lebih dalam (estimasi waktu, dependencies antar step) |

**Tier 3 — Background/Utility (post Phase 2, butuh desain infrastruktur):**

| Sub-agent | Fokus | Catatan desain |
|-----------|-------|----------------|
| **Memory Summarizer** | Compress context panjang jadi Long-term Context, jalan di background | Harusnya trigger otomatis saat token mendekati limit — bukan explicit call dari main agent |
| **Formatter** | Transform output ke format target (JSON, markdown, HTML, tabel) | Lightweight, bisa jadi skill biasa dulu |
| **Validator** | Cek output sesuai schema/constraint sebelum kirim ke sistem eksternal | Bisa jadi bagian Critic, tidak harus agent terpisah |

---

### Keputusan Desain (perlu dikonfirmasi)

| Pertanyaan | Keputusan |
|---|---|
| Siapa yang bisa jadi subagent? | Whitelist di `tools_config.subagents` (list agent UUID) |
| Workspace subagent | Isolated — workspace terpisah per subagent run |
| Tools subagent | Inherit dari DB config agent itu sendiri |
| Session subagent | Ephemeral (tidak disimpan ke DB sebagai session penuh) |
| Memory subagent | Isolated — tidak bocor ke parent |
| System sub-agents | Pre-built agents (Researcher, Coder, dll) tersedia sebagai "library" — user pilih mana yang di-whitelist, bukan semua auto-aktif |
| Model sub-agents | Configurable per sub-agent; default hemat (haiku/gpt-4o-mini) kecuali Critic yang butuh reasoning kuat |

---

### TODO List

---

#### 2.1 — Schema & Model Update

- [x] **Tambah field `subagents` ke `tools_config` schema (dokumentasi)** ✅
  - Format:
    ```json
    {
      "subagents": {
        "enabled": true,
        "agent_ids": ["uuid-1", "uuid-2"]
      }
    }
    ```
  - Tidak butuh migration DB — `tools_config` sudah JSONB, cukup update dokumentasi dan defaults
  - Update `CLAUDE.md` dan Postman collection dengan contoh field baru

---

#### 2.2 — `build_subagents()` di `agent_runner.py`

- [x] **Buat fungsi `build_subagents(agent_ids, db, tools_config, sandbox) -> list[SubAgent]`**
  - Import dari `deepagents.middleware.subagents import SubAgent`
  - Untuk setiap `agent_id` di whitelist:
    1. Load `Agent` dari DB dengan `select(Agent).where(Agent.id == agent_id)`
    2. Bangun tools subagent berdasarkan `agent.tools_config`:
       - Memory tools (scope = subagent session ID agar isolated)
       - Skill tools
       - HTTP tools (jika enabled di config subagent)
       - **Tidak** include: escalation, scheduler, wa_agent_manager (subagent tidak punya channel)
    3. Buat `DockerBackend` terpisah jika subagent config punya sandbox enabled:
       - Gunakan `DockerSandbox(session_id=f"{parent_session_id}_sub_{agent_id}")`
       - Workspace isolated di `{SANDBOX_BASE_DIR}/{parent_session_id}_sub_{agent_id}/`
    4. Construct `SubAgent` TypedDict:
       ```python
       SubAgent(
           name=agent.name,           # dipakai main agent saat call task()
           description=agent.instructions[:200],  # ringkasan untuk main agent
           system_prompt=agent.instructions,
           model=agent.model,
           tools=subagent_tools,
       )
       ```
  - Return list kosong jika `tools_config.subagents` tidak enabled atau `agent_ids` kosong

- [x] **Handle error gracefully**
  - Jika agent_id tidak ditemukan di DB → log warning, skip (jangan crash)
  - Jika DB error saat load subagent config → return list kosong, log error

---

#### 2.3 — Integrasi ke `run_agent()`

- [x] **Panggil `build_subagents()` di `run_agent()` sebelum `create_deep_agent()`**
  - Letakkan setelah tools assembly (setelah MCP tools), sebelum graph creation
  - Pattern:
    ```python
    subagent_list = []
    if _is_enabled(tools_config, "subagents", default=False):
        _sub_ids = tools_config.get("subagents", {}).get("agent_ids", [])
        subagent_list = await build_subagents(_sub_ids, db, tools_config, sandbox)
        if subagent_list:
            active_groups.append(f"subagents({len(subagent_list)})")
    ```

- [x] **Pass `subagents` ke `create_deep_agent()`**
  ```python
  graph = create_deep_agent(
      model=llm,
      tools=tools,
      system_prompt=system_prompt,
      backend=backend,
      subagents=subagent_list or None,
  )
  ```

- [x] **Cleanup sandbox subagent**
  - Setelah graph selesai (success dan error path), close semua `DockerSandbox` subagent
  - Simpan list sandbox subagent yang dibuat agar bisa di-close di finally block

---

#### 2.4 — Logging & Observability

- [x] **Log subagent tool calls**
  - Deep Agents SDK emit tool calls `task(name=..., task=...)` — sudah ter-log via `AgentLogger` yang ada
  - Tambah log khusus saat subagent list dibangun: `log.info("agent_run.subagents_ready", names=[s["name"] for s in subagent_list])`

- [ ] **Persist subagent interactions ke DB (opsional, diskusi dulu)**
  - Opsi 1: Cukup catat di parent session sebagai ToolMessage (sudah otomatis via AgentLogger)
  - Opsi 2: Buat child session di DB untuk setiap subagent run (lebih traceable, lebih complex)
  - **Rekomendasi:** Opsi 1 dulu, Opsi 2 kalau ada kebutuhan audit trail

---

#### 2.5 — System Prompt Update

- [x] **Update system prompt template di `run_agent()` untuk inform agent tentang subagent**
  - Saat `subagent_list` tidak kosong, tambahkan blok ke system prompt:
    ```
    ## Available Subagents
    Kamu bisa delegate task ke subagent berikut menggunakan tool `task(name=..., task=...)`:
    - **{name}**: {description}
    ```
  - Letakkan di Agent Context Block (sudah ada di system prompt builder)

---

#### 2.6 — API & UI Update

- [x] **Update Postman collection** ✅
  - Tambah request "Buat Orchestrator Agent" dengan contoh `subagents` field
  - Disisipkan di folder Agents setelah MCP agents

- [x] **Update UI-DEV** ✅
  - `setAgentFormDefaults()` di `app.js`: tambah `subagents: { enabled: false, agent_ids: [] }` sebagai default
  - `index.html`: tambah hint text untuk field subagents di tools config textarea

---

#### 2.7 — Testing

- [x] **Manual test scenario 1: Task decomposition + subagent** ✅
  - Orchestrator (claude-haiku) + Researcher (gpt-4o-mini, http enabled)
  - Log confirmed: `build_subagents.loaded`, `agent_run.subagents_ready`, `agent_step.tool_call: task`
  - End-to-end berhasil: orchestrator delegate riset MCP ke researcher, hasil dikompilasi jadi jawaban final
  - Bug ditemukan & fix: model string OpenRouter (`"openai/gpt-4o-mini"`) harus pass sebagai `ChatOpenAI` instance ke SDK

- [x] **Manual test scenario 2: Subagent dengan sandbox** ✅
  - Orchestrator (sandbox=false) + Coder (sandbox=true)
  - Workspace subagent isolated di `{session_id}_sub_{agent_id}/` — terpisah dari parent
  - Parent session tidak punya workspace directory (sandbox=false) ✓
  - Coder berhasil write dan read file dalam sandbox-nya sendiri ✓

- [x] **Manual test scenario 3: Agent ID tidak valid di whitelist** ✅
  - Test dengan 3 invalid IDs: valid UUID tapi tidak ada di DB, UUID random, dan string bukan UUID
  - Log: `build_subagents.not_found` (x2), `build_subagents.invalid_uuid` (x1)
  - Agent tetap jalan normal tanpa subagent — tidak crash ✓

- [x] **Manual test scenario 4: Subagent loop protection** ✅ (tidak perlu guard tambahan)
  - Setup circular: orchestrator → researcher → orchestrator (whitelist sirkular)
  - **Temuan:** Loop rekursif TIDAK terjadi secara otomatis
  - SDK tidak auto-invoke subagent config milik subagent yang dipanggil — subagent hanya menjalankan task-nya sendiri
  - Guard alami: `recursion_limit = agent_max_steps * 2` di LangGraph sebagai backstop akhir
  - **Kesimpulan:** Tidak perlu guard manual di `build_subagents()` untuk saat ini

---

---

#### 2.8 — System Sub-agent Library (Pre-built Specialists)

- [x] **Buat seed script / migration untuk pre-built sub-agents** ✅
  - Buat 4 agent di DB sebagai "system agents" dengan flag `is_system=true` (atau cukup prefix nama `sys_`)
  - Konfigurasi masing-masing:
    ```
    sys_researcher  → http enabled, RAG enabled, no sandbox
    sys_coder       → sandbox enabled, no http
    sys_writer      → memory + skills, no sandbox, no http
    sys_analyst     → sandbox enabled (pandas/numpy image), no http
    ```
  - System agents ini tidak muncul di list agent biasa (filter by is_system)
  - User bisa whitelist UUID mereka di `tools_config.subagents.agent_ids`

- [x] **Dokumentasi: contoh orchestrator + sub-agent setup** ✅
  - Buat contoh Postman request: orchestrator agent dengan semua 4 system sub-agent di whitelist
  - Tulis instruksi di `CLAUDE.md` cara setup agent sebagai sub-agent

---

## Phase 3 — Quality Gate & Background Agents (Post Phase 2)

> Tier 2 dan Tier 3 dari katalog sub-agent — butuh desain lebih dalam karena behavior berbeda dari Executor biasa.

### 3.1 — Critic/Reviewer (Quality Gate)

- [x] **Implementasi Critic sebagai sub-agent (Opsi B)** ✅
  - Critic tidak dipanggil bebas oleh main agent — ia dipanggil otomatis *setelah* setiap Executor call
  - Opsi A: Wrap Executor sub-agent dengan `CriticMiddleware` (intercept output sebelum dikembalikan ke main agent)
  - Opsi B: Main agent punya instruction eksplisit: "always call critic after executor, retry if rejected"
  - Opsi B lebih mudah diimplementasi dulu — Critic jadi sub-agent biasa, main agent yang atur flow-nya
- [ ] **Critic system prompt**: harus bisa output terstruktur (approve/reject + feedback)
- [ ] **Loop protection**: Critic tidak boleh trigger Critic lain (depth guard)

### 3.2 — Memory Summarizer (Background)

- [x] **Implementasi Memory Summarizer** ✅
  - Trigger: `user_msg_count >= 10` (re-summarize setiap 10 pesan berikutnya)
  - Cache di `session.metadata_["context_summary"]` — tidak perlu migrasi DB
  - Inject ke system prompt sebagai `## Conversation Context Summary` block
  - `sys_critic` ditambahkan sebagai system sub-agent ke-5 (hardcoded)

### 3.3 — Infrastruktur lanjutan

- [ ] **Dynamic subagent discovery**: Main agent bisa query `GET /v1/agents` via http tool untuk discover agent lain, bukan hardcoded UUID
- [ ] **Subagent result caching**: Cache hasil subagent yang sama dalam satu session
- [ ] **Subagent timeout**: Konfigurasi timeout terpisah untuk subagent run (default lebih pendek dari parent)
- [ ] **Child session persistence**: Simpan subagent run sebagai child session di DB untuk audit trail
- [ ] **`is_system` flag di tabel `agents`**: pisahkan system sub-agents dari user agents di API list

---

## File yang Akan Diubah/Dibuat

| File | Aksi | Keterangan |
|------|------|-----------|
| `app/core/agent_runner.py` | Modifikasi | Tambah `build_subagents()`, integrasi ke `run_agent()`, update `create_deep_agent()` call |
| `app/core/agent_runner.py` | Modifikasi | Update system prompt builder untuk include subagent list |
| `managed-agents.postman_collection.json` | Modifikasi | Tambah contoh `subagents` tools_config |
| `UI-DEV/app.js` | Modifikasi | Update default tools_config dengan subagents field |
| `UI-DEV/index.html` | Modifikasi | Update hint text |
| `CLAUDE.md` | Modifikasi | Dokumentasikan tools_config.subagents |

> `app/core/deep_agent_backend.py` tidak perlu diubah — sudah final dari Phase 1.

---

## Urutan Pengerjaan

### Phase 2 (Executor sub-agents — segera)

1. Konfirmasi keputusan desain (session ephemeral vs DB)
2. Buat `build_subagents()` function di `agent_runner.py`
3. Integrasi ke `run_agent()` + update `create_deep_agent()` call
4. Update system prompt builder (blok Available Subagents)
5. Buat seed script 4 system sub-agents (Researcher, Coder, Writer, Analyst)
6. Manual test scenario 1–3
7. Investigasi + handle loop protection (scenario 4)
8. Update Postman + UI-DEV
9. Update dokumentasi

### Phase 3 (Quality Gate — setelah Phase 2 stabil)

1. Desain & diskusi Critic middleware (Opsi A vs B)
2. Investigasi Memory Summarizer di SDK
3. Implementasi Critic sebagai sub-agent biasa (Opsi B dulu)
4. Evaluasi: apakah perlu `is_system` flag di DB atau cukup konvensi nama
