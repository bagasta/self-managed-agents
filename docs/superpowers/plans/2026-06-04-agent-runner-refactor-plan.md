# Agent Runner Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pecah `app/core/engine/agent_runner.py` (3118 baris) menjadi orchestrator tipis + modul guard/detector per-domain, **tanpa mengubah behavior runtime apa pun**.

**Architecture:** Pertahankan `agent_runner.py` sebagai **facade**: `run_agent()` tetap entrypoint, dan semua symbol private yang diimpor test tetap bisa di-import dari `app.core.engine.agent_runner` (re-export). Logic guard/detector/middleware dipindah ke modul kecil per-domain dengan dependency eksplisit. Pola identik dengan refactor `builder_tools.py` (Fase 1–2 di `2026-06-04-long-script-refactor-plan.md`).

**Tech Stack:** Python 3, LangChain/LangGraph (Deep Agents), pytest. Jalankan semua test dengan `PYTHONPATH=. .venv/bin/python -m pytest`.

---

## Konteks & Kaitan

- Ini adalah pendalaman dari **Fase 3** di `docs/superpowers/plans/2026-06-04-long-script-refactor-plan.md` (Agent Runner Boundary Audit). Plan master itu high-level; plan ini yang executable.
- Pelajaran wajib dari refactor terakhir (lihat memory `arthur_build_stall_fix` update 2026-06-04): refactor `builder_tools` aman, tapi **perubahan config (model + max_tokens) yang ikut ke-bundle di working tree yang sama** bikin regресi latency. **Refactor ini harus murni pindah kode — nol perubahan perilaku, prompt, model, atau config.**

## Prinsip Aman (WAJIB)

- Jangan rewrite besar dalam satu langkah. Satu task = satu domain.
- **Nol perubahan behavior.** Kalau menemukan bug saat memindah, catat — jangan perbaiki di task yang sama.
- `agent_runner.py` tetap facade: setiap symbol di "Re-export Contract" di bawah harus tetap importable.
- Setiap task punya test gate yang harus **match baseline** sebelum commit.
- Jangan sentuh dirty changes yang tidak terkait (seed_arthur, prompt_builder, builder_* yang belum di-commit).
- Jangan ubah: MCP-first routing, honest-failure/auth recovery, interrupt/cancel behavior, subagent behavior.

## Re-export Contract (INVARIANT)

Setelah **setiap** task, perintah ini HARUS sukses tanpa error (ini safety net utama — test banyak yang import symbol private ini):

```bash
PYTHONPATH=. .venv/bin/python -c "from app.core.engine.agent_runner import (
    run_agent,
    BlockTaskToolMiddleware,
    ExternalServiceFallbackGuardMiddleware,
    _build_google_mcp_auth_failure_reply,
    _build_google_mcp_unavailable_reply,
    _build_google_mcp_validation_reply,
    _builder_google_auth_agent_id,
    _candidate_external_user_ids,
    _direct_whatsapp_send_guard_reply,
    _extract_direct_whatsapp_confirmation_payload,
    _extract_google_mcp_step_error,
    _extract_requested_slide_count,
    _filter_whatsapp_unsafe_mcp_tools,
    _google_workspace_server_has_auth,
    _graph_result_from_output,
    _is_direct_whatsapp_meta_request,
    _is_direct_whatsapp_text_send_context,
    _is_google_auth_or_scope_error,
    _is_google_chat_intent,
    _is_google_forms_authoring_intent,
    _is_google_sheets_authoring_intent,
    _is_google_slides_relayout_intent,
    _is_operator_envelope,
    _needs_builder_create_completion,
    _needs_deploy_followup,
    _needs_google_forms_followup,
    _needs_google_sheets_followup,
    _needs_google_slides_followup,
    _needs_whatsapp_file_delivery_followup,
    _operator_escalation_reply_guard,
    _prioritize_direct_whatsapp_text_send_tools,
    _remove_google_workspace_mcp_server,
    _route_google_workspace_blocker_to_owner_if_customer,
    _task_result_guard_reply,
)
print('REEXPORT OK')"
```

Expected output: `REEXPORT OK`

