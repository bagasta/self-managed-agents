# Arthur Progressive Skills and Runtime Refactor Plan

Tanggal: 22 Juli 2026
Status: Core implementation selesai lokal; lihat `ARTHUR_PROGRESSIVE_SKILLS_IMPLEMENTATION_2026-07-22.md` untuk bukti dan release boundary
Scope: Arthur secara universal untuk discovery, create, edit, integrasi, demo, pembayaran, knowledge, dan lifecycle agent

## 1. Latar belakang

Arthur saat ini dikendalikan oleh kombinasi rulebook panjang, soul, runtime directives, deskripsi tool, prompt composer, validator, dan reply guard. Rulebook utama telah tumbuh menjadi sekitar 57 ribu karakter dan banyak aturan kritis muncul berulang di beberapa lapisan.

Dampak yang terlihat:

- pertanyaan discovery dapat berulang karena model dan validator bekerja dari representasi state berbeda;
- model dapat menjanjikan kemampuan sebelum konfigurasi dan tool membuktikannya;
- proses create dapat dinyatakan selesai sebelum integrasi wajib siap;
- intent sederhana seperti meminta OAuth dapat berakhir pada pesan tool perantara seperti "agent sudah diedit";
- perubahan source, prompt database, dan service live dapat berbeda versi;
- seluruh workflow dan banyak tool disajikan sekaligus meskipun turn hanya membutuhkan sebagian kecil.

Refactor ini mengadopsi progressive skill loading: kernel prompt kecil selalu aktif, metadata skill tersedia dengan biaya context rendah, isi skill dimuat hanya ketika state atau intent membutuhkannya, dan perpindahan workflow dikendalikan runtime state machine.

## 2. Tujuan

1. Mengganti model utama Arthur menjadi `deepseek/deepseek-v4-flash` melalui OpenRouter.
2. Menambahkan content-aware model routing: dokumen diproses oleh Mistral Document AI dan gambar dibaca oleh `openai/gpt-4.1-mini`, sementara DeepSeek tetap menjadi orchestrator.
3. Memangkas system message Arthur menjadi kernel yang singkat, stabil, dan tidak berisi seluruh SOP.
4. Mengubah rulebook operasional menjadi skills modular dengan progressive disclosure.
5. Menjadikan runtime state machine sebagai otoritas fase, tool yang boleh digunakan, dan terminal condition.
6. Menyimpan discovery facts, evidence, confirmation, question history, integration state, dan build progress secara persisten.
7. Mencegah pertanyaan berulang, asumsi tersembunyi, create prematur, serta klaim selesai palsu.
8. Memastikan integrasi wajib seperti Google Workspace selesai sampai OAuth dan smoke test sebelum agent disebut siap.
9. Membuat deployment source, prompt/skill version, seed database, restart service, dan observability berjalan sebagai satu release unit.

## 3. Non-goals

- Tidak memindahkan authorization, entitlement, security policy, input validation, idempotency, atau postcondition enforcement ke markdown.
- Tidak memberikan Arthur akses filesystem atau sandbox umum.
- Tidak membuat satu skill raksasa yang selalu dimuat.
- Tidak mengubah semua model default child agent dalam pekerjaan ini; migrasi model dibatasi pada Arthur kecuali ditemukan dependency langsung.
- Tidak menghapus validator dan reply guard sebelum state machine terbukti mencakup perilakunya.
- Tidak mengaktifkan thinking mode DeepSeek sebelum compatibility tool-calling teruji.

## 4. Prinsip arsitektur

### 4.1 Runtime adalah otoritas

LLM memahami bahasa, mengekstrak kandidat fakta, merumuskan pertanyaan, serta menulis blueprint/SOP/instructions. Runtime menentukan:

- state build saat ini;
- fakta mana yang valid dan memiliki evidence;
- skill wajib;
- tool yang diekspos;
- apakah sebuah operasi boleh dijalankan;
- apakah tujuan turn sudah tercapai;
- apakah agent boleh disebut siap.

### 4.2 Progressive disclosure

Tiga tingkat context:

1. Metadata skill: nama, deskripsi, trigger, versi, dan state yang didukung.
2. Skill instructions: isi markdown dimuat saat dipilih runtime.
3. References/templates: dimuat hanya oleh skill yang sedang aktif bila dibutuhkan.

### 4.3 Postcondition, bukan tool terakhir

Balasan user-facing ditentukan oleh goal completion. Contoh:

- `update_agent` sukses tetapi user meminta OAuth: proses belum selesai;
- `create_agent` sukses tetapi Google masih `auth_pending`: agent dasar dibuat, belum siap;
- link demo berhasil tetapi konektor inti belum diuji: demo dibolehkan hanya dengan status fitur yang jujur;
- tool gagal setelah side effect: runtime membaca ulang state sebelum retry.

### 4.4 Fail closed untuk aksi material

