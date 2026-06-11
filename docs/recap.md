# Recap: Deep Agent SaaS Hardening — MCP, Subagent, Sandbox, Builder Entitlements

## 2026-06-11 — Rekap Eskalasi Tersedia untuk Admin/Operator

Admin testing menemukan bahwa setelah beberapa eskalasi masuk, operator bertanya "ada berapa pesan eskalasi hari ini?" tetapi agent menjawab belum ada data. Ini terjadi karena eskalasi disimpan sebagai `Message(role="escalation")` di session customer, bukan sebagai memory/riwayat chat di session operator.

### Root Cause
- Operator mode hanya aktif saat operator reply notifikasi eskalasi atau mengonfirmasi pending draft.
- Pertanyaan admin bebas seperti "ada berapa pesan eskalasi hari ini?" tidak masuk operator mode, sehingga diproses sebagai chat customer biasa.
- Prompt operator juga belum menerima ringkasan escalation messages lintas session customer.

### Fix
- `channels.py`: mendeteksi intent rekap eskalasi dari operator (`berapa/rekap/daftar/laporan ... eskalasi`) meskipun tidak reply pesan eskalasi.
- `channels.py`: saat intent rekap terdeteksi, query `messages.role='escalation'` untuk agent yang sama sejak awal hari Asia/Jakarta, lalu format total dan daftar eskalasi terbaru.
- `prompt_builder.py`: operator prompt sekarang memuat "Konteks admin/operator yang tersedia" dan instruksi eksplisit untuk menjawab rekap eskalasi dari blok tersebut.

### Validasi
- `PYTHONPATH=. .venv/bin/python -m pytest tests/test_whatsapp_spam_escalation.py::test_operator_phone_without_escalation_reply_is_customer_turn tests/test_whatsapp_spam_escalation.py::test_operator_phone_with_quoted_escalation_is_operator_turn tests/test_whatsapp_spam_escalation.py::test_operator_send_confirmation_uses_pending_draft_without_quote tests/test_whatsapp_spam_escalation.py::test_operator_revision_uses_pending_draft_without_escalation_quote tests/test_whatsapp_spam_escalation.py::test_operator_escalation_recap_request_is_operator_turn_without_quote tests/test_whatsapp_spam_escalation.py::test_format_operator_escalation_recap_counts_today_rows tests/test_whatsapp_spam_escalation.py::test_operator_prompt_includes_escalation_recap_context tests/test_operator_reply_draft.py -q` -> 11 passed.

## 2026-06-11 — Revisi Draft Eskalasi Operator Menimpa Draft Lama

Admin testing menemukan bug di flow eskalasi WhatsApp: operator reply notifikasi eskalasi, agent membuat draft untuk customer, operator minta "buat lebih sopan", tetapi saat operator mengetik `kirim` pesan yang terkirim masih draft pertama.

### Root Cause
- Operator mode hanya aktif untuk reply langsung ke pesan eskalasi atau konfirmasi `kirim` saat ada pending draft.
- Pesan revisi seperti "buat lebih sopan" biasanya membalas draft agent, bukan notifikasi eskalasi, sehingga sempat diperlakukan sebagai customer turn biasa.
- Akibatnya hasil draft revisi tidak selalu tersimpan ulang ke `pending_operator_text_reply`; konfirmasi `kirim` masih mengambil pending lama.

### Fix
- `_should_treat_as_operator_turn()` sekarang menjaga percakapan tetap di operator mode selama ada pending operator draft aktif dan pesan berikutnya adalah teks biasa.
- Draft revisi yang dihasilkan agent bisa memakai active escalation route, lalu menimpa `pending_operator_text_reply` sebelum operator mengetik `kirim`.
- Regression test ditambahkan untuk revisi draft tanpa quote eskalasi dan format draft sopan seperti screenshot.

### Validasi
- `PYTHONPATH=. .venv/bin/python -m pytest tests/test_whatsapp_spam_escalation.py::test_operator_phone_without_escalation_reply_is_customer_turn tests/test_whatsapp_spam_escalation.py::test_operator_phone_with_quoted_escalation_is_operator_turn tests/test_whatsapp_spam_escalation.py::test_operator_send_confirmation_uses_pending_draft_without_quote tests/test_whatsapp_spam_escalation.py::test_operator_revision_uses_pending_draft_without_escalation_quote tests/test_whatsapp_spam_escalation.py::test_extract_operator_text_draft_uses_corrected_separator_block tests/test_whatsapp_spam_escalation.py::test_extract_operator_text_draft_uses_polite_revision_quote tests/test_whatsapp_spam_escalation.py::test_pending_operator_text_confirmation_sends_saved_corrected_draft tests/test_operator_reply_draft.py -q` -> 11 passed.

## 2026-06-11 — Outbound WhatsApp Anti-Spam Limit Disesuaikan ke 3 Pesan

Admin testing menemukan request normal seperti "kirim pesan promo ini dengan gambar ke +62..." bisa dibalas "dibatasi sementara untuk mencegah spam" pada percobaan kedua. Ini bukan guard inbound spam customer, melainkan guard outbound saat agent diminta mengirim ke nomor lain.

### Root Cause
- `wa_outbound_guard.py` memakai `WA_OUTBOUND_DIRECT_LIMIT = 1` dengan window 300 detik per device+target.
- Untuk `wa-dev-service` dan device `wadev_*`, source dinormalisasi menjadi shared key yang sama (`wadev_shared`), jadi percobaan dari beberapa agent demo ke nomor tujuan sama ikut berbagi counter.
- Detector spam eksplisit tetap benar: kata seperti `spam`, `berkali-kali`, `100 kali`, `flood`, dan sejenisnya diblok sebelum send.

### Fix
- Limit outbound normal dinaikkan menjadi 3 pesan per 5 menit per device+nomor tujuan.
- Pesan rate-limit sekarang menyebut aturan konkret: maksimal 3 pesan dalam 5 menit.
- Regression test memastikan request "kirim pesan promo ini dengan gambar..." tidak dianggap spam eksplisit, dan percobaan ke-4 baru diblok.

### Validasi
- `PYTHONPATH=. .venv/bin/python -m pytest tests/test_whatsapp_direct_send.py::test_outbound_wa_spam_request_detector_blocks_bulk_same_number tests/test_whatsapp_direct_send.py::test_outbound_wa_window_treats_wadev_devices_as_shared_number tests/test_whatsapp_direct_send.py::test_send_to_number_blocks_spam_request_before_channel_send tests/test_whatsapp_direct_send.py::test_direct_text_send_context_does_not_capture_media_requests -q` -> 4 passed.

## 2026-06-11 — SOP Gate Tidak Lagi Mencabut Tool Media WhatsApp

Admin testing lewat WhatsApp menemukan regresi saat user meminta gambar/dokumen dikirim: agent menjawab "Tool send_whatsapp_image tidak tersedia di run ini" walaupun session berjalan di channel WhatsApp.

### Root Cause
- `build_agent_tool_setup()` sudah selalu menambahkan `send_whatsapp_image` dan `send_whatsapp_document` untuk session WhatsApp.
- Setelah itu `sop_runtime_gate.filter_tools_by_sop()` masih menghapus kedua tool tersebut jika latest SOP `draft`, `needs_review`, atau hilang.
- Akibatnya runtime prompt bisa melihat `whatsapp_media` sebagai active group, tetapi tool konkret sudah dicabut sebelum graph jalan.

### Fix
- `sop_runtime_gate.py`: SOP maturity tetap dihitung untuk readiness/launch warning, tapi runtime tidak lagi mencabut channel-level WhatsApp media delivery tools.
- `tests/test_sop_runtime_gate.py`: regression test diperbarui agar SOP locked tetap mempertahankan `send_whatsapp_image` dan `send_whatsapp_document`.

### Validasi
- `PYTHONPATH=. .venv/bin/python -m pytest tests/test_sop_runtime_gate.py -q` -> 5 passed.
- `PYTHONPATH=. .venv/bin/python -m pytest tests/test_whatsapp_direct_send.py::test_send_whatsapp_image_uses_current_attachment_without_sandbox tests/test_whatsapp_direct_send.py::test_disabled_whatsapp_media_prevents_file_delivery_claim tests/test_whatsapp_direct_send.py::test_direct_text_send_context_does_not_capture_media_requests -q` -> 3 passed.

## 2026-06-09 — WhatsApp PDF Delivery Loop di wa-dev-service

Testing agent buatan Arthur lewat nomor demo `wa-dev-service` menemukan bug saat user minta laporan visualisasi data/PDF dikirim ke WhatsApp. Agent berhasil membuat PDF di sandbox, tapi berulang kali hanya membalas teks seperti "sudah saya kirim" tanpa attachment.

### Masalah yang Ditemukan
- **Tool kirim file sebenarnya ada.** `wa-dev-service` dan `wa-service` harus punya kemampuan runtime yang sama untuk media delivery; bedanya hanya cara koneksi WhatsApp (kode pairing vs scan QR). Jadi akar masalah bukan karena `wa-dev-service` tidak bisa kirim dokumen.
- **Runtime salah membaca envelope owner.** Pesan WA owner datang dalam format `<OWNER>... Pesan: ok/iya`, tapi `_operator_message_payload` hanya membersihkan `<OPERATOR>`. Akibatnya metadata nomor WA di envelope ikut terbaca dan turn seperti "ok" setelah konteks PDF bisa masuk jalur direct text-send, bukan jalur kirim dokumen.
- **SOP latest bisa mencabut media tools.** Live log run `5fea0047` menunjukkan `agent_tool_setup.sop_locked_tools_removed` karena latest operating manual agent masih `needs_review`. Walaupun config agent punya `whatsapp_media`, runtime tetap menghapus tool media berdasarkan SOP latest.
- **Guard klaim attachment kurang ketat.** Kalimat seperti "Saya kirim file PDF-nya ke WhatsApp sekarang" belum dianggap klaim media delivery, sehingga bisa lolos lewat `send_to_number` sebagai teks biasa.
- **Path file sandbox belum durable untuk turn berikutnya.** PDF dibuat di `/workspace/shared/...pdf`, tapi path itu belum dipersist sebagai artifact session. Saat user lanjut bilang "kirim sekarang/kirim file pdf", runtime tidak punya recovery path deterministik untuk langsung memanggil `send_whatsapp_document`.

### Perubahan Utama
- `agent_step_utils.py`: `_operator_message_payload` sekarang membersihkan envelope `<OWNER>` seperti `<OPERATOR>`, sehingga isi pesan owner saja yang dipakai untuk deteksi direct-send.
- `agent_runner.py`: menambahkan penyimpanan `latest_shared_artifact` dan `shared_artifacts` di `session.metadata_`, recovery path dari history, pencocokan ekstensi file yang diminta, dan fast-path deterministik untuk mengirim file WhatsApp dari artifact sebelum model masuk loop.
- `agent_runner.py`: setelah graph selesai membuat artifact `/workspace/shared`, runtime mencatat artifact; jika request memang minta kirim file dan media tool tersedia, runtime langsung memanggil tool dokumen/gambar, bukan mengandalkan LLM mengarang balasan.
- `escalation_tool.py`: media-claim guard diperluas untuk frasa "saya kirim file", "saya kirim pdf", "berikut saya kirimkan", "cek attachment/lampiran", dan variasi terkait supaya `send_to_number` tidak boleh mengklaim attachment.
- `builder_intent.py`: workflow "laporan PDF", "visualisasi data", "grafik", dan "chart" dikenali sebagai generated-file/file-delivery workflow, jadi Arthur tidak membuat agent dengan config yang melemahkan sandbox/subagent/media requirement.

### Validasi
- Focused regression:
  `PYTHONPATH=. .venv/bin/python -m pytest tests/test_whatsapp_direct_send.py tests/test_whatsapp_progress.py tests/test_sop_runtime_gate.py tests/test_builder_tools.py -q`
- Hasil: **194 passed**.
- Tambahan case menutup bug utama:
  - `<OWNER>... Pesan: ok` setelah konteks PDF tidak lagi dianggap direct text-send.
  - frasa "Saya kirim file PDF-nya..." diblok sebagai klaim palsu kalau belum lewat media tool.
  - artifact `/workspace/shared/*.pdf` bisa disimpan dan dikirim ulang dari history/session saat user minta "kirim file pdf".
  - visualisasi data/laporan PDF dihitung sebagai workflow file generated.

### Catatan Operasional
- Catatan lama: sebelumnya latest `agent_operating_manuals` yang masih `needs_review` bisa membuat runtime mencabut `whatsapp_media`. Per 2026-06-11, SOP gate tidak lagi mencabut tool media WhatsApp; SOP tetap penting untuk readiness/launch review, bukan untuk ketersediaan attachment tool.
- Setelah deploy, test manual yang paling representatif: minta agent buat PDF, tunggu file selesai, lalu kirim "kirim file pdf" atau "kirim sekarang". Expected: ada tool media `send_whatsapp_document` dan user menerima attachment, bukan hanya teks klaim terkirim.
- Jangan klaim "file sudah terkirim" dari layer agent kecuali ada eksekusi tool media atau deterministic delivery path yang benar-benar menghasilkan send attempt.

## 2026-06-05 — Builder Stall, Tier/Update Block & Eskalasi Operator (4 Fix)

Lanjutan testing pra-launch (DB sudah di-wipe bersih + Arthur di-seed ulang, model `gpt-4.1-mini`). Empat bug ditemukan saat user bikin & iterasi agent "Admin Laundry Kiloan" lewat nomor demo (wa-dev-service).

### Masalah & Fix
- **`fac0d66` — Arthur mandek setelah `plan_agent`.** Auto-continue `_needs_builder_create_completion` bail karena cek entitlement-nya naif: `"entitlement" in result_text` ikut match field sukses `creation_entitlement_check` di output `plan_agent`. Akibatnya auto-continue tak nyala → stall guard keluarin "Maaf, kendala sistem, coba lagi". Fix: deteksi blok asli lewat field terstruktur (`creation_entitlement_check.checked && !allowed`), bukan substring.
- **`519b8a7` — Update agent diblok "upgrade Tier 2".** User Trial (1/1 agent) minta perbaiki agent existing; Arthur ikut panggil `plan_agent` (planner agent BARU) → cek limit-create gagal → salah nyuruh upgrade. Padahal update existing tak kena limit jumlah, dan eskalasi/whatsapp_media tidak di-gate tier. Fix: kalau blok karena **jumlah agent**, `plan_agent.next_action` arahkan Arthur ke `update_agent`, bukan pitch upgrade.
- **`2e46836` — Balasan operator tak diteruskan ke customer.** Operator reply pesan eskalasi, tapi agent (gpt-4.1-mini) cuma jawab "Baik, saya catat" — forwarding bergantung LLM bikin draft. Fix gabungan: (1) deterministik — `_maybe_stage_operator_text_draft` di `channels.py` langsung stage teks operator jadi draft (konfirmasi `kirim`) saat reply quote eskalasi yang resolve ke customer; (2) instruksi mode-operator di `prompt_builder.py` untuk path yang masih lewat agent.
- **`1e219f4` — Case tak tertutup + caption media hilang.** (a) Setelah balasan terkirim, reply lagi ke notifikasi lama re-draft → sekarang case ditandai `escalation_replied_case` dan re-reply ditolak ("kasus sudah ditutup"). (b) Regresi dari `2e46836`: flow media masuk lagi dengan prompt internal `[OPERATOR_MEDIA_PENDING]` yang ke-stage sebagai draft → agent tak bikin caption. Fix: draft-deterministik skip prompt internal itu agar agent compose caption seperti semula.

