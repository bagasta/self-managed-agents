# TODO — Semantic MCP-First Refactor

Tujuan dokumen ini:
- Mengubah routing MCP dari heuristik keyword-heavy menjadi semantic tool choice yang lebih natural untuk Deep Agents / LangChain
- Menjaga agar external service tidak jatuh ke sandbox sebagai fallback palsu
- Menjaga Arthur tetap stabil, tetap pintar, dan tidak terdistraksi oleh perubahan runtime global
- Menjaga semua perubahan tetap aman untuk arsitektur SaaS multi-tenant

## Prinsip Utama

- MCP adalah tool layer, bukan intent classifier.
- Pemilihan tool sebaiknya semantic oleh model, setelah tool surface dibentuk dengan benar.
- Sandbox dipakai untuk file/code execution, bukan untuk meniru side effect layanan eksternal.
- Subagent dipakai untuk context isolation dan specialized delegation, bukan branch default untuk layanan eksternal.
- Arthur adalah special policy class. Jangan samakan policy Arthur dengan agent operasional biasa.
- Project ini adalah SaaS multi-tenant. Semua policy runtime harus menjaga boundary per user, per agent, per session, dan per tenant.

## Scope

- In scope:
  - Runtime agent operasional biasa
  - Google Workspace MCP policy
  - Sandbox vs MCP policy
  - Subagent delegation policy untuk external-service flows
  - Regression protection untuk Arthur
- Out of scope:
  - Rewrite total Arthur builder flow
  - Rewrite total semua preset builder
  - Perubahan UI/dashboard

## SaaS Constraints

- Semua perubahan harus aman untuk model SaaS, bukan agent personal tunggal.
- Tool behavior harus tetap tenant-aware:
  - agent hanya boleh melihat / mengubah resource milik tenant yang benar
  - auth dan token tidak boleh bocor lintas tenant
  - memory tidak boleh tercampur antar user
  - sandbox/subagent workspace tidak boleh menjadi jalur akses data tenant lain
- Arthur harus tetap menjadi entrypoint onboarding/configuration untuk SaaS:
  - open registration tetap aman
  - builder tools tetap scoped ke pemilik agent
  - perubahan runtime global tidak boleh merusak boundary ownership

## Multi-Tenant Guardrails

- Audit semua tool yang menyentuh resource platform:
  - create/update/read agent
  - memory
  - MCP auth helper
  - WhatsApp device assignment
  - scheduler / reminders
- Pastikan setiap tool tetap scoped oleh identifier yang benar:
  - `owner_phone`
  - `external_user_id`
  - `agent_id`
  - `session_id`
- Pastikan semantic MCP-first tidak mengubah boundary auth:
  - token Google tetap per user / per tenant
  - link auth tetap dibangkitkan untuk user yang benar
  - tool availability jangan mengasumsikan semua tenant punya integrasi yang sama
- Pastikan subagent tidak menjadi celah privilege escalation:
  - subagent tidak boleh mendapat tool lebih luas dari kebutuhan
  - subagent tidak boleh mengakses resource tenant lain lewat fallback path

## Phase 1 — Petakan Policy yang Ada

- Audit semua titik yang memakai heuristic intent / keyword untuk routing MCP, terutama Google Workspace.
- Audit semua titik yang membuat sandbox parent, deploy tools, dan subagent list.
- Audit semua prompt/rulebook yang memaksa fallback atau menambah bias ke sandbox.
- Audit semua guard reply override yang bergantung pada heuristic lama.
- Dokumentasikan per file: mana semantic choice, mana hard routing, mana guardrail.

Target file awal:
- `app/core/engine/google_mcp_support.py`
- `app/core/engine/agent_tool_setup.py`
- `app/core/engine/agent_runner.py`
- `app/core/tools/mcp_tool.py`
- `app/core/engine/prompt_builder.py`

## Phase 2 — Pisahkan Policy Class per Jenis Agent

- Tambahkan konsep policy class yang eksplisit di runtime:
  - `builder`
  - `operational`
  - opsional nanti: `coding`, `research`, `support`
- Arthur harus masuk `builder` policy class.
- Agent biasa masuk `operational` policy class.
- Jangan biarkan refactor MCP-first otomatis mengubah tool behavior Arthur.

Definisi awal:
- `builder`:
  - builder tools dominan
  - workflow creation/update/verify tetap rulebook-driven
  - MCP tidak jadi pusat perilaku kecuali memang dibutuhkan eksplisit oleh Arthur
- `operational`:
  - tool choice semantic
  - MCP-first untuk external service
  - sandbox hanya untuk local execution
  - tenant boundary tetap ketat walaupun tool choice lebih fleksibel

## Phase 3 — Ubah Google Workspace jadi Semantic MCP-First

- Kurangi ketergantungan pada `_is_google_mcp_intent(...)` sebagai branch utama runtime.
- Pertahankan heuristic hanya sebagai fallback safety atau hint, bukan source of truth utama.
- Pastikan Google Workspace MCP tools selalu bisa dipilih secara semantic saat tersedia.
- Pastikan jika tool Google Workspace tersedia, sandbox tidak dipakai untuk simulasi kerja Google.
- Pastikan jika auth/error MCP gagal, agent berhenti dengan pesan jujur, bukan fallback ke sandbox atau klaim palsu.
- Pastikan availability Google Workspace MCP dihitung per tenant / per user yang sedang aktif, bukan secara global.