> Catatan: simbol Google MCP (`_build_google_mcp_*`, `_extract_google_mcp_step_error`, `_extract_requested_slide_count`, `_is_google_*_intent`, `_needs_google_*_followup`, `_is_google_auth_or_scope_error`, `_candidate_external_user_ids`) **sudah di-import dari `google_mcp_support.py`** di `agent_runner.py` (~line 62), bukan didefinisikan di sini. Jangan pindahkan — cukup pastikan import existing tetap ada.

## Target File Structure

| Modul baru | Tanggung jawab | Fungsi yang dipindah (dari `agent_runner.py`) |
| --- | --- | --- |
| `app/core/engine/agent_identity.py` | Resolusi phone/owner/session | `_session_real_phone`, `_normalized_agent_operator_ids`, `_session_sender_phone`, `_is_customer_whatsapp_session`, `_owner_notification_target` |
| `app/core/engine/agent_google_routing.py` | Routing/runtime Google Workspace (bukan auth recovery inti) | `_google_workspace_server_has_auth`, `_remove_google_workspace_mcp_server`, `_google_workspace_customer_blocker_reply`, `_route_google_workspace_blocker_to_owner_if_customer`, `_is_google_chat_intent`, `_extract_auth_url_from_builder_steps`, `_builder_google_auth_agent_id`, `_append_builder_google_auth_link_if_needed` |
| `app/core/engine/agent_whatsapp_guards.py` | Deteksi & guard direct-send WhatsApp | `_is_direct_whatsapp_send_confirmation`, `_is_direct_whatsapp_send_request`, `_is_direct_whatsapp_meta_request`, `_is_direct_whatsapp_text_send_context`, `_extract_direct_whatsapp_confirmation_payload`, `_filter_whatsapp_unsafe_mcp_tools`, `_prioritize_direct_whatsapp_text_send_tools`, `_has_send_to_number_step`, `_looks_like_direct_send_success_claim`, `_has_prior_send_to_number_evidence`, `_has_reply_to_user_step`, `_has_prior_reply_to_user_evidence`, `_direct_whatsapp_send_guard_reply` |
| `app/core/engine/agent_reply_guards.py` | Reply guard task/escalation/operator | `_task_result_guard_reply`, `_operator_escalation_reply_guard`, `_operator_message_payload`, `_is_operator_envelope` |
| `app/core/engine/agent_followups.py` | Deteksi & directive followup (deploy/file/builder-create/website) | `_has_external_service_fallback_blocked_step`, `_step_text`, `_has_public_url_in_text`, `_has_public_url_in_steps`, `_extract_shared_workspace_file_path`, `_extract_shared_workspace_file_from_steps`, `_has_whatsapp_media_send_step`, `_is_whatsapp_file_delivery_request`, `_needs_whatsapp_file_delivery_followup`, `_whatsapp_file_delivery_followup_message`, `_is_website_or_app_request`, `_has_code_creation_evidence`, `_needs_builder_create_completion`, `_builder_create_completion_directive`, `_needs_deploy_followup`, `_deploy_followup_message` |
| `app/core/engine/agent_middleware.py` | Middleware LangGraph | `BlockTaskToolMiddleware`, `ExternalServiceFallbackGuardMiddleware` |

`agent_runner.py` setelah selesai: hanya `run_agent()` + `_parse_step_result_json`/`_graph_result_from_output` (helper generik yang erat dengan run loop) + blok import-dan-reexport.

---

## Task 0: Baseline Karakterisasi

**Files:**
- Tidak ada perubahan kode. Hanya rekam baseline.

- [ ] **Step 1: Jalankan full regression suite terkait & rekam jumlah pass**

Run:
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_tool_call_orchestration.py \
  tests/test_mcp_tool_priority.py \
  tests/test_mcp_fallbacks.py \
  tests/test_whatsapp_progress.py \
  tests/test_whatsapp_direct_send.py \
  tests/test_session_lock_and_history.py \
  tests/test_reply_guard.py \
  tests/test_deploy_path.py \
  tests/test_ghost_reply.py \
  tests/test_google_mcp_reply_overrides.py \
  tests/test_google_mcp_subagent_routing.py \
  tests/test_google_mcp_slides_errors.py \
  tests/test_google_slides_template_intent.py \
  tests/test_google_forms_followup_detection.py \
  tests/test_google_sheets_followup_detection.py \
  tests/test_subscription_service.py
