# Plan Perbaikan Arthur, Owner, dan Runtime Context Injection

Tanggal: 2026-05-29

## Ringkasan Masalah

Platform ini sedang membangun SaaS agent builder, bukan chatbot biasa. Karena itu, hal-hal yang menentukan identitas dan batas kerja agent tidak boleh hanya ditulis oleh Arthur ke `instructions` atau `soul`. Prompt buatan Arthur tetap berguna untuk persona, SOP bisnis, dan gaya bicara, tetapi tidak boleh menjadi satu-satunya sumber kebenaran untuk:

- siapa Owner agent;
- apakah Owner adalah superadmin;
- apakah agent dibuat oleh Arthur;
- tools apa saja yang benar-benar aktif;
- integrasi mana yang tersambung atau belum tersambung;
- kapan agent harus minta Owner memberi akses atau keputusan;
- apa yang boleh/tidak boleh diklaim agent kepada user.

Jika kontrak-kontrak ini hanya bergantung pada LLM yang menulis system message, risiko halusinasi tetap tinggi. Agent bisa mengaku punya akses Google padahal belum auth, mengaku bisa memakai tool yang tidak aktif, lupa siapa Owner, atau tidak tahu kapan harus eskalasi ke bosnya. Untuk launch SaaS, ini belum cukup aman.

## Status Saat Ini

Yang sudah ada:

- `Agent.owner_external_id` sudah menjadi field canonical untuk Owner agent.
- `Agent.operator_ids` masih dipakai sebagai kompatibilitas legacy dan daftar operator/admin.
- Arthur punya builder tools internal: `create_agent`, `update_agent`, `delete_agent`, `get_agent_detail`, `list_my_agents`, `set_agent_memory`, `generate_google_auth_link`, dan tools pendukung lain.
- `create_agent` mengikat agent ke `owner_phone` dan memasukkan owner ke `operator_ids`.
- `build_agent_context_block()` sudah meng-inject Platform Context ke system prompt runtime.
- `tool_builder` dan `agent_tool_setup` sudah menentukan tools runtime dari `tools_config`.
- Google MCP runtime punya mekanisme auth link dan fallback ke owner untuk auth pada beberapa path.
- Arthur rulebook sudah mengarahkan bahasa non-teknis untuk Google dan WhatsApp trial.

Gap utama:

- Platform context belum cukup menjadi "immutable contract" yang selalu menang atas prompt buatan Arthur.
- Tools aktif belum dijelaskan dalam bahasa operasional yang stabil dan tidak bergantung pada LLM penulis prompt.
- Status integrasi, terutama Google Workspace, belum selalu di-inject sebagai state eksplisit: tersambung, belum tersambung, auth expired, link auth tersedia/tidak.
- `soul` dan `instructions` masih terlalu berat sebagai sumber identitas permanen.
- SOP/workflow kerja agent masih terlalu sering digabung ke `instructions`, sehingga tidak menjadi dokumen operasi yang bisa dibaca, diaudit, diverifikasi, dan diwajibkan runtime saat agent menjalankan pekerjaan.
- Metadata "dibuat oleh Arthur" belum menjadi field/data source yang jelas. Saat ini lebih banyak hidup di prompt/memory.
- Belum ada launch gate yang memverifikasi agent baru sadar Owner, sadar tool aktual, dan tidak mengklaim akses integrasi yang belum ready.

## Prinsip Perbaikan

1. Runtime harus menjadi sumber kebenaran.
   - Owner, role current user, tools aktif, integrasi, quota, channel, dan escalation harus diambil dari DB/runtime, lalu di-inject oleh platform.

2. Arthur hanya menulis bagian bisnis.
   - Arthur boleh menulis persona, SOP, contoh percakapan, data yang dikumpulkan, dan knowledge bisnis.
   - Arthur tidak boleh menjadi satu-satunya sumber untuk identitas platform, tools aktif, atau auth status.

3. SOP harus menjadi artifact operasi, bukan sekadar prompt.
   - SOP agent disimpan sebagai `Agent Operating Manual` terpisah dari `instructions`.
   - `instructions` mengatur persona, gaya bicara, dan batas komunikasi.
   - SOP mengatur cara kerja: trigger, data wajib, langkah, decision point, tool yang dipakai, eskalasi, output, dan status review.
   - Runtime wajib bisa membuat agent membaca SOP yang relevan sebelum menjalankan workflow penting.

4. Prompt buatan LLM harus bisa salah tanpa merusak safety.
   - Jika Arthur lupa menulis Owner, runtime tetap inject Owner.
   - Jika Arthur menulis Google bisa dipakai, runtime tetap inject status auth sebenarnya.
   - Jika Arthur menulis tool tertentu ada, runtime tetap expose hanya tools yang benar-benar aktif.
   - Jika Arthur menulis SOP kurang lengkap, runtime tetap memberi status `draft`/`needs_review` dan membatasi keberanian agent.

