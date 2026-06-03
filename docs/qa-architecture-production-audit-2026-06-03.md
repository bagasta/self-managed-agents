# QA Arsitektur & Kesiapan Produksi — 2026-06-03

Audit menyeluruh atas cara kerja agent, dengan fokus utama pada **Arthur saat membuat system message + SOP**. Semua temuan diverifikasi langsung ke kode (bukan asumsi). Melengkapi `docs/qa-production-agent-workflow-audit-2026-06-03.md`.

## Verdict singkat

**Belum siap produksi penuh.** Pipeline build agent jalan, tapi ada celah yang membuat agent bisnis bisa **bertindak di luar SOP** dan biaya bisa **tak terkendali**. Yang paling berbahaya bukan bug crash — melainkan kontrol yang hanya berupa instruksi teks (soft), bukan enforcement runtime (hard).

Severity: **P0** = blokir launch, **P1** = harus sebelum buka publik, **P2** = utang teknis.

---

## A. Arthur: system message + SOP (fokus utama)

### A1. [P0] SOP "draft/needs_review" tidak punya hard gating — hanya himbauan teks
**Bukti:**
- `prompt_builder.py:199-206` — kalau `maturity ∈ {draft, needs_review}` hanya menambah kalimat *"kamu hanya boleh intake, klarifikasi, ringkasan, eskalasi"*.
- `agent_tool_setup.py` — satu-satunya pencabutan tool runtime adalah `deploy` (line ~259-263). **Tidak ada** pencabutan tool berdasarkan `maturity`/`owner_review_required`.

**Kenapa bahaya:** SOP draft/generic tetap punya akses penuh ke `send_whatsapp_document`, escalation, payment-flow, dll. Larangan "hanya intake/klarifikasi" murni teks → LLM bisa mengabaikannya dan mengeksekusi order/booking/refund/approve dengan SOP setengah jadi. Inilah sumber rasa "SOP-nya nggak dipatuhi".

**Fix:** Saat `maturity ∈ {draft, needs_review}` atau `owner_review_required=True`, cabut secara fisik tool aksi-final (media send, payment confirm) di `agent_tool_setup`, sisakan hanya intake/recall/escalate. Enforcement, bukan prompt.

### A2. [P1] Tabel `agent_operating_manual` membuang field SOP penting (lossy persistence)
**Bukti:**
- `agent_sop_service.py:508-538` membangun manual dengan `human_approval_points`, `validation_checklist`, `escalation_rules` (level-atas), `state_plan`, `knowledge_plan`, `memory_plan`.
- `models/agent_operating_manual.py:24-34` hanya punya kolom: `source, domain, domain_confidence, maturity, owner_review_required, missing_context, assumptions, workflows, created_by_agent_id, reviewed_by, reviewed_at`.
- `agent_sop_service.py:924-931` (upsert) hanya menulis subset itu; `:861-876` (row→artifact) hanya membaca subset itu.

**Kenapa bahaya:** Artifact lengkap pertama ada di `tools_config.operating_manual`, tapi begitu runtime membaca **row DB** (`get_latest_agent_operating_manual`), checklist validasi, titik approval manusia, dan state-plan **hilang**. Justru bagian itulah yang menahan agent bisnis dari improvisasi. Versi DB lebih sempit dari versi yang dilihat owner saat create.

**Fix:** Tambah kolom `artifact JSONB` (simpan manual ternormalisasi penuh) ATAU kolom eksplisit untuk field yang hilang. Backfill dari `agents.tools_config->operating_manual`. Format prompt baca artifact penuh.

### A3. [P1] Kegagalan baca SOP dari DB di-swallow diam-diam
**Bukti:** `agent_sop_service.py:879-897` — `except Exception: pass` lalu fallback ke `tools_config`. Tidak ada log, tidak ada `agent_id`. Dipakai sebelum assembly prompt di `agent_runner.py:1420-1425`.

