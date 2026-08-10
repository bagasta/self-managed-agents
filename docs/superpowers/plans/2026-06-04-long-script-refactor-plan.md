# Long Script Refactor Plan

Tanggal: 2026-06-04
Scope: backend/runtime Arthur dan file panjang di managed-agents-project.

## Tujuan

Kurangi risiko "bom waktu" dari file ribuan baris tanpa mengubah behavior yang sudah benar. Refactor harus bertahap, testable, dan mempertahankan public API lama selama transisi.

## Prinsip Aman

- Jangan rewrite besar dalam satu langkah.
- Jangan ganti behavior sambil memindah kode, kecuali bug fix kecil yang sengaja dicatat.
- Pertahankan import lama sebagai facade sampai semua call site aman.
- Satu fase hanya boleh punya satu tema domain.
- Setiap fase harus punya test gate sebelum lanjut.
- Jangan sentuh dirty changes yang tidak terkait.
- Untuk perubahan runtime Arthur, validasi dengan `PYTHONPATH=. .venv/bin/python`.

## Snapshot File Panjang

| Prioritas | File | Ukuran terakhir | Risiko utama |
| --- | --- | ---: | --- |
| P0 | `app/core/tools/builder_tools.py` | 401 lines setelah ekstraksi katalog, identity helpers, Google builder helpers, JSON helpers, intent/workflow classifiers, fallback writers, read-only tool factory, user/quota tool factory, planning tool factory, soul writer factory, blueprint writer factory, operating manual writer factory, instruction writer factory, verify tool factory, validation tool factory, agent management tool factory, connector tool factory, channel tool factory, create tool factory, update tool factory, verify helpers, update helpers, dan runtime text helpers | Facade utama kini terutama wiring dan writer bridge |
| P0 | `app/core/engine/agent_runner.py` | 3118 lines | Hot path runtime, history, MCP, WA typing/progress, prompt, tool execution |
| P1 | `app/core/engine/google_mcp_support.py` | 2629 lines | Auth recovery, MCP prep, wrapper behavior, reply override bercampur |
| P1 | `app/api/channels.py` | 1742 lines | API channel terlalu banyak domain dalam satu route file |
| P2 | `UI-DEV/app.js` | 1635 lines | Frontend state/action/UI bercampur, tapi bukan fokus backend saat ini |
| P2 | `app/core/engine/prompt_builder.py` | 1040 lines | Prompt sections mulai besar, perlu modularisasi setelah behavior stabil |

## Fase 0 - Safety Baseline

Status: completed

Checklist:

- [x] Hitung file paling panjang.
- [x] Ekstrak katalog statis builder dari `builder_tools.py` ke `builder_catalog.py`.
- [x] Pertahankan import lama `AGENT_PRESETS`, `RUNTIME_LIMITATIONS`, `_TOOLS_CONFIG_DOCS`.
- [x] Jalankan focused regression suite Arthur/builder/WA/MCP.
- [ ] Tambahkan/refresh dokumentasi recap setelah refactor cukup signifikan.

Acceptance criteria:

- Existing tests tetap pass.
- Tidak ada public function builder yang hilang.
- Tidak ada perubahan behavior runtime yang disengaja di fase ini.

Validasi:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_builder_tools.py \
  tests/test_agent_builder_phase1.py \
  tests/test_agent_builder_phase2.py \
  tests/test_agent_builder_phase4.py \
  tests/test_agent_quota_service.py \
  tests/test_whatsapp_progress.py \
  tests/test_whatsapp_direct_send.py \
  tests/test_tool_call_orchestration.py \
  tests/test_mcp_tool_priority.py