Jika state, ownership, evidence, permission, atau postcondition tidak dapat diverifikasi, runtime tidak menjalankan aksi material dan meminta informasi yang benar-benar hilang.

## 5. Target architecture

```text
WhatsApp inbound
  -> resolve identity/session/agent
  -> persist inbound message
  -> load build state + facts + evidence + question history
  -> classify intent deterministically where possible
  -> resolve workflow state
  -> select required skill(s)
  -> expose state-scoped tools
  -> build compact prompt:
       kernel + current state + confirmed facts + missing facts
       + loaded skill + tool schemas
  -> LLM/tool loop
  -> validate tool input and state transition
  -> verify postconditions from database/external service
  -> render goal-based final reply
  -> persist transition, tool outcomes, and outbound message
```

## 6. Persistent state design

Tambahkan entitas build draft yang terikat pada owner dan session. Nama tabel final ditentukan saat implementasi setelah memeriksa migration conventions; kandidat: `agent_build_drafts`.

Field minimum:

- `id`
- `owner_external_id`
- `session_id`
- `target_agent_id` nullable
- `target_agent_name`
- `intent`
- `workflow_state`
- `facts_json`
- `evidence_json`
- `question_history_json`
- `required_integrations_json`
- `integration_status_json`
- `artifact_status_json`
- `confirmation_status`
- `idempotency_keys_json`
- `prompt_version`
- `skill_versions_json`
- `engine_version`
- `state_version` untuk optimistic concurrency control
- `last_inbound_message_id`
- `created_at`, `updated_at`, `completed_at`, `expires_at`

Setiap discovery fact memiliki bentuk:

```json
{
  "value": "...",
  "status": "unknown|asked|answered|derived|confirmed|rejected",
  "source_message_id": "...",
  "evidence_quote": "...",
  "updated_at": "..."
}
```

Aturan:

- `derived` hanya untuk kesimpulan berisiko rendah dan tidak boleh dipakai sebagai permission;
- aksi eksternal, data source, eskalasi, connector, channel initiation, dan approval harus `answered` atau `confirmed`;
- rangkuman akhir menampilkan proposed defaults secara eksplisit;
- build draft lama tidak boleh tertukar dengan create/edit request baru.

Aturan concurrency:

- setiap transition memakai compare-and-swap terhadap `state_version`;
- setiap inbound message memiliki idempotency key;
- side effect tool menyimpan idempotency key dan result reference;
- dua pesan WhatsApp yang masuk berdekatan tidak boleh menjalankan transition dari predecessor yang sama;
- transition matrix menentukan allowed predecessor/successor dan ditolak bila versi state sudah berubah;
- retry selalu membaca ulang state dan side-effect result sebelum mengulang tool.

Kebijakan session legacy harus dipilih sebelum rollout. Default yang direkomendasikan:

- build lama yang belum melakukan side effect tetap berjalan pada legacy engine sampai selesai atau expired;
- build lama yang sudah memiliki agent target tidak di-adopt otomatis;
- build baru setelah feature flag aktif memakai engine baru;
- tidak ada dual-write tanpa `engine_version` dan ownership lock;
- operator dapat melakukan reset/adopt manual dengan audit log jika dibutuhkan.

## 7. Workflow states

State inti:

1. `INTENT`
2. `DISCOVERY_CONTEXT`
3. `DISCOVERY_WORKFLOW`
4. `DISCOVERY_BEHAVIOR`
5. `DISCOVERY_KNOWLEDGE_FALLBACK`
6. `DISCOVERY_DATA_INTEGRATION`
7. `DISCOVERY_GOLIVE`
8. `SUMMARY_CONFIRMATION`
9. `PLAN`
10. `COMPOSE`
11. `VALIDATE`
12. `CREATE_OR_UPDATE`
13. `VERIFY`
14. `CONNECT_REQUIRED_SERVICES`
15. `CONNECTOR_SMOKE_TEST`
16. `DEMO`
17. `USER_REVIEW`
18. `ACTIVATE_CHANNEL`
19. `COMPLETE`
20. `BLOCKED_RECOVERABLE`
21. `FAILED_TERMINAL`

Transisi harus didefinisikan dalam kode dan diuji sebagai tabel, bukan hanya dijelaskan di prompt.

## 8. Skill taxonomy

Skill awal yang direncanakan:

### `arthur-discovery`

- memahami perubahan kebutuhan;
- memilih grup discovery berikutnya;
- membedakan fakta, proposed default, dan permission;
- membuat pertanyaan maksimal 2–3 per pesan;
- merangkum untuk konfirmasi.

### `arthur-create-agent`

- plan, blueprint, operating manual, instructions, soul;
- validasi payload;
- create idempotent;
- readback dan verify.

### `arthur-edit-agent`

- resolve target agent;
- membedakan edit dari create baru;
- diff user-facing;
- confirmation untuk perubahan material;
- update dan verify.

### `arthur-google-workspace`