5. Agent harus diperlakukan seperti staff.
   - Agent punya Owner sebagai bos/superadmin.
   - Agent tahu dia dibuat/dikonfigurasi lewat Arthur.
   - Agent tahu kapan harus minta keputusan, izin, data, atau auth dari Owner/operator.
   - Agent punya manual kerja yang wajib dibaca seperti staff baru membaca SOP perusahaan.

6. User-facing language harus non-teknis.
   - Runtime boleh tahu `tools_config`, MCP, token, server, auth URL.
   - Agent ke Owner/customer harus bicara sederhana: "akses Google belum terhubung", "Owner perlu buka link ini", "saya teruskan ke admin".

## Target Arsitektur

### 1. Runtime Platform Contract

Buat satu builder fungsi/data object yang menjadi kontrak runtime, misalnya:

```text
PlatformRuntimeContract
|-- agent_identity
|   |-- agent_id
|   |-- agent_name
|   |-- created_by: Arthur / platform / manual
|   |-- created_by_agent_id
|
|-- ownership
|   |-- owner_external_id
|   |-- owner_display_name
|   |-- owner_role: superadmin
|   |-- operator_ids
|   |-- current_user_role: owner / operator / customer / unknown
|
|-- capabilities
|   |-- active_tools
|   |-- active_integrations
|   |-- unavailable_tools
|   |-- channel_capabilities
|
|-- integration_state
|   |-- google_workspace: enabled / connected / needs_auth / auth_expired / unavailable
|   |-- auth_link_available
|   |-- connected_external_user_id
|
|-- escalation_policy
|   |-- owner_is_primary_superadmin
|   |-- operator_phone
|   |-- escalation_tool_available
|   |-- when_to_escalate
|
|-- operating_manual
|   |-- manual_id
|   |-- manual_version
|   |-- sop_maturity: draft / usable / verified / needs_review
|   |-- required_review: true / false
|   |-- workflow_count
|   |-- unavailable_or_incomplete_workflows
```

Kontrak ini dibangun dari DB dan runtime state, bukan dari generated prompt.

### 2. Mandatory System Prompt Blocks

`build_system_prompt()` harus menyusun prompt dengan urutan kira-kira:

```text
1. Platform Runtime Contract
2. Ownership and Role Rules
3. Actual Tool and Integration Capabilities
4. Auth and External Access State
5. Agent Operating Manual summary and SOP usage rule
6. Agent business instructions from Arthur
7. Soul/memory/RAG/context
8. Channel-specific rules
9. Safety/reply guard hints
```

Blok 1 sampai 4 wajib berasal dari platform dan tidak boleh ditimpa oleh `instructions`/`soul`.
Blok 5 juga berasal dari artifact SOP/runtime, bukan dari narasi bebas `instructions`.

### 3. Owner/Superadmin Injection

Setiap run harus meng-inject aturan seperti:

- Owner agent ini adalah `<owner_external_id>`.
- Owner adalah bos dan superadmin agent ini.
- Jika current user adalah Owner/operator, instruksinya diprioritaskan selama tidak melanggar safety.
- Jika current user adalah customer, jangan bocorkan data internal Owner.
- Jika agent tidak tahu sesuatu, butuh keputusan bisnis, butuh approval, atau ada masalah yang tidak bisa diselesaikan, agent harus minta Owner/operator membantu.

Ini harus muncul dari runtime walaupun Arthur tidak menulisnya.

### 4. Created-by-Arthur Awareness

Jangan hanya tulis "dibuat Arthur" di prompt hasil compose. Tambahkan metadata agent:

- `created_by_agent_id`
- `created_by_agent_name`
- `created_by_type`: `arthur_builder`, `dashboard`, `api`, atau `system`

Jika belum mau migration besar, tahap awal bisa inject dari `tools_config` atau memory `platform_identity`, tetapi target launch sebaiknya field eksplisit di DB.

Runtime prompt lalu menyebut:

- "Kamu dibuat/dikonfigurasi oleh Arthur, Agent Builder platform ini."
- "Untuk perubahan konfigurasi besar, arahkan Owner bicara ke Arthur."

### 5. Actual Tool Capability Injection

Tools aktif harus diambil dari tool registry aktual setelah `tools_config` diproses, bukan dari teks instructions.

Contoh blok runtime:

```text
TOOLS AKTIF SAAT INI
- Memory: aktif. Kamu bisa mengingat preferensi dan konteks user.
- Escalation: aktif. Kamu bisa meneruskan kasus ke Owner/operator.
- Google Workspace: aktif tetapi belum terhubung. Jangan klaim bisa membaca/membuat Google Docs sampai Owner login.
- WhatsApp Media: tidak aktif. Jangan janji mengirim file/gambar langsung lewat WhatsApp.
- Sandbox: tidak aktif. Jangan klaim bisa menjalankan kode atau membaca file Excel secara langsung.
```

Format internal boleh structured, tetapi bahasa ke agent harus operasional. Jangan biarkan agent menyimpulkan sendiri dari `tools_config` mentah.

### 6. Google Auth State Injection

Google adalah area rawan halusinasi. Runtime harus inject status eksplisit:

- Google tidak diaktifkan: jangan bilang bisa akses Google.
- Google diaktifkan tapi belum auth: minta Owner buka link auth.
- Google auth expired: minta Owner hubungkan ulang.
- Google connected: gunakan MCP Google sebagai sumber kebenaran.
- Auth link gagal dibuat: bilang Owner perlu coba lagi atau hubungi admin platform, jangan mengarang link.

Saat Google belum auth, agent harus bicara seperti:

```text
Saya belum bisa akses Google milik Owner karena belum dihubungkan. Owner perlu buka link login Google dari Arthur/platform dulu, setelah itu saya bisa lanjut.
```

Bukan:

```text
Saya akan cek Google Drive sekarang.
```

### 7. Arthur Builder Runtime Awareness

Arthur sendiri harus mendapat platform contract yang lebih kuat:

- Arthur tahu dia control-plane builder, bukan agent customer biasa.
- Arthur tahu user yang chat adalah Owner platform.
- Arthur tahu Owner sudah punya paket/subscription.
- Arthur tahu CRUD agent adalah tugas utamanya.
- Arthur tahu operasi platform harus lewat builder tools internal, bukan HTTP/ngrok.
- Arthur tahu setelah create/update dia harus lanjut setup yang bisa dilakukan sendiri: Google auth link, WA trial, QR/scan, verification.

Ini harus berada di `Arthur Builder Mode` runtime injection, bukan hanya `system-message-builder.md`.

### 8. Agent Creation Flow Baru

Flow ideal:

```text
Owner request
|
Arthur reads PlatformRuntimeContract
|
Arthur plan business workflow
|
Arthur selects SOP template or generic SOP builder
|
Arthur composes Agent Operating Manual and business instructions separately
|
validate_agent_config checks tool and workflow safety
|
create_agent stores:
  - business instructions
  - agent operating manual / SOP artifact
  - owner_external_id
  - created_by metadata
  - tools_config
  - channel_type
  - escalation_config
|
runtime always injects:
  - Owner/superadmin
  - created by Arthur
  - actual tools
  - integration status
  - SOP maturity and required SOP usage rule
  - auth/escalation rules
```

### 9. Update Flow untuk Agent Lama

Agent lama yang belum punya kontrak baru harus diperbaiki tanpa recreate:

1. `list_my_agents`
2. `get_agent_detail(include_instructions=true)`
3. Detect missing runtime metadata:
   - no `owner_external_id`
   - no created_by metadata
   - missing escalation/operator config
   - tools_config inconsistent with instructions
4. `update_agent` untuk patch config/instructions bisnis jika perlu.
5. Backfill metadata/memory/platform identity.
6. Verify dengan `get_agent_detail`.

Targetnya agent lama juga launch-safe.

### 10. Agent Operating Manual dan SOP Workflow

Arthur harus menghasilkan manual kerja terpisah untuk agent, bukan menyembunyikan semua workflow di `instructions`.

Artifact yang disimpan:

```text
AgentOperatingManual
|-- agent_id
|-- version
|-- source: arthur_template / arthur_generic / owner_provided / manual_reviewed
|-- domain
|-- domain_confidence: low / medium / high
|-- maturity: draft / usable / verified / needs_review
|-- owner_review_required: true / false
|-- missing_context
|-- assumptions
|-- workflows[]
    |-- workflow_id
    |-- name
    |-- trigger
    |-- goal
    |-- required_inputs
    |-- steps
    |-- decision_points
    |-- allowed_tools
    |-- escalation_rules
    |-- prohibited_actions
    |-- final_output
    |-- examples
```

`instructions` tetap dipakai untuk persona, gaya bicara, channel tone, dan batasan komunikasi. SOP dipakai untuk cara kerja operasional.

#### Interview Owner dan Konteks Tidak Lengkap

Arthur tidak boleh bergantung pada Owner yang selalu memberi konteks lengkap. Interview harus punya dua lapis:

1. Pertanyaan minimum wajib:
   - bidang bisnis;
   - tugas utama agent;
   - tipe permintaan customer paling sering;
   - data yang wajib dikumpulkan;
   - aturan harga/stok/jadwal/pembayaran bila relevan;
   - kapan agent harus eskalasi ke Owner/operator;
   - hal yang agent tidak boleh lakukan.
2. Jika Owner tetap tidak menjawab lengkap, Arthur membuat SOP `draft` yang aman:
   - agent boleh menyapa, menggali kebutuhan, mengumpulkan data, dan membuat ringkasan;
   - agent tidak boleh mengarang harga, stok, kebijakan refund, keputusan legal/medis/finansial, atau janji yang belum diberi Owner;
   - workflow penting diberi `owner_review_required=true`;
   - Arthur harus menjelaskan ke Owner dengan bahasa sederhana bahwa agent sudah dibuat dengan SOP dasar dan masih butuh review untuk keputusan bisnis.

#### Template SOP Per Bidang

Arthur perlu punya registry template SOP per bidang. Contoh awal:

- F&B: terima order, cek menu, pembayaran, delivery/pickup, komplain.
- Travel: inquiry trip, itinerary, booking, pembayaran, perubahan jadwal.
- Ecommerce: katalog, rekomendasi produk, checkout, refund, follow-up.
- Jasa lokal: booking, survey, quotation, invoice, revisi.
- Klinik/wellness: appointment, batasan konsultasi, reminder, eskalasi ke staff.
- Pendidikan/kursus: inquiry kelas, placement, jadwal, pembayaran, reminder.
- Properti: inquiry unit, kualifikasi lead, jadwal viewing, follow-up.

Setiap template harus berupa struktur workflow, bukan prompt panjang. Template boleh punya placeholder yang diisi dari hasil interview Owner.

#### Bidang Tidak Ada Template

Jika bidang tidak ada di registry, Arthur tidak boleh berhenti dan tidak boleh mengarang seolah template domain sudah tersedia. Gunakan `Generic SOP Builder`:

```text
1. Identifikasi tujuan workflow.
2. Tentukan trigger.
3. Tentukan data/input wajib.
4. Tentukan langkah kerja berurutan.
5. Tentukan decision point.
6. Tentukan tool yang boleh dipakai.
7. Tentukan batasan risiko.
8. Tentukan aturan eskalasi.
9. Tentukan output akhir.
10. Tandai asumsi dan konteks yang belum terkonfirmasi.
```

Manual hasil generic builder harus diberi:

- `source=arthur_generic`
- `domain_confidence=low|medium`
- `maturity=draft|needs_review`
- `owner_review_required=true`

#### Runtime SOP Enforcement

Runtime harus punya tool/resource terpisah untuk SOP, misalnya:

- `read_agent_sop(workflow_id=None)`
- `list_agent_workflows()`
- `get_workflow_steps(workflow_id)`
- `check_sop_before_action(workflow_id, intended_action)`

Aturan enforcement:

- Untuk workflow penting, agent wajib membaca SOP relevan sebelum menjalankan tool/action eksternal.
- Jika SOP `draft` atau `needs_review`, agent harus menahan keputusan final dan eskalasi ke Owner/operator untuk area yang belum jelas.
- Jika workflow tidak ditemukan, agent memakai intake-safe behavior: tanya kebutuhan, kumpulkan data, ringkas, lalu eskalasi.
- Jika SOP melarang aksi tertentu, reply guard harus mencegah agent mengklaim aksi itu bisa dilakukan.
- SOP usage harus masuk log agar bisa diaudit: workflow yang dibaca, action yang dilakukan, dan apakah ada escalation.

## Rencana Implementasi Bertahap

### Phase 1: Contract Runtime Injection

File utama:

- `app/core/engine/prompt_builder.py`
- `app/core/engine/agent_tool_setup.py`
- `app/core/engine/tool_builder.py`
- `app/core/engine/google_mcp_support.py`

Task:

- Buat helper `build_platform_runtime_contract(agent_model, session, tools, integration_runtime)`.
- Inject Owner/superadmin/current role ke prompt setiap run.
- Inject actual active tool list dengan deskripsi operasional.
- Inject unavailable/disabled capability yang sering di-halu-kan: Google, WhatsApp media, sandbox, deploy, RAG, scheduler.
- Tambah tests untuk memastikan generated instructions tidak bisa menghapus blok platform contract.

Acceptance:

- Agent tanpa soul tetap tahu Owner.
- Agent dengan instructions salah tetap tidak mengklaim tool yang disabled.
- Customer tidak dianggap Owner.
- Owner/operator dikenali sebagai superadmin/operator.

### Phase 2: Integration State and Auth Guard

File utama:

- `app/core/engine/google_mcp_support.py`
- `app/core/engine/agent_runner.py`
- `app/core/tools/builder_tools.py`

Task:

- Runtime Google state menjadi structured: `disabled`, `enabled_needs_auth`, `connected`, `auth_expired`, `error`.
- Inject Google state ke prompt.
- Jika Google tool gagal auth, reply guard harus mengubah hasil menjadi pesan jujur ke Owner.
- Arthur setelah create/update Google wajib generate auth link jika mungkin.
- Jangan pernah klaim Google action sukses tanpa MCP step sukses.

Acceptance:

- Request Google tanpa auth menghasilkan permintaan login ke Owner.
- Request Google dengan auth expired menghasilkan re-auth, bukan halu.
- Request Google dengan connected MCP memakai MCP tool sebagai sumber kebenaran.

### Phase 3: Created-by Metadata

File utama:

- `app/models/agent.py`
- Alembic migration baru
- `app/core/tools/builder_tools.py`
- `app/api/agents.py`

Task:

- Tambah field:
  - `created_by_type`
  - `created_by_agent_id`
  - `created_by_agent_name`
- `create_agent` dari Arthur mengisi `arthur_builder`.
- Agent manual/dashboard/API mengisi source berbeda.
- Runtime prompt inject source ini.
- Backfill existing Arthur-created agents bila bisa infer dari memory/log/operator scope.

Acceptance:

- Agent baru tahu dibuat oleh Arthur dari DB metadata.
- Tidak perlu mengandalkan `soul` untuk created-by awareness.

### Phase 4: Tool Truth Registry

File utama:

- `app/core/engine/tool_builder.py`
- `app/core/config_schema.py`
- `app/core/tools/*`

Task:

- Buat registry capabilities:
  - internal name
  - human-facing capability
  - enabled condition
  - disabled reason
  - required auth/config
  - user-facing fallback sentence
- Prompt builder memakai registry ini untuk inject capabilities.
- Reply guard memakai registry ini untuk mencegah klaim tool disabled.

Acceptance:

- Jika `whatsapp_media=false`, agent tidak janji kirim file WA.
- Jika `sandbox=false`, agent tidak janji menjalankan kode.
- Jika `rag=true` tapi belum ada dokumen, agent tahu perlu dokumen.

### Phase 5: Agent Operating Manual dan SOP Runtime

File utama:

- `app/models/agent.py`
- Alembic migration baru atau JSON storage sementara di `Agent.tools_config`/memory versioned
- `app/core/tools/builder_tools.py`
- `app/core/domain/skill_service.py` atau service baru `agent_sop_service.py`
- `app/core/engine/prompt_builder.py`
- `app/core/engine/agent_runner.py`
- `app/core/engine/reply_guard.py`

Task:

- Definisikan schema `AgentOperatingManual` dan `AgentWorkflowSOP`.
- Buat registry template SOP per bidang dengan minimal 6-8 domain awal.
- Buat `Generic SOP Builder` untuk bidang yang belum punya template.
- Update Arthur Builder Mode agar interview Owner memakai pertanyaan minimum wajib.
- Update `create_agent` agar menyimpan `instructions` dan SOP artifact secara terpisah.
- Update `update_agent` agar perubahan bisnis/SOP membuat manual version baru.
- Tambah runtime tools/resource: `read_agent_sop`, `list_agent_workflows`, `get_workflow_steps`, dan `check_sop_before_action`.
- Inject SOP summary, maturity, dan aturan "baca SOP sebelum workflow penting" ke prompt runtime.
- Tambah guard agar workflow dengan SOP `draft`/`needs_review` tidak melakukan keputusan final tanpa Owner/operator.
- Tambah log structured untuk SOP usage dan blocked action.

Acceptance:

- Agent baru dari Arthur punya SOP artifact walaupun `instructions` tidak berisi SOP lengkap.
- Jika Owner memberi konteks minim, Arthur membuat SOP `draft` dengan batas aman dan tidak bilang agent launch-ready penuh.
- Jika domain tidak ada template, Arthur memakai generic builder dan menandai `owner_review_required=true`.
- Agent yang menjalankan order/refund/booking/payment workflow membaca SOP relevan lebih dulu.
- Agent tidak mengarang policy/harga/stok jika SOP belum memuat data itu.
- Reply guard bisa memblokir klaim yang bertentangan dengan SOP prohibited actions.

### Phase 6: Arthur Launch Readiness Checks

File utama:

- `app/core/tools/builder_tools.py`
- tests builder/e2e
- optional admin script

Task:

- `verify_agent` harus mengecek:
  - owner present
  - created_by present
  - operating manual present
  - SOP maturity acceptable for intended workflows
  - tools_config matches instructions claims
  - Google auth state if Google enabled
  - escalation_config for workflows that need human approval
  - channel setup for WhatsApp