```

## Fase 1 - Builder Tools Domain Split

Status: completed

Target: turunkan `builder_tools.py` tanpa mengubah tool contract.

Urutan aman:

1. [x] Pindahkan ownership/policy helpers ke `app/core/tools/builder_identity.py`.
   - Kandidat: `_owner_variants`, `_is_probable_lid`, `_best_owner_identifier`, `_agent_belongs_to_owner`, `_owner_filter`, `_latest_owned_agent_for_trial`, `_blocked_agent_policy_reason`.
   - Tetap re-export atau import balik di `builder_tools.py` kalau test masih mengimpor helper lama.

2. [x] Pindahkan Google builder helpers ke `app/core/tools/builder_google.py`.
   - Kandidat: `_google_workspace_mcp_server_config`, `_enable_google_workspace_tools`, `_has_google_workspace_tools`, `_google_workspace_option`, `_negates_google_workspace`.
   - Jangan ubah kontrak `generate_google_auth_link` atau `update_agent(enable_google_workspace=True)`.

3. [x] Pindahkan preset detection dan workflow classifier ke `app/core/tools/builder_intent.py`.
   - Kandidat: `_detect_preset`, `_detect_preset_from_config`, `_looks_like_*`, `_critical_workflow_config_errors`.
   - Test harus membuktikan routing preset lama tetap sama.

4. [x] Pindahkan JSON/LLM parsing helpers ke `app/core/tools/builder_json.py`.
   - Kandidat: `_parse_json_arg`, `_strip_json_wrapper`, `_extract_balanced_json_object`, `_repair_llm_json_text`, `_complete_truncated_json`, `_parse_llm_json_object`.
   - Ini rendah risiko karena pure function.

5. [x] Pindahkan fallback writer ke `app/core/tools/builder_fallbacks.py`.
   - Kandidat: `_fallback_agent_blueprint`, `_fallback_agent_instructions`, `mark_manual_needs_review_if_fallback`.
   - Jangan ubah output text sebelum ada snapshot/focused tests.

Acceptance criteria:

- `build_builder_tools(...)` tetap entrypoint tunggal.
- Semua tool names yang diekspos Arthur tetap sama.
- Test lama yang import dari `app.core.tools.builder_tools` tetap pass.
- Line count `builder_tools.py` turun bertahap, bukan karena logic dihapus.

Test gate:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_builder_tools.py
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_agent_builder_phase2.py tests/test_agent_builder_phase4.py
```

## Fase 2 - Break Up `build_builder_tools()`

Status: completed

Target: pecah function 3000+ lines tanpa mengganti daftar tool.

Strategi:

- Buat factory kecil per kategori tool:
  - [x] `build_builder_read_tools(...)`
  - [x] `build_builder_user_tools(...)`
  - [x] `build_builder_planning_tools(...)`
  - [x] `build_builder_soul_tools(...)`
  - [x] `build_builder_blueprint_tools(...)`
  - [x] `build_builder_manual_tools(...)`
  - [x] `build_builder_instruction_tools(...)`
  - [x] `build_builder_verify_tools(...)`
  - [x] `build_builder_validation_tools(...)`
  - [x] `build_builder_management_tools(...)`
  - [x] `build_builder_connector_tools(...)`
  - [x] `build_builder_channel_tools(...)`
  - [x] `build_builder_create_tools(...)`
  - [x] `build_builder_update_tools(...)`
  - `build_builder_connector_tools(...)`
- `build_builder_tools(...)` tetap facade yang menggabungkan list tool.
- Hindari dependency global baru. Parameter yang dibutuhkan harus eksplisit.
- Jangan ubah decorator `@tool` behavior tanpa test.

Acceptance criteria:

- Urutan tool tetap kompatibel kecuali ada alasan eksplisit.
- Arthur masih punya kategori:
  - User Management
  - Plan & Billing
  - Agent Builder
  - Agent Management
  - Channel Management
  - Workspace/App Connectors
  - Runtime Support
- Runtime probe aman untuk create/update/delete temp agent tetap bisa dijalankan.