- menentukan Google explicit/optional/not-needed;
- mengaktifkan config;
- generate OAuth;
- authorization check;
- memilih atau membuat resource target;
- smoke test;
- recovery auth/scope errors.

### `arthur-whatsapp-demo-channel`

- demo-first;
- trial code/link dan vCard order;
- activation nomor user;
- QR/device readiness;
- pemisahan shared trial dan dedicated Arthur session.

### `arthur-files-knowledge`

- membedakan attachment, generated file, RAG knowledge, dan cloud document;
- menetapkan file capability;
- ingestion dan readiness knowledge base.

### `arthur-subscription-payment`

- subscription identity;
- entitlement dan slot;
- payment link;
- upgrade/top-up guidance;
- retry setelah entitlement berubah.

### `arthur-lifecycle-safety`

- list/detail/renew/delete;
- target confirmation;
- safety/policy refusal;
- audit trail.

Setiap skill memiliki:

- YAML metadata: name, description, triggers, supported states, version;
- instructions;
- preconditions;
- allowed tool groups;
- postconditions;
- recovery paths;
- examples dan anti-examples;
- optional references/templates.

Aturan komposisi skill:

- maksimal satu primary workflow skill per turn;
- policy mixin hanya untuk keamanan atau connector requirement yang benar-benar lintas workflow;
- precedence: runtime safety policy -> state contract -> primary skill -> policy mixin -> user preference;
- tetapkan budget maksimum jumlah skill dan token per turn melalui hasil baseline/eval;
- konflik instruksi membuat run fail closed dan dicatat, bukan digabung berdasarkan urutan kebetulan.

## 9. Skill storage and loading

Project sudah memiliki tabel skill dengan `name`, `description`, dan `content_md`, serta tool `create_skill`, `list_skills`, dan `use_skill`.

Refactor yang diperlukan:

1. Simpan source-of-truth skill sebagai file version-controlled di repo.
2. Tambahkan seed/sync command yang menulis skill ke database Arthur.
3. Tambahkan metadata `version`, `triggers`, `supported_states`, checksum, dan status enabled.
4. Load seluruh metadata aktif Arthur dengan budget kecil pada awal run atau cache per version.
5. Untuk state kritis, runtime memilih dan memuat skill secara otomatis; jangan menunggu LLM memanggil `list_skills`.
6. `use_skill` tetap tersedia untuk workflow non-kritis atau skill tambahan, tetapi bukan satu-satunya loader.
7. Catat skill version yang dipakai setiap run.

Trust boundary skill:

- `system_skill` Arthur terpisah dari skill yang dibuat user;
- hanya admin/release process yang dapat menerbitkan `system_skill`;
- bundle system skill immutable, versioned, checksum-verified, dan idealnya ditandatangani;
- loader Arthur memakai allowlist bundle/version, bukan pencarian seluruh skill user;
- references tidak boleh keluar dari packaged bundle;
- actor pembuat, reviewer, publisher, dan activator tercatat dalam audit log;
- checksum mendeteksi perubahan tetapi tidak menggantikan authorization.

Filesystem path tidak diberikan kepada Arthur. Loader membaca source yang sudah di-seed atau packaged oleh backend.

## 10. Compact Arthur kernel prompt

Kernel baru hanya memuat:

- identitas Arthur dan scope;
- bahasa/cara bicara dasar;
- runtime state sebagai sumber kebenaran;
- larangan mengarang fakta dan hasil tool;
- kewajiban memakai skill yang disediakan;
- larangan mengklaim selesai sebelum terminal condition;
- security boundary dan policy global;
- instruksi untuk menyampaikan blocker secara jujur.

Kernel tidak memuat:

- seluruh urutan create/update;
- daftar panjang config preset;
- detail OAuth;
- demo flow;
- payment flow;
- seluruh file/RAG rules;
- contoh percakapan semua use case.

Target ukuran awal: maksimum 6–10 ribu karakter sebelum runtime state dan skill. Target final ditentukan melalui token measurement dan eval, bukan hanya jumlah baris.

## 11. State-scoped tool exposure

Kurangi 24 builder tools yang terlihat sekaligus.

Contoh mapping:

- discovery: platform capabilities, subscription, presets, plan;
- compose: blueprint, operating manual, instructions, soul, validation;
- create: create, verify, detail;
- Google connect: detail, update bila perlu, verify, generate auth;
- demo: detail, trial link, available devices;
- lifecycle: list/detail/update/delete/renew;
- payment: subscription, payment link.

Tool safety tetap enforced server-side walaupun tool tidak sengaja terekspos.

## 12. Arthur model routing and DeepSeek V4 Flash migration

### 12.1 Model configuration