### Tier feature matrix (referensi)
Trial: 1 agent / 2jt token / no subagents · Starter: 1 / 10jt / subagents · Pro: 2 / 20jt / +deepseek · Enterprise: ∞ / 100jt / semua model. Yang di-gate: jumlah agent, token, subagents, model. Eskalasi & whatsapp_media TIDAK di-gate.

### Validasi
- TDD untuk tiap fix; test DB-backed di `tests/test_builder_create_completion.py`, `tests/test_plan_agent_update_routing.py`, `tests/test_operator_reply_draft.py`.
- Full suite `tests/`: **733 passed**, 9 skipped. 2 gagal = pre-existing tak terkait (`test_google_mcp_subagent_routing::test_builder_policy_is_not_redirected_by_google_mcp_intent`, `test_whatsapp_spam_escalation::test_operator_activate_reenables_quoted_customer`).

### Sisa follow-up
- Reliability run gagal mid-flight (Google MCP `401` + kemungkinan timeout build) — belum disentuh.
- LID addressing wa-dev (customer `user_phone` = `...@lid`): pengiriman pakai LID; perlu dicek kalau ada kasus media `kirim` yang gagal sampai.

## 2026-06-05 — Task Hilang & Halusinasi Portofolio (3 Fix)

### Masalah yang Ditemukan
- User (PA Bagas, WA `62895619356936`) malam 06-04 jam 22:11 minta agent bikin **web dimsum**. Agent membalas progress notice "Masih saya proses ya", lalu task **hilang**. Pagi 06-05 user bilang "lanjut yg pembuatan web" → agent malah bikin **landing page portofolio**.
- Investigasi DB (sesi `e5805345`): timeline loncat 22:10:46 → 08:34, pesan dimsum 22:11 **nol** di tabel `messages`, dan **nol** row di `runs` jam 22:11. Konfirmasi 1 nomor + 1 agent = 1 sesi (`wa_helpers.py` get-or-create by `normalize_phone`), jadi task memang masuk ke agent ini tapi tak tersimpan.
- **Akar 1 (data loss):** pesan masuk user di-`flush` (bukan `commit`) di `agent_runner.run_agent` (`§7`). Caller WA (`channels.py:1133-1145`) `db.rollback()` saat run cancel/timeout/error → pesan + run record yang belum commit **terhapus**. Progress notice WA = side-effect non-transaksional → bertahan, bikin user kira task diproses.
- **Akar 2 (halusinasi):** waktu pagi "lanjut web" tak ada grounding di history (sudah keburu hilang), prompt subagent (`prompt_builder.py:364`) punya **contoh few-shot konkret** "landing page portfolio untuk Bagas, section About & Projects" → model menyalinnya verbatim (task args persis sama). Diperparah hard-rule "langsung panggil task(), dilarang nulis teks" → agent tak boleh nanya klarifikasi.
- Run 21:59 sudah nunjukin Google MCP `401 Unauthorized` → infra rapuh, kemungkinan pemicu kegagalan run 22:11.

### Perubahan Utama (3 commit)
- **`369c34d` — anti-halusinasi prompt** (`prompt_builder.py`): contoh portofolio-Bagas yang bisa disalin → placeholder generik; tambah blok **"ANTI-HALUSINASI TASK"** (isi task wajib dari pesan/history, bukan contoh prompt; "lanjut X" tanpa jejak di history → wajib klarifikasi, bukan menebak).
- **`541f452` — pesan masuk durable** (`agent_runner.py`): helper `_persist_inbound_user_message()` `commit` pesan user **sebelum** graph jalan. Rollback caller tak bisa lagi menghapus request. Aman karena sessionmaker `expire_on_commit=False`.
- **`eebf382` — jejak gagal + recovery** (`agent_runner.py`): helper `_persist_run_failure()` set status terminal + `commit`, dipakai di 3 jalur raising (CancelledError, TimeoutError, Exception umum) yang dulu `flush`+`raise` → trace hilang. Jalur Exception umum sekarang juga kirim recovery message ke user (dulu diam → HTTP 500).

### Validasi
- TDD, test DB-backed: `tests/test_inbound_message_durability.py` (flush-only hilang saat rollback → commit bertahan, untuk pesan & status run), `tests/test_subagent_task_grounding.py` (no contoh portofolio + ada aturan klarifikasi).
- Full suite `tests/`: **724 passed**, 9 skipped. 2 gagal = pre-existing tak terkait (`test_google_mcp_subagent_routing::test_builder_policy_is_not_redirected_by_google_mcp_intent`, `test_whatsapp_spam_escalation::test_operator_activate_reenables_quoted_customer`; gagal juga saat perubahan di-stash).

### Sisa Follow-up (belum dikerjakan)
- Reliability: kenapa run 22:11 gagal di awal (Google MCP `401` + kemungkinan timeout build web lambat). Dampak sudah jauh berkurang (task tak hilang, user dikabari, ada jejak `runs.status`), tapi run-nya sendiri masih bisa gagal di tengah.

## 2026-06-04 — Fix Latency Arthur (Revert Model Reasoning)

### Masalah yang Ditemukan
- Setelah kerjaan terakhir, Arthur jadi sangat lambat memproses pesan — bahkan untuk request sepele seperti minta kode trial WA ke nomor demo.
- Investigasi: BUKAN karena refactor `builder_tools` (itu behavior-preserving, 264 test pass). Penyebabnya perubahan config yang ikut ke-bundle di working tree yang sama:
  - `scripts/seed_arthur.py`: model Arthur `openai/gpt-4.1-mini` → `deepseek/deepseek-v4-flash` (model reasoning) dan `max_tokens` 2048 → 60000.
- Mekanisme lambat: `agent_llm.py:20` meneruskan `max_tokens` apa adanya ke `ChatOpenAI` tanpa rem; model reasoning emit reasoning trace tiap step (lihat `reasoning_tokens` di `agent_callbacks.py`); prompt builder baru mewajibkan "klasifikasi kategori sebelum tiap tool call" → reasoning ekstra tiap step. Tiap langkah agent loop jadi jauh lebih lama dari gpt-4.1-mini.

### Perubahan Utama
- Revert model Arthur ke `openai/gpt-4.1-mini` + `max_tokens` 2048 di `scripts/seed_arthur.py`.
- Sinkronkan baris "Model Arthur sendiri" di `system-message-builder.md` ke `gpt-4.1-mini`.
- Update assertion test `tests/test_agent_builder_phase4.py::test_rulebook_uses_current_arthur_model` mengikuti revert.
- Model writer untuk blueprint/instructions/manual/soul (`deepseek/deepseek-v4-pro`) TETAP — itu panggilan one-shot, bukan loop per-step. Prinsip: loop runtime pakai model cepat non-reasoning; reasoning hanya untuk tugas berat one-shot (writer/subagent).

### Catatan Operasional
- Perubahan ada di `seed_arthur.py`, jadi Arthur di DB baru berubah setelah `PYTHONPATH=. .venv/bin/python scripts/seed_arthur.py` dijalankan ulang (sudah dilakukan). Config agent dibaca dari DB tiap run, jadi efektif tanpa perlu nunggu; restart backend hanya bila ada cache config.

## 2026-06-04 — Agent Runner Guard/Middleware Extraction (Fase 3)

### Masalah yang Ditemukan
- `app/core/engine/agent_runner.py` membengkak ke 3118 baris: orchestration `run_agent` bercampur dengan puluhan guard/detector lintas-domain (Google routing, WhatsApp direct-send, reply guard task/escalation, deploy/file/builder-create followup) plus 2 middleware LangGraph.
- Banyak test (≈34 symbol private) mengimpor langsung dari `agent_runner`, jadi refactor harus mempertahankan semua symbol tetap importable dari `agent_runner` (facade).

### Perubahan Utama
- Ekstraksi guard/detector/middleware ke modul per-domain, `agent_runner.py` tetap facade re-export:
  - `agent_step_utils.py` — leaf bebas-dependency: `_parse_step_result_json`, `_operator_message_payload`, `_is_operator_envelope`, `_URL_RE`, `_has_whatsapp_media_send_step` (helper lintas-domain ditaruh di leaf agar graph import mengarah ke bawah, bukan sibling-to-sibling).
  - `agent_identity.py` — resolusi phone/owner/session.
  - `agent_google_routing.py` — routing/runtime Google Workspace + builder auth-link.
  - `agent_whatsapp_guards.py` — 13 detector/guard direct-send WhatsApp + konstanta privatnya.
  - `agent_reply_guards.py` — `_task_result_guard_reply`, `_operator_escalation_reply_guard`.
  - `agent_followups.py` — 15 detector deploy/file-delivery/builder-create/website.
  - `agent_middleware.py` — `BlockTaskToolMiddleware`, `ExternalServiceFallbackGuardMiddleware`.
- Pure refactor: nol perubahan behavior/prompt/model/config. Semua kode dipindah verbatim.
- Task 8 (dekomposisi body `run_agent`) dikerjakan SEBAGIAN: hanya `_pre_run_quota_gate(...)` diekstrak (helper module-level di `agent_runner` supaya patch target test tetap berlaku). Sisa body post-graph SENGAJA dibiarkan inline karena bukan blok bersih — ada closure `_apply_run_usage` + ~12 titik early-return terjalin di cabang deploy/followup/guard; mengekstraknya = risiko ubah behavior. Dicatat sebagai follow-up di plan.
- `agent_runner.py` turun dari 3118 → 1980 baris (−36%); 7 modul per-domain baru. Body `run_agent` masih ~1747 baris (didominasi state machine post-graph yang ditunda).

### Validasi
- Re-export contract (34 symbol) → `REEXPORT OK` setelah tiap task.
- Full regression suite tetap identik baseline: **286 passed + 1 failure pre-existing** (`tests/test_google_mcp_subagent_routing.py::test_builder_policy_is_not_redirected_by_google_mcp_intent`, sudah merah sebelum refactor di `fc4d1af`, di luar scope agent_runner).
- `import app.main` → OK.
- 12 commit kecil (`3f7f09a..72c7cb3`), tiap task: pindah verbatim → re-export contract → test gate → commit.
- Plan: `docs/superpowers/plans/2026-06-04-agent-runner-refactor-plan.md`.

## 2026-06-04 — Arthur Builder Tools Refactor dari 3000+ Baris ke Facade Modular

### Masalah yang Ditemukan
- `app/core/tools/builder_tools.py` sudah menjadi file ribuan baris yang mencampur katalog preset, policy/ownership helper, writer tool, create/update agent, channel trial, connector Google, validation, verify, dan agent management.
- Ukuran file membuat fixing Arthur berisiko karena perubahan kecil mudah menyentuh area yang tidak terkait.

### Perubahan Utama
- `builder_tools.py` dipertahankan sebagai facade `build_builder_tools(...)`, sementara logic dipindah per kategori ke module kecil:
  `builder_catalog.py`, `builder_identity.py`, `builder_google.py`, `builder_json.py`, `builder_intent.py`, `builder_fallbacks.py`, `builder_read_tools.py`, `builder_user_tools.py`, `builder_planning_tools.py`, `builder_blueprint_tools.py`, `builder_manual_tools.py`, `builder_instruction_tools.py`, `builder_soul_tools.py`, `builder_verify_tools.py`, `builder_validation_tools.py`, `builder_management_tools.py`, `builder_connector_tools.py`, `builder_channel_tools.py`, `builder_create_tools.py`, `builder_update_tools.py`, dan `builder_runtime_text.py`.
- Public entrypoint, nama tool Arthur, urutan tool, dan compatibility import lama tetap dijaga.
- `create_agent` dan `update_agent` dipisahkan ke factory sendiri dengan dependency eksplisit, tapi patch seam lama untuk `Agent`, writer, logger, dan settings tetap kompatibel.
- Ukuran file setelah refactor:
  - `app/core/tools/builder_tools.py` turun menjadi 401 baris.
  - Logic create/update masih terbesar, tapi sudah terisolasi: `builder_create_tools.py` 659 baris dan `builder_update_tools.py` 547 baris.

### Validasi
- `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_builder_tools.py` → 117 passed.
- Focused builder/deploy suite → 193 passed.
- Broad Arthur/WA/MCP regression suite → 264 passed.
- Smoke test khusus flow Arthur bikin agent:
  - compose blueprint dan instructions;
  - validate config sebelum create;
  - `create_agent` dengan owner metadata, SOP/operating manual, Google instruction append, dan policy blocker;
  - WA dev trial fallback;
  - verify readiness sebelum launch;
  - hasil: 45 passed.
- Smoke test runtime/prompt Arthur builder:
  - Arthur paham CRUD agent sebagai tugas utama;
  - prompt mencegah loop pertanyaan "lanjut/continue";
  - kategori tool terdokumentasi;
  - pipeline validate -> create -> list -> get detail -> update tetap jalan;
  - tenant isolation tetap memblokir akses agent user lain;
  - builder runtime tetap skip sandbox/subagent walau config drift;
  - hasil: 21 passed.
- Regression suite terakhir:
  - `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_builder_tools.py tests/test_agent_builder_phase1.py tests/test_agent_builder_phase2.py tests/test_agent_builder_phase4.py tests/test_agent_quota_service.py tests/test_whatsapp_progress.py tests/test_whatsapp_direct_send.py tests/test_tool_call_orchestration.py tests/test_mcp_tool_priority.py`
  - hasil: 264 passed, 1 warning existing terkait `asyncio.get_event_loop()` di test helper.

### Penilaian Setelah Refactor
- Arthur untuk bikin agent sudah sehat di deterministic local/regression test.
- Refactor membuat boundary tool Arthur jauh lebih jelas: user management, agent builder, integration/connector, channel/WhatsApp, validation, verify, create/update, dan agent management tidak lagi numpuk di satu script raksasa.
- Blast radius fixing berikutnya jauh lebih kecil karena perubahan bisa diarahkan ke module sesuai kategori.
- Sisa risiko teknis: `builder_create_tools.py` dan `builder_update_tools.py` masih cukup panjang, sehingga nanti masih layak dipecah lagi kalau area create/update makin sering berubah.
- Google live service tidak dites karena service memang belum aktif/tidak merespons. WhatsApp real send production juga tidak dipaksa; validasi saat ini adalah local deterministic test dan regression suite.