```
Expected: catat angka, mis. `N passed`. Angka ini jadi **acuan wajib** untuk semua task berikutnya. Kalau ada yang fail SEBELUM refactor, stop dan laporkan — jangan mulai refactor di atas suite merah.

- [ ] **Step 2: Jalankan Re-export Contract check (lihat bagian atas), pastikan `REEXPORT OK`.**

- [ ] **Step 3: Commit checkpoint (kosong/marker) bila perlu, atau lanjut langsung.**

---

## Task 1: Ekstrak Identity Helpers → `agent_identity.py`

Mulai dari grup paling rendah-risiko (pure functions, sedikit dependency).

**Files:**
- Create: `app/core/engine/agent_identity.py`
- Modify: `app/core/engine/agent_runner.py` (hapus definisi, tambah import+reexport)

- [ ] **Step 1: Buat modul baru** berisi fungsi: `_session_real_phone`, `_normalized_agent_operator_ids`, `_session_sender_phone`, `_is_customer_whatsapp_session`, `_owner_notification_target`. Copy verbatim dari `agent_runner.py`. Bawa juga import yang dibutuhkan (`Session` model, `normalize_phone`, `is_probable_whatsapp_lid`, dll) — cek baris import di `agent_runner.py` untuk yang relevan.

- [ ] **Step 2: Di `agent_runner.py`**, hapus 5 definisi tsb, ganti dengan:
```python
from app.core.engine.agent_identity import (
    _is_customer_whatsapp_session,
    _normalized_agent_operator_ids,
    _owner_notification_target,
    _session_real_phone,
    _session_sender_phone,
)
```

- [ ] **Step 3: Re-export Contract check** — jalankan perintah `REEXPORT OK` di atas. Expected: `REEXPORT OK`.

- [ ] **Step 4: Test gate**
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_whatsapp_direct_send.py tests/test_session_lock_and_history.py tests/test_ghost_reply.py
```
Expected: jumlah pass = subset baseline (tidak ada fail baru).

- [ ] **Step 5: Commit**
```bash
git add app/core/engine/agent_identity.py app/core/engine/agent_runner.py
git commit -m "refactor: extract session/phone identity helpers from agent_runner"
```

---

## Task 2: Ekstrak Google Routing Helpers → `agent_google_routing.py`

> Hanya routing/runtime Google + builder auth-link. **JANGAN** sentuh auth recovery inti yang ada di `google_mcp_support.py`.

**Files:**
- Create: `app/core/engine/agent_google_routing.py`
- Modify: `app/core/engine/agent_runner.py`

- [ ] **Step 1: Buat modul** berisi: `_google_workspace_server_has_auth`, `_remove_google_workspace_mcp_server`, `_google_workspace_customer_blocker_reply`, `_route_google_workspace_blocker_to_owner_if_customer`, `_is_google_chat_intent`, `_extract_auth_url_from_builder_steps`, `_builder_google_auth_agent_id`, `_append_builder_google_auth_link_if_needed`. Copy verbatim. `_route_..._if_customer` butuh `_session_real_phone`/`_owner_notification_target` → import dari `agent_identity`. Bawa juga dependency lain (wa_client send, logger, dll) sesuai yang dipakai fungsi-fungsi ini di `agent_runner.py`.

- [ ] **Step 2: Di `agent_runner.py`**, hapus 8 definisi, ganti dengan import+reexport dari `agent_google_routing`.

- [ ] **Step 3: Re-export Contract check.** Expected: `REEXPORT OK`.

- [ ] **Step 4: Test gate**
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_google_mcp_reply_overrides.py \
  tests/test_google_mcp_subagent_routing.py \
  tests/test_reply_guard.py \
  tests/test_mcp_tool_priority.py