- Ubah Arthur seed model menjadi `deepseek/deepseek-v4-flash`.
- Tetapkan konfigurasi terpisah dan terversi: `ARTHUR_PRIMARY_MODEL=deepseek/deepseek-v4-flash`, `ARTHUR_DOCUMENT_MODEL=mistral-ocr-latest`, dan `ARTHUR_IMAGE_MODEL=openai/gpt-4.1-mini`.
- Tambahkan model ke curated API model catalog.
- Update existing Arthur database row melalui seed/migration.
- Pastikan model ID dan provider terlihat pada runtime logs.
- Tetapkan provider routing policy OpenRouter, provider preference bila digunakan, timeout, retry, dan fallback behavior secara eksplisit.
- Jangan mengizinkan fallback diam-diam ke model yang belum lulus golden tool-call eval.
- Tetapkan maximum tool rounds, invalid-JSON repair policy, dan circuit breaker provider.

### 12.2 Thinking mode decision

Tahap pertama memakai mode eksplisit yang paling kompatibel dengan tool loop saat ini. Jangan mengandalkan provider default.

Sebelum enabling thinking mode:

- verifikasi bagaimana OpenRouter + LangChain menyimpan `reasoning_content`/`reasoning_details`;
- uji multi-step tool calls;
- uji continuation setelah tool result;
- uji error 400 dan retry behavior;
- pastikan reasoning tidak bocor ke WhatsApp atau persistence yang tidak semestinya.

Jika compatibility belum terbukti, gunakan non-thinking mode dengan state machine deterministik sebagai baseline. Thinking mode dapat diaktifkan melalui feature flag setelah eval.

### 12.3 Content-aware attachment routing

DeepSeek V4 Flash diperlakukan text-only dan tetap menjadi satu-satunya orchestrator Arthur. Pemilihan model attachment dilakukan secara deterministik dari MIME type, extension, dan hasil file validation sebelum prompt dibentuk:

| Input | Processor/model wajib | Peran |
|---|---|---|
| Teks/chat tanpa attachment | `deepseek/deepseek-v4-flash` | Discovery, reasoning, state transition, tool selection, dan jawaban final |
| Dokumen seperti PDF, DOCX, dan PPTX | Mistral Document AI, model `mistral-ocr-latest` melalui `/v1/ocr` | Ekstraksi teks, struktur, tabel, dan metadata dokumen |
| Gambar seperti JPEG, PNG, WebP, screenshot, foto produk, atau QR | `openai/gpt-4.1-mini` | Deskripsi visual dan ekstraksi informasi yang relevan dengan pertanyaan user |
| Scanned PDF | Mistral Document AI sebagai document route | OCR per halaman; tidak salah diklasifikasikan sebagai image route hanya karena setiap halaman berupa gambar |
| Pesan dengan beberapa attachment berbeda | Processor masing-masing attachment, lalu DeepSeek | Menggabungkan evidence terstruktur dan melanjutkan workflow |

`mistral-ocr-latest` dipilih karena project sudah memakai endpoint Mistral OCR tersebut pada `app/core/domain/file_processor.py`; implementasi Arthur harus menggunakan adapter yang sama atau service bersama, bukan memaksa model OCR masuk ke generic chat-completions route.

Plain text (`.txt`, `.md`) dan data tabular sederhana (`.csv`) boleh diparse secara deterministik tanpa model OCR, tetapi hasilnya tetap masuk ke kontrak evidence dokumen. File Office/PDF yang user minta untuk dibaca tidak boleh diam-diam dialihkan ke DeepSeek.

### 12.4 Attachment evidence contract

Processor Mistral dan GPT-4.1 Mini tidak boleh memilih builder tool, mengubah workflow state, membuat agent, atau mengirim balasan final. Keduanya hanya menghasilkan `AttachmentEvidence` terstruktur, minimal:

- `attachment_id`, filename, validated MIME type, size, dan checksum;
- `route` (`document`, `image`, atau `plain_text`) dan model/provider yang benar-benar dipakai;
- extracted text atau visual description;
- page/region/section provenance bila tersedia;
- confidence dan warning seperti unreadable, partial, encrypted, truncated, atau unsupported;
- extraction timestamp dan processor version.

DeepSeek menerima pertanyaan user beserta evidence terstruktur ini, bukan raw binary dan bukan klaim tanpa sumber. Fakta hasil pembacaan attachment berstatus `extracted_evidence`, bukan otomatis `user_confirmed`.

### 12.5 Fallback and failure semantics

Istilah fallback di sini berarti fallback berdasarkan modality: DeepSeek mendelegasikan pembacaan attachment kepada processor yang tepat. Ini bukan izin untuk mengganti model secara acak ketika provider gagal.