## 2026-06-03 — Arthur Builder SOP Semantic, Bahasa Fleksibel, dan Tier Check Awal

### Masalah yang Ditemukan
- Arthur masih bisa mengecek tier/quota terlambat, yaitu baru saat `create_agent`, sehingga user menunggu proses blueprint/instructions dulu sebelum tahu paketnya tidak cukup.
- Bahasa agent buatan Arthur terlalu diasumsikan Bahasa Indonesia, padahal user bisa memakai bahasa lain atau campur bahasa.
- SOP agent buatan Arthur terlalu mudah jatuh ke template yang salah karena sinyal keyword generik seperti `data`, `barang`, `stok`, `harga`, `delivery`, atau `pembayaran`.
- Untuk real case usaha persiapan acara, Arthur sempat salah mendeteksi sebagai data analyst/ecommerce/F&B atau payment-delivery workflow, padahal kebutuhan user adalah intake event, cek harga/stok ke owner, lalu follow-up.
- Instruction writer bisa mengembalikan output kosong atau mengarang nama brand bisnis seperti `FixEvent`; keduanya berisiko membuat agent tidak sesuai kebutuhan user.

### Perubahan Utama
- `plan_agent` sekarang melakukan preview entitlement di awal lewat subscription user: slot agent, limit paket, model, fitur Google, subagent, dan WhatsApp dicek sebelum Arthur lanjut compose/create.
- Prompt Arthur di `system-message-builder.md` diarahkan memanggil `get_user_subscription()` sebelum `plan_agent`, lalu berhenti jika `plan_status=blocked_by_subscription`.
- Preset dan runtime prompt sekarang memakai aturan bahasa fleksibel: ikuti bahasa user, default Indonesia hanya jika bahasa user tidak jelas.
- Ditambahkan `compose_agent_operating_manual()` dan SOP/Agent Operating Manual terpisah dari instructions; runtime agent menerima `## SOP Workflow Detail` dari operating manual.
- `create_agent` otomatis membuat/menyimpan operating manual dari blueprint atau semantic context jika Arthur tidak mengirim manual eksplisit.
- Domain SOP sekarang punya jalur semantic untuk konteks event/local service, termasuk template `event_service`; konteks acara tidak lagi jatuh ke ecommerce hanya karena ada kata barang/harga/stok.
- Deteksi payment approval diperketat: DP/pelunasan sebagai policy bisnis biasa tidak lagi memaksa state `waiting_payment -> payment_review -> approved -> delivery`.
- Writer output kosong sekarang fallback ke deterministic instructions, bukan dianggap valid.
- Guard brand hallucination ditambahkan di instructions/soul/create: jika user tidak menyebut nama bisnis eksplisit, agent memakai "bisnis ini/usaha ini" dan tidak mengarang brand.
- Arthur builder tanpa subagent tetap memakai timeout yang cukup untuk flow create, tetapi tidak sepanjang flow subagent/deploy.

### Validasi
- `PYTHONPATH=. .venv/bin/python -m pytest tests/test_builder_tools.py tests/test_session_lock_and_history.py -q` -> 148 passed.
- E2E Arthur dengan user awam membuat agent `Event Helper Final`:
  - step pertama `get_user_subscription`;
  - preset `cs_whatsapp_basic`;
  - Google/MCP off karena user bilang "tanpa Google";
  - sandbox/deploy/subagents off;
  - operating manual source `arthur_operating_manual_writer_auto`;
  - domain SOP event-specific, bukan ecommerce/F&B;
  - tidak ada brand karangan.
- E2E agent buatan Arthur:
  - customer minta harga dan kepastian barang untuk acara ulang tahun;
  - agent menyimpan konteks ke memory;
  - agent memanggil `escalate_to_human` sebelum reply;
  - agent tidak mengarang harga, stok, atau keputusan final.

## 2026-06-02 — Customer Google Blocker Notify Owner dan Arthur Builder Timeout

### Masalah yang Ditemukan
- Agent operasional WhatsApp bisa membalas customer dengan detail internal saat Google Calendar/Workspace gagal, termasuk menyebut koneksi akun dan nomor admin/operator.
- Nomor operator dari `escalation_config.operator_phone` masih diinjeksi ke prompt customer biasa, sehingga model punya bahan untuk membocorkan nomor itu.
- Arthur builder tanpa subagent masih mendapat timeout/recursion ceiling 8x agent normal; dengan default 300 detik, flow create agent bisa menunggu jauh di atas 10 menit sebelum timeout.
- `create_agent` masih menyarankan `compose_agent_soul` setelah create jika soul belum tersimpan, yang bisa menambah roundtrip LLM pasca-create dan membuat WhatsApp terasa menggantung.

### Perubahan Utama
- `run_agent` sekarang merutekan blocker Google/Calendar pada sesi WhatsApp customer sebagai insiden internal: kirim notifikasi ke Owner lewat WhatsApp device agent, sertakan error ringkas dan link reconnect Google jika ada.
- Reply ke customer untuk blocker Google/Calendar diganti menjadi aman: data dicatat, Owner sudah/perlu mengecek sistem penjadwalan, tanpa nomor admin, link auth, atau istilah Google Workspace.
- Prompt runtime customer tidak lagi mengekspos nomor operator/admin; operator/Owner tetap mendapat konteks identitas saat mereka sendiri yang chat agent.
- Arthur builder tanpa subagent dibatasi ke recursion limit lebih kecil dan timeout maksimal 540 detik; flow subagent/deploy tetap mendapat ceiling panjang.
- `_call_instruction_writer` untuk blueprint/instructions/soul sekarang punya timeout 45 detik per panggilan dan tidak retry jika timeout.
- `create_agent` tidak lagi mendorong `compose_agent_soul` sebagai langkah wajib setelah create; soul pasca-create dibuat opsional agar flow create bisa selesai lebih cepat.

### Validasi
- `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_google_mcp_reply_overrides.py tests/test_whatsapp_direct_send.py tests/test_builder_tools.py::TestUpdateAgent tests/test_memory_service.py` -> 85 passed.
- `git diff --check -- app/core/engine/agent_runner.py app/core/engine/prompt_builder.py app/core/tools/builder_tools.py tests/test_google_mcp_reply_overrides.py tests/test_whatsapp_direct_send.py docs/recap.md` -> clean.
- `git diff --check` global masih melaporkan `.gitignore:84: new blank line at EOF` dari working tree yang tidak terkait bugfix ini.

## 2026-06-02 — Versioned Selective Memory Refresh untuk Update Agent Arthur

### Masalah yang Ditemukan
- Update agent lewat Arthur langsung mengubah `instructions`/`tools_config`, tetapi memory lama seperti `soul` dan `agent_blueprint` masih bisa ikut masuk prompt.
- Wipe total memory terlalu destruktif karena bisa menghapus konteks lama yang masih berguna dan menyulitkan debugging.

### Perubahan Utama
- Ditambahkan plan `Versioned Selective Memory Refresh` di `Dokumentasi Arsitektur/arthur-owner-runtime-injection-plan.md`.
- Runtime memory sekarang membaca `agent_context_version` dan memprioritaskan `soul:vN` jika tersedia; fallback legacy `soul` tetap dipakai kalau versi aktif belum ada.
- `build_memory_context` tidak lagi memasukkan arsip versi seperti `soul:vN`, `agent_blueprint:vN`, dan `setup_summary:vN` ke prompt long-term memory biasa.
- `update_agent` mendapat `refresh_memory_mode = "none" | "selective" | "major"` dengan default `selective`.
- Untuk update workflow/persona/SOP/tools/escalation/integrasi, `update_agent` menulis `soul:vN`, `agent_blueprint:vN`, `setup_summary:vN`, dan `agent_context_version`.
- Arthur Builder Mode diarahkan memakai default selective untuk update besar, `none` untuk update kecil seperti rename, dan tidak melakukan wipe memory lama.

### Validasi
- `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_memory_service.py tests/test_builder_tools.py::TestUpdateAgent tests/test_whatsapp_direct_send.py::test_builder_prompt_blocks_repeated_continue_questions` -> 16 passed.
- `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_tool_capability_registry.py tests/test_reply_guard.py tests/test_whatsapp_direct_send.py tests/test_google_mcp_reply_overrides.py tests/test_builder_tools.py tests/test_agent_builder_phase4.py tests/test_agent_created_by_audit.py tests/test_wa_identity.py tests/test_memory_service.py` -> 230 passed.

## 2026-06-02 — Arthur Setup Status, RAG Readiness, dan Audit Launch Categories

### Masalah yang Ditemukan
- `verify_agent` belum mengecek kondisi knowledge base/RAG: agent bisa punya RAG aktif tapi dokumen kosong, sehingga berisiko mengklaim menjawab berdasarkan dokumen yang belum ada.
- Arthur belum punya field status setup yang siap dibacakan ke Owner dengan bahasa non-teknis; output readiness masih lebih cocok untuk engineer.
- Script audit existing agents masih fokus ke `created_by_*`, belum menghasilkan kategori launch readiness seperti ready, needs fix, dan needs manual review.

### Perubahan Utama
- `verify_agent` sekarang menghitung dokumen agent saat RAG aktif dan memblokir launch dengan `rag_documents_required` kalau dokumen masih kosong.
- Output `verify_agent` mendapat `setup_status_for_owner`: ringkasan awam, item status setup, dan next steps untuk Owner.
- Arthur Builder Mode diarahkan memakai `setup_status_for_owner` sebagai sumber kebenaran saat menjelaskan status setup, bukan raw blockers/warnings.
- `scripts/audit_agent_created_by_metadata.py` sekarang menambahkan readiness category per agent: `ready`, `needs_fix`, atau `needs_manual_review`, termasuk blocker owner, Google auth, RAG docs, WhatsApp setup, dan escalation.
- System/builder agent dikecualikan dari kewajiban owner di audit existing agents.

### Validasi
- `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_builder_tools.py::TestVerifyAgentReadiness tests/test_agent_created_by_audit.py tests/test_whatsapp_direct_send.py::test_builder_prompt_blocks_repeated_continue_questions` -> 17 passed.
- `PYTHONPATH=. .venv/bin/python scripts/audit_agent_created_by_metadata.py --json` -> total 12 agent; readiness: 1 ready, 8 needs_fix, 3 needs_manual_review.

## 2026-05-29 — Arthur Owner Runtime Contract, Created-by Metadata, dan Tool Truth Registry

### Masalah yang Ditemukan
- Arthur dan agent hasil buatannya masih terlalu bergantung pada `instructions`/`soul` buatan LLM untuk fakta platform seperti Owner, superadmin, created-by, dan tools aktif.
- Agent bisa berisiko mengklaim kemampuan yang tidak aktif, misalnya mengirim file WhatsApp, menjalankan kode, deploy, atau membuat Google Docs, hanya karena teks instructions menyebut kemampuan itu.
- Agent lama belum punya metadata `created_by_*`, sehingga runtime tidak punya sumber DB yang jelas untuk menyatakan agent dibuat oleh Arthur/platform/API.
- `verify_agent` belum cukup kuat sebagai launch readiness gate untuk owner, Google auth, workflow payment/admin approval, channel WhatsApp, dan metadata platform.
- Audit existing agent belum bisa membedakan agent yang metadata source-nya aman dibackfill dari agent yang perlu manual review.

### Perubahan Utama
- Ditambahkan `PlatformRuntimeContract` di prompt runtime untuk inject Owner/superadmin, current user role, created-by metadata, dan runtime tool contract setiap run.
- Runtime prompt sekarang tetap menyatakan Owner sebagai bos/superadmin walaupun `soul` kosong atau generated instructions salah.
- Google Workspace runtime sekarang inject state eksplisit: disabled, enabled-needs-auth, connected, auth/error, dan unknown-auth, sehingga agent diarahkan minta Owner login/re-auth saat belum valid.
- Model `Agent` mendapat metadata DB baru:
  - `created_by_type`
  - `created_by_agent_id`
  - `created_by_agent_name`
- Alembic migration `017_agent_created_by_metadata.py` ditambahkan sebagai merge head dari `015` dan `016`; setelah `alembic upgrade head`, head Alembic menjadi `017`.
- Arthur `create_agent` sekarang menyimpan `created_by_type="arthur_builder"`, `created_by_agent_name="Arthur"`, dan `created_by_agent_id` jika tersedia.
- Agent manual/API diberi `created_by_type="api"` saat dibuat lewat endpoint API; seed Arthur/system agents mengisi `created_by_type="system"`.
- `get_agent_detail`, `list_my_agents`, dan `verify_agent` sekarang expose metadata/readiness agar Arthur bisa melihat kondisi launch agent tanpa menebak.
- Ditambahkan script `scripts/audit_agent_created_by_metadata.py`:
  - default read-only audit;
  - `--apply` hanya backfill high-confidence;
  - `--json` untuk output machine-readable.
- Audit DB aktual menemukan 12 agent: Arthur dibackfill high-confidence sebagai `system`, 11 agent lain ditandai manual review karena tidak ada bukti reliable untuk menebak source.
- Ditambahkan `tool_capability_registry.py` sebagai source of truth capability: label user-facing, enabled condition, disabled reason, fallback sentence, dan claim patterns.
- `prompt_builder` sekarang memakai tool truth registry untuk Runtime Tool Contract, bukan daftar capability ad hoc.
- `reply_guard` sekarang menerima `tools_config` dan `active_groups`, lalu rewrite klaim palsu untuk high-risk capability yang disabled, seperti WhatsApp Media, Sandbox, Deploy, Scheduler, RAG, Escalation, dan Google Workspace.

### Validasi
- `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_tool_capability_registry.py tests/test_reply_guard.py tests/test_whatsapp_direct_send.py tests/test_google_mcp_reply_overrides.py tests/test_builder_tools.py tests/test_agent_builder_phase4.py tests/test_agent_created_by_audit.py` -> 210 passed.
- `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_wa_identity.py` -> 6 passed.
- `PYTHONPATH=. .venv/bin/alembic heads` -> `017 (head)`.
- `PYTHONPATH=. .venv/bin/python scripts/audit_agent_created_by_metadata.py --apply` -> updated 1 high-confidence agent (`Arthur -> system`), 11 manual review.
- `git diff --check` clean untuk file yang disentuh.

### Sisa Berikutnya
- Tambah RAG/doc readiness state: RAG aktif tapi belum ada dokumen harus meminta Owner upload dokumen, bukan mengklaim "berdasarkan dokumen".
- Perluas launch readiness/audit existing agents agar mencakup owner, created-by, tools, Google auth, WhatsApp device, RAG docs, dan escalation workflow dalam satu laporan.

## 2026-05-29 — Arthur Builder Proactive Setup dan Final Reply Delivery Trace

