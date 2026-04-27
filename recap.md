  Building a self-hosted managed agent platform using LangChain DeepAgents, OpenRouter for multi-model support, and Daytona or Docker+gVisor as a free sandbox. PRD is finalized — next step is scaffolding the Milestone 1 FastAPI skeleton.
  
  -Building a self-hosted multi-model agent platform inspired by Claude Managed Agents, using FastAPI, LangChain, OpenRouter, and Docker sandbox. Milestone 1 scaffold is complete — next step is installing dependencies and running the stack.

  - Platform managed agent sudah berjalan penuh — LLM, tool calling, database, dan Docker sandbox dengan internet aktif semua    
     berfungsi. Milestone 1 selesai, langkah berikutnya adalah memulai Milestone 2. 

  -  Platform managed agent Milestone 1 selesai dan berjalan penuh. PRD Milestone 2 sudah diupdate dengan memory system dan
     skill/tool creator. Next: mulai implementasi fitur-fitur Milestone 2.    

  - Milestone 2 selesai diimplementasi. Fitur baru: Memory System (remember/recall/forget + inject ke system prompt), Skill Library (create_skill/list_skills/use_skill), Self-Extending Tool Creator (create_tool/list_tools/run_custom_tool + dynamic load), API endpoints baru (/memory, /skills, /custom-tools per agent), dan Alembic migration (agent_memories, skills, custom_tools tables). Next: Milestone 3 — API key auth, basic web admin, LangSmith.

  - Perbaikan flow Self-Extending Capabilities: Fix ValueError pada pembuatan docstring dinamis untuk `create_tool`. Peningkatan *System Prompt* secara strict agar Agent diwajibkan menggunakan `use_skill(name="X")` jika diminta memakai skill, dan wajib menggunakan `run_custom_tool()` untuk langsung mengeksekusi tool baru dalam satu sesi yang sama (karena belum diregistrasikan ke LangChain toolkit pada runtime saat ini). Kodingan sudah di-push ke Github.

  - Milestone 2 lanjutan: Multi-model diperkuat (temperature per-agent, default model diupdate ke claude-sonnet-4-6). Tambah /v1/models endpoint dengan 15+ model dari Anthropic/OpenAI/Google/Meta/Mistral/DeepSeek/Qwen. HTTP Tool (http_get/http_post dengan allowed_hosts, timeout) dan RAG Tool (search_knowledge_base via PostgreSQL ILIKE) ditambahkan sebagai opt-in di tools_config. Document management API (/v1/agents/{id}/documents) untuk knowledge base RAG. tools_config sekarang benar-benar mengontrol tool mana yang aktif per agent. Migration 003 untuk tabel documents dan kolom temperature.

  Building a managed agent platform. RAG is now working after enabling it on the CV agent. Next: continue testing or move to the next milestone feature.  

  Building a managed agent platform. RAG is now working correctly. Next: continue with anyremaining Milestone 2 features or fixes


Building a managed AI agent platform with proactive scheduling, human escalation via channels, and MCP tool support. Dev
  UI is functional with escalation simulator. Next: run the server and verify the operator-to-customer message flow works end to
  end.

  Membangun platform managed AI agent berbasis FastAPI. Semua TODO selesai dan Postman collection baru saja diperbarui     
  dengan contoh lengkap untuk semua endpoint, termasuk MCP config, Slack/webhook session, dan semua default tools. Tidak ada task 
  pending. 

  Working on real-time delivery of scheduled agent reminders to the dev UI. Added SSE endpoint and wired it into the chat  
  section — restart the server to test.

Building a managed agent platform. Just fixed scheduled reminders so they appear in the dev UI chat in real-time via SSE. No
     pending tasks — all features are working.      