**Kenapa bahaya:** Masalah migrasi/row korup/permission DB tersembunyi. Agent jalan dengan SOP embedded basi sementara `verify_agent` & runtime tampak sehat → drift senyap.

**Fix:** Log exception + `agent_id`; munculkan readiness blocker untuk "SOP load failure". Hanya jalur "tabel belum dimigrasi (dev)" yang boleh fallback senyap.

### A4. [P0] Validator delivery file masih wajib `send_whatsapp_document`, bertentangan dengan kontrak parent-delivery
**Bukti:**
- `system-message-builder.md` & `builder_tools.py:573-575,633-636` mengajarkan kontrak benar: subagent tulis ke `/workspace/shared/<file>`, return `SIAP_DIKIRIM_PARENT`, **parent** yang kirim.
- Tapi `builder_tools.py:1845-1846` masih: `if "send_whatsapp_document" not in instructions: errors.append(...)`. Pola sama di `~3732`, `~4761`, `~5061` (validate + create_agent).

**Kenapa bahaya:** Kontrak runtime = parent delivery, tapi validator cuma cek satu nama tool. Arthur terdorong menulis instruksi membingungkan hanya demi lolos validasi; marker aman (`/workspace/shared`, `SIAP_DIKIRIM_PARENT`, "subagent dilarang kirim WA") tidak divalidasi.

**Fix:** Ganti cek "wajib sebut `send_whatsapp_document`" → validator kontrak parent-delivery: wajib ada `/workspace/shared` + `SIAP_DIKIRIM_PARENT` + larangan subagent kirim WA + parent media-send setelah artifact balik.

### A5. [P1] Fallback blueprint/instruksi generik dipakai senyap saat LLM writer gagal
**Bukti:** `_INSTRUCTION_WRITER_MODEL = "deepseek/deepseek-v4-pro"` (timeout 45s, `builder_tools.py:2885-2896`). Saat JSON generator gagal dipulihkan → `_fallback_agent_blueprint` / `_fallback_agent_instructions` (`:2118,:2614`) dengan assumption *"blueprint fallback dibuat karena output JSON tidak bisa dipulihkan"*.

**Kenapa bahaya:** Agent bisa go-live dengan SOP generik tanpa sinyal jelas ke owner bahwa ini hasil fallback, bukan hasil pemahaman bisnis. Kualitas turun diam-diam.

**Fix:** Kalau jalur fallback aktif, paksa `maturity=needs_review` + `owner_review_required=True` + readiness warning eksplisit ke owner ("SOP ini hasil cadangan, perlu kamu review").

### A6. [P2] `maturity` default agresif ke `usable`
**Bukti:** `builder_tools.py:3008-3009,3507-3508` — `manual.setdefault("maturity","usable")`; prompt menyuruh model jangan set `needs_review` "hanya karena harga/detail belum lengkap".

**Risiko:** Bias ke "usable" + A1 (tanpa hard gating) = agent dengan data kritis belum lengkap tetap punya akses aksi penuh. Pertimbangkan default `needs_review` sampai owner konfirmasi sekali.

---

## B. Arsitektur lebih luas

### B1. [P0] Kuota token TIDAK di-enforce
**Bukti:** `subscription_service.py` mendefinisikan `token_quota` per plan dan `max_agents` **di-cek** (line 343: blok kalau `agents_used >= max_agents`). Tapi `tokens_used` hanya **dicatat setelah run** (`agent_runner.py:1944` `run_record.tokens_used = ...`). Tidak ada cek pre-run yang memblokir saat `tokens_used >= token_quota`.

**Kenapa bahaya:** Trial 2M token bisa dilampaui tanpa batas → biaya OpenRouter tak terkendali, monetisasi bocor. Ini risiko bisnis langsung di produksi.

**Fix:** Pre-run gate di entry (`messages.py`/`agent_runner`): tolak/halt + pesan upgrade kalau `tokens_used >= token_quota` (hormati `grace_until`).