### Masalah yang Ditemukan
- Arthur bisa membuat atau mengedit agent dengan benar, tetapi jawaban WhatsApp tetap membuat user bingung karena hanya menyebut "sudah jadi/sudah diedit" tanpa langkah berikutnya.
- Untuk agent jasa CV, Arthur belum cukup tegas memasukkan alur bayar dulu, bukti transfer ke admin, approval admin, lalu delivery CV.
- Log `agent_step.llm_response` bisa menunjukkan jawaban final yang lengkap, tetapi `agent_run.complete reply_len` lebih pendek karena `ensure_non_empty_reply()` mengganti final reply substantif menjadi fallback pendek setelah `update_agent`.
- Jalur WA belum punya log eksplisit untuk teks final yang benar-benar dikirim, sehingga sulit membandingkan LLM final, guard result, dan delivery WhatsApp.

### Perubahan Utama
- Prompt runtime Arthur dan `system-message-builder.md` sekarang menekankan builder proaktif: siapkan cara test, pasang WhatsApp, login Google, dan next step setelah create/update.
- Rule workflow bisnis diperjelas supaya payment, bukti transfer, admin approval, eskalasi, dan delivery tidak hilang dari instruksi agent.
- Planner CV service sekarang mengaktifkan kemampuan file generation/media (`sandbox`, `whatsapp_media`, `subagents`) untuk kebutuhan membuat dan mengirim CV/dokumen.
- Reply guard sekarang menganggap frasa seperti "sudah saya perbarui" sebagai final reply sukses yang jelas, sehingga tidak mengganti jawaban lengkap Arthur dengan fallback pendek.
- `agent_runner` mencatat `agent_run.final_reply_overridden_by_non_empty_guard` jika guard mengubah final reply, dan `/v1/channels/wa/incoming` mencatat `wa_incoming.final_reply_sent` berisi panjang serta preview teks yang benar-benar dikirim ke WA.

### Validasi
- `PYTHONPATH=. .venv/bin/python -m pytest tests/test_reply_guard.py tests/test_whatsapp_direct_send.py::test_builder_prompt_blocks_repeated_continue_questions tests/test_builder_tools.py::TestBuilderToolsReturnsList::test_cv_service_agent_enables_file_generation_and_media_tools tests/test_builder_tools.py::TestBuilderToolsReturnsList::test_travel_planning_request_uses_personal_assistant_not_faq tests/test_builder_tools.py::TestBuilderToolsReturnsList::test_explicit_google_calendar_request_enables_google_workspace tests/test_builder_tools.py::TestBuilderToolsReturnsList::test_existing_google_form_order_link_does_not_enable_workspace` -> 19 passed, 1 warning.

## 2026-05-28 — Arthur Builder Quota Exemption

### Masalah yang Ditemukan
- Arthur adalah platform agent builder, tetapi masih melewati gate quota yang sama seperti agent customer biasa.
- Jika quota token agent atau subscription owner habis, Arthur bisa ikut terblokir dan user tidak bisa membuat, memperbaiki, atau menyiapkan agent.
- Usage token Arthur juga masih berpotensi tercatat ke usage agent/owner, padahal Arthur harus diperlakukan sebagai infrastruktur builder.
- Arthur masih bisa salah memperlakukan link Google Form existing dari user sebagai request Google Workspace, lalu reply guard mengganti jawaban normal menjadi error "tool Google belum terpanggil".
- Pada flow edit agent, Arthur masih bisa membalas janji progress seperti "langsung aku betulin" atau meminta placeholder, bukan langsung menjalankan update agent.

### Perubahan Utama
- `app/core/domain/agent_quota_service.py` sekarang mengenali builder/system agent lewat `capabilities=["builder"|"system"]` atau `tools_config.builder=True`.
- Builder agent langsung lolos dari `check_agent_quota()`, termasuk limit token agent, masa aktif agent, status subscription owner, dan limit token subscription owner.
- `record_agent_token_usage()` tidak lagi menambah `tokens_used` agent maupun owner subscription untuk builder agent.
- Agent customer biasa tetap memakai enforcement quota yang sama seperti sebelumnya.
- Google Workspace intent detector sekarang mengabaikan Google Form link yang diberikan sebagai info order/customer flow, sehingga link `forms.gle` existing tidak memicu override "Google tool belum dieksekusi".
- Prompt Arthur diperketat untuk kode trial, edit agent existing, kemampuan baca file Excel/WhatsApp media, dan larangan meminta placeholder yang tidak perlu.
- Reply guard builder sekarang mengganti janji progress setelah `update_agent` sukses dengan pesan hasil update yang jelas.

### Validasi
- `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_google_mcp_reply_overrides.py tests/test_whatsapp_direct_send.py tests/test_reply_guard.py tests/test_agent_quota_service.py --maxfail=1` -> 68 passed.

## 2026-05-28 — WhatsApp Spam Auto-Disable, Session Concurrency, dan Locust Harness

### Masalah yang Ditemukan
- Spam burst dari nomor WhatsApp yang sama bisa lolos dari auto-disable karena deduplikasi inbound masih berbasis `timestamp` detik, bukan `message_id`.
- Request yang dibatalkan saat spam/interruption bisa berakhir `HTTP 500` karena session DB mencoba `commit()` pada transaksi yang sudah invalid.
- Request yang sudah telanjur menunggu `session_run_lock` masih bisa lanjut menjalankan agent setelah sesi customer di-set `ai_disabled`, sehingga token tetap terbakar.
- Burst request paralel dari sender yang sama bisa membuat beberapa row `sessions` baru sekaligus, sehingga spam counter dan status AI terpecah antar session.
- Repo belum punya harness load test khusus untuk memukul `/v1/channels/wa/incoming` sebagai banyak user sekaligus.

### Perubahan Utama
- `wa-service` dan `wa-dev-service` sekarang meneruskan `message_id` WhatsApp ke backend Python.
- Deduplikasi inbound WA di `app/api/wa_helpers.py` diprioritaskan ke `message_id`, dengan fallback ke key berbasis `timestamp` hanya untuk payload lama.
- Handler `/v1/channels/wa/incoming` melakukan `db.rollback()` eksplisit untuk jalur `cancelled` dan `timeout`, lalu cleanup task memakai `session_id` lokal agar tidak menyentuh ORM object yang sudah expired.
- Setelah lock session didapat, backend sekarang re-check `ai_disabled`; request lama yang terlambat masuk lock langsung berhenti tanpa menjalankan LLM.
- `find_or_create_wa_session()` sekarang memakai `pg_advisory_xact_lock` per `agent + normalized sender` untuk mencegah duplikasi session saat burst paralel.
- Ditambahkan folder `locust-load/` berisi `locustfile.py`, `README.md`, dan `requirements.txt` untuk test normal traffic, probe Arthur, dan spam burst WhatsApp.
- Assertion Locust untuk spam disesuaikan: burst dianggap sukses jika sesi menjadi `ai_disabled`, baik response pertama membawa `reason=spam_auto_disabled` maupun request lanjutan hanya mengembalikan `status=ai_disabled`.

### Validasi
- `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_whatsapp_spam_escalation.py tests/test_whatsapp_direct_send.py tests/test_whatsapp_progress.py --maxfail=1` -> 67 passed.
- `.venv/bin/locust -f locust-load/locustfile.py --host http://127.0.0.1:18000 --headless --users 1 --spawn-rate 1 --run-time 12s --stop-timeout 240 --tags spam --only-summary` -> exit code 0, 24 request, 0 fail.
- Smoke lokal juga menunjukkan batas lingkungan yang nyata: device `b1992dcc-dcfd-49ec-a0d2-ca2068ac0d64` belum ada di `wa-service` lokal, jadi delivery WA nyata masih `send_failed` walaupun backend spam guard sudah bekerja.

## 2026-05-28 — Arthur Builder Guard, Progress Notice, dan WA Dev Delivery Fallback

### Masalah yang Ditemukan
- `update_agent()` bisa crash dengan `UnboundLocalError` saat edit biasa tidak menyentuh Google Workspace karena `google_workspace_enabled` hanya dibuat di cabang tertentu.
- Arthur masih bisa berhenti di tengah flow builder dengan jawaban seperti "soul sudah siap", sehingga user tidak mendapat kepastian agent benar-benar sudah dibuat.
- Request agent untuk buzzer/politik belum diblok di layer tool, jadi prompt saja tidak cukup.
- Progress WhatsApp terlalu lambat dan hanya aktif untuk sebagian tool panjang; flow Arthur builder bisa terlihat diam saat banyak user mengetes bersamaan.
- Pada `wa-dev-service`, Python bisa sudah punya final reply di response API tetapi gagal mengirim balik ke WhatsApp; router Go sebelumnya tidak punya fallback jika Python melaporkan final delivery gagal.

### Perubahan Utama
- `update_agent()` sekarang menginisialisasi `google_workspace_enabled=False` sebelum cabang update apa pun.
- Policy guard deterministik ditambahkan di `plan_agent`, `validate_agent_config`, `create_agent`, dan `update_agent` untuk menolak agent buzzer/politik.
- Prompt Arthur, rulebook seed, dan reply guard diperketat:
  - Arthur dilarang berhenti setelah `compose_agent_soul`.
  - Jika tool `create_agent`/`update_agent` sukses tapi final reply tidak jelas, reply guard membangun pesan sukses natural dari hasil tool.
- Runtime WA menjadwalkan notice proses panjang sejak awal run dan juga untuk tool builder penting, dengan delay default `wa_long_progress_notice_seconds=25`.
- `send_wa_message()` memakai timeout send 30 detik dan retry singkat untuk text send.
- `/v1/channels/wa/incoming` sekarang mengembalikan metadata `reply_delivery`; jika final send gagal, status menjadi `send_failed`.
- `wa-dev-service` membaca status `send_failed` dan mengirim fallback langsung ke `msg.ChatID` supaya response yang sudah dibuat tidak hilang.

### Validasi
- `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_reply_guard.py tests/test_builder_tools.py tests/test_whatsapp_progress.py --maxfail=1` -> 75 passed, 1 warning.
- `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_whatsapp_spam_escalation.py tests/test_whatsapp_direct_send.py --maxfail=1` -> 55 passed.
- `cd wa-dev-service && go test ./...` -> passed.

## 2026-05-26 — Hardening WhatsApp, Arthur Builder, Escalation, Quota, dan Agent Research

### Latar Belakang
- Fokus hari ini adalah menaikkan kualitas SaaS agent builder supaya user non-teknis bisa membuat agent yang benar-benar operasional untuk personal assistant, CS usaha, riset, dan workflow berbasis WhatsApp.
- Bug yang muncul mayoritas ada di area runtime WhatsApp, eskalasi admin, Arthur sebagai builder, quota token, dan agent yang memakai integrasi eksternal seperti Google Docs.
- Target perbaikan: agent tidak boleh asal klaim sukses, tidak boleh salah route pesan, tidak boleh spam hasil, dan harus bisa mengubah konfigurasi agent secara nyata ketika user meminta fitur baru.

### Masalah yang Ditemukan
- Tool `create_wa_dev_trial_link` crash dengan `NameError: default_target is not defined`, sehingga flow Arthur untuk membuat link trial WA bisa gagal.
- Agent coding/deploy bisa mengirim hasil berulang kali di WhatsApp: QR, URL, file `.txt`, lalu pesan final lagi, padahal user hanya meminta link.
- Media atau dokumen dari user/admin di WhatsApp bisa salah dianggap sebagai eskalasi, terutama saat dikirim dari nomor admin atau saat tidak ada quoted escalation message yang valid.
- Reply WhatsApp belum selalu membawa konteks pesan yang dibalas, sehingga agent kehilangan referensi saat user membalas chat lama.
- Eskalasi payment proof tidak stabil: saat admin reply kasus dengan jawaban valid, agent bisa memproses ulang task customer atau membuat ulang output, bukan hanya meneruskan keputusan admin ke customer.
- Interruption/spam message sempat mengirim pesan tambahan seperti “Oke, saya stop proses sebelumnya...”, yang membingungkan customer.
- Tier 3 perlu unlimited agent dan quota token 100 juta, tetapi enforcement token per agent belum menghentikan agent ketika limit tercapai.
- Arthur terlalu sering meminta konfirmasi berulang saat membuat agent, termasuk setelah user sudah jelas bilang “langsung”, “buatkan”, atau “gausah banyak tanya”.
- Arthur bisa klaim agent sudah di-update untuk Google Docs tanpa readback konfigurasi agent yang benar.
- User-facing reply masih membocorkan istilah teknis internal seperti “MCP”, padahal istilah itu tidak cocok untuk customer SaaS.
- Agent research bisa muter saat `write_file` gagal karena file sudah ada, lalu mencoba path yang sama lagi.
- `compose_agent_blueprint` bisa gagal parse JSON dari model (`Expecting ',' delimiter`) dan hanya fallback ke blueprint generik.
- Dokumentasi arsitektur belum lengkap untuk kebutuhan presentasi project.

### Perubahan Utama
- WhatsApp escalation routing diperketat:
  - media/dokumen operator hanya dianggap eskalasi jika pesan tersebut adalah reply ke pesan eskalasi yang berisi ID kasus dan nomor customer.
  - media/dokumen biasa dari admin tidak lagi otomatis dianggap eskalasi.
  - quoted/reply context WhatsApp sekarang diteruskan ke agent agar agent memahami konteks pesan yang sedang dibalas.
- Flow eskalasi admin diperjelas:
  - balasan admin ke kasus eskalasi diperlakukan sebagai keputusan/operator reply untuk customer.
  - agent tidak boleh menjalankan ulang pekerjaan customer hanya karena admin menjawab eskalasi.
  - lampiran customer untuk kasus eskalasi tetap dikirim ke operator dengan konteks kasus.
- Runtime interruption diperbaiki:
  - proses lama bisa dihentikan saat user mengirim pesan baru.
  - pesan internal “saya stop proses sebelumnya” tidak lagi dikirim ke customer.
- Arthur builder diperkuat:
  - instruksi builder mengurangi confirmation loop saat intent user sudah jelas.
  - update agent sekarang harus dibuktikan dengan readback `get_agent_detail` sebelum Arthur mengklaim selesai.
  - untuk Google Docs/Sheets/Drive/Gmail/Calendar, Arthur memakai flag update khusus `enable_google_workspace=true` agar konfigurasi benar-benar berubah, bukan sekadar menulis instruksi umum.
  - setelah integrasi Google aktif, Arthur diarahkan membuat link koneksi Google lewat tool resmi, bukan menyuruh user memahami endpoint internal.
- Istilah teknis internal disanitasi:
  - user-facing prompt dan reply tidak lagi memakai kata “MCP”.
  - wording diganti menjadi “integrasi Google”, “Google Docs”, atau “Google Workspace”.
  - guard Google Workspace tetap jujur jika tool belum dieksekusi, tetapi tidak membocorkan detail internal.