- Buat command/script untuk audit semua agent existing.
- Arthur final reply tidak boleh bilang launch-ready jika verification gagal.

Acceptance:

- Agent payment/admin approval tanpa escalation gagal verification.
- Agent Google tanpa auth tidak dianggap ready untuk Google action.
- Agent WhatsApp tanpa WA device diarahkan ke onboarding.
- Agent tanpa SOP artifact gagal full launch readiness.
- Agent dengan SOP `draft` hanya lolos sebagai intake-safe, bukan full workflow-ready.

### Phase 7: Regression Test Suite

Minimal test baru:

- `test_runtime_injects_owner_superadmin_even_without_soul`
- `test_runtime_injects_created_by_arthur_from_metadata`
- `test_runtime_tool_contract_lists_only_actual_tools`
- `test_google_enabled_without_auth_asks_owner_for_auth`
- `test_disabled_whatsapp_media_prevents_file_delivery_claim`
- `test_customer_session_does_not_become_owner`
- `test_owner_session_gets_superadmin_role`
- `test_arthur_builder_mode_knows_crud_is_primary_job`
- `test_generated_prompt_cannot_override_platform_contract`
- `test_arthur_creates_sop_artifact_separate_from_instructions`
- `test_missing_business_context_creates_draft_intake_safe_sop`
- `test_unknown_domain_uses_generic_sop_builder_with_owner_review`
- `test_agent_reads_sop_before_critical_workflow_action`
- `test_verify_agent_blocks_full_launch_without_operating_manual`

## Launch Gate

Platform belum layak launch sebelum gate ini hijau:

1. Semua agent runtime mendapat Owner/superadmin context dari platform.
2. Semua agent runtime mendapat actual tools/capabilities dari registry.
3. Google auth state di-inject dan diuji.
4. Arthur CRUD flow create/update/delete/list diverifikasi end-to-end.
5. Agent baru punya Agent Operating Manual/SOP artifact yang terpisah dari `instructions`.
6. Runtime bisa membatasi agent berdasarkan maturity SOP: `draft`, `usable`, `verified`, atau `needs_review`.
7. Agent baru tidak bergantung pada soul untuk tahu Owner dan created-by.
8. Reply guard mencegah fake success untuk tool/integrasi/SOP yang gagal atau dilarang.
9. Audit existing agents bisa menandai agent yang belum launch-safe.

## Prioritas Eksekusi

Urutan paling pragmatis:

1. Phase 1: runtime contract injection.
2. Phase 2: Google auth state injection.
3. Phase 4: tool truth registry.
4. Phase 5: Agent Operating Manual dan SOP runtime.
5. Phase 6: verify/audit agent readiness.
6. Phase 3: DB metadata migration untuk created-by.
7. Phase 7: perluas regression suite.

Alasan Phase 3 tidak harus pertama: Owner sudah ada di DB lewat `owner_external_id`, jadi launch risk terbesar bisa dikurangi dulu lewat runtime injection. Metadata created-by tetap penting, tapi bisa menyusul setelah kontrak runtime stabil.

## Todo List

### P0 - Wajib Sebelum Launch

- [x] Buat `PlatformRuntimeContract` sebagai object/helper runtime untuk Owner, current user role, created-by metadata, dan tools aktif. Catatan: Google integration state masih di-inject lewat `GoogleMcpRuntime`.
- [x] Inject Owner/superadmin/current user role dari `PlatformRuntimeContract` ke system prompt setiap run.
- [x] Inject actual active tools dan disabled tools dengan bahasa operasional, bukan raw `tools_config`.
- [x] Inject Google Workspace state: disabled, enabled-needs-auth, connected, auth-expired/error, atau unknown-auth.
- [x] Pastikan Google action tanpa auth mendapat runtime instruction untuk meminta Owner login/re-auth dan tidak mengarang hasil.
- [x] Pastikan customer biasa tidak pernah diklasifikasikan sebagai Owner/superadmin.
- [x] Perkuat Arthur Builder Mode di runtime agar Arthur sadar CRUD agent adalah tugas utama control-plane.
- [x] Update `verify_agent` agar mengecek owner, platform identity/created-by awareness awal, Google auth, escalation workflow, dan channel readiness.
- [x] Tambah reply guard registry-wide untuk mencegah fake success ketika tool/integrasi disabled. Progress: high-risk capability claims sekarang dicek dari tool truth registry; Google auth failure guard tetap memakai guard khusus Google.
- [x] Buat regression tests P0 inti untuk runtime Owner/tools/Google auth/verify readiness dan jadikan bagian dari focused local validation.

### P1 - Penting Setelah Kontrak Runtime Stabil