```
Expected: tidak ada fail baru vs baseline.

- [ ] **Step 5: Commit**
```bash
git add app/core/engine/agent_google_routing.py app/core/engine/agent_runner.py
git commit -m "refactor: extract Google Workspace routing helpers from agent_runner"
```

---

## Task 3: Ekstrak WhatsApp Send Guards → `agent_whatsapp_guards.py`

**Files:**
- Create: `app/core/engine/agent_whatsapp_guards.py`
- Modify: `app/core/engine/agent_runner.py`

- [ ] **Step 1: Buat modul** berisi 13 fungsi WhatsApp direct-send (lihat tabel File Structure). Copy verbatim + import dependency yang dipakai (`BaseMessage`, regex helpers, dll).

- [ ] **Step 2: Di `agent_runner.py`**, hapus 13 definisi, ganti dengan import+reexport.

- [ ] **Step 3: Re-export Contract check.** Expected: `REEXPORT OK`.

- [ ] **Step 4: Test gate**
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_whatsapp_direct_send.py tests/test_whatsapp_progress.py
```
Expected: tidak ada fail baru.

- [ ] **Step 5: Commit**
```bash
git add app/core/engine/agent_whatsapp_guards.py app/core/engine/agent_runner.py
git commit -m "refactor: extract WhatsApp direct-send guards from agent_runner"
```

---

## Task 4: Ekstrak Reply Guards → `agent_reply_guards.py`

**Files:**
- Create: `app/core/engine/agent_reply_guards.py`
- Modify: `app/core/engine/agent_runner.py`

- [ ] **Step 1: Buat modul** berisi: `_task_result_guard_reply`, `_operator_escalation_reply_guard`, `_operator_message_payload`, `_is_operator_envelope`. Copy verbatim + dependency.

- [ ] **Step 2: Hapus 4 definisi di `agent_runner.py`, ganti import+reexport.**

- [ ] **Step 3: Re-export Contract check.** Expected: `REEXPORT OK`.

- [ ] **Step 4: Test gate**
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_reply_guard.py tests/test_whatsapp_progress.py tests/test_whatsapp_direct_send.py
```
Expected: tidak ada fail baru.

- [ ] **Step 5: Commit**
```bash
git add app/core/engine/agent_reply_guards.py app/core/engine/agent_runner.py
git commit -m "refactor: extract task/escalation reply guards from agent_runner"
```

---

## Task 5: Ekstrak Followup Detectors → `agent_followups.py`

**Files:**
- Create: `app/core/engine/agent_followups.py`
- Modify: `app/core/engine/agent_runner.py`

- [ ] **Step 1: Buat modul** berisi 16 fungsi followup/deploy/file-delivery/builder-create/website (lihat tabel). Copy verbatim + dependency.

- [ ] **Step 2: Hapus 16 definisi di `agent_runner.py`, ganti import+reexport.**

- [ ] **Step 3: Re-export Contract check.** Expected: `REEXPORT OK`.

- [ ] **Step 4: Test gate**
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_deploy_path.py tests/test_reply_guard.py tests/test_whatsapp_progress.py
```
Expected: tidak ada fail baru.

- [ ] **Step 5: Commit**
```bash
git add app/core/engine/agent_followups.py app/core/engine/agent_runner.py
git commit -m "refactor: extract deploy/file/builder followup detectors from agent_runner"
```

---

## Task 6: Ekstrak Middleware → `agent_middleware.py`

**Files:**
- Create: `app/core/engine/agent_middleware.py`
- Modify: `app/core/engine/agent_runner.py`

- [ ] **Step 1: Buat modul** berisi `BlockTaskToolMiddleware`, `ExternalServiceFallbackGuardMiddleware`. Copy verbatim. Bawa import `AgentMiddleware` dan dependency guard (mis. helper dari `agent_policy`). Kalau middleware butuh detector yang sudah dipindah di Task 3/5, import dari modul barunya.

- [ ] **Step 2: Hapus 2 definisi di `agent_runner.py`, ganti import+reexport.**

- [ ] **Step 3: Re-export Contract check.** Expected: `REEXPORT OK`.