- Trial WA dev diperbaiki:
  - bug `default_target` di `create_wa_dev_trial_link` ditutup.
  - jika agent target kosong, flow memilih agent milik user yang terbaru dan bukan Arthur/builder agar trial tidak nyasar ke agent lama seperti CV Maker.
- Token dan tier diperbaiki:
  - Tier 3 disiapkan untuk unlimited agent.
  - quota token Tier 3 dinaikkan menjadi 100 juta.
  - enforcement quota agent ditambahkan agar agent tidak terus berjalan setelah limit tercapai.
- Agent research dan file workspace diperbaiki:
  - prompt memory tidak lagi mendorong agent menyimpan semua hal ke file.
  - `write_file` yang gagal karena file sudah ada tidak boleh diretry terus ke path sama; agent harus edit/read atau membuat path baru bila file memang dibutuhkan.
  - research default diarahkan menjawab di chat dan menyimpan memori penting, bukan muter di virtual FS.
- Blueprint generation diperkuat:
  - parsing output JSON dibuat lebih robust dengan repair/extraction fallback.
  - fallback blueprint dibuat lebih kontekstual agar tidak terlalu generik ketika generator gagal.
- Dokumentasi presentasi ditambahkan:
  - folder `Dokumentasi Arsitektur/` dibuat.
  - isi dokumentasi menjelaskan layer backend, Arthur, memory, RAG, WhatsApp services, sandbox, subagent, token/quota, MCP/integrasi eksternal, dan visualisasi ASCII arsitektur/flow.

### File dan Area yang Tersentuh
- Backend/runtime:
  - `app/core/engine/agent_runner.py`
  - `app/core/engine/prompt_builder.py`
  - `app/core/engine/google_mcp_support.py`
  - `app/core/engine/wa_reply_delivery.py`
  - `app/core/engine/agent_tool_setup.py`
  - `app/core/engine/subagent_builder.py`
- Builder tools dan Arthur:
  - `app/core/tools/builder_tools.py`
  - `system-message-builder.md`
  - `scripts/seed_arthur.py`
- WhatsApp API/service boundary:
  - `app/api/channels.py`
  - `app/api/messages.py`
  - `app/api/wa_helpers.py`
  - `wa-service`
  - `wa-dev-service`
- Subscription/quota:
  - `app/core/domain/subscription_service.py`
  - `app/core/domain/agent_quota_service.py`
  - `alembic/versions/016_tier3_token_quota_100m.py`
- Dokumentasi:
  - `Dokumentasi Arsitektur/`
  - `docs/recap.md`
- Regression tests:
  - `tests/test_builder_tools.py`
  - `tests/test_whatsapp_direct_send.py`
  - `tests/test_whatsapp_progress.py`
  - `tests/test_whatsapp_spam_escalation.py`
  - `tests/test_agent_quota_service.py`
  - `tests/test_subscription_service.py`
  - `tests/test_mcp_tool_priority.py`
  - `tests/test_google_mcp_reply_overrides.py`
  - `tests/test_google_mcp_subagent_routing.py`

### Validasi
- Focused Python tests:
  - `PYTHONPATH=. .venv/bin/python -m pytest tests/test_builder_tools.py tests/test_whatsapp_direct_send.py tests/test_mcp_tool_priority.py tests/test_google_mcp_reply_overrides.py tests/test_google_mcp_subagent_routing.py -q`
  - Hasil: `117 passed, 1 warning`.
- Google Workspace compatibility/guard tests:
  - `PYTHONPATH=. .venv/bin/python -m pytest tests/test_mcp_config_compat.py tests/test_mcp_server_map.py tests/test_google_drive_tool_guard.py tests/test_google_calendar_manage_event_guard.py -q`
  - Hasil: `15 passed`.
- Compile check:
  - `PYTHONPATH=. .venv/bin/python -m compileall app/core/tools/builder_tools.py app/core/engine/prompt_builder.py app/core/engine/google_mcp_support.py app/core/engine/agent_runner.py scripts/seed_arthur.py`
- Go WhatsApp service tests dijalankan untuk perubahan reply/escalation boundary pada service WhatsApp.

### Catatan Operasional
- Setelah deploy/restart backend, Arthur perlu di-seed ulang agar rulebook terbaru dari `system-message-builder.md` masuk ke konfigurasi Arthur:
  - `PYTHONPATH=. .venv/bin/python scripts/seed_arthur.py`
- Backend/worker/WhatsApp service perlu restart agar prompt runtime, sanitizer, routing escalation, dan quota enforcement aktif di proses yang sedang berjalan.
- Untuk agent lama yang perlu Google Docs, Arthur sekarang harus menjalankan update konfigurasi agent lalu readback, bukan hanya menjawab “sudah saya update”.

**Tanggal**: 2026-05-26  
**Status**: Selesai lokal + regression tests focused pass

## 2026-05-22 — Refactor `agent_runner.py` (Phase 1)

### Latar Belakang
- `app/core/engine/agent_runner.py` tumbuh terlalu panjang dan menampung banyak concern sekaligus (prompting, tool assembly, subagent setup, progress formatting, dan runtime orchestration).
- Dampaknya: code review lambat, regression risk tinggi saat patch kecil, dan sulit melakukan isolasi test per concern.

### Perubahan Refactor
- Ekstraksi concern setup tools ke modul terpisah:
  - `app/core/engine/agent_tool_setup.py`
  - `app/core/engine/tool_builder.py`
- Ekstraksi concern prompt/context builder ke modul terpisah:
  - `app/core/engine/prompt_builder.py`
- Ekstraksi concern subagent construction ke modul terpisah:
  - `app/core/engine/subagent_builder.py`
- Ekstraksi helper WhatsApp progress message ke modul ringan:
  - `app/core/engine/wa_progress.py`
- `agent_runner.py` diposisikan sebagai orchestration entrypoint (`run_agent`) dan wiring antar komponen, bukan lokasi implementasi detail semua concern.

### Dampak Teknis
- Separation of concerns lebih jelas: perubahan di prompt/tool/subagent tidak perlu menyentuh orchestration utama.
- Risiko conflict antar patch turun karena area edit lebih sempit per fitur.
- Lebih mudah menambah unit test terarah per modul (builder/formatter) tanpa mem-bloat test `run_agent`.
- Fondasi untuk phase berikutnya: memecah guard/helper yang masih tersisa di `agent_runner.py` agar ukuran file terus turun bertahap tanpa rewrite besar.

### Catatan Status
- Refactor ini fokus pada modularisasi internal dan mempertahankan behavior existing (non-breaking intent).
- File target utama yang di-refactor:
  - `app/core/engine/agent_runner.py`
  - `app/core/engine/agent_tool_setup.py`
  - `app/core/engine/prompt_builder.py`
  - `app/core/engine/subagent_builder.py`
  - `app/core/engine/tool_builder.py`
  - `app/core/engine/wa_progress.py`

**Tanggal**: 2026-05-22
**Status**: ✅ Selesai lokal + live Google Slides verified

## Scope Update

Perbaikan lanjutan untuk bug AI Staff 21 Mei pada Google Workspace MCP:

- Agent operasional harus memakai MCP secara semantic saat tool tersedia.
- Sandbox/subagent tidak boleh menjadi fallback palsu untuk aksi external service.
- Arthur tetap builder-first dan tidak ikut terdampak policy runtime operasional.
- Saat Google auth belum tersambung atau token expired, agent harus langsung memberi reconnect link.
- Tool call MCP Google Workspace harus memakai runtime lokal `http://localhost:8002/mcp`, bukan devtunnel/port auth 8003.
- Agent coding dengan `deploy=true` + subagent harus otomatis lanjut deploy via Cloudflare tunnel setelah berhasil menulis website/app; tidak boleh berhenti hanya dengan laporan file/kode sudah dibuat.
- Agent coder untuk website/web app/frontend harus memakai vanilla HTML/CSS/JavaScript terpisah tanpa framework, npm/npx, Tailwind, CDN library, atau inline CSS/JS agar cepat dan ringan di sandbox.
- Arthur dan semua agent baru yang dibuat Arthur default punya browsing Tavily (`tavily_search`, `tavily_extract`) selama `TAVILY_API_KEY` tersedia di environment.
- WhatsApp progress message tidak boleh mengirim pesan awal seperti "Saya mulai kerjakan lewat subagent..." saat user hanya minta cek/search; cukup typing indicator dan final answer.

## Masalah Terbaru

- Setelah refactor semantic MCP-first, preflight Google auth error tidak selalu menghentikan graph lebih awal karena hard parent-only branch tidak lagi default.
- Akibatnya model bisa menjawab sendiri dulu, misalnya meminta user reconnect dari pengaturan atau bertanya apakah link perlu dibuat, padahal backend sudah bisa membuat `auth_url`.
- Reply auth failure sebelumnya masih dibentuk lewat LLM, sehingga tidak deterministic dan bisa tidak langsung actionable.
- Follow-up `sudah/ok sudah` setelah auth link bisa kehilangan konteks Google karena pesan terbaru tidak mengandung keyword Google. Akibatnya graph sempat mencoba `task/sys_coder`; guard memang memblokir fallback, tetapi reply akhir menjadi tidak actionable.
- Setelah OAuth berhasil, follow-up `sudah` sempat menjawab “Ada yang bisa saya bantu?” karena request Google lama tidak direplay eksplisit ke graph.
- Candidate auth pertama (`session.external_user_id`) bisa belum connected, sementara candidate fallback (`operator_ids[0]`) sudah connected. Sebelum fix, `preflight_error` dari candidate pertama tidak dibersihkan setelah token fallback berhasil, sehingga reply HTTP bisa tertimpa auth blocker walaupun tool MCP sudah sukses membuat Slides.
- Backend integration call ke devtunnel 8003 bisa timeout sebelum mengecek token fallback lokal; untuk local-dev dengan `WORKSPACE_MCP_PREFER_LOCAL=true`, backend harus memakai `http://localhost:8003` untuk `/status`, `/token`, dan `/connect`.
- Prompt "2 halaman" sebelumnya tidak dikenali sebagai target 2 slide, sehingga workflow bisa berhenti setelah 1 slide atau membuat deck kedua terpisah.
- Ada kebingungan antara dua endpoint:
  - `8002` = MCP runtime untuk tool execution.
  - `8003` = integration/auth management untuk status, token, connect, dan short auth link.
- Untuk agent coding/deploy dengan subagent, `sys_coder` sudah punya tool `deploy_app`, tetapi runner belum punya post-run correction. Jika subagent menulis `/workspace/src/index.html` lalu final reply tidak berisi URL, parent bisa menganggap task selesai tanpa Cloudflare tunnel.
- Prompt coder lama masih membuka peluang framework modern untuk dashboard/app kompleks. Ini membuat task website bisa lama karena npm install/build dan membebani sandbox, padahal kebutuhan SaaS saat ini lebih cocok static HTML/CSS/JS untuk hasil cepat.
- Agent baru buatan Arthur sebelumnya tidak punya semantic web search default; yang ada hanya HTTP raw opsional. Untuk SaaS, default browsing harus tersedia tanpa user memahami API/URL.
- Auto-progress callback WhatsApp sebelumnya mengirim message langsung saat tool `task` start. Untuk subagent research, ini terlihat seperti jawaban mengganggu sebelum hasil final.

## Akar Bug

- Semantic MCP-first benar untuk tool choice, tetapi auth blocker tetap perlu deterministic guard sebelum graph saat Google Workspace belum connected.
- Guard pre-graph sebelumnya terlalu terikat ke `google_mcp_parent_only`; ketika parent-only menjadi legacy explicit switch, auth failure bisa lolos ke graph.
- `_build_google_mcp_auth_failure_reply()` masih memberi kesempatan LLM menulis wording sendiri sebelum link ditempel, sehingga reply bisa kurang tegas.

## Solusi

- Tambah policy runtime eksplisit:
  - `builder` untuk Arthur / Agent Builder.
  - `operational` untuk agent biasa.
- Legacy Google Workspace parent-only branch sekarang hanya aktif jika `mcp.google_workspace_parent_only = true`.
- Untuk default semantic MCP-first:
  - sandbox dan subagent tetap boleh tersedia untuk coding/deploy flow.
  - Google Workspace MCP tools tetap diprioritaskan saat tersedia.
  - `ExternalServiceFallbackGuardMiddleware` memblokir tool fallback seperti `task`, `execute`, `write_file`, `edit_file`, `read_file`, dan `sandbox_write_binary_file` jika payload jelas mencoba menjalankan aksi Google Workspace.
- Arthur/builder policy tidak dipasangi external-service fallback guard operational, sehingga Arthur tetap memakai builder tools internal.
- Auth failure Google Workspace sekarang deterministic:
  - jika Google belum connected/token expired dan request user adalah Google Workspace, runner stop sebelum graph saat tidak ada MCP tools usable.
  - agent langsung mengirim reconnect link jika `auth_url` tersedia.
  - agent tidak lagi bertanya “mau saya buatkan link?” untuk case auth blocker.
- `_build_google_mcp_auth_failure_reply()` tidak lagi bergantung pada LLM untuk membuat pesan auth failure; fungsi ini mengembalikan pesan fixed dan actionable.
- Follow-up auth recovery sekarang dideteksi dari history terbaru:
  - pesan pendek seperti `sudah`, `ok sudah`, `sudah login`, atau `sudah reconnect` dianggap kelanjutan OAuth hanya jika history terbaru berisi auth blocker Google.
  - role history `assistant` dan `agent` sama-sama dikenali karena final graph replies tersimpan sebagai `agent`.
  - jika token masih belum aktif, runner kembali stop sebelum graph dan mengirim link reconnect baru tanpa `task`, sandbox, atau tool auth LLM-ish.
- Saat follow-up auth recovery punya request Google lama, runner membangun `execution_user_message` eksplisit dari request tersebut, sehingga `sudah` melanjutkan pekerjaan lama, bukan dianggap chat kosong.
- Jika token fallback berhasil, `auth_url` dan `preflight_error` stale dibersihkan sebelum MCP client dibuka.
- Reply override akhir tidak boleh menimpa success yang sudah memiliki Google Workspace artifact/tool output valid, meskipun masih ada auth error stale.
- Jika DeepAgents tetap memilih `task` untuk aksi Google Workspace dan guard memblokirnya, runner melakukan retry MCP-only dengan `create_react_agent` dan hanya Google Workspace MCP tools.
- `_extract_requested_slide_count()` sekarang mengenali `halaman`, `page/pages`, dan `lembar`, bukan hanya `slide`.
- Follow-up Slides sekarang dipicu juga ketika total slide hasil `get_presentation` masih kurang dari jumlah yang diminta user.
- Runtime MCP Google Workspace tetap diarahkan ke `WORKSPACE_MCP_RUNTIME_URL` / `WORKSPACE_MCP_URL_LOCAL`, yaitu `http://localhost:8002/mcp` pada env lokal.
- Port 8003 tetap dipakai hanya untuk integration/auth service (`/v1/integrations/google/connect`, `/status`, `/token`, `/start`), bukan untuk MCP tool execution.
- Tambah deploy follow-up guard:
  - hanya aktif saat `deploy=true`, user meminta website/web app/landing page/portfolio/dashboard/frontend, dan langkah sebelumnya menunjukkan file/kode sudah dibuat.
  - jika belum ada URL public di final reply atau tool steps, runner melanjutkan graph satu putaran khusus untuk deploy.
  - jika subagent aktif, follow-up menginstruksikan parent memanggil `task()` ke `sys_coder` agar `deploy_app()` dijalankan dari workspace subagent yang berisi file, bukan workspace parent yang berbeda.
  - jika URL sudah ada, deploy tidak diulang.