### B2. [P1] Keamanan sandbox tak terverifikasi + duplikasi modul
**Bukti:** Ada **4** file sandbox/backend: `app/core/infra/sandbox.py`, `app/core/sandbox.py`, `app/core/engine/deep_agent_backend.py`, `app/core/deep_agent_backend.py`. Grep flag keamanan Docker (`network_disabled`, `mem_limit`, `pids_limit`, `cap_drop`, `read_only`, `user=`) **tidak menemukan apa pun**.

**Kenapa bahaya:** Kalau container `execute` punya akses jaringan penuh + tanpa limit memori/CPU/pid + jalan sebagai root → eksekusi kode dari konten customer = permukaan serangan. Duplikasi modul = risiko edit file yang salah/dead code.

**Fix:** Verifikasi config container aktual; set `mem_limit`, `nano_cpus`, `pids_limit`, `cap_drop=ALL`, non-root user, dan pertimbangkan `network_disabled` default. Konsolidasikan 4 file → satu sumber kebenaran.

### B3. [P1] Test suite penuh tidak hijau (hang)
**Bukti (dari audit sebelumnya, masih relevan):** suite terfokus lulus (`test_builder_tools.py` 105 passed) tapi `pytest -q` penuh menggantung & harus dibunuh.

**Fix:** Jalankan per-direktori dengan `--timeout`/`--maxfail=1`, isolasi test yang hang, jadikan gate rilis lebih kecil tapi wajib hijau.

### B4. [Bagus] WA identity / LID guard sudah benar
`utils/wa_identity.py` — `is_probable_whatsapp_lid` (>15 digit atau `@lid`) menolak LID sebagai provisioning ID; WA wajib `channel_config.phone_number` nyata. Ini menutup bug user-row salah. Pertahankan.

### B5. [Bagus] Tool Capability Registry + reply_guard
`tool_capability_registry.py` membangun "Runtime Tool Contract" dari capability aktual dan `reply_guard` memblok klaim halu (sudah-eskalasi, sudah-deploy, sudah-kirim-file). Arah benar. **Catatan:** ini mencegah *klaim* halu di teks balasan, **bukan** mencegah *aksi* di luar SOP (lihat A1).

### B6. [P2] Memory scoping benar, tapi awas `scope=None` global
`memory_service.py` — `soul` global (`scope=None`), sisanya scoped `external_user_id`. Aman dari cross-user leak. Pastikan tidak ada penulis yang lupa isi `scope` untuk data per-customer (mis. `:369` `upsert_memory(..., scope=scope)` — verifikasi `scope` selalu terisi di jalur customer).

---

## Urutan perbaikan disarankan

1. **B1 token quota gate** — risiko biaya/bisnis langsung, fix sempit.
2. **A1 hard tool gating per maturity** — akar "SOP tidak dipatuhi".
3. **A4 validator parent-delivery** — high-confidence, terkait bug WA/subagent yang sudah teramati.
4. **A2 persistensi SOP penuh (JSONB artifact) + backfill**.
5. **A3 + A5 — hentikan swallow senyap & fallback senyap; ubah jadi readiness signal**.
6. **B2 audit & konsolidasi sandbox**.
7. **B3 stabilkan suite**; tambah regресi untuk: gating maturity, parent-delivery, preservasi field SOP, quota gate.
8. Migrasi di staging → seed Arthur → smoke test WA nyata: buat agent bisnis, agent generated-file, update agent, cek status owner.

---

## Catatan metodologi
Diverifikasi langsung: `builder_tools.py`, `prompt_builder.py`, `agent_sop_service.py`, `models/agent_operating_manual.py`, `agent_runner.py`, `agent_tool_setup.py`, `reply_guard.py`, `tool_capability_registry.py`, `subscription_service.py`, `utils/wa_identity.py`, `memory_service.py`, `channels.py`. Belum diverifikasi langsung (perlu lanjutan): config container Docker aktual (B2), penyebab hang suite (B3).