Test gate:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_builder_tools.py tests/test_tool_call_orchestration.py
```

## Fase 3 - Agent Runner Boundary Audit

Target: jangan langsung bedah `agent_runner.py` sebelum boundary lama dipahami.

Audit dulu:

- Mapping `run_agent(...)` dari input sampai final response.
- Boundary yang sudah diekstrak sebelumnya:
  - `agent_tool_setup.py`
  - `tool_builder.py`
  - `prompt_builder.py`
  - `subagent_builder.py`
  - `wa_progress.py`
- Identifikasi block yang masih bisa dipindah tanpa behavior change:
  - session/history loading
  - WA typing lifecycle
  - Google MCP runtime preparation call site
  - reply override handling
  - quota accounting wrapper

Aturan:

- Jangan ubah MCP-first routing.
- Jangan ubah honest-failure/auth recovery behavior.
- Jangan ubah interrupt/cancel behavior.
- Jangan sentuh subagent behavior kecuali test spesifik dibuat dulu.

Test gate:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_tool_call_orchestration.py \
  tests/test_mcp_tool_priority.py \
  tests/test_whatsapp_progress.py \
  tests/test_session_lock_and_history.py
```

## Fase 4 - Google MCP Support Split

Target: pisahkan Google orchestration agar debug lebih mudah.

Kandidat module:

- `google_mcp_auth.py`: token lookup, auth URL, reauth, external_user_id variants.
- `google_mcp_runtime.py`: prepare runtime, tool ordering, MCP client configuration.
- `google_mcp_reply.py`: reply override, artifact detection, truthful failure.
- `google_mcp_intent.py`: Google intent detection, plain form link guard.

Aturan:

- Upstream `/home/bagas/google-workspace-mcp/google_workspace_mcp` tetap source of truth saat ada failure Google.
- Jangan fallback ke sandbox ketika MCP tools tersedia.
- Jangan klaim Google action sukses tanpa artifact/tool evidence.

Test gate:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_mcp_tool_priority.py \
  tests/test_google_mcp_reply_overrides.py \
  tests/test_google_mcp_subagent_routing.py
```

## Fase 5 - Channels API Split

Target: kurangi `app/api/channels.py` tanpa mengubah endpoint public.

Kandidat split:

- `app/api/channels_whatsapp.py`
- `app/api/channels_google.py`
- `app/api/channels_sessions.py`
- `app/api/channels_webhooks.py`
- shared helpers di `app/api/wa_helpers.py` atau service layer baru.

Aturan:

- Endpoint path dan response schema tidak berubah.
- Route include tetap dari facade lama kalau perlu.
- Jangan campur refactor route dengan perubahan WA service contract.

Test gate:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_whatsapp_direct_send.py tests/test_whatsapp_progress.py
```

## Fase 6 - Prompt Builder Split

Target: prompt tetap sama secara behavior, tapi section builder lebih mudah diaudit.

Kandidat split:

- `prompt_time.py`
- `prompt_tool_categories.py`
- `prompt_runtime_context.py`
- `prompt_builder_mode.py`

Aturan:

- Jangan ubah wording Arthur kecuali ada test atau user request.
- Current time block wajib tetap ada untuk semua agent.
- Arthur tool categories wajib hanya muncul saat `builder` aktif.

Test gate:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_agent_builder_phase4.py tests/test_whatsapp_direct_send.py
```

## Definition of Done

Refactor dianggap selesai kalau:

- Tidak ada file backend hot path di atas 2500 lines tanpa alasan kuat.
- `builder_tools.py` tidak lagi menyimpan katalog, parsing JSON, fallback writer, Google helper, ownership helper, dan tool factory sekaligus.
- `agent_runner.py` hanya menjadi orchestrator utama, bukan tempat semua detail runtime hidup.
- Google MCP code punya boundary auth/runtime/reply/intent yang jelas.
- Public API, route path, tool name, dan behavior Arthur tetap kompatibel.
- Target regression suite pass.
- `docs/recap.md` diperbarui untuk refactor besar yang sudah selesai.

## Stop Conditions

Berhenti dan evaluasi ulang kalau:

- Ada test regression di behavior Arthur create/update/delete agent.
- Tool Arthur hilang atau berubah nama.
- Google MCP mulai fallback ke sandbox.
- Arthur mulai klaim sukses padahal tool gagal.
- Endpoint public berubah tanpa sengaja.
- Dirty changes user ikut terseret dalam diff refactor.