- Prompt coding/deploy disederhanakan untuk web:
  - `sys_coder` wajib membuat `/workspace/src/index.html`, `/workspace/src/styles.css`, dan `/workspace/src/script.js` jika butuh interaksi.
  - tidak boleh inline CSS/JS di HTML.
  - tidak boleh memakai React/Next/Vue/Svelte/Astro/Tailwind/Bootstrap/Vite/npm/npx/CDN library/framework frontend untuk task web.
  - Arthur preset `coding_deploy_agent` dan deploy prompt umum ikut menyuntikkan aturan vanilla ini saat mendelegasikan ke `sys_coder`.
- Tambah Tavily browsing:
  - `.env` memakai `TAVILY_API_KEY`; `.env.example` mendokumentasikan variabelnya tanpa secret.
  - `.env` juga memakai `TAVILY_FORCE_IPV4=true` karena host lokal sempat resolve `api.tavily.com` ke IPv6/NAT64 dan TLS timeout; IPv4 langsung berhasil.
  - Runtime memuat Tavily secara default jika key tersedia; agent tetap bisa mematikan dengan `tools_config.tavily=false`.
  - Arthur seed dan semua preset builder default `tavily=true`.
  - `system-message-builder.md` mengarahkan Arthur memakai Tavily untuk riset eksternal, bukan HTTP/ngrok untuk operasi platform internal.
  - Live API test ke agent `Bas` berhasil memanggil `tavily_search` dan mengembalikan URL sumber Tavily.
- WhatsApp progress behavior:
  - Auto progress untuk `task`/subagent start tidak lagi dikirim ke user.
  - Auto progress text umum diganti menjadi delayed long-process notice maksimal 1x setelah 75 detik: "Masih saya proses ya..."
  - Notice dibatalkan jika final reply selesai sebelum delay.
  - Prompt WA sekarang melarang progress awal seperti "saya mulai" dan membatasi `notify_user` hanya untuk proses lama, retry/error, atau blocker.

## File yang Diubah

- `app/core/engine/agent_policy.py`
  - Policy class `builder` vs `operational`.
  - Legacy explicit switch `mcp.google_workspace_parent_only`.
  - Guard helper untuk blok fallback external-service.
- `app/core/engine/agent_tool_setup.py`
  - Runtime setup memakai policy helper.
  - Sandbox/subagent tidak lagi otomatis dimatikan oleh keyword Google Workspace kecuali legacy switch aktif.
- `app/core/engine/agent_runner.py`
  - Pasang `ExternalServiceFallbackGuardMiddleware` untuk agent operational.
  - Auth/preflight error Google Workspace sekarang bisa block before graph untuk request Google Workspace walaupun parent-only legacy tidak aktif.
  - Fetch auth link ulang jika belum ada sebelum mengirim reply auth failure.
  - Treat auth-recovery follow-up sebagai kelanjutan Google Workspace request untuk pre-graph blocker.
  - Tambah MCP-only retry setelah fallback guard memblokir `task` untuk aksi Google Workspace.
  - Tambah auto deploy follow-up untuk website/app yang sudah ditulis tapi belum mengembalikan URL public.
- `app/core/engine/subagent_builder.py`
  - Prompt `sys_coder` sekarang hard default vanilla HTML/CSS/JS terpisah untuk semua task web/frontend agar tidak membebani sandbox.
- `app/core/engine/prompt_builder.py`
  - Deploy instructions umum sekarang melarang framework/frontend package dan inline CSS/JS untuk website/web app.
- `app/core/tools/builder_tools.py`
  - Preset Arthur `coding_deploy_agent` dan template instruksi coding agent sekarang meneruskan aturan vanilla web stack ke `sys_coder`.
- `app/core/tools/tavily_tool.py`
  - Tool baru `tavily_search` dan `tavily_extract` berbasis Tavily API.
- `scripts/seed_arthur.py`
  - Arthur default `tavily=true` untuk browsing eksternal.
- `app/core/engine/wa_progress.py`
  - `task` progress message disuppress agar WhatsApp tidak menerima preview delegasi subagent.
- `app/core/engine/google_mcp_support.py`
  - Auth failure reply fixed, jujur, dan langsung menyertakan reconnect link jika tersedia.
  - Tambah detector `is_google_auth_recovery_followup()` untuk follow-up OAuth berbasis history.
  - Tambah replay request Google terakhir, local integration runtime URL, cleanup stale preflight setelah token fallback berhasil, stale-auth success guard, dan parser jumlah slide `halaman/page`.
- `tests/test_google_mcp_subagent_routing.py`
  - Regression untuk semantic MCP-first, legacy parent-only explicit switch, operational fallback guard, dan Arthur builder policy.
- `tests/test_google_mcp_reply_overrides.py`
  - Regression bahwa auth failure langsung mengirim reconnect link.
- `docs/semantic-mcp-refactor-todo.md`
  - Checklist implementation lokal selesai, live verification tetap pending.

## Expected Behavior Saat Ditest

Jika user meminta:

```text
buatkan Google Slides 5 halaman tentang bahaya rokok, kasih link hasilnya
```

Dan Google belum connected/token expired, agent harus langsung menjawab:

```text
Google Workspace belum terhubung atau tokennya sudah expired, jadi saya belum menjalankan request ini.

Klik link ini untuk reconnect Google:
<auth_url>

Setelah selesai, balas `sudah` supaya saya lanjutkan.
```

Setelah user selesai OAuth dan membalas `sudah`, agent seharusnya mencoba lagi memakai Google Workspace MCP tools. Tool execution harus memakai MCP runtime `http://localhost:8002/mcp`. Link OAuth/reconnect boleh berasal dari integration/auth service 8003 karena itu endpoint auth management, bukan endpoint tool MCP.

Jika user membalas `sudah` tetapi token masih belum aktif, agent harus kembali mengirim blocker deterministic dengan reconnect link baru dan `steps=[]`; tidak boleh masuk `task/sys_coder`, sandbox, atau klaim progress.

Jika token sudah aktif pada fallback owner/operator, agent harus lanjut membuat Google Slides dengan MCP tools. Auth status/token backend memakai local integration API `http://localhost:8003`, sedangkan MCP tool execution tetap `http://localhost:8002/mcp`.

## Verifikasi Lokal

- `PYTHONPATH=. .venv/bin/python -m py_compile app/core/engine/agent_policy.py app/core/engine/agent_runner.py app/core/engine/agent_tool_setup.py`
  - Hasil: passed.
- `PYTHONPATH=. .venv/bin/python -m py_compile app/core/engine/google_mcp_support.py app/core/engine/agent_runner.py app/core/tools/mcp_tool.py`
  - Hasil: passed.
- `PYTHONPATH=. .venv/bin/pytest -q tests/test_google_mcp_subagent_routing.py tests/test_google_mcp_reply_overrides.py tests/test_mcp_tool_priority.py tests/test_mcp_server_map.py tests/test_builder_tools.py tests/test_agent_builder_phase4.py::TestAgentRunnerIntegration`
  - Hasil awal: `74 passed in 4.72s`.
- Setelah fix follow-up `sudah`:
  - `PYTHONPATH=. .venv/bin/pytest -q tests/test_google_mcp_reply_overrides.py tests/test_google_mcp_subagent_routing.py tests/test_mcp_server_map.py`
    - Hasil: `41 passed in 2.27s`.
  - `PYTHONPATH=. .venv/bin/pytest -q tests/test_google_mcp_subagent_routing.py tests/test_google_mcp_reply_overrides.py tests/test_mcp_tool_priority.py tests/test_mcp_server_map.py tests/test_builder_tools.py tests/test_agent_builder_phase4.py::TestAgentRunnerIntegration`
    - Hasil: `77 passed in 4.14s`.
- Setelah fix replay intent, stale auth, local integration runtime, MCP-only retry, dan slide count:
  - `PYTHONPATH=. .venv/bin/pytest -q tests/test_google_slides_template_intent.py tests/test_google_mcp_reply_overrides.py tests/test_google_mcp_subagent_routing.py tests/test_mcp_server_map.py`
    - Hasil: `61 passed in 2.16s`.

## Live API Partial Test 2026-05-22

Agent test dibuat via API dengan model `openai/gpt-4.1-mini`, `mcp.google_workspace`, `sandbox: true`, dan `subagents.enabled: true`.

Hasil:

- Request Google Slides saat Google belum connected:
  - reply pertama langsung memberi reconnect link.
  - `steps=[]`.
  - tidak ada fallback sandbox/subagent.
- Follow-up `sudah` sebelum token aktif:
  - sebelum patch follow-up: sempat mencoba `task/sys_coder`, lalu guard memblokir.
  - setelah patch follow-up: kembali deterministic auth blocker dengan reconnect link baru dan `steps=[]`.
- Follow-up `sudah` setelah token aktif:
  - sebelum replay patch: bisa menjawab “Ada yang bisa saya bantu?”.
  - setelah replay patch: request Google lama dilanjutkan.
- Runtime token check:
  - candidate session `codex-live-smoke-user` belum connected.
  - candidate fallback operator `codex-live-smoke` connected dan token valid.
  - setelah local integration patch, `prepare_google_mcp_runtime()` inject token fallback dan `mcp_client_context()` load `122` Google Workspace MCP tools dari `http://localhost:8002/mcp`.
- Fresh Google Slides live test setelah patch:
  - Request: `buatkan Google Slides 2 halaman tentang manfaat olahraga pagi, kasih link hasilnya`.
  - Response HTTP berisi link:
    `https://docs.google.com/presentation/d/1dZ7uXDJB59aeBGy2_JQmCGiRA_idbzYPnZX5iIcJkg0/edit`
  - `get_presentation` memverifikasi `Total Slides: 2`.
  - Tool yang dipakai adalah Google Workspace MCP (`get_presentation`, `create_presentation`, `batch_update_presentation`); bukan sandbox/subagent untuk hasil final.
- Sandbox coding task:
  - `write_file` berhasil membuat `/workspace/output/smoke_result.txt`.
- Forced subagent task:
  - `task` ke `sys_coder` berhasil menjalankan script dan mengembalikan output `subagent-ok`.
- MCP server mapping:
  - `_build_server_map()` memilih `http://localhost:8002/mcp` walaupun input URL berupa devtunnel, karena `WORKSPACE_MCP_RUNTIME_URL` dan `WORKSPACE_MCP_PREFER_LOCAL=true`.

## Pending

- Jalankan smoke di session baru yang bersih agar history lama tidak ikut mempengaruhi urutan tool.
- Jika ingin production-hardening berikutnya: simpan state pending Google request secara eksplisit di session metadata, bukan hanya deteksi dari history.

---

**Tanggal**: 2026-05-21
**Status**: ✅ Selesai

## Scope

Analisa dan perbaikan bug pada project LangChain Deep Agent untuk produk SaaS:

- Agent Builder sebagai interface user membuat AI agent.
- MCP tools, terutama Google Workspace MCP.
- Sub Agent dengan DeepAgents backend.
- Docker Sandbox untuk eksekusi kode/file/deploy.
- Entitlement subscription plan untuk create/update agent.

## Masalah

- MCP context bisa menelan exception/cancellation karena memakai `except BaseException`, sehingga error utama dari agent run dapat hilang.
- Saat sandbox aktif, deploy tools ikut terekspos walaupun `deploy` tidak diaktifkan.
- Subagent yang seharusnya punya sandbox sendiri bisa fallback ke plain subagent jika compile gagal; efeknya `write_file` dan `deploy_app` bisa memakai workspace berbeda.
- File helper sandbox menerima path user tanpa boundary check yang kuat, sehingga path traversal berisiko keluar dari workspace.
- WhatsApp media tools membaca file via shell command tanpa quote path.
- Agent Builder hanya mengecek slot agent, belum mengecek entitlement model/subagents/WhatsApp dari subscription plan.
- Google MCP intent belum cukup mengenali request bahasa Indonesia seperti "kalender", "surel", dan "dokumen google".
- React/Google-MCP graph bisa tidak punya checkpointer, tetapi runner tetap memanggil `aget_state()`, sehingga run gagal dengan `ValueError: No checkpointer set`.
- Saat run lama masih aktif, pesan user terbaru bisa tertahan terlalu lama karena cancellation menunggu task lama selesai/cleanup.
- Test suite punya beberapa kontrak lama/stale dan fixture eksternal yang tidak tersedia (`respx_mock`).

## Akar Bug

- Lifecycle MCP client tidak dipisah jelas antara error saat connect dan error saat body context berjalan.
- `deploy` dianggap implisit dari `sandbox`, padahal deploy adalah kapabilitas lebih tinggi dan harus opt-in.
- Fallback subagent non-sandbox menyembunyikan kegagalan compile DeepAgents, tetapi merusak isolasi workspace.
- Path `/workspace/...` belum dinormalisasi ke workspace host dengan validasi `relative_to(root)`.
- Agent Builder tidak punya satu fungsi validasi entitlement yang dipakai konsisten saat create/update.
- `agent_runner` mengasumsikan semua graph punya checkpointer; mode Google MCP parent-only dan fallback React agent tidak selalu aman terhadap asumsi ini.
- `cancel_active_run()` menunggu terlalu lama pada tool/subagent yang lambat membatalkan diri, sehingga lock session tetap menahan pesan terbaru.
- Test API berbasis `fastapi.testclient.TestClient` menggantung di environment ini bahkan untuk FastAPI minimal, sehingga perlu dipisah dari verifikasi non-TestClient.

## Solusi

- `mcp_client_context()` sekarang:
  - hanya menangkap `Exception` saat connect/load tools
  - tidak menelan `CancelledError` atau exception dari body context
  - menutup client via `finally` dengan `aclose()`/`close()`
- Parent deploy tools hanya dimuat jika `tools_config.deploy` aktif.
- Sandbox file resolver baru membatasi semua path ke workspace atau shared dir:
  - `/workspace/foo` dipetakan ke workspace host
  - `../...` diblokir
  - error dikembalikan sebagai `[error] ...`
- Subagent sandbox sekarang fail-closed:
  - jika `create_deep_agent(..., backend=DockerBackend(sub_sandbox))` gagal, sandbox ditutup dan error dinaikkan
  - tidak ada fallback diam-diam ke plain `SubAgent`