- document route selalu mencoba Mistral terlebih dahulu;
- image route selalu mencoba GPT-4.1 Mini terlebih dahulu;
- retry dibatasi, memakai idempotency key, exponential backoff, timeout, dan circuit breaker;
- kegagalan Mistral tidak boleh diam-diam dialihkan ke GPT-4.1 Mini atau DeepSeek untuk menebak isi dokumen;
- kegagalan GPT-4.1 Mini tidak boleh membuat Arthur menjawab berdasarkan filename/caption seolah sudah melihat gambar;
- jika processor tetap gagal, Arthur menyimpan status `attachment_processing_failed`, menjelaskan file mana yang gagal dan meminta retry/re-upload hanya bila memang diperlukan;
- file encrypted, corrupt, terlalu besar, atau format tidak didukung menghasilkan blocker konkret;
- maksimum satu processor route per attachment dan satu retry otomatis agar tidak terjadi fallback loop serta biaya ganda;
- raw attachment hanya disimpan/diteruskan sesuai retention, tenant isolation, access-control, dan privacy policy yang berlaku.

### 12.6 Validation references

- Mistral menyediakan `mistral-ocr-latest` pada endpoint `/v1/ocr` untuk document extraction dan structured output: https://docs.mistral.ai/api/endpoint/ocr
- Mistral Document AI mendukung PDF, DOCX, PPTX, image input, struktur dokumen, tabel, dan confidence scores: https://docs.mistral.ai/studio-api/document-processing/basic_ocr
- OpenAI mendukung image input content pada API, dan project sudah memakai canonical OpenRouter ID `openai/gpt-4.1-mini`: https://platform.openai.com/docs/guides/images-vision

## 13. Composer refactor

Blueprint, operating manual, instructions, dan soul menerima evidence ledger, bukan seluruh history mentah.

Setiap composer wajib:

- memakai confirmed facts;
- menandai proposed defaults;
- tidak menciptakan permission, integration, escalation target, data source, atau channel action;
- menghasilkan machine-readable assumptions list;
- gagal closed jika input kritis hilang;
- menyertakan version dan source build ID.

Composer prompts juga dipisahkan menjadi templates/references skill agar tidak ikut pada semua turn.

## 14. Question deduplication

Setiap pertanyaan memiliki canonical `question_id` dan fact targets.

Aturan:

- pertanyaan yang sama tidak dirender dua kali berturut-turut;
- jika jawaban relevan tetapi ambigu, follow-up harus menjelaskan ambiguity;
- validator mengembalikan missing fact IDs, bukan teks pertanyaan final;
- renderer membuat pertanyaan dengan mempertimbangkan assistant question history;
- jawaban seperti “iya”, “tidak”, atau “sudah” di-resolve terhadap pertanyaan pending yang tepat;
- setiap jawaban mencatat dependency facts dan `state_version` saat jawaban diberikan;
- jika user mengubah tujuan, channel, workflow, connector, atau fakta induk, jawaban turunannya di-invalidasi dengan alasan terstruktur;
- pertanyaan boleh ditanyakan ulang setelah invalidation, tetapi log dan renderer harus membedakannya dari duplikasi yang tidak perlu;
- reset user menghapus pending build/question state yang terkait user.

## 15. Integration transaction

Status konektor:

`not_required -> required -> configured -> auth_pending -> authorized -> resource_ready -> verified`

Google Sheets dianggap siap hanya jika:

- config Google tersimpan;
- OAuth authorized;
- sheet target ada;
- struktur kolom diketahui;
- append smoke test sukses.

Smoke test tidak boleh menulis marker ke worksheet produksi user secara langsung. Gunakan salah satu resource yang tervalidasi ownership-nya:

- spreadsheet sandbox khusus tenant; atau
- temporary worksheet terisolasi yang dibuat dan dibersihkan secara idempotent.

Jika cleanup gagal, resource test harus tetap dapat diidentifikasi dan dilaporkan tanpa menghapus data user lain.

`create_agent` tidak harus rollback jika OAuth belum selesai, tetapi status keseluruhan harus `setup_pending`, bukan `complete`.

## 16. Reply rendering

Final reply dibangun dari workflow outcome:

- `needs_user_input`: pertanyaan yang belum terjawab;
- `setup_pending`: apa yang sudah dibuat dan satu aksi user berikutnya;
- `blocked_recoverable`: blocker, dampak, dan retry aman;
- `complete`: bukti postcondition dan next optional step;
- `failed_terminal`: kegagalan jujur tanpa klaim success.

Status user-facing dibakukan:

- `agent_created`: record agent sudah dibuat tetapi setup belum lengkap;
- `setup_pending`: ada aksi user/service yang masih wajib;
- `demo_limited`: demo tersedia dengan batas fitur yang disebut eksplisit;
- `production_ready`: seluruh connector wajib dan smoke test fungsi inti lulus.

Kata seperti "selesai", "siap", atau "sudah jadi" harus dipetakan ke status tersebut. Agent dengan connector inti belum boleh disebut `production_ready`.

Reply guard lama dipertahankan selama migrasi, diberi observability untuk mendeteksi override, lalu dikurangi setelah state renderer stabil.

## 17. Observability and release versioning

Setiap run Arthur log minimal:

- app commit SHA;
- Arthur primary model/provider;
- attachment route, detected MIME, processor model/provider, extraction status, latency, dan retry count;
- Arthur prompt/kernel version;
- loaded skill names/versions;
- build ID dan workflow state before/after;
- available tool group dan called tools;
- validation/postcondition result;
- reply override source;
- connector status transition;
- latency dan token usage per stage.

Health endpoint/deploy metadata harus menunjukkan commit dan prompt/skill bundle version.

## 18. Release process

Satu release Arthur harus mencakup urutan aman:

1. database migration additive;
2. upload bundle kernel/skills immutable dalam status inactive;
3. verifikasi checksum, signature/authorization, dan dependency bundle;
4. deploy backend dengan feature flag baru masih OFF;
5. restart worker;
6. health/version verification pada engine lama;
7. aktifkan bundle/version secara atomik untuk canary;
8. cache invalidation terarah;
9. smoke test WhatsApp pada dedicated Arthur session;
10. smoke test shared trial flow;
11. rollout bertahap;
12. rollback command dan previous prompt/skill bundle.

Git push bukan deployment dan tidak boleh dilaporkan sebagai runtime selesai.

## 19. Implementation phases

### Phase 0 — Baseline and evidence

- capture current prompt/tool/token metrics;
- kumpulkan run log percakapan gagal;
- tambah transcript regression fixtures;
- tambah commit/prompt/model observability;
- dokumentasikan current DB prompt version.
- ukur baseline numerik: duplicate-question rate, false-completion rate, OAuth-link delivery, tool validation errors, p50/p95 latency, input/output token per turn, dan retry rate.
- tetapkan canary stop thresholds sebelum implementation behavior dimulai.

Exit criteria: failure dapat direproduksi dan source-vs-runtime dapat dibuktikan.

### Phase 1 — DeepSeek compatibility harness

- tambahkan model catalog entry;
- buat isolated agent-run eval untuk tool calling;
- uji non-thinking dan thinking modes;
- buat deterministic attachment router dan kontrak `AttachmentEvidence`;
- hubungkan document route ke Mistral `mistral-ocr-latest` dan image route ke `openai/gpt-4.1-mini`;
- uji JSON/schema, repeated tool calls, reasoning continuation, dan handoff evidence dari processor ke DeepSeek;
- uji bahwa processor attachment tidak menerima builder tools dan tidak dapat mengubah state;
- uji timeout, retry, circuit breaker, file invalid, dan provider unavailable tanpa cross-model guessing;
- pilih mode baseline melalui hasil eval.

Exit criteria: ketiga model route lulus compatibility suite dan provenance test sebelum DeepSeek menjadi default Arthur.

### Phase 2 — Persistent build state

- migration dan model build draft;
- repository/service API;
- evidence extraction contract;
- pending question resolver;
- reset/expiry behavior;
- state transition tests.
- optimistic locking dengan `state_version`;
- idempotency per inbound message dan side effect;
- legacy-session policy dan `engine_version`;
- jalankan state extraction/persistence dalam shadow mode terlebih dahulu tanpa mengendalikan reply/tool.

Exit criteria: restart proses atau compaction history tidak menghilangkan build state.

### Phase 3 — Progressive skills runtime

- definisikan skill metadata schema;
- buat repo skill bundle;
- seed/sync ke database;
- inject metadata budget;
- deterministic skill resolver;
- automatic content loader;
- skill version logging.
- trust separation system skill vs user skill;
- immutable bundle activation dan allowlist;
- satu primary skill per turn plus policy mixin terbatas;

Exit criteria: hanya skill relevan yang masuk context dan operasi kritis tidak berjalan tanpa skill/state yang benar.

### Phase 4 — Prompt and tool scoping

- ganti rulebook besar dengan kernel;
- pindahkan workflow detail ke skills;
- state-scoped tools;
- refactor composer inputs;
- pertahankan safety hard rules di code/kernel.

Exit criteria: prompt awal berkurang material tanpa penurunan compliance eval.

### Phase 5 — Integration and goal-based completion

- connector state transaction;
- OAuth chain;
- Sheets resource selection dan smoke test;
- goal-based reply renderer;
- idempotent retry;
- dedupe questions.

Exit criteria: transcript Minsel selesai tanpa pertanyaan duplikat dan tidak berhenti pada “sudah saya edit”.

### Phase 6 — Staged rollout

- shadow eval terhadap transcript produksi teranonimisasi;
- internal operator canary;
- dedicated Arthur canary;
- sebagian kecil user;
- full rollout setelah thresholds terpenuhi;
- pertahankan old/new engine dalam shadow comparison;
- simpan bundle dan engine lama minimal satu release penuh setelah full rollout;
- pangkas prompt lama bertahap hanya setelah parity lintas seluruh use case dan thresholds terpenuhi.

## 20. Test and evaluation matrix

### Unit tests