Refactor yang diinginkan:
- Dari:
  - keyword intent -> parent-only hard branch
- Menjadi:
  - tool availability + policy guard -> semantic tool choice -> truthful failure

## Phase 4 — Tundukkan Subagent Bawaan Deep Agents

- Evaluasi pemakaian `task` / default general-purpose subagent dari Deep Agents.
- Untuk flow external service:
  - jangan biarkan `task` menjadi jalur default
  - jangan biarkan general-purpose subagent mengambil alih kerja MCP
- Untuk flow coding/research:
  - subagent tetap boleh dipakai sesuai kebutuhan

Keputusan yang perlu diimplementasikan:
- External service flow:
  - main agent memakai MCP langsung
- Coding/deploy flow:
  - subagent seperti `sys_coder` tetap boleh aktif
- Research/heavy multi-step flow:
  - subagent tetap dipakai untuk context quarantine jika memang berguna

## Phase 5 — Evaluasi Interpreter / Programmatic Tool Calling

- Pelajari apakah workflow Google Workspace tertentu lebih cocok dipindahkan ke interpreter/PTC dibanding prompt biasa.
- Kandidat awal:
  - `create_presentation -> get_presentation -> batch_update_presentation`
  - `create_spreadsheet -> modify_sheet_values -> verify`
  - flow auth helper + retry ringan
- Jangan pakai interpreter untuk semua hal.
- Gunakan interpreter hanya jika urutan tool memang deterministik, bercabang, atau butuh retry/aggregation.

Deliverable fase ini:
- keputusan tertulis: flow mana tetap prompt-based, flow mana pindah ke interpreter/PTC

## Phase 6 — Lindungi Arthur

- Arthur harus tetap builder-first.
- Jangan expose tool berlebih ke Arthur hanya karena runtime global berubah.
- Pastikan builder tools tetap surface utama Arthur:
  - `create_agent`
  - `update_agent`
  - `get_agent_detail`
  - `list_my_agents`
  - `verify_agent`
  - `set_agent_memory`
  - `send_agent_wa_qr`
- Pastikan Arthur tetap tidak drift ke:
  - sandbox coding yang tidak perlu
  - MCP yang tidak relevan
  - HTTP/ngrok/platform API legacy untuk operasi internal
- Pastikan perubahan policy tidak merusak fungsi Arthur sebagai onboarding layer SaaS:
  - registrasi user baru
  - tenant scoping agent ownership
  - assignment WA device
  - auth link generation untuk integrasi user

Kalau perlu:
- tambahkan policy guard khusus Arthur
- atau explicit deny list tool tertentu untuk Arthur

## Phase 7 — Tambah Regression Test

- Tambah regression test untuk semantic MCP-first pada agent operasional.
- Tambah regression test bahwa external service tidak fallback ke sandbox saat MCP tersedia.
- Tambah regression test bahwa auth failure tetap jujur.
- Tambah regression test Arthur tetap builder-first.
- Tambah regression test Arthur tidak mulai drift ke tool yang tidak relevan.

Minimal test coverage:
- Google Slides request -> tool MCP diprioritaskan
- Google MCP unavailable -> reply jujur, tidak pakai sandbox
- Subagent tidak mengambil alih external service flow
- Arthur tetap memakai builder tools internal
- Arthur tidak rusak oleh perubahan runtime umum

## Phase 8 — Rollout Aman

- Kerjakan perubahan bertahap, jangan big bang.
- Urutan implementasi yang disarankan:
  1. dokumentasi policy class
  2. guard untuk Arthur
  3. semantic MCP-first untuk agent operasional
  4. kurangi heuristic branch lama
  5. evaluasi interpreter/PTC
  6. perkuat test
- Setelah tiap fase:
  - jalankan targeted regression
  - cek Arthur tidak berubah perilakunya

## Checklist Eksekusi

- [ ] Petakan seluruh keyword/intent branch MCP yang aktif sekarang
- [ ] Definisikan policy class `builder` vs `operational`
- [ ] Tambahkan guard agar Arthur tidak ikut terpengaruh runtime global
- [ ] Refactor Google Workspace ke semantic MCP-first
- [ ] Pastikan sandbox tidak jadi fallback palsu untuk external services
- [ ] Pastikan subagent tidak mengambil jalur MCP external service
- [ ] Putuskan apakah Slides/Sheets workflow perlu interpreter/PTC
- [ ] Tambah regression test untuk agent operasional
- [ ] Tambah regression test khusus Arthur
- [ ] Verifikasi live behavior setelah deploy

## Success Criteria

- Agent operasional memilih MCP secara semantic saat tool tersedia.
- External service tidak lagi dominan jatuh ke sandbox.
- Failure MCP menghasilkan pesan jujur dan deterministik.
- Subagent hanya dipakai saat memang tepat, bukan default branch untuk external services.
- Arthur tetap builder-first dan kualitasnya tidak turun.
- Boundary multi-tenant tetap aman setelah refactor.
- Tidak ada regresi ownership, auth scoping, atau memory isolation antar tenant.