Adding per-agent subscription with token quota, expiry period, and unique API key per agent. Migration 007 is ready —    
  next step is running alembic upgrade 007 to apply it. (disable recaps in /config)

   Built a WhatsApp integration using a Go microservice (whatsmeow) in wa-service/ and updated the Python API. Next step:   
  run `alembic upgrade 008` to apply the database migration. (disable recaps in /config)

  Built a Go WhatsApp microservice using whatsmeow in the wa-service folder, plus Python integration endpoints. Next action:
     run alembic upgrade head to apply migration 008.

     Working on a managed agent platform's WhatsApp integration. All bugs are fixed and confirmed working. No immediate next   
     action needed.                                                                                                          
                      
   Redesign eskalasi WhatsApp: Operator kini punya **session sendiri** (bukan inject [OPERATOR] ke session user).
   - channels.py: `lookup_user_id = operator_phone` untuk operator — session terpisah per pengirim.
   - `escalation_user_jid` dicari dari session user yang escalation_active=True, diteruskan ke run_agent().
   - agent_runner.py: jika escalation_user_jid ada, inject prompt "SESI OPERATOR" dan set user_jid closure pada
     tool reply_to_user sehingga pesan dikirim tepat ke user yang dieskalasi (bukan ke operator).
   - memory_service: scoped per external_user_id (nomor WA) mencegah kebocoran memori antar user.
   - Tidak perlu flag routing tambahan — operator cukup baca Chat ID dari history pesan eskalasi.

   Bug Fix eskalasi WhatsApp (21 Apr 2026):

   1. Migration fix: 009_memory_scope.py memakai `down_revision = "008_agent_whatsapp"` (nama file),
      padahal revision ID di 008_agent_whatsapp.py adalah `"008"` → Alembic KeyError.
      Fix: ubah ke `down_revision = "008"`. Kolom `scope` di tabel agent_memories sekarang berhasil dibuat.

   2. Routing bug — reply_to_user kirim ke operator bukan user:
      Root cause: di sesi operator, session.channel_config["user_phone"] berisi JID operator sendiri.
      Kode lama meng-override user_jid terlalu terlambat sehingga routing tetap ke operator.
      Fix (escalation_tool.py): load session hanya untuk ambil device_id; user_jid dari closure
      langsung di-set ke ch_cfg["user_phone"] sebelum send_message dipanggil.

   3. Format notifikasi eskalasi diperbarui agar operator tau Chat ID tujuan:
        🚨 [CS AI Clevio] Eskalasi pertanyaan customer
        ID Kasus: esc_<timestamp>_<session_prefix>
        Chat ID/no wa: <user_wa_jid>
        Pertanyaan customer: <summary>
        → Agent akan menyusun draft & minta konfirmasi sebelum kirim.

   4. Flow operator diubah: Draft → Konfirmasi → Kirim (sebelumnya langsung kirim):
      - Agent susun draft rapi dari jawaban operator (fix format, JANGAN tambah konten)
      - Agent tampilkan draft ke operator + tanya "Sudah OK? Ketik 'kirim'..."
      - Setelah operator konfirmasi → reply_to_user(draft) → balas "Terkirim ✓"
      - Berlaku untuk sesi operator baru maupun legacy [OPERATOR] prefix path.

Fixed sandbox so custom tools can install and use third-party
      packages. No next action needed, all changes are live.      

  Fix sandbox agent (21 Apr 2026) — 3 root cause:

  1. Image python:3.12-slim tidak punya curl.
     Fix: ganti DOCKER_SANDBOX_IMAGE=python:3.12 di .env (full Debian, curl sudah include).

  2. PIP_USER=1 + PYTHONUSERBASE=/workspace/.local menyebabkan pip install ke user site,
     tapi Python menonaktifkan user site saat jalan sebagai root di Docker.
     Akibatnya import requests/library apapun gagal meski pip install sukses.
     Fix: hapus PIP_USER=1 dan PYTHONUSERBASE dari environment container — pip install
     system-wide sebagai root, package langsung bisa diimport.

  3. json.dumps(args) menghasilkan JSON literal (false/true/null) yang di-embed langsung
     sebagai Python code di _all_args = {json.dumps(args)}.
     Python tidak kenal false — hanya False. Hasilnya NameError tiap tool dipanggil dengan boolean/null args.
     Fix: double-encode args jadi string (json.dumps(json.dumps(args))), lalu di runtime
     parse balik dengan json.loads(...) sehingga tipe Python-nya benar.



 Memperbaiki flow QR WhatsApp untuk agent Arthur agar QR yang dikirim ke user bisa di-scan dan terhubung. Langkah      
  berikutnya: update instruksi Arthur di DB agar dia pakai tool send_agent_wa_qr dengan agent_id, bukan membuat device sendiri.
   (disable recaps in /config)                                                                                                 