- WhatsApp media file read memakai `shlex.quote(path)` sebelum `base64 -w 0`.
- Subscription entitlement dibuat eksplisit:
  - model harus ada di `allowed_models` jika allowlist tidak kosong
  - subagents dicek terhadap `plan.subagents_allowed`
  - WhatsApp channel dicek terhadap `plan.wa_connect`
  - validasi dipakai di `create_agent()` dan update `model/tools_config`
- Google MCP intent ditambah keyword Indonesia untuk routing parent-only MCP.
- React/Google-MCP graph sekarang diberi `MemorySaver` checkpointer saat dibuat.
- Runner memakai fallback aman ke output `ainvoke()` jika graph tetap tidak punya checkpointer, jadi `No checkpointer set` tidak lagi menjatuhkan run.
- Interrupted run ditandai `cancelled` dengan usage token yang sudah terkumpul.
- Session cancellation dibuat responsif:
  - task lama di-cancel
  - ditunggu singkat `1.5s`
  - jika cleanup/tool call belum berhenti, lock session di-force release agar pesan terbaru bisa diproses
- Health endpoint dipisah:
  - `/health` = liveness ringan tanpa DB
  - `/health/detailed` = readiness DB/WA/scheduler dengan timeout
- Alias kompatibilitas ditambahkan untuk import lama:
  - `app.core.subagent_builder`
  - `app.core.deep_agent_backend`
  - `app.core.transcription_service`
  - `app.core.sandbox`
- Test helper `respx_mock` lokal ditambahkan agar test transkripsi tidak bergantung plugin eksternal.
- `.gitignore` sekarang tetap ignore root `test_*.py`, tetapi mengizinkan `tests/test_*.py`.

## Flow Kerja Agent Saat Subagent + MCP Aktif

### Flow yang benar untuk intent Google Workspace

1. User meminta aksi Google Workspace, misalnya kalender, sheet, docs, slides, forms, Gmail, atau Drive.
2. `prepare_google_mcp_runtime()` menyiapkan runtime MCP dan auth state.
3. Google MCP tools diprioritaskan di parent agent.
4. Jika intent terdeteksi sebagai Google MCP dan MCP configured:
   - subagent build dilewati untuk turn itu
   - prompt memberi instruksi agar parent agent memakai MCP tool langsung
   - sandbox tidak dipakai untuk meniru external service
5. Tool wrapper Google MCP melakukan guard/normalisasi payload sebelum call MCP.
6. Agent mengembalikan hasil/link final ke user.

### Flow yang benar untuk coding/deploy

1. Parent agent menerima request coding/deploy.
2. Parent agent delegasi ke subagent sandbox seperti `sys_coder`.
3. `sys_coder` dicompile sebagai DeepAgent dengan `DockerBackend(sub_sandbox)`.
4. File operations (`write_file`, `edit_file`, `read_file`) dan deployment tools memakai workspace sub_sandbox yang sama.
5. Jika compile subagent gagal, run gagal eksplisit; tidak fallback ke workspace parent.
6. Jika WhatsApp media aktif, subagent boleh generate file di `/workspace/output/...` dan mengirim langsung via `send_whatsapp_document` atau `send_whatsapp_image`.

### Flow yang diblokir

- Google Workspace task tidak boleh dominan memakai sandbox ketika MCP tersedia.
- Deploy tools tidak boleh muncul hanya karena sandbox aktif.
- Sandbox subagent tidak boleh fallback ke plain SubAgent saat backend compile gagal.
- Agent Builder tidak boleh membuat/update agent yang melebihi plan subscription.
- Pesan terbaru user tidak boleh menunggu run lama sampai timeout penuh jika run lama bisa di-cancel.

### Flow interrupt pesan terbaru

1. Pesan baru masuk pada session yang masih punya run aktif.
2. Runtime memanggil `cancel_active_run(session_id)`.
3. Task lama menerima `CancelledError` dan run record ditandai `cancelled`.
4. Runtime menunggu cleanup singkat sampai `1.5s`.
5. Jika task lama belum selesai karena tool/subagent/HTTP call lambat unwind, lock session dilepas paksa.
6. Pesan terbaru diproses sebagai run baru sehingga user mendapat respons cepat.
7. Full pause/resume mid-tool belum dianggap aman; resume yang benar perlu checkpoint persisten dan checkpoint kooperatif di level tool/subagent.

## File yang Diubah

| File | Perubahan |
|------|-----------|
| `app/core/tools/mcp_tool.py` | MCP context tidak swallow exception/cancellation; close client via `finally` |
| `app/core/engine/agent_tool_setup.py` | Deploy tools hanya dimuat jika `deploy` enabled; Google MCP parent-only skip subagents |
| `app/core/engine/subagent_builder.py` | Sandbox subagent compile fail-closed dengan backend sub_sandbox |
| `app/core/infra/sandbox.py` | Path resolver aman; traversal blocked; concurrent limit return tuple konsisten |
| `app/core/engine/tool_builder.py` | Quote path untuk WhatsApp media file read |
| `app/core/domain/subscription_service.py` | Tambah `validate_agent_entitlements()` |
| `app/core/tools/builder_tools.py` | Enforce entitlement saat create/update agent; platform capabilities kompatibel |
| `app/core/engine/google_mcp_support.py` | Tambah keyword intent Google MCP bahasa Indonesia |
| `app/core/engine/agent_runner.py` | Checkpointer React graph, fallback hasil `ainvoke()`, dan marking run `cancelled` saat interrupt |
| `app/core/engine/session_lock.py` | Cancellation grace responsif `1.5s` lalu force release lock untuk pesan terbaru |
| `app/models/agent.py` | Python-side JSON defaults untuk list/dict field |
| `app/main.py` | Health liveness/readiness dipisah; startup task diberi timeout/test guard |
| `app/core/engine/wa_progress.py` | Progress WA kembali eksplisit dengan path dan status selesai |
| `.gitignore` | `tests/test_*.py` tidak lagi ikut ignored |
| `tests/conftest.py` | Fixture lokal `respx_mock` |
| `tests/test_session_lock_and_history.py` | Regression test checkpointer fallback dan responsive cancel |
| `app/core/subagent_builder.py`, `app/core/deep_agent_backend.py`, `app/core/transcription_service.py`, `app/core/sandbox.py` | Alias import kompatibilitas |

## Command Verifikasi

```bash
.venv/bin/python -m py_compile \
  app/core/tools/mcp_tool.py \
  app/core/engine/agent_tool_setup.py \
  app/core/engine/subagent_builder.py \
  app/core/infra/sandbox.py \
  app/core/engine/tool_builder.py \
  app/core/engine/google_mcp_support.py \
  app/core/domain/subscription_service.py \
  app/core/tools/builder_tools.py \
  app/models/agent.py \
  app/main.py \
  app/core/engine/session_lock.py \
  app/core/engine/wa_progress.py \
  app/core/subagent_builder.py \
  app/core/deep_agent_backend.py \
  app/core/transcription_service.py \
  app/core/sandbox.py \
  tests/conftest.py
```

Hasil: compile lolos.

```bash
.venv/bin/python -m pytest -q tests \
  --ignore=tests/test_api_full_coverage.py \
  --ignore=tests/test_users_api.py \
  --ignore=tests/test_subscriptions_api.py \
  --ignore=tests/test_user_api_keys.py \
  --maxfail=1 -vv
```

Hasil: `289 passed, 9 skipped, 10 warnings`.

Tambahan verifikasi untuk fix `No checkpointer set` dan interrupt run:

```bash
.venv/bin/python -m py_compile \
  app/core/engine/agent_runner.py \
  app/core/engine/session_lock.py \
  tests/test_session_lock_and_history.py
```

Hasil: compile lolos.

```bash
timeout 120s .venv/bin/python -m pytest -q tests/test_session_lock_and_history.py --maxfail=1 -vv
```

Hasil: `42 passed, 7 warnings`.

```bash
timeout 240s .venv/bin/python -m pytest -q tests \
  --ignore=tests/test_api_full_coverage.py \
  --ignore=tests/test_users_api.py \
  --ignore=tests/test_subscriptions_api.py \
  --ignore=tests/test_user_api_keys.py \
  --maxfail=1
```

Hasil: `292 passed, 9 skipped, 10 warnings`.

```bash
.venv/bin/python -m pytest -q tests/test_transcription_service.py --maxfail=1 -vv
```

Hasil: `13 passed`.

```bash
.venv/bin/python -m pytest -q tests/test_whatsapp_progress.py --maxfail=1 -vv
```

Hasil: `5 passed`.

## Catatan Verifikasi

- Full API suite berbasis `fastapi.testclient.TestClient` tidak dipakai sebagai gate di environment ini karena `TestClient` menggantung bahkan pada FastAPI minimal.
- Warning tersisa:
  - deprecation warning DeepAgents `files_update`
  - warning lama event loop di test builder pipeline
- Keduanya bukan failure runtime dari fix MCP/subagent/sandbox.

---

# Recap: WhatsApp Escalation Media Forwarding + Operator Reply Routing

**Tanggal**: 2026-05-19
**Status**: ✅ Selesai

## Masalah

- Customer mengirim bukti order/pembayaran dalam bentuk gambar, tetapi saat agent eskalasi ke operator, file gambarnya tidak ikut diteruskan.
- Pesan eskalasi ke operator belum selalu mencantumkan nomor customer dengan jelas, sehingga operator/agent bisa bingung balasan harus dikirim ke siapa.
- Saat operator memakai fitur reply WhatsApp pada pesan eskalasi, agent belum cukup eksplisit diarahkan untuk mengirim balasan operator ke customer terkait.

## Akar Bug

- `process_wa_media()` memang menyimpan media ke workspace, tetapi metadata media belum tersimpan/ter-commit ke `session.metadata_` sebelum `run_agent()` berjalan.
- `escalate_to_human()` membaca session dari DB session lain, sehingga metadata lampiran yang baru di-`flush` belum terlihat.
- Format notifikasi eskalasi terlalu teknis dan kurang “routing-friendly” untuk operator.
- Runtime prompt operator belum punya sinyal khusus bahwa pesan operator berasal dari reply/quote pesan eskalasi tertentu.

## Solusi

- `process_wa_media()` sekarang return `media_meta` berisi `media_type`, `filename`, `workspace_path`, `mimetype`, dan ukuran file.
- `/wa/incoming` menyimpan `last_incoming_media` ke `session.metadata_` dan langsung `commit` sebelum agent run.
- `escalate_to_human()` meneruskan lampiran terakhir customer ke operator:
  - image/sticker via `send_wa_image`
  - document via `send_wa_document`
- Format notifikasi eskalasi dibuat eksplisit:

```text
ESKALASI PESAN DARI CUSTOMER
ID Kasus: esc_xxx
Nomor customer/user: 628xxxx
Pesan: <isi/ringkasan pesan user>

Cara balas customer:
Reply pesan ini di WhatsApp, lalu tulis jawaban untuk customer.
Agent akan mengirim balasan ke nomor customer di atas.
```

- `find_escalation_context()` sekarang menandai routing quote dengan:
  `ROUTING: operator_reply_quoted_escalation`
- Runtime prompt operator memakai sinyal itu untuk langsung memanggil `reply_to_user(message)` saat operator membalas pesan eskalasi via fitur reply WhatsApp.
- Arthur rulebook ikut diupdate supaya agent baru memahami pola eskalasi media dan reply operator ini sejak awal.

## File yang Diubah

| File | Perubahan |
|------|-----------|
| `app/api/wa_helpers.py` | `process_wa_media()` return metadata media; `find_escalation_context()` tambah routing note untuk quoted escalation |
| `app/api/channels.py` | Simpan dan commit `last_incoming_media` sebelum `run_agent()` |
| `app/core/tools/escalation_tool.py` | Format notifikasi baru; forward lampiran customer ke operator; simpan `escalation_customer_phone` |
| `app/core/engine/prompt_builder.py` | Runtime prompt operator paham quoted escalation dan boleh langsung `reply_to_user` |
| `app/core/tools/builder_tools.py` | Tool hints escalation diperjelas untuk agent baru |
| `system-message-builder.md` | Arthur diarahkan membuat agent WhatsApp yang paham escalation media + operator reply routing |
| `scripts/seed_arthur.py` | Soul note Arthur diselaraskan dengan create-agent flow baru |

## Command Verifikasi

```bash
.venv/bin/python -m py_compile \
  app/api/wa_helpers.py \
  app/api/channels.py \
  app/core/tools/escalation_tool.py \
  app/core/engine/prompt_builder.py \
  app/core/tools/builder_tools.py \
  scripts/seed_arthur.py
```

Hasil: compile lolos.

```bash
.venv/bin/python scripts/seed_arthur.py
```

Hasil: Arthur berhasil diupdate ke versi `15`.

## Hasil Test Manual

- Customer kirim bukti order/bukti pembayaran berupa gambar.
- Agent eskalasi ke operator.
- Operator menerima pesan eskalasi dengan format nomor customer + pesan customer yang jelas.
- Lampiran bukti dari customer ikut diteruskan ke operator.
- Operator memakai fitur reply WhatsApp pada pesan eskalasi.
- Agent mengirim balasan operator ke customer yang benar.

---

# Recap: Google Forms Workflow Mode — Auto Isi Konten + Link Final

**Tanggal**: 2026-05-18
**Status**: ✅ Selesai

## Masalah

Agent sempat berhasil membuat Google Form, tapi berhenti di `title` saja tanpa melanjutkan isi deskripsi + pertanyaan, sehingga user tetap harus follow-up manual.

## Akar Bug

- Secara API, `create_form` memang hanya boleh `title` saat create.
- Saat retry sudah ada, tapi sebelumnya ada alur yang membuat agent masih bisa berhenti terlalu cepat tanpa workflow lanjutan.

## Solusi

- Tambah deteksi intent pembuatan/pengisian Forms di `agent_runner`.
- Aktifkan **FORMS WORKFLOW MODE** untuk request terkait Forms.
- Workflow wajib:
  1. `create_form` (title-only)
  2. `batch_update_form` (isi `updateFormInfo` + `createItem` pertanyaan)
  3. `get_form` (verifikasi + ambil link)
  4. kirim URL final ke user
- Jika user tidak memberi daftar pertanyaan, agent diminta membuat draft minimal 5 pertanyaan relevan.

## File yang Diubah

| File | Perubahan |
|------|-----------|
| `app/core/engine/agent_runner.py` | Tambah helper intent Forms + injeksi prompt `FORMS WORKFLOW MODE` |
| `tests/test_google_slides_template_intent.py` | Tambah test deteksi intent Forms |