- [x] Tambah DB metadata `created_by_type`, `created_by_agent_id`, dan `created_by_agent_name`.
- [x] Buat Alembic migration untuk metadata created-by.
- [x] Isi metadata created-by saat Arthur memanggil `create_agent`.
- [x] Backfill metadata agent lama yang bisa diidentifikasi high-confidence. Hasil DB saat ini: Arthur dibackfill sebagai `system`; 11 agent lain tetap manual review karena tidak ada bukti reliable.
- [x] Buat tool truth registry yang memetakan tools internal ke capability user-facing, enabled condition, disabled reason, fallback sentence, dan claim patterns.
- [x] Ubah prompt builder agar memakai tool truth registry, bukan daftar tool ad hoc.
- [x] Ubah `get_agent_detail` dan `list_my_agents` agar menampilkan readiness/metadata penting untuk Arthur.
- [x] Buat audit script untuk menandai agent existing yang belum launch-safe dari sisi `created_by_*`: `scripts/audit_agent_created_by_metadata.py`.
- [ ] Definisikan schema `AgentOperatingManual` dan `AgentWorkflowSOP`.
- [ ] Buat registry template SOP per bidang untuk domain awal: F&B, travel, ecommerce, jasa lokal, klinik/wellness, pendidikan/kursus, dan properti.
- [ ] Buat `Generic SOP Builder` untuk domain tanpa template.
- [ ] Update Arthur interview flow agar mengejar pertanyaan minimum wajib dan membuat SOP `draft` jika konteks Owner minim.
- [ ] Simpan SOP artifact terpisah dari `instructions` saat `create_agent` dan `update_agent`.
- [ ] Tambah runtime SOP tools/resource: `read_agent_sop`, `list_agent_workflows`, `get_workflow_steps`, dan `check_sop_before_action`.
- [ ] Inject SOP summary, maturity, dan aturan penggunaan SOP ke runtime prompt.
- [ ] Tambah guard untuk menahan workflow penting jika SOP masih `draft`/`needs_review`.

### P2 - Hardening dan Operasional

- [ ] Tambah dashboard/API endpoint internal untuk melihat Agent Launch Readiness.
- [ ] Tambah log structured untuk setiap runtime contract yang di-inject.
- [ ] Tambah log structured untuk SOP usage: workflow dibaca, action yang dijalankan, blocked action, dan escalation.
- [ ] Tambah metric jumlah run yang blocked karena missing auth, disabled tool, atau missing owner.
- [ ] Tambah metric jumlah run yang blocked karena SOP missing/draft/needs_review.
- [ ] Tambah smoke test end-to-end Arthur create -> verify -> WA trial -> Google auth.
- [ ] Tambah dokumentasi operator tentang cara memperbaiki agent yang gagal readiness audit.
- [ ] Tambah seed/backfill script untuk menyegarkan Arthur setelah rulebook/runtime contract berubah.

### Regression Tests Wajib

- [x] `test_runtime_injects_owner_superadmin_even_without_soul`
- [x] `test_runtime_injects_created_by_arthur_from_metadata`
- [x] `test_runtime_tool_contract_lists_only_actual_tools`
- [x] `test_runtime_tool_contract_lists_disabled_tools_with_reason`
- [x] `test_google_enabled_without_auth_asks_owner_for_auth`
- [x] `test_google_auth_expired_asks_owner_to_reconnect`
- [ ] `test_google_connected_requires_real_mcp_success_before_claiming_done`
- [x] `test_disabled_whatsapp_media_prevents_file_delivery_claim`
- [x] `test_disabled_sandbox_prevents_code_execution_claim`
- [x] `test_disabled_google_workspace_claim_is_rewritten`
- [x] `test_rag_enabled_without_documents_asks_for_documents`
- [x] `test_customer_session_does_not_become_owner`
- [x] `test_owner_session_gets_superadmin_role`
- [x] `test_arthur_builder_mode_knows_crud_is_primary_job`
- [x] `test_generated_prompt_cannot_override_platform_contract`
- [x] `test_verify_agent_blocks_launch_without_owner`
- [x] `test_verify_agent_blocks_google_agent_without_auth`
- [x] `test_verify_agent_blocks_payment_workflow_without_escalation`
- [x] `test_audit_existing_agents_flags_missing_created_by_metadata`
- [ ] `test_arthur_creates_sop_artifact_separate_from_instructions`
- [ ] `test_missing_business_context_creates_draft_intake_safe_sop`
- [ ] `test_unknown_domain_uses_generic_sop_builder_with_owner_review`
- [ ] `test_agent_reads_sop_before_critical_workflow_action`
- [ ] `test_sop_draft_blocks_final_refund_or_payment_decision`
- [ ] `test_verify_agent_blocks_full_launch_without_operating_manual`

### Definition of Done