Fixing WhatsApp bot behavior: blocked broadcast/status messages, fixed group @mention detection via LID mapping, and      
     scoped sessions by group JID vs sender phone. No pending action needed—all fixes are live and working.

Fixed WhatsApp document sending and a bug where documents were routed to the wrong number via LID JID mismatch. Next:     
restart the server and test by asking the agent to send a document again. 
Added WhatsApp media understanding: images are sent as multimodal vision input, documents are text-extracted and included
     in the message. Fixed a bug where document extraction silently failed due to wrong settings import. No next action needed.

Added markdown-to-WhatsApp conversion so agent replies display cleanly without raw markdown syntax. No further action 
  needed unless testing reveals edge cases. (disable recaps in /config

Migrasi ke Deep Agents SDK (23 Apr 2026):

1. Migrasi LLM executor dari LangGraph `create_react_agent` ke `deepagents.create_deep_agent`.
   - deepagents memberi agen kemampuan planning (`write_todos`) dan virtual FS tools (`ls`, `read_file`, `write_file`, `edit_file`, `grep`) secara otomatis.
   - Fallback ke `create_react_agent` jika deepagents tidak tersedia (try/except).
   - Rename sandbox tools untuk menghindari konflik nama: `write_file` → `sandbox_write_file`, `read_file` → `sandbox_read_file`.

2. Requirements upgrade ke langchain v1.x ekosistem:
   - deepagents>=0.5.0, langgraph>=1.0.0, langchain>=1.0.0, langchain-openai>=1.0.0, langchain-mcp-adapters>=0.2.0

3. Agent Context Block: setiap system prompt kini diawali blok metadata otomatis:
   - Agent ID, Agent Name, Model, Active Tools, Channel, User Phone, User Role (operator/user), Session ID.
   - Role ditentukan dari `operator_ids` list dan `escalation_config.operator_phone`.

4. `operator_ids` field baru di model Agent (migration 011):
   - List nomor WA/JID yang punya akses operator per agent.
   - Dipakai di channels.py untuk deteksi operator di wa/incoming webhook.
   - Coexist dengan legacy `escalation_config.operator_phone`.

5. Conservative tool defaults:
   - ON by default: memory, skills, escalation.
   - OFF by default: sandbox, tool_creator, scheduler, http, mcp, whatsapp_media, wa_agent_manager.
   - `send_agent_wa_qr` dipindah dari whatsapp_media ke tool group baru `wa_agent_manager` (opt-in).

6. Lazy sandbox init: DockerSandbox hanya dibuat jika `tools_config.sandbox = true`.

Next: jalankan `alembic upgrade head` untuk apply migration 011 (operator_ids column).

Fix TURRRRRR (agent manager) + QR scan (23 Apr 2026):

1. TURRRRRR tidak bisa edit agent karena http_tool.py hanya punya http_get/http_post.
   Fix: tambah http_patch dan http_delete ke http_tool.py.

2. TURRRRRR pakai sandbox bash untuk hit API dan kirim QR (harusnya pakai tools).
   Fix: enable http: true dan wa_agent_manager: true di tools_config TURRRRRR.
   Update instruksi TURRRRRR: wajib pakai http_patch untuk PATCH, send_agent_wa_qr untuk kirim QR.

3. QR tidak bisa di-scan setelah dikirim via WhatsApp.
   Root cause: QR di-generate 256px dengan qrcode.Medium — terlalu kecil dan rapuh setelah kompresi WA.
   Fix: ubah ke qrcode.High, 512px di wa-service/device_manager.go dan wa-dev-service/whatsapp.go.
   Rebuild binary diperlukan: make wa-build.

Update Postman + UI-DEV setelah migrasi Deep Agents (23 Apr 2026):

- Postman collection: semua agent create body diupdate — tools_config pakai defaults baru
  (sandbox/tool_creator/scheduler OFF, memory/skills/escalation ON), tambah field `operator_ids`,
  WA agent pakai `whatsapp_media: true`, nama request dirapikan.
- UI-DEV/app.js: `setAgentFormDefaults()` dan `createWAAgent()` disesuaikan dengan defaults baru.
- UI-DEV/index.html: hint text tools config diupdate mencerminkan defaults konservatif.
- wa-dev-service/connections.json: tidak ada perubahan (data runtime).

Upgrade wa-dev-service setara wa-service (23 Apr 2026):

Sebelumnya wa-dev-service tidak bisa kirim reminder, terima gambar/file, atau eskalasi.
Root cause: wa-dev memanggil /v1/agents/{id}/sessions/{id}/messages langsung — bypass semua
logika media, session, escalation, dan scheduler di Python.

Fix:

1. wa-dev-service/router.go:
   - forwardToAgent() menggantikan callAgentAPI(): POST ke /v1/channels/wa/incoming dengan
     virtual device_id = "wadev_{agentID}". Python menangani session, media, escalation, reminder.
   - handleConnect() disederhanakan: hanya validasi agent dan simpan {from → agentID, chatID}.
     Session Python dibuat otomatis saat pesan pertama masuk.
   - lookupOperatorAgent(): cek apakah pengirim adalah operator di agent manapun via endpoint baru.
     Jika ya, auto-route tanpa perlu 'connect {agentID}' — eskalasi bisa langsung dibalas operator.

2. wa-dev-service/store.go: hapus AgentKey dan SessionID (tidak diperlukan lagi).

3. wa-dev-service/whatsapp.go: tambah dukungan pesan grup dengan deteksi @mention bot
   (termasuk LID account mapping), sama persis seperti wa-service.

4. app/api/channels.py:
   - wa_incoming: agent lookup by agent.id langsung jika device_id berawalan "wadev_".
   - GET /v1/channels/wa-dev/operator-route?phone=...: endpoint baru, dipakai Go router untuk
     auto-route operator tanpa perlu connect command.

5. app/core/wa_client.py: send_wa_message/send_wa_image/send_wa_document mendeteksi prefix
   "wadev_" dan route ke wa-dev-service /send/* alih-alih wa-service /devices/{id}/send*.

6. app/config.py: tambah wa_dev_service_url (default http://localhost:8081).

Hasil: reminder, gambar/dokumen, dan eskalasi semua berfungsi di wa-dev. Cara connect tetap sama
("connect {agentID}"). Rebuild binary diperlukan: make wa-dev-build (atau go build di wa-dev-service/).

Deploy ke VPS production (23 Apr 2026):

1. Project di-deploy ke VPS `194.238.23.242` (user clevio) via Docker + Traefik.
   - Domain: https://managed-agent.chiefaiofficer.id
   - Postgres: pakai instance yang sudah ada di VPS via host.docker.internal
   - wa-service (Go) jalan sebagai container tersendiri dalam network internal Docker
   - UI-DEV di-serve sebagai static files di /ui/ via FastAPI StaticFiles

2. Git workflow: VPS clone dari https://github.com/bagasta/self-managed-agents.git
   Update cukup: git pull → docker compose up -d --build

3. Bug fix @lid JID untuk media WhatsApp:
   Root cause: whatsmeow otomatis resolve @s.whatsapp.net → @lid untuk pesan teks,
   tapi TIDAK untuk media (gambar/dokumen). Akibatnya send-image OK tapi gambar tidak sampai.
   Fix: tambah helper resolveJID() di wa-service/device_manager.go yang pakai IsOnWhatsApp()
   untuk lookup JID yang benar sebelum upload dan kirim media. Berlaku untuk SendImage dan SendDocument.

4. Bug fix AgentLogger:
   LangChain terbaru kadang pass None sebagai serialized di on_chain_start callback.
   Fix: tambah guard `if not serialized: return` sebelum .get() di agent_runner.py.

Deploy wa-dev-service ke production (24 Apr 2026):

1. Tambah wa-dev-service ke docker-compose.prod.yml sebagai container tersendiri.
   - Dockerfile baru di wa-dev-service/Dockerfile (sama strukturnya dengan wa-service).
   - Dashboard di-expose via Traefik subpath `/wa-dev/` dengan StripPrefix middleware.
   - `const API = '/wa-dev'` di dashboard/index.html agar JS API calls ikut subpath.
   - Volume `deploy_wa_dev_store` untuk persistent SQLite session.

2. Bug fix WA_DEV_SERVICE_URL tidak terbaca:
   - Default config `http://localhost:8081` — di Docker, localhost adalah API container sendiri.
   - Fix: tambah `WA_DEV_SERVICE_URL=http://wa-dev-service:8081` ke .env.prod.
   - `docker compose restart` tidak cukup — harus `up -d` agar container di-recreate dengan env baru.

3. Bug fix send_whatsapp_image / send_whatsapp_document salah deteksi base64 vs file path:
   - Kondisi lama: `not image_path_or_base64.startswith("/")` selalu True untuk base64 string.
   - Akibatnya base64 diperlakukan sebagai nama file dan dijalankan sebagai `base64 -w 0 iVBOR...` di sandbox.
   - Fix: helper `_looks_like_base64(s)` — cek panjang ≥ 50 char dan fullmatch `[A-Za-z0-9+/]+=*`.
   - Berlaku untuk send_whatsapp_image dan send_whatsapp_document di agent_runner.py.

4. Bug fix sandbox tidak bisa dipakai di VPS (Docker-in-Docker path mismatch):
   - Root cause: sandbox_data pakai named Docker volume (`deploy_sandbox_data`).
     Saat sandbox code membuat container baru via docker.sock dan mount `/tmp/agent-sandboxes/...`,
     path itu tidak ada di host — hanya ada di dalam API container sebagai volume mount.
   - Fix: ganti ke host bind mount `-v /tmp/agent-sandboxes:/tmp/agent-sandboxes` di compose.
     Sekarang path sama antara API container dan sandbox container yang dibuat Docker socket.
   - Pastikan `mkdir -p /tmp/agent-sandboxes` di VPS host sebelum deploy.

Bug fix QR dikirim ke LID number bukan nomor HP (24 Apr 2026):

Root cause: wa-service memformat webhook `from = "+" + evt.Info.Sender.User`.
Untuk akun @lid, Sender.User adalah LID number (misal 236116347228384), bukan nomor HP.
Python menyimpannya sebagai user_phone di session. Saat agent kirim QR image,
resolveJID mencoba lookup `236116347228384` via IsOnWhatsApp() — gagal karena itu bukan nomor HP —
lalu fallback ke `236116347228384@s.whatsapp.net` yang tidak deliver ke user.

Fix: di channels.py, `effective_reply_target` untuk semua DM sekarang pakai `reply_target`
(= body.chat_id) bukan body.from_. chat_id dari wa-service sudah berisi full JID yang benar
(contoh: `236116347228384@lid`) karena diambil dari evt.Info.Chat.String().
resolveJID menerima JID dengan "@", langsung parse dan kirim ke @lid — deliver benar.

Catatan: sesi existing yang punya user_phone = "+LIDnumber" lama akan otomatis terupdate
ke chat_id yang benar saat user kirim pesan berikutnya (ada logika update session di channels.py).

Migrasi Deep Agents SDK — Phase 2 & 3: Sub-agents + Quality Gate + Memory Summarizer (24 Apr 2026):

Phase 2 — Sub-agent Spawning:

1. `build_subagents()` di agent_runner.py:
   - Jika `agent_ids` kosong → auto-load 5 hardcoded system sub-agents (tidak dari DB).
   - Jika `agent_ids` diisi → load agent dari DB by UUID, construct ChatOpenAI instance per sub-agent.
   - Setiap sub-agent punya isolated sandbox workspace: `{session_id}_sys_{name}/` atau `{session_id}_sub_{agent_id}/`.
   - Invalid UUID di whitelist: log warning dan skip — tidak crash.

2. 5 System Sub-agents (hardcoded di `_SYSTEM_SUBAGENTS`, tidak bergantung DB):
   - `sys_critic`     → quality reviewer, approve/reject output, model: gpt-4o-mini
   - `sys_researcher` → riset via HTTP, model: gpt-4o-mini
   - `sys_coder`      → Python sandbox, tulis + jalankan kode, model: gpt-4o-mini
   - `sys_writer`     → tulis/edit/terjemah konten, model: gpt-4o-mini
   - `sys_analyst`    → analisis data pandas/numpy di sandbox, model: gpt-4o-mini
   Model bisa diubah di `app/core/agent_runner.py` baris ~592–658 field `"model"` per entry.

3. Integrasi ke `run_agent()`:
   - Panggil `build_subagents()` tanpa guard `if agent_ids:` — auto-discover aktif meski agent_ids kosong.
   - Pass `subagents=subagent_list or None` ke `create_deep_agent()`.
   - Sub-sandboxes di-close di finally block (success dan error path).

4. System prompt update: blok `## Available Subagents` di-inject otomatis saat subagent_list tidak kosong.

5. `tools_config.subagents` schema:
   ```json
   { "subagents": { "enabled": true } }                          // auto-load 5 system sub-agents
   { "subagents": { "enabled": true, "agent_ids": ["uuid"] } }  // load dari DB
   ```

6. Bug fix: SDK tidak bisa resolve OpenRouter model string (`openai/gpt-4o-mini`).
   Fix: selalu construct `ChatOpenAI(model=..., base_url="https://openrouter.ai/api/v1")` dan pass instance, bukan string.

7. Loop protection: tidak perlu guard manual — SDK tidak auto-invoke subagent config milik subagent yang dipanggil.
   Backstop: `recursion_limit = agent_max_steps * 2` via LangGraph.

Phase 3 — Quality Gate & Memory Summarizer:

1. Critic (sys_critic) — Opsi B (sub-agent eksplisit):
   - Main agent memanggil sys_critic secara eksplisit setelah executor selesai.
   - Output critic: `VERDICT: APPROVED` atau `VERDICT: REJECTED` + feedback spesifik.
   - Loop protection via recursion_limit; tidak perlu depth guard tambahan.

2. Memory Summarizer:
   - Trigger: `_SUMMARY_TRIGGER = 10` user messages dalam session.
   - Cache di `session.metadata_["context_summary"]` — tidak perlu migrasi DB.
   - Di-inject ke system prompt sebagai `## Conversation Context Summary` block.
   - Re-summarize setiap 10 pesan berikutnya secara kumulatif.
   - `_maybe_summarize_context()` dipanggil di `run_agent()` setelah LLM creation.

Update Postman + UI-DEV (24 Apr 2026):
- Postman: Orchestrator Agent request body pakai `subagents: { "enabled": true }` (tanpa agent_ids),
  instructions diupdate mencantumkan semua 5 system sub-agents dan pola critic review.
- UI-DEV/index.html: hint text diupdate — jelaskan auto-load 5 system sub-agents vs UUID custom.
- UI-DEV/app.js: default tools_config sudah include `subagents: { enabled: false, agent_ids: [] }`.

Fix Scheduler/Reminder (24 Apr 2026):

Bug 1 — Reminder meleset jam (timezone):
Root cause: ISO datetime yang diisi agent selalu dianggap UTC, padahal user minta jam WIB.
Contoh: user minta jam 15:00 WIB → agent nulis "2026-04-24T15:00:00" → dieksekusi 15:00 UTC = 22:00 WIB (7 jam meleset).
Fix:
- scheduler_tool.py: ISO datetime parse sekarang dianggap WIB (UTC+7), dikonversi ke UTC sebelum disimpan.
- scheduler_tool.py: _compute_next_run() untuk cron expression dievaluasi dalam basis waktu WIB.
- scheduler_service.py: croniter di _run_job() juga dievaluasi WIB untuk next_run_at.
- Docstring diupdate: inject "Waktu sekarang (WIB): ..." agar agent pakai waktu lokal sebagai acuan.

Bug 2 — Tidak bisa lebih dari 1 reminder:
Root cause: _set_reminder() cancel reminder lama jika label sama. Agent LLM cenderung pakai label
generik ("reminder") untuk semua reminder → reminder pertama hilang saat reminder kedua dibuat.
Fix: jika label sudah ada dan aktif, otomatis append suffix _2/_3/dst. daripada cancel.
Docstring diupdate: jelaskan ke LLM bahwa label berbeda direkomendasikan, tapi label duplikat
tidak akan menghapus yang lama — akan dibuat sebagai reminder terpisah dengan suffix otomatis.

Typing Indicator WhatsApp (24 Apr 2026):

Tambah efek "sedang mengetik..." di kedua WA service agar user tahu AI sedang memproses.

wa-service/device_manager.go:
- handleIncoming(): kirim ChatPresenceComposing ke chat yang sama sesaat setelah pesan diterima,
  sebelum payload diteruskan ke Python webhook.
- SendMessage(): kirim ChatPresencePaused sebelum mengirim pesan balasan.

wa-dev-service/whatsapp.go:
- handleMessage(): kirim ChatPresenceComposing sebelum onMessage dipanggil.
- SendText(): kirim ChatPresencePaused sebelum mengirim pesan balasan.

Efek: user melihat "sedang mengetik..." sejak pesan diterima sampai AI selesai memproses.
Rebuild binary diperlukan setelah update: make wa-build dan rebuild wa-dev-service.

WhatsApp User Name Recognition + Error Handling (27 Apr 2026):

1. Agent mengenali nama WhatsApp user (push_name):
   - wa-service/device_manager.go: tambah `push_name: evt.Info.PushName` ke webhook payload.
   - wa-dev-service/whatsapp.go: tambah field PushName ke struct IncomingMessage, diisi dari evt.Info.PushName.
   - wa-dev-service/router.go: teruskan push_name ke Python saat forwardToAgent().
   - app/api/channels.py: tambah field push_name di WAIncomingMessage, pass ke run_agent sebagai sender_name.
   - app/core/agent_runner.py: _build_agent_context_block() menerima sender_name dan inject sebagai
     "Current User Name" di Agent Context Block. WhatsApp system prompt juga di-inject hint nama user
     agar agent menyapa user dengan namanya.
   Rebuild Go binary diperlukan: make wa-build + make wa-dev-build.

2. Error handling WhatsApp — pesan generik ke user, detail ke developer:
   - app/core/agent_runner.py: exception di graph.ainvoke() sekarang di-raise (bukan return sebagai final_reply)
     agar channels.py bisa menangkap dan menentukan respons.
   - app/api/channels.py (wa_incoming): saat run_agent raise exception:
     a. Kirim traceback lengkap ke nomor developer 62895619356936 via send_wa_message dari device agent.
     b. Kirim pesan generik ke user: "Maaf, terjadi gangguan sementara. Silakan coba lagi dalam beberapa saat."
     c. Return HTTP 200 (bukan 500) agar Go service tidak retry.

Klarifikasi perilaku sandbox vs virtual FS (27 Apr 2026):

Agent dengan `sandbox: true` menggunakan DockerBackend sebagai filesystem backend deepagents SDK —
artinya tools `write_file`, `read_file`, dll. dari deepagents SDK jalan lewat Docker, bukan in-memory.
Jika Docker mati, semua operasi file gagal meski task-nya hanya "tulis file".

Agent tanpa `sandbox: true` menggunakan StateBackend (in-memory virtual FS) — tidak butuh Docker,
file hanya ada di memory selama session dan tidak bisa dieksekusi sungguhan.

Ini bukan bug — by design. Prerequisite: Docker harus aktif untuk agent yang punya sandbox: true.
Error handling sudah benar: jika Docker mati, traceback dikirim ke developer number dan user
menerima pesan generik.

Bug fix wa-dev-service dashboard QR tidak muncul di localhost (27 Apr 2026):

Root cause: `const API = '/wa-dev'` di dashboard/index.html di-hardcode untuk Traefik production.
Saat akses langsung ke localhost:8081, fetch ke `/wa-dev/connect-wa` menghasilkan 404 plain text,
bukan JSON — JS crash saat `.json()` dengan "Unexpected non-whitespace character after JSON".

Fix: deteksi otomatis berdasarkan pathname:
  const API = window.location.pathname.startsWith('/wa-dev') ? '/wa-dev' : '';
Production (via Traefik /wa-dev/) tetap pakai prefix, localhost jalan tanpa prefix.

Bug fix scheduler concurrent + developer notify via wa-dev (27 Apr 2026):

Bug 1 — "Session is already flushing" saat set 2 reminder sekaligus:
Root cause: _set_reminder() memakai DB session yang di-pass dari agent_runner.
LangGraph menjalankan beberapa tool call secara concurrent (asyncio.gather) —
kedua _set_reminder() memanggil db.flush() pada session yang sama bersamaan → crash.
Fix: _set_reminder() kini membuat session sendiri via AsyncSessionLocal() di dalam fungsi,
isolated dari concurrent calls lain. Session di-commit di dalam async with block sendiri.

Bug 2 — Developer tidak diberitahu saat error via wa-dev:
Root cause: exception di blok send_wa_message(device_id, _DEVELOPER_PHONE, ...) ditelan
diam-diam oleh `except Exception: pass` tanpa logging.
Fix: ganti dengan `log.warning("wa_incoming.developer_notify_failed", error=str(_notify_exc))`
agar error notifikasi developer tampil di log dan bisa di-debug.