## Command Verifikasi

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_google_slides_template_intent.py tests/test_google_mcp_slides_errors.py tests/test_google_mcp_reply_overrides.py
```

Hasil: `11 passed`

---

# Recap: Google Slides Template Mode — Auto Rapi untuk Prompt Non-Teknis

**Tanggal**: 2026-05-18
**Status**: ✅ Selesai

## Masalah

Prompt seperti `rapihkan kontennya jadikan 3 slide` masih sering menghasilkan layout jelek karena agent fokus menulis teks, bukan menyusun slide dengan shape yang rapi.

## Solusi

- Tambah deteksi intent relayout Slides di `agent_runner`
- Jika user minta rapihkan/restruktur slide, aktifkan **template mode** otomatis
- Template mode memaksa agent:
  - target jumlah slide dari prompt user (default `3`)
  - buat slide dengan pola `createSlide -> createShape -> insertText`
  - **tidak** menulis ke page/slide object ID
  - pakai maksimal 2–3 shape utama per slide
  - merangkum konten panjang menjadi poin inti agar tidak numpuk

## File yang Diubah

| File | Perubahan |
|------|-----------|
| `app/core/engine/agent_runner.py` | Tambah injeksi prompt `SLIDES TEMPLATE MODE` saat intent relayout terdeteksi |
| `tests/test_google_slides_template_intent.py` | Tambah test untuk deteksi intent dan ekstraksi jumlah slide |

## Command Verifikasi

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_google_slides_template_intent.py tests/test_google_mcp_slides_errors.py tests/test_google_mcp_reply_overrides.py
```

Hasil: `7 passed`

---

# Recap: Escalation Routing Fix — Reply-Based Operator Routing + LID Display Fix

**Tanggal**: 2026-05-12
**Status**: ✅ Selesai

## Masalah

1. **Routing eskalasi ambiguous**: `find_escalation_context` hanya ambil eskalasi *terbaru* — jika ada 10 eskalasi paralel, operator reply ke eskalasi ke-3 tapi agent respond ke eskalasi ke-10.
2. **LID number tampil sebagai nomor telepon**: Nomor seperti `62813334989922328` (17 digit LID) tampil di notifikasi eskalasi sebagai "WA Customer" karena `GetPNForLID` dipanggil dengan AD JID (dengan device suffix) bukan NonAD JID.
3. **`phone_number` di session diisi LID number**: Ketika LID resolution gagal, `phone_from` = LID number, disimpan ke `channel_config.phone_number` dan ditampilkan sebagai nomor telepon customer.

## Solusi

### Reply-based Routing (Fix utama untuk multi-eskalasi)

Operator **wajib REPLY** (quote) pesan notifikasi eskalasi. Go extract `quoted_text` dari `ContextInfo.QuotedMessage`, Python parse `case_id` dari teks itu, lookup session berdasarkan `metadata_["escalation_case_id"]`.

```
Eskalasi terjadi
  → case_id = "esc_1234567_abc123"
  → session.metadata_["escalation_case_id"] = case_id
  → notifikasi ke operator WAJIB di-REPLY
         ↓
Operator reply (quote) pesan eskalasi
  → Go: quotedText = ContextInfo.QuotedMessage.GetConversation()
  → Python: parse "ID Kasus: esc_1234567_abc123" dari quoted_text
  → Lookup session WHERE metadata_["escalation_case_id"] == case_id
  → Route ke session TEPAT ✓
  → Fallback: ambil eskalasi terbaru jika operator tidak quote
```

### LID Resolution Fix (Go)

```go
// Sebelum: pass AD JID → GetPNForLID selalu gagal
client.Store.LIDs.GetPNForLID(ctx, evt.Info.Sender)

// Sesudah: strip device suffix dulu
client.Store.LIDs.GetPNForLID(ctx, evt.Info.Sender.ToNonAD())

// Jika tetap gagal → phoneFrom = "" (bukan fallback ke LID number)
```

### Guard LID di Python

- `channels.py`: Jangan simpan `phone_number` ke session jika panjangnya > 15 digit (LID, bukan telepon)
- `escalation_tool.py`: Validasi `phone_number` — tolak jika > 15 digit
- `escalation_tool.py`: Hapus baris `WA JID` dari notifikasi (tidak perlu, membingungkan operator)

## File yang Diubah

| File | Perubahan |
|------|-----------|
| `wa-service/device_manager.go` | `GetPNForLID` pakai `ToNonAD()`; jika gagal set `phoneFrom=""`; extract `quoted_text` dari `ContextInfo.QuotedMessage`; kirim di webhook payload; `SendMessage` pakai `resolveJID()` |
| `app/api/channels.py` | Tambah `quoted_text` ke `WAIncomingMessage`; pass ke `find_escalation_context`; guard LID di `phone_number` (max 15 digit) |
| `app/api/wa_helpers.py` | `find_escalation_context` terima `quoted_text`, parse `case_id` via regex, query `session.metadata_["escalation_case_id"]`; fallback ke latest jika tidak ditemukan |
| `app/core/tools/escalation_tool.py` | Simpan `case_id` di `session.metadata_`; hapus `WA JID` line dari notifikasi; validasi `phone_number` ≤15 digit; instruksi operator ganti jadi "💬 REPLY pesan ini" |

## Format Notifikasi Baru

```
🚨 [CS AI] ESKALASI
━━━━━━━━━━━━━━━━━━━━━━━━
ID Kasus: esc_1234567_abc123
WA Customer: +628123456789
Nama: Wira Adi
Alasan: permintaan refund
Pesan: ...
━━━━━━━━━━━━━━━━━━━━━━━━
💬 REPLY pesan ini untuk menjawab customer ini.
Format balasan:
<OPERATOR>
Pesan: [instruksi]
```

## Teknik dari wago-project

Inspeksi `/home/bagas/wago-project` — wago sudah implement `GetPNForLID(ctx, v.Info.Sender.ToNonAD())` dengan benar + `SenderLID` field terpisah di webhook payload. Pola ini diadopsi ke wa-service.

---

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
## 2026-05-18 — Google MCP Re-auth Link Stabilization (WhatsApp-safe)

### Problem
- User menerima link reconnect Google yang sangat panjang dari endpoint `connect`.
- Saat dibuka lewat WhatsApp, URL kadang rusak/terpotong sehingga flow berakhir ke `.../callback` tanpa query `code`.
- Error yang muncul: `{"detail":[{"type":"missing","loc":["query","code"],"msg":"Field required"...}]}`.

### Root Cause
- URL OAuth `/authorize` berisi query sangat panjang (scope banyak) dan raw URL langsung dikirim ke user.
- Beberapa client (terutama WhatsApp/open-in-browser) tidak stabil untuk URL sepanjang itu.

### Changes Implemented
- File diubah: `/home/bagas/google-workspace-mcp/app/api/integrations.py`.
- Menambahkan mekanisme short-link server-side untuk auth URL:
  - Menyimpan raw authorize URL ke memory store dengan TTL 15 menit.
  - Menghasilkan token pendek (`t=<token>`).
  - Mengembalikan `auth_url` pendek yang aman dikirim via chat.
- Menambahkan endpoint redirect:
  - `GET /v1/integrations/google/start?t=<token>`
  - Validasi token + expiry, lalu `302 Redirect` ke raw authorize URL.
- Endpoint `POST /v1/integrations/google/connect` sekarang return:
  - `auth_url`: short URL (`/start?t=...`) untuk user klik.
  - `raw_auth_url`: URL panjang asli (opsional untuk debug/log internal).

### Expected Behavior After Patch
- Agent tetap mengirim link reconnect, tapi link jadi pendek dan stabil.
- User klik short-link, server yang mengarahkan ke URL authorize penuh.
- Risiko URL rusak/mangled di WhatsApp berkurang drastis.

### Operational Notes
- Karena short-link disimpan in-memory, token hilang saat service restart dan akan expired otomatis setelah 15 menit.
- Jika link expired, user tinggal request reconnect lagi (agent akan generate link baru).

### Follow-up: OAuth invalid_scope fix
- Dari log MCP `8002` dan integration `8003`, auth flow sempat gagal dengan callback:
  - `error=invalid_scope`
  - `error_description=Client was not registered with scope profile`
- Root cause: request scope mengandung scope pendek `profile`/`email` yang tidak diterima oleh authorization server MCP untuk client yang didaftarkan.
- Perbaikan:
  - Menghapus scope pendek `profile` dan `email` dari request OAuth.
  - Tetap memakai `openid` + scope URL Google yang dibutuhkan (`userinfo.email`, `userinfo.profile`, dst).
  - Memperbaiki endpoint callback agar bisa menangani `error` dan `error_description` secara graceful, tidak lagi menghasilkan `422` karena `code` kosong.

### Follow-up 2: invalid_scope (chat.memberships) remediation
- Gejala terbaru: callback kembali dengan `error=invalid_scope` dan pesan seperti:
  - `Client was not registered with scope https://www.googleapis.com/auth/chat.memberships`
- Akar masalah: dynamic client registration di authorization server belum menyimpan deklarasi scope selengkap scope yang diminta saat `/authorize`.
- Perbaikan di `/home/bagas/google-workspace-mcp/app/api/integrations.py`:
  - Menambahkan field `scope` saat `POST /register` pada:
    - `_get_or_register_client()` (registrasi awal)
    - `_register_new_client()` (forced re-register)
  - Memperluas preflight authorize check agar jika terdeteksi `invalid_scope` (selain `unregistered`) maka otomatis trigger re-register client lalu regenerate `auth_url`.
- Dampak:
  - Link reconnect baru akan mengarah ke client OAuth yang sudah terdaftar dengan scope yang dibutuhkan tools.

---

## 2026-05-18 — Google MCP Hardening + Live Smoke Suite (Final)

Runbook operasional tim: `docs/google-mcp-runbook.md`

### Ringkasan Hasil
- Integrasi Google MCP sudah distabilkan untuk flow OAuth/re-auth dan validasi tool live.
- Akses edit layanan utama terverifikasi live (safe/non-destruktif):
  - Sheets: create + write
  - Slides: create + batch update
  - Docs: create + modify text
  - Drive: create + update metadata
  - Calendar: create + update event
  - Gmail: draft (tanpa kirim)
  - Tasks: create list + create task
  - Forms: create + batch update
  - Contacts: create + update
- Guard anti-halu sudah ditambahkan: saat MCP timeout/unavailable, agent tidak boleh claim "lagi proses".
- Guard auth/scope ditingkatkan: jika error auth/scope muncul dari hasil tool, agent otomatis arahkan re-auth.

### Perubahan Kode Penting
1. `/home/bagas/google-workspace-mcp/app/api/integrations.py`
   - Migrasi flow ke OAuth Google langsung (external provider mode) + refresh token Google.
   - Perluasan scope Google (`mail.google.com`, calendar events/read, drive.file, docs/sheets readonly, dll).
   - Short-link reconnect (`/v1/integrations/google/start?t=...`) agar aman dipakai via WhatsApp.

2. `/home/bagas/managed-agents-project/app/core/tools/mcp_tool.py`
   - Runtime URL MCP Google tidak dipaksa localhost secara default.
   - Local override hanya aktif jika `WORKSPACE_MCP_PREFER_LOCAL=true`.

3. `/home/bagas/managed-agents-project/app/core/engine/agent_runner.py`
   - Override reply jika MCP unavailable agar tetap jujur (tidak claim progress palsu).
   - Deteksi auth/scope error diperluas (termasuk dari hasil step tool Google, bukan hanya connection error).
   - Tambah system notice schema usage untuk mengurangi argumen tool yang salah (`range_name`, dst).

4. Test suite baru:
   - `/home/bagas/managed-agents-project/tests/test_google_mcp_reply_overrides.py`
   - `/home/bagas/managed-agents-project/tests/test_google_mcp_live_smoke.py`

5. Helper script baru:
   - `/home/bagas/managed-agents-project/scripts/generate_google_mcp_reauth_link.py`

6. Makefile targets baru:
   - `mcp-smoke-live`
   - `mcp-smoke-live-strict`
   - `mcp-smoke-live-reauth`
   - `mcp-smoke-live-onboard`

### Command Operasional (Untuk Tim)

#### 1) Onboarding tester
```bash
make mcp-smoke-live-onboard
```

#### 2) Generate link re-auth fresh
```bash
make mcp-smoke-live-reauth
```

#### 3) Jalankan live smoke suite (safe)
```bash
make mcp-smoke-live
```

#### 4) Jalankan live smoke suite strict
```bash
make mcp-smoke-live-strict
```

#### 5) Jalankan pytest langsung (tanpa Make)
```bash
RUN_GOOGLE_MCP_LIVE_SMOKE=true \
GOOGLE_MCP_INTEGRATION_URL=http://localhost:8003 \
GOOGLE_MCP_URL=http://localhost:8002/mcp \
GOOGLE_MCP_EXTERNAL_USER_ID=62895619356936 \
GOOGLE_MCP_AGENT_ID=46ed1c39-c343-4d42-a5ff-2559f43efa0e \
/home/bagas/managed-agents-project/.venv/bin/python -m pytest -q tests/test_google_mcp_live_smoke.py
```

#### 6) Mode strict via pytest langsung
```bash
RUN_GOOGLE_MCP_LIVE_SMOKE=true \
GOOGLE_MCP_LIVE_SMOKE_STRICT=true \
GOOGLE_MCP_INTEGRATION_URL=http://localhost:8003 \
GOOGLE_MCP_URL=http://localhost:8002/mcp \
GOOGLE_MCP_EXTERNAL_USER_ID=62895619356936 \
GOOGLE_MCP_AGENT_ID=46ed1c39-c343-4d42-a5ff-2559f43efa0e \
/home/bagas/managed-agents-project/.venv/bin/python -m pytest -q tests/test_google_mcp_live_smoke.py
```

#### 7) Override target user/agent saat test
```bash
GOOGLE_MCP_EXTERNAL_USER_ID=<external_user_id> \
GOOGLE_MCP_AGENT_ID=<agent_id> \
make mcp-smoke-live
```

#### 8) Rebuild service terkait (jika ada perubahan Go/Python service)
```bash
make wa-dev-build
make wa-build
```

> Catatan: service Python (API 8000 / integration 8003) dan MCP (8002) tetap perlu restart sesuai cara deploy masing-masing environment (systemd/docker/supervisor/screen).

### Known Behavior / Non-Issue
- `create_form` gagal jika payload create mengandung field yang tidak diizinkan saat create (mis. description). Gunakan `title` saja saat create, lalu ubah via `batch_update_form`.
- `batch_update_form` harus pakai **Form ID edit** (mis. `1Wu...`), bukan responder ID (`1FA...`).
- `manage_event` update yang ubah waktu perlu menyertakan `start_time` dan `end_time`.
- `modify_sheet_values` wajib pakai `range_name` (bukan `range`).

### Bukti Validasi Terakhir
- Live suite terakhir: `9 passed` pada `tests/test_google_mcp_live_smoke.py`.
- Regression guard suite: lulus (`test_google_mcp_reply_overrides.py` + `test_mcp_fallbacks.py`).