- [x] Agent baru tetap tahu Owner dan tools aktual walaupun `soul` kosong.
- [x] Agent lama/runtime lama mendapat runtime contract saat run tanpa harus recreate.
- [x] Agent mendapat Google auth state eksplisit dan tidak diarahkan mengklaim akses Google sebelum auth valid.
- [x] Agent tidak bisa mengklaim tool disabled sebagai tersedia.
- [x] Arthur bisa menjelaskan status setup ke Owner dengan bahasa non-teknis.
- [x] Focused P0 regression tests yang sudah diimplementasikan hijau.
- [x] Audit existing agents menghasilkan daftar agent aman dan agent yang perlu diperbaiki.
- [ ] Arthur membuat Agent Operating Manual/SOP artifact terpisah dari `instructions`.
- [ ] Agent memakai SOP artifact sebagai sumber cara kerja operasional, bukan hanya narasi di prompt.
- [ ] Agent dengan konteks bisnis minim berjalan dalam mode intake-safe sampai Owner melengkapi/review SOP.
- [ ] Domain tanpa template memakai generic SOP builder dan ditandai butuh review Owner.
- [ ] Readiness audit membedakan full workflow-ready dari intake-safe/draft.

## Keputusan Arsitektur

Keputusan final yang disarankan:

- Jangan jadikan `instructions` dan `soul` sebagai sumber kebenaran platform.
- Jadikan `instructions` dan `soul` sebagai layer bisnis/persona saja.
- Semua fakta platform masuk dari runtime-injected contract.
- Semua klaim kemampuan harus berdasarkan registry tools aktual.
- Semua klaim integrasi harus berdasarkan state auth aktual.
- Semua workflow penting harus berdasarkan Agent Operating Manual/SOP artifact, bukan hanya `instructions`.
- SOP dengan maturity `draft` atau `needs_review` harus membatasi agent ke intake, klarifikasi, ringkasan, dan eskalasi.
- Owner adalah superadmin agent dan harus muncul dari runtime setiap run.
- Arthur adalah builder/control-plane interface dan harus mendapat runtime mode khusus setiap run.

## Plan Tambahan: Versioned Selective Memory Refresh

Masalah lanjutan:

- Saat Arthur mengupdate agent, `instructions` dan `tools_config` baru langsung aktif, tetapi memory lama seperti `soul` atau `agent_blueprint` masih bisa ikut masuk prompt.
- Jika memory lama bertentangan dengan konfigurasi baru, agent bisa bias ke konteks lama walaupun update sudah sukses.
- Wipe total memory terlalu berisiko karena bisa menghapus detail yang masih berguna dan membuat debugging sulit.

Prinsip refresh yang aman:

- Gunakan **versioned selective refresh**, bukan wipe total.
- Setiap update besar membuat versi konteks aktif baru, misalnya `agent_context_version=3`, `soul:v3`, `agent_blueprint:v3`, dan `setup_summary:v3`.
- Runtime hanya memprioritaskan memory versi aktif untuk identitas/workflow agent.
- Memory lama tetap disimpan sebagai arsip/debug, tetapi tidak otomatis mengalahkan konteks aktif.
- `instructions` terbaru dan runtime contract selalu menang jika konflik dengan memory lama.
- Update kecil boleh memakai `refresh_memory_mode="none"` agar memory tidak berubah.
- Update workflow/persona memakai `refresh_memory_mode="selective"` sebagai default Arthur.
- Update total bisnis/SOP memakai `refresh_memory_mode="major"` agar versi konteks baru ditulis eksplisit.

Rencana implementasi:

1. Tambah helper memory untuk membaca `agent_context_version` dan memilih `soul:v{version}` jika tersedia; fallback ke `soul` legacy jika versi aktif belum ada.
2. Di `update_agent`, tambah parameter `refresh_memory_mode = "none" | "selective" | "major"` dengan default `selective`.
3. Jika update menyentuh `instructions`, `description`, `tools_config`, `escalation_config`, atau Google Workspace:
   - mode `none`: jangan ubah memory;
   - mode `selective`: tulis versi baru untuk `soul:vN`, `agent_blueprint:vN`, `setup_summary:vN`, lalu set `agent_context_version=N`;
   - mode `major`: sama seperti selective, tetapi tandai update sebagai perubahan besar di `setup_summary:vN`.
4. Simpan memory legacy `soul` sebagai fallback kompatibilitas, tetapi runtime versi aktif harus lebih prioritas.
5. Tambah regression tests:
   - runtime memakai `soul:vN` saat `agent_context_version=N`;
   - fallback ke `soul` legacy tetap jalan;
   - `update_agent` dengan default selective menulis memory versi baru;
   - `refresh_memory_mode="none"` tidak menulis memory baru.