- [ ] **Step 4: Test gate**
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_mcp_tool_priority.py tests/test_google_mcp_subagent_routing.py tests/test_tool_call_orchestration.py
```
Expected: tidak ada fail baru.

- [ ] **Step 5: Commit**
```bash
git add app/core/engine/agent_middleware.py app/core/engine/agent_runner.py
git commit -m "refactor: extract LangGraph middleware from agent_runner"
```

---

## Task 7: Checkpoint — Verifikasi Full Suite + Ukur Penurunan

**Files:** tidak ada perubahan kode.

- [ ] **Step 1: Full suite (sama persis dengan Task 0)** — bandingkan jumlah pass dengan baseline. HARUS identik.

- [ ] **Step 2: Ukur line count**
```bash
wc -l app/core/engine/agent_runner.py app/core/engine/agent_*.py
```
Expected: `agent_runner.py` turun signifikan (target < ~2000 baris setelah Task 1–6; sisanya didominasi body `run_agent`).

- [ ] **Step 3: Update `docs/recap.md`** dengan entri refactor agent_runner (tanggal, modul yang dibuat, hasil test, line count sebelum/sesudah). Pola sama dengan entri recap refactor builder_tools.

- [ ] **Step 4: Commit**
```bash
git add docs/recap.md
git commit -m "docs: recap agent_runner guard/middleware extraction"
```

---

## Task 8 (OPSIONAL, RISIKO TINGGI): Dekomposisi Body `run_agent()`

> Body `run_agent` (~1770 baris) penuh local state & closure. Ini **paling berharga tapi paling berisiko**. Kerjakan HANYA setelah Task 1–7 hijau, dan **PR/commit terpisah**. Kalau ragu, stop di Task 7 — file sudah jauh lebih sehat.

**Prasyarat:** Blok yang mau diekstrak harus punya boundary jelas (input/output eksplisit, bukan menyentuh banyak local var). Kalau sebuah blok belum punya test, **tulis characterization test dulu** (snapshot output untuk input tetap) sebelum memindah.

Kandidat ekstraksi (urut dari paling aman), masing-masing jadi fungsi terpisah di `app/core/engine/agent_run_phases.py` dengan parameter eksplisit + return value:

- [ ] **Step 1:** `_pre_run_quota_gate(agent_model, db, log) -> AgentRunResult | None` — blok gate kuota (~baris 1423–1454). Return `AgentRunResult` kalau diblokir, `None` kalau lolos. Test: mock subscription habis → return reply kuota; cukup → `None`.
- [ ] **Step 2:** Re-export Contract check + `tests/test_subscription_service.py` gate. Commit.
- [ ] **Step 3:** Ekstrak blok post-run guard application (urutan pemanggilan `_task_result_guard_reply` / `_operator_escalation_reply_guard` / `_direct_whatsapp_send_guard_reply` / followup directives) ke `_apply_post_run_guards(...)` dengan state eksplisit. Test: full suite guard.
- [ ] **Step 4:** Re-export Contract check + full suite. Commit.
- [ ] **Step 5:** Ekstrak persistence (tulis messages + run_record + token usage) ke `_persist_run(...)`. Test: `tests/test_session_lock_and_history.py`.
- [ ] **Step 6:** Re-export Contract check + full suite. Commit.

Setelah ini `run_agent()` idealnya jadi orchestrator ~150–300 baris yang memanggil fase-fase tersebut.

---

## Definition of Done

- `agent_runner.py` tidak lagi menyimpan definisi guard/detector/middleware; hanya `run_agent` + helper run-loop generik + import/reexport.
- Re-export Contract check selalu `REEXPORT OK`.
- Full regression suite (Task 0) pass dengan jumlah identik baseline.
- Nol perubahan behavior/prompt/model/config (diff murni pindah kode).
- `docs/recap.md` diperbarui.
- (Jika Task 8 dikerjakan) `run_agent` jadi orchestrator tipis.

## Stop Conditions

Berhenti dan evaluasi ulang kalau:

- Jumlah pass turun vs baseline (regресi).
- Re-export Contract check gagal (`ImportError`).
- Ada perubahan behavior yang tidak disengaja (mis. wording reply, urutan tool, routing MCP).
- Muncul circular import antar modul `agent_*` baru → hentikan, atur ulang boundary (taruh shared helper di modul paling bawah, mis. `agent_identity`).
- Dirty changes user (seed_arthur, prompt_builder, builder_* belum-commit) ikut terseret ke diff refactor.
- Task 8: sebuah blok ternyata menyentuh terlalu banyak local state untuk diekstrak aman → biarkan inline, dokumentasikan, jangan paksa.