- state transitions;
- fact/evidence status;
- MIME/extension validation dan deterministic attachment routing;
- `AttachmentEvidence` schema dan provenance;
- question dedupe;
- skill resolver;
- tool scope;
- idempotency;
- connector status;
- goal-based replies;
- prompt version/seed sync.

### Integration tests

- OpenRouter DeepSeek tool loop;
- text-only message -> DeepSeek tanpa memanggil attachment processor;
- PDF/DOCX/PPTX -> Mistral `mistral-ocr-latest` -> evidence -> DeepSeek;
- scanned PDF tetap memakai Mistral document route;
- JPEG/PNG/WebP/screenshot/QR -> `openai/gpt-4.1-mini` -> evidence -> DeepSeek;
- mixed document dan image menghasilkan evidence terpisah tanpa kehilangan provenance;
- Mistral/GPT processor tidak memperoleh builder tools atau write access ke workflow state;
- timeout/provider failure menghasilkan `attachment_processing_failed`, bukan jawaban hasil tebakan atau fallback loop;
- encrypted, corrupt, oversized, MIME mismatch, dan unsupported file ditolak dengan blocker konkret;
- create -> verify;
- update -> verify;
- update Google -> auth link;
- OAuth complete -> resource setup -> append test;
- demo link/vCard ordering;
- reset user removes build state;
- backend restart preserves build state.

### Transcript evals

- simple CS;
- survey outbound/inbound ambiguity;
- personal assistant;
- research;
- data analyst;
- content/social;
- coding/deploy request;
- file receive/generate/both/text-only;
- user meminta rangkuman dokumen, pembacaan screenshot, identifikasi foto produk, dan ekstraksi QR;
- attachment tidak terbaca dan user melakukan retry/re-upload;
- Google explicit/optional/declined;
- user changes requirements mid-flow;
- short confirmations seperti “iya”, “tidak”, dan “sudah”;
- malicious prompt injection;
- subscription slot exhausted;
- duplicate agent name;
- OAuth service unavailable.

### Quality thresholds

- zero create sebelum required facts confirmed;
- zero duplicate canonical question pada consecutive turns;
- zero success claim tanpa postcondition;
- zero fabricated tool result/link;
- zero jawaban yang mengklaim telah membaca attachment ketika processor gagal atau belum selesai;
- 100% PDF/DOCX/PPTX yang tervalidasi masuk document route dan 100% image MIME yang tervalidasi masuk image route pada routing test suite;
- zero cross-model fallback diam-diam untuk attachment;
- OAuth link delivery success memenuhi target yang disepakati;
- tool argument validation error rate tidak lebih buruk dari baseline;
- p95 latency dan token cost berada dalam budget;
- Indonesian response quality lulus human review.

Nilai numerik final tidak boleh memakai istilah abstrak seperti "tidak lebih buruk" atau "dalam budget". Phase 0 wajib mengisi baseline dan release gate pada tabel evaluasi sebelum coding behavior. Contoh gate yang harus diberi angka:

- duplicate canonical question rate;
- false completion rate;
- OAuth link delivery success rate;
- invalid tool argument rate;
- p95 latency;
- token per completed workflow;
- canary error/rollback threshold.

## 21. Rollback strategy

- feature flags untuk state machine, progressive skills, scoped tools, model, thinking mode, dan goal renderer;
- previous Arthur model tersimpan;
- previous kernel/skill bundle version dapat diaktifkan kembali;
- migration additive pada awal rollout;
- jangan drop legacy columns/guards pada release pertama;
- rollback tidak menghapus build drafts atau audit trail;
- setiap canary failure memiliki automatic stop threshold.

## 22. Expected code areas

Area yang kemungkinan berubah:

- `scripts/seed_arthur.py`
- `system-message-builder.md` atau penggantinya sebagai compact kernel source
- `app/core/engine/agent_llm.py`
- `app/core/engine/agent_runner.py`
- `app/core/engine/agent_tool_setup.py`
- `app/core/engine/prompt_builder.py`
- `app/core/engine/agent_followups.py`
- `app/core/engine/reply_guard.py`
- `app/core/engine/tool_builder.py`
- `app/core/tools/builder_*`
- `app/core/domain/skill_service.py`
- `app/models/skill.py`
- model/service/migration baru untuk build state
- `app/api/models.py`
- deploy/health/version metadata
- focused unit, integration, dan transcript tests

Daftar final harus ditentukan setelah implementation trace; tidak semua file harus diubah sekaligus.

## 23. Acceptance criteria

Refactor dianggap selesai jika:

1. Arthur live memakai `deepseek/deepseek-v4-flash` dan mode reasoning yang tervalidasi untuk chat, orchestration, dan tool loop.
2. Dokumen tervalidasi diproses oleh Mistral `mistral-ocr-latest`, gambar tervalidasi dibaca oleh `openai/gpt-4.1-mini`, dan route/model aktual terlihat di log.
3. Kegagalan processor attachment menghasilkan blocker jujur tanpa hallucination, cross-model guessing, atau klaim bahwa file sudah dibaca.
4. Kernel prompt dan skill bundle memiliki version yang terlihat di log/health.
5. Workflow create/edit bertahan terhadap restart dan history compaction.
6. Arthur tidak mengulang pertanyaan canonical yang sudah dijawab.
7. Arthur tidak membuat fakta operasional tanpa evidence atau confirmed default.
8. Tool yang terlihat sesuai state dan skill relevan dimuat otomatis.
9. Agent dengan Google wajib tidak disebut siap sebelum OAuth/resource/smoke test selesai.
10. Permintaan “buatkan link Google” berakhir dengan link atau blocker konkret, bukan pesan edit generik.
11. Transcript regressions lintas use case lulus.
12. Deployment verification membuktikan commit, model routes, prompt, dan skill bundle live sesuai release.

## 24. Open questions for implementation review

1. Apakah build state memakai tabel baru atau memperluas session metadata? Rekomendasi awal: tabel baru agar audit, expiry, dan multiple drafts lebih aman.
2. Apakah skill selection sepenuhnya deterministic atau hybrid? Rekomendasi: deterministic untuk workflow kritis, model-assisted untuk workflow pendukung.
3. Apakah Google smoke test memakai spreadsheet sandbox tenant atau temporary worksheet? Rekomendasi: spreadsheet sandbox tenant sebagai default; jangan menulis ke worksheet produksi hanya untuk membuktikan OAuth.
4. Apakah non-thinking atau thinking mode menjadi baseline? Diputuskan dari compatibility eval, bukan asumsi.
5. Berapa budget metadata dan loaded skills per turn? Diukur dari token profile dan quality eval.
6. Format dokumen tambahan apa yang wajib masuk Mistral route pada rollout pertama? Minimum yang diputuskan: PDF, DOCX, dan PPTX; XLSX harus ditambahkan hanya setelah upload validation dan extraction test tersedia.

## 25. Sub-agent critical review incorporated

Plan ini direview oleh sub-agent independen dengan instruksi anti-sycophant. Kritik prioritas yang diterima dan dimasukkan:

1. first slice terlalu besar dan sulit di-debug;
2. bundle skill harus tersedia sebelum backend baru diaktifkan;
3. session legacy dan dual-engine ownership harus memiliki policy eksplisit;
4. state transition memerlukan optimistic locking dan idempotency;
5. smoke test tidak boleh mencemari spreadsheet produksi;
6. status `created`, `setup_pending`, `demo_limited`, dan `production_ready` harus dibedakan;
7. provider/fallback DeepSeek perlu golden eval dan routing policy;
8. system skill memerlukan trust boundary terpisah dari user skill;
9. primary skill, mixin, precedence, dan token budget harus dibatasi;
10. jawaban lama harus dapat di-invalidasi ketika dependency fact berubah;
11. acceptance criteria harus menjadi angka release gate;
12. engine/prompt lama dipertahankan setidaknya satu release penuh.

## 26. Revised execution gates

Eksekusi dipecah menjadi gate yang dapat divalidasi dan di-rollback secara independen:

### Gate A — Observability only

- commit/model/prompt/skill/engine version logging;
- feature flags dalam keadaan OFF;
- baseline metrics dan transcript fixtures;
- tidak ada perubahan perilaku user-facing.

### Gate B — Model routing compatibility only

- tambahkan catalog/config untuk DeepSeek primary, Mistral document processor, dan GPT-4.1 Mini image processor;
- golden tool-call eval non-thinking dan thinking;
- deterministic MIME routing dan `AttachmentEvidence` handoff;
- provider routing, timeout, retry, circuit breaker, max rounds, dan no-silent-cross-model-fallback policy;
- document/image failure and hallucination regression tests;
- belum mengganti model Arthur production.

### Gate C — Persistent state shadow

- migration additive;
- state extraction dan transition simulation;
- optimistic locking/idempotency;
- policy session legacy;
- state shadow tidak mengontrol tool/reply.

### Gate D — Trusted skill loader

- satu system skill vertical (`arthur-google-workspace`);
- immutable bundle, allowlist, version/checksum;
- satu primary skill;
- metadata/content loading metrics;
- feature flag canary saja.

### Gate E — Google transaction

- explicit Sheets requirement;
- update/create -> verify -> OAuth -> authorization -> sandbox resource -> smoke test;
- goal-based status/reply;
- tidak menulis ke worksheet produksi.

### Gate F — Canary and independent switches

- shadow comparison;
- dedicated Arthur canary;
- shared trial canary;
- compact kernel activation, DeepSeek activation, document route, dan image route dilakukan dengan flag terpisah;
- lanjut ke persentase user hanya setelah release gates numerik lulus.

Urutan ini menggantikan vertical slice lama yang menjalankan terlalu banyak perubahan sekaligus. Model migration dan compact prompt tidak diaktifkan bersamaan, sehingga rollback dapat mengisolasi sumber regresi.
