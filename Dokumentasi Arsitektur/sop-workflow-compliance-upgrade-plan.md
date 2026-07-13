# SOP Workflow Compliance Upgrade Plan

Tanggal: 2026-07-08

Dokumen ini adalah bahan persiapan upgrade besar untuk membuat Arthur dan agent buatan Arthur jauh lebih patuh terhadap SOP/workflow, terutama di WhatsApp. Tujuan akhirnya bukan membuat agent kaku, tetapi membuat agent dan manusia bisa bekerja bersama secara aman: agent menangani intake, klarifikasi, pencarian data, update status, dan pekerjaan rutin; manusia masuk saat keputusan bisnis, approval, risiko, atau informasi belum pasti.

Referensi eksternal utama:

- Waaru WhatsApp AI Agents: https://www.waaru.app/whatsapp-ai-agents
- Waaru About: https://www.waaru.app/about

Catatan penting: informasi Waaru di dokumen ini berasal dari klaim publik/marketing mereka, bukan audit independen terhadap source code. Karena itu, Waaru dipakai sebagai referensi pola arsitektur, bukan benchmark kebenaran implementasi.

## Ringkasan Eksekutif

Project ini sudah punya fondasi yang kuat untuk workflow compliance:

- `AgentOperatingManual` sudah ada sebagai artifact terpisah dari `instructions`.
- SOP bisa menyimpan `workflows`, `state_plan`, `human_approval_points`, `escalation_rules`, `maturity`, dan `owner_review_required`.
- Runtime sudah memuat SOP ke prompt agent.
- Readiness check sudah bisa memblokir agent tanpa SOP atau SOP yang masih `draft`/`needs_review`.
- Escalation/handoff ke manusia sudah ada.
- Tool call dan tool result sudah terekam di `Message`.
- Tool error bisa dikembalikan ke model agar model memperbaiki argumen atau menjelaskan blocker.

Namun, untuk mencapai kepatuhan workflow yang benar-benar kuat, masih ada gap utama:

- Belum ada workflow state engine yang menyimpan `current_step`, `filled_slots`, dan `next_required_action` secara eksplisit.
- Belum ada mid-flow off-script handler yang menjawab pertanyaan sampingan lalu kembali ke step workflow semula.
- `allowed_tools` di SOP belum benar-benar menjadi hard runtime allowlist per workflow/step.
- SOP gate saat ini belum memblokir final action nyata karena `FINAL_ACTION_TOOLS` masih kosong.
- Approval gate belum menjadi policy engine umum berbasis action/threshold/risk.
- Observability belum menyimpan trace keputusan policy secara lengkap dalam satu record.

Target upgrade: jadikan SOP sebagai runtime contract, bukan hanya prompt context.

## Perbandingan Waaru vs Project Ini

| Area | Waaru | Project Ini |
|---|---:|---:|
| WhatsApp agent runtime | Ada | Ada |
| LLM choice | Ada, per workspace | Ada, per agent lewat `Agent.model` |
| Tool calling | Ada, typed schema | Ada, LangChain/MCP/custom tools |
| Validasi tool args | Ada, diklaim JSON schema | Ada sebagian; Pydantic/StructuredTool untuk beberapa tool, HTTP masih generic JSON string |
| Custom REST tool | Ada, OpenAPI spec jadi typed tool | Partial; ada `http_get/post/patch/delete`, tapi belum OpenAPI-to-tool typed registry |
| MCP native | Ada, diklaim first-party MCP server | Partial; project ini bisa consume MCP tools, belum terlihat first-party MCP server untuk mengontrol platform |
| Memory percakapan | Ada | Ada, via `sessions/messages` |
| Memory lintas sesi | Ada | Ada, via memory service/layered memory |
| Contact/workspace brain | Ada, diklaim 3 level | Partial; ada agent memory/RAG, belum sejelas contact/workspace brain ala Waaru |
| Policy envelope | Ada | Partial; ada `tools_config`, `safety_policy`, capabilities, allowed senders, SOP gate |
| Tool allowlist per workflow | Ada, diklaim | Partial; SOP punya `allowed_tools`, tapi runtime gate belum benar-benar memfilter per workflow |
| Approval gates | Ada, diklaim threshold/action based | Partial; ada escalation/operator approval flow, tapi belum policy engine threshold umum |
| Refusal rules | Ada, diklaim | Partial; ada safety/prompt guards dan policy blockers, belum refusal engine terstruktur per workspace/workflow |
| Brand voice guardrails | Ada, diklaim | Ada sebagian lewat instructions/SOP/prompt, belum policy object khusus brand voice |
| Rate limits per contact/workspace | Ada, diklaim | Belum kelihatan sebagai policy runtime workflow |
| Mid-flow AI injection | Ada, ini selling point Waaru | Belum full; belum ada no-match detector + off-script handler + resume state engine umum |
| Workflow state/slot tracker | Kemungkinan ada/diimplikasikan | Partial; ada SOP `state_plan`, tapi belum terlihat persisted `current_step/filled_slots` |
| SOP/Operating Manual artifact | Tidak eksplisit sebagai istilah utama | Ada kuat: `AgentOperatingManual`, workflows, maturity, owner review |
| SOP readiness gate | Tidak jelas dari klaim publik | Ada: draft/needs_review jadi blocker readiness dan prompt restriction |
| Runtime hard gate dari SOP | Tidak jelas detailnya | Partial; ada `filter_tools_by_sop`, tapi `FINAL_ACTION_TOOLS` masih kosong, jadi belum memblokir aksi final nyata |
| Human handoff | Ada | Ada, `escalate_to_human`, operator session, `reply_to_user` |
| Resume setelah approval manusia | Ada, diklaim handoff context | Ada sebagian: `SYSTEM_OPERATOR_APPROVAL` resume dari history/memory |
| Observability trace | Ada, diklaim lengkap | Partial; ada `Run`, `Message.tool_args/tool_result`, callback log, tapi belum trace record lengkap prompt/model/policy/confidence |
| Confidence threshold | Ada, diklaim | Belum terlihat sebagai runtime threshold umum |
| Tool error retry | Ada, diklaim structured error | Ada: tool error dikembalikan ke model agar self-correct |
| WhatsApp media/file delivery | Tidak fokus di klaim utama | Ada cukup kuat: media tools, reply guards, delivery validation |

## Prinsip Upgrade

### 1. SOP bukan instruksi panjang, SOP adalah kontrak runtime

Agent tidak boleh hanya diberi prompt "ikuti SOP". Runtime harus tahu:

- workflow apa yang aktif,
- step apa yang sedang berjalan,
- data apa yang wajib dikumpulkan,
- tool apa yang boleh dipakai,
- action apa yang dilarang,
- action apa yang butuh approval,
- kapan harus eskalasi,
- kapan workflow dianggap selesai.

Prompt tetap dipakai untuk bahasa, reasoning, dan natural conversation. Tetapi kepatuhan utama harus dijaga oleh runtime.

### 2. Agent boleh fleksibel dalam percakapan, tetapi state workflow tidak boleh hilang

Contoh:

1. Workflow sedang di step `ask_student_name`.
2. User bertanya: "Biayanya berapa?"
3. Agent boleh menjawab dari knowledge base jika datanya ada.
4. Setelah itu agent harus kembali ke step `ask_student_name`.

Ini pola mid-flow injection:

```text
workflow step aktif
-> user off-script
-> jawab pertanyaan sampingan dengan batas knowledge/policy
-> jangan ubah current_step
-> resume pertanyaan workflow semula
```

### 3. Manusia adalah bagian resmi dari workflow, bukan fallback darurat saja

Handoff harus punya struktur:

- reason,
- workflow_id,
- current_step,
- data yang sudah terkumpul,
- data yang belum lengkap,
- action yang diminta dari operator,
- pesan/draft untuk customer,
- keputusan operator,
- resume instruction setelah approval.

### 4. Draft/needs_review berarti intake-safe, bukan production-ready

SOP dengan maturity `draft` atau `needs_review` hanya boleh:

- menyapa,
- memahami kebutuhan,
- mengumpulkan data,
- menjawab FAQ yang benar-benar ada di knowledge,
- membuat ringkasan,
- eskalasi ke owner/operator.

SOP tersebut tidak boleh:

- mengonfirmasi harga/stok/jadwal final,
- memproses refund,
- menyetujui pembayaran,
- mengirim deliverable final,
- membuat janji final atas nama bisnis,
- menjalankan side effect berisiko.

## Target Arsitektur Baru

### High-level Flow

```text
WhatsApp inbound message
-> normalize identity and session
-> persist inbound message
-> load agent runtime contract
-> load active workflow state
-> classify turn intent
   - in_flow_answer
   - off_script_question
   - correction_or_update
   - approval_event
   - human_help_request
   - unsafe_or_blocked_action
-> evaluate SOP policy
-> expose only allowed tools
-> run LLM/tool loop
-> validate output and side effects
-> update workflow state
-> persist trace
-> send reply or escalate
```

### Runtime Contract

Setiap run agent harus punya runtime contract yang eksplisit:

```json
{
  "agent_id": "uuid",
  "agent_name": "AsDosBot",
  "channel": "whatsapp",
  "owner": {
    "owner_external_id": "628xxx",
    "operator_ids": ["628yyy"]
  },
  "sop": {
    "manual_id": "agent_operating_manual",
    "version": 3,
    "maturity": "usable",
    "owner_review_required": false
  },
  "active_workflow": {
    "workflow_id": "mode_1_registration",
    "current_step": "ask_student_name",
    "required_slots": ["student_name", "class", "topic", "schedule"],
    "filled_slots": {},
    "allowed_tools": ["remember", "recall", "escalate_to_human"],
    "blocked_actions": ["confirm_registration"],
    "approval_required_actions": ["final_confirm_registration"]
  },
  "policy": {
    "intake_safe": false,
    "requires_owner_review": false,
    "refusal_rules": [],
    "rate_limits": {}
  }
}
```

## Data Model yang Dibutuhkan

### 1. `agent_workflow_states`

Menyimpan posisi workflow per session/contact.

```text
agent_workflow_states
- id uuid
- agent_id uuid
- session_id uuid
- external_user_id text
- workflow_id text
- current_step text
- status text
  - active
  - waiting_user
  - waiting_operator
  - blocked
  - completed
  - abandoned
- filled_slots jsonb
- missing_slots jsonb
- last_user_intent text
- last_off_script_question text
- resume_prompt text
- last_policy_decision_id uuid nullable
- created_at timestamptz
- updated_at timestamptz
```

Contoh isi:

```json
{
  "workflow_id": "mode_1_registration",
  "current_step": "ask_class",
  "status": "waiting_user",
  "filled_slots": {
    "student_name": "Raka"
  },
  "missing_slots": ["class", "topic", "schedule"],
  "resume_prompt": "Tanyakan kelas atau program yang diikuti."
}
```

### 2. `agent_policy_decisions`

Menyimpan keputusan runtime setiap turn.

```text
agent_policy_decisions
- id uuid
- run_id uuid
- agent_id uuid
- session_id uuid
- workflow_id text nullable
- current_step text nullable
- turn_classification text
- decision text
  - allow
  - allow_with_constraints
  - block
  - escalate
  - ask_clarification
  - answer_off_script_then_resume
- policy_reasons jsonb
- allowed_tools jsonb
- blocked_tools jsonb
- approval_required boolean
- confidence numeric nullable
- created_at timestamptz
```

Contoh:

```json
{
  "turn_classification": "off_script_question",
  "decision": "answer_off_script_then_resume",
  "policy_reasons": [
    "question_answerable_from_knowledge",
    "do_not_advance_workflow_step"
  ],
  "allowed_tools": ["recall", "remember", "escalate_to_human"],
  "blocked_tools": ["send_whatsapp_document", "http_post"],
  "approval_required": false
}
```

### 3. `agent_workflow_events`

Event sourcing ringan untuk audit workflow.

```text
agent_workflow_events
- id uuid
- workflow_state_id uuid
- run_id uuid nullable
- event_type text
  - workflow_started
  - step_entered
  - slot_filled
  - off_script_answered
  - policy_blocked
  - escalated
  - operator_approved
  - operator_rejected
  - workflow_resumed
  - workflow_completed
- payload jsonb
- created_at timestamptz
```

Event ini akan membantu debugging ketika owner bertanya: "Kenapa agent melewati pertanyaan ini?" atau "Kenapa agent tidak lanjut setelah approval?"

## Upgrade SOP Artifact

SOP saat ini sudah punya `workflows`, tetapi perlu distandarkan agar runtime bisa mengeksekusinya.

### Workflow Schema Baru

```json
{
  "workflow_id": "mode_1_registration",
  "name": "Pendaftaran Mode 1",
  "trigger": "User ingin daftar atau ikut kelas Mode 1",
  "goal": "Mengumpulkan data pendaftaran lengkap sebelum konfirmasi admin",
  "maturity": "usable",
  "steps": [
    {
      "step_id": "ask_student_name",
      "order": 1,
      "type": "collect_slot",
      "prompt": "Tanyakan nama lengkap peserta.",
      "required_slots": ["student_name"],
      "allowed_tools": ["remember", "recall", "escalate_to_human"],
      "blocked_actions": ["confirm_registration"],
      "on_complete": "ask_class"
    },
    {
      "step_id": "ask_class",
      "order": 2,
      "type": "collect_slot",
      "prompt": "Tanyakan kelas/program yang diikuti.",
      "required_slots": ["class"],
      "allowed_tools": ["remember", "recall", "escalate_to_human"],
      "blocked_actions": ["confirm_registration"],
      "on_complete": "ask_topic"
    },
    {
      "step_id": "final_review",
      "order": 99,
      "type": "approval_gate",
      "prompt": "Ringkas data dan eskalasi ke admin untuk konfirmasi.",
      "required_slots": ["student_name", "class", "topic", "schedule"],
      "allowed_tools": ["escalate_to_human"],
      "approval_required": true,
      "approval_actor": "operator",
      "on_approved": "send_confirmation",
      "on_rejected": "ask_revision"
    }
  ],
  "off_script_policy": {
    "answer_if_known": true,
    "knowledge_sources": ["rag", "memory", "sop"],
    "if_unknown": "ask_clarification_or_escalate",
    "resume_current_step": true
  },
  "completion_criteria": [
    "Semua required_slots terisi",
    "Admin/operator sudah approve jika approval_required",
    "Customer menerima ringkasan final"
  ]
}
```

### Slot Schema

SOP perlu mendefinisikan slot, bukan hanya teks "data yang dikumpulkan".

```json
{
  "slots": {
    "student_name": {
      "label": "Nama peserta",
      "type": "string",
      "required": true,
      "validation": {
        "min_length": 2
      }
    },
    "schedule": {
      "label": "Jadwal pilihan",
      "type": "datetime_or_text",
      "required": true,
      "validation": {
        "allow_relative_time": true
      }
    },
    "payment_proof": {
      "label": "Bukti pembayaran",
      "type": "media_or_text",
      "required": false,
      "approval_required": true
    }
  }
}
```

## Mid-flow Off-script Handler

Ini bagian paling penting untuk membuat agent terasa natural tapi tetap patuh.

### Problem

User WhatsApp jarang mengikuti form secara rapi. Mereka bisa:

- menjawab sebagian,
- bertanya balik,
- mengubah jawaban sebelumnya,
- kirim voice note,
- kirim gambar,
- minta manusia,
- lompat ke pembayaran,
- bertanya hal di luar flow.

Kalau agent hanya prompt-based, agent bisa kehilangan urutan workflow.

### Solusi

Tambahkan classifier sebelum LLM utama atau sebagai deterministic pre-step:

```text
classify_turn(user_message, active_workflow_state, sop)
```

Output:

```json
{
  "classification": "off_script_question",
  "detected_slots": {},
  "question": "Biayanya berapa?",
  "should_advance_step": false,
  "resume_step": "ask_student_name",
  "recommended_action": "answer_then_resume"
}
```

### Behavior

Jika pertanyaan bisa dijawab dari SOP/RAG/memory:

```text
Jawab singkat.
Lalu lanjutkan pertanyaan step aktif.
```

Contoh:

```text
Biaya kelasnya Rp250.000 per sesi sesuai info yang tersedia.
Sekarang saya lanjut lengkapi pendaftarannya ya. Nama lengkap peserta siapa?
```

Jika tidak bisa dijawab:

```text
Saya belum punya info pasti soal itu. Saya catat untuk admin.
Sekarang saya lanjut data yang dibutuhkan dulu ya. Nama lengkap peserta siapa?
```

Jika pertanyaan berisiko:

```text
Saya perlu teruskan ini ke admin dulu karena menyangkut keputusan final.
Sambil menunggu admin, saya bantu lengkapi data pendaftarannya ya. Nama lengkap peserta siapa?
```

## Runtime Tool Gating

Saat ini project punya `filter_tools_by_sop`, tetapi daftar `FINAL_ACTION_TOOLS` masih kosong. Itu berarti SOP gate belum benar-benar menahan tool final action.

### Target

Tool exposure harus ditentukan oleh:

1. agent capabilities,
2. tools_config,
3. SOP maturity,
4. active workflow,
5. current step,
6. policy decision turn ini.

### Kategori Tool

```text
safe_intake_tools
- recall
- remember
- update_daily
- update_longterm
- escalate_to_human
- notify_user

knowledge_tools
- rag search
- tavily_search
- tavily_extract

communication_tools
- send_to_number
- reply_to_user
- send_whatsapp_image
- send_whatsapp_document

external_side_effect_tools
- http_post
- http_patch
- http_delete
- Google create/update/send tools
- scheduler set/cancel tools

final_action_tools
- confirm_order
- create_booking
- approve_refund
- send_final_deliverable
- send_whatsapp_document for final delivery workflows
- create_invoice/payment/payment confirmation tools
```

### Rule

Jika SOP `draft`/`needs_review`:

- keep: safe intake, knowledge lookup, escalation,
- block: final action,
- block or require approval: external side effect.

Jika workflow step punya `allowed_tools`:

- expose hanya union dari safe runtime tools + `allowed_tools` step.

Jika action butuh approval:

- jangan expose final tool langsung,
- expose `escalate_to_human`,
- setelah approval event, expose resume tools sesuai `on_approved`.

## Policy Engine

Policy engine perlu dibuat sebagai modul eksplisit, misalnya:

```text
app/core/engine/workflow_policy.py
```

Fungsi utama:

```python
evaluate_workflow_policy(
    agent,
    session,
    sop,
    workflow_state,
    user_message,
    requested_action=None,
) -> WorkflowPolicyDecision
```

Output:

```json
{
  "decision": "allow_with_constraints",
  "turn_classification": "off_script_question",
  "workflow_id": "mode_1_registration",
  "current_step": "ask_student_name",
  "allowed_tools": ["recall", "remember", "escalate_to_human"],
  "blocked_tools": ["http_post", "send_whatsapp_document"],
  "must_resume_step": true,
  "approval_required": false,
  "response_constraints": [
    "answer only from SOP/RAG/memory",
    "do not advance workflow step",
    "ask student_name after answer"
  ],
  "audit_reasons": [
    "active workflow step is ask_student_name",
    "user asked off-script FAQ",
    "SOP says resume_current_step=true"
  ]
}
```

## Arthur Upgrade

Arthur harus menjadi builder yang menghasilkan agent workflow-ready, bukan hanya prompt-ready.

### Saat membuat agent baru

Arthur wajib menghasilkan:

1. agent identity,
2. operating manual,
3. workflow schema,
4. slot schema,
5. approval policy,
6. escalation policy,
7. tool policy,
8. launch readiness report,
9. test scenario.

Urutan ideal:

```text
plan_agent
-> compose_agent_blueprint
-> compose_agent_operating_manual
-> validate_workflow_schema
-> validate_policy_envelope
-> compose_agent_instructions
-> validate_agent_config
-> create_agent
-> verify_agent
-> create_workflow_smoke_tests
```

### Arthur interview minimum

Sebelum create agent, Arthur minimal harus tahu:

- siapa user/customer yang akan chat,
- workflow utama,
- data wajib yang harus dikumpulkan,
- kapan agent boleh menjawab sendiri,
- kapan agent harus tanya admin,
- siapa operator/admin,
- output akhir apa,
- apakah ada pembayaran/approval/file/dokumen,
- apakah agent boleh melakukan side effect eksternal.

Jika belum cukup:

- jangan create full workflow-ready agent,
- buat draft intake-safe saja,
- set `maturity=draft`,
- set `owner_review_required=true`,
- jelaskan ke owner data apa yang masih kurang.

### Arthur harus menolak workflow berbahaya tanpa policy

Contoh harus block:

- Agent pembayaran tanpa escalation.
- Agent refund tanpa approval.
- Agent booking tanpa aturan availability.
- Agent delivery file final tanpa media tool.
- Agent Google Workspace tanpa auth path.
- Agent yang diminta "langsung konfirmasi order" tanpa data harga/stok/SOP.

## Agent Buatan Arthur

Agent runtime harus punya mode kerja berikut:

### 1. Intake Mode

Dipakai jika:

- SOP missing,
- SOP draft,
- workflow belum dikenali,
- data bisnis kurang,
- user meminta action di luar policy.

Agent boleh:

- bertanya,
- mengumpulkan data,
- menjawab FAQ yang pasti,
- merangkum,
- eskalasi.

Agent tidak boleh:

- membuat keputusan final,
- mengklaim action eksternal sudah terjadi,
- mengirim deliverable final tanpa tool success,
- melewati approval manusia.

### 2. Workflow Mode

Dipakai jika:

- SOP usable/verified,
- workflow terdeteksi,
- current_step aktif,
- policy decision allow.

Agent harus:

- mengikuti step,
- mengisi slot,
- menjawab off-script lalu resume,
- update state,
- log event.

### 3. Approval Waiting Mode

Dipakai jika:

- action butuh operator,
- payment proof diterima,
- refund/booking/final delivery butuh approval.

Agent harus:

- eskalasi dengan ringkasan lengkap,
- status session menjadi `waiting_operator`,
- jangan lanjut final action,
- saat operator approve, resume dari `on_approved`.

### 4. Completed Mode

Dipakai jika:

- completion criteria terpenuhi,
- final reply/delivery berhasil,
- state ditutup.

Agent harus:

- menyimpan ringkasan hasil,
- tidak mengulang workflow kecuali user memulai ulang atau koreksi.

## Observability dan Audit

Setiap run harus bisa menjawab pertanyaan ini:

- SOP versi berapa yang dipakai?
- Workflow apa yang aktif?
- Step apa sebelum user mengirim pesan?
- User message diklasifikasi sebagai apa?
- Policy memutuskan allow/block/escalate kenapa?
- Tool apa saja yang diekspos?
- Tool apa yang diblokir?
- Slot apa yang terisi?
- Apakah agent menjawab off-script?
- Apakah agent resume ke step yang benar?
- Apakah operator masuk?
- Siapa yang approve?
- Action final terjadi setelah approval atau tidak?

### Minimum trace

```json
{
  "run_id": "uuid",
  "model": "anthropic/claude-sonnet-4-6",
  "agent_id": "uuid",
  "session_id": "uuid",
  "sop_version": 3,
  "workflow_id": "mode_1_registration",
  "current_step_before": "ask_student_name",
  "turn_classification": "off_script_question",
  "policy_decision": "answer_off_script_then_resume",
  "allowed_tools": ["recall", "remember", "escalate_to_human"],
  "blocked_tools": ["send_whatsapp_document", "http_post"],
  "tool_calls": [],
  "state_update": {
    "current_step_after": "ask_student_name",
    "filled_slots_delta": {}
  },
  "final_reply_type": "answer_then_resume"
}
```

## Roadmap Implementasi

### Phase 0 - Inventory dan test baseline

Tujuan: tahu perilaku saat ini sebelum operasi besar.

Checklist:

- Audit semua tool yang bisa side effect.
- Klasifikasikan tool menjadi safe, knowledge, communication, external side effect, final action.
- Catat workflow existing yang paling penting: Arthur builder flow, trial WA flow, payment approval flow, file delivery flow, AsDosBot Mode 1.
- Tambah regression test untuk kasus "user off-script lalu agent harus resume step".
- Tambah test bahwa SOP draft tidak boleh final action.

Output:

- `tool_policy_registry.py`
- test baseline workflow compliance
- daftar final action tools awal

### Phase 1 - Workflow state persistence

Tujuan: agent tidak kehilangan posisi workflow.

Checklist:

- Buat model `AgentWorkflowState`.
- Buat model `AgentWorkflowEvent`.
- Buat service:
  - `get_or_create_workflow_state`
  - `advance_workflow_step`
  - `fill_workflow_slots`
  - `mark_waiting_operator`
  - `resume_after_operator_approval`
  - `complete_workflow`
- Inject active state ke prompt.
- Simpan event setiap step berubah.

Acceptance criteria:

- Jika user menjawab slot, state ter-update.
- Jika user bertanya off-script, state tidak maju.
- Jika workflow selesai, status menjadi `completed`.

### Phase 2 - Turn classifier dan mid-flow handler

Tujuan: agent bisa menjawab pertanyaan sampingan tanpa keluar workflow.

Checklist:

- Buat `classify_workflow_turn`.
- Deteksi:
  - in-flow answer,
  - off-script question,
  - correction,
  - human request,
  - approval signal,
  - unsafe/final action request.
- Tambah response constraints ke prompt.
- Setelah LLM reply, update state sesuai classifier.

Acceptance criteria:

- User bertanya FAQ di tengah flow, agent menjawab dan kembali ke pertanyaan aktif.
- User mengoreksi jawaban lama, slot lama berubah, step disesuaikan.
- User minta admin, agent eskalasi dan state menjadi `waiting_operator`.

### Phase 3 - SOP policy engine

Tujuan: kepatuhan SOP menjadi hard decision.

Checklist:

- Buat `WorkflowPolicyDecision`.
- Evaluasi SOP maturity.
- Evaluasi workflow/step allowed tools.
- Evaluasi blocked actions.
- Evaluasi approval required.
- Hasil policy disimpan ke `agent_policy_decisions`.
- Tool setup memakai policy decision untuk memfilter tools.

Acceptance criteria:

- Tool final action tidak muncul saat SOP draft.
- Tool di luar `allowed_tools` step tidak muncul.
- Action approval tidak dieksekusi sebelum operator approve.

### Phase 4 - Arthur workflow schema upgrade

Tujuan: Arthur menghasilkan SOP yang executable.

Checklist:

- Upgrade `compose_agent_operating_manual` agar menghasilkan `steps`, `slots`, `approval_gates`, dan `off_script_policy`.
- Tambah validator schema SOP.
- Tambah validator semantic:
  - payment harus punya approval gate,
  - file final harus punya delivery tool,
  - booking harus punya availability rule,
  - refund harus punya approval/refusal rule,
  - unknown policy harus escalation.
- Tambah owner review UX untuk SOP.

Acceptance criteria:

- Arthur tidak membuat agent workflow-ready jika workflow belum punya slot/step/policy cukup.
- Agent hasil Arthur punya SOP yang bisa dibaca runtime tanpa parsing bebas.

### Phase 5 - Approval and handoff hardening

Tujuan: manusia dan agent bekerja dalam satu workflow, bukan dua percakapan terpisah.

Checklist:

- Escalation payload wajib membawa workflow state.
- Operator approval event harus menulis `workflow_event`.
- Approval harus mengubah state dari `waiting_operator` ke step berikutnya.
- Jika operator menolak, state kembali ke clarification/revision step.
- Draft-to-operator dan reply-to-customer tetap dipisahkan.

Acceptance criteria:

- Setelah operator approve pembayaran, agent lanjut dari step `delivery`, bukan mulai ulang.
- Operator bisa melihat data yang sudah terkumpul dan action yang diminta.
- Agent tidak menjalankan final action dari sesi operator kecuali event approval memang mengizinkan resume.

### Phase 6 - Observability dashboard/API

Tujuan: owner/dev bisa audit kenapa agent bertindak begitu.

Checklist:

- API untuk melihat active workflow state per session.
- API untuk melihat policy decisions per run.
- API untuk melihat workflow event timeline.
- Dashboard "why did agent do this?"
- Filter run yang blocked karena SOP/policy.

Acceptance criteria:

- Dev bisa trace satu WhatsApp conversation dari inbound message sampai policy decision, tool call, state update, dan reply.
- Owner bisa lihat bagian SOP yang perlu review.

## Test Scenarios Wajib

### 1. Off-script FAQ lalu resume

```text
Agent: Nama lengkap peserta siapa?
User: Biayanya berapa?
Agent: Biayanya Rp250.000 sesuai info yang tersedia. Sekarang lanjut ya, nama lengkap peserta siapa?
```

Expected:

- classification: `off_script_question`
- current_step tetap `ask_student_name`
- no final action
- event `off_script_answered`

### 2. Slot answer normal

```text
Agent: Nama lengkap peserta siapa?
User: Raka Pratama
Agent: Baik, Raka Pratama. Kelas/program yang diikuti apa?
```

Expected:

- slot `student_name=Raka Pratama`
- step maju ke `ask_class`

### 3. User lompat ke final confirmation

```text
User: Udah, langsung daftarin aja.
```

Expected:

- jika slot belum lengkap, block final confirmation,
- agent tanya data kurang,
- policy reason: `missing_required_slots`.

### 4. Payment proof butuh approval

```text
User: Ini bukti transfernya.
```

Expected:

- state `waiting_operator`,
- `escalate_to_human` dipanggil,
- tidak ada delivery final sebelum approval.

### 5. Operator approve

```text
[SYSTEM_OPERATOR_APPROVAL]
Jenis approval: pembayaran customer sudah dikonfirmasi
```

Expected:

- state lanjut ke `delivery`,
- agent tidak tanya pembayaran lagi,
- final delivery hanya jika tool tersedia dan sukses.

### 6. SOP draft

Expected:

- agent hanya intake/klarifikasi/ringkas/escalate,
- final action tools blocked,
- readiness menunjukkan owner review required.

## Risiko

### Risiko 1 - Terlalu kaku

Jika semua hal dibuat state machine ketat, agent bisa terasa seperti form.

Mitigasi:

- off-script handler selalu aktif,
- response tetap natural,
- state hanya mengontrol "apa yang boleh maju", bukan gaya bicara.

### Risiko 2 - SOP otomatis terlihat terlalu matang

Arthur bisa generate SOP yang terlihat lengkap padahal belum dikonfirmasi owner.

Mitigasi:

- fallback/generic SOP wajib `needs_review`,
- owner review required jelas,
- readiness tidak boleh "green" untuk SOP generik yang belum diverifikasi.

### Risiko 3 - Tool gating salah memblokir kemampuan berguna

Contoh: WhatsApp media diblokir padahal user minta kirim file yang aman.

Mitigasi:

- klasifikasi tool detail,
- bedakan media transport vs final business delivery,
- uji regresi file delivery.

### Risiko 4 - State rusak saat multi-message cepat

User WhatsApp bisa kirim banyak pesan beruntun.

Mitigasi:

- session lock,
- idempotency per message_id,
- workflow state update atomic,
- event log append-only.

### Risiko 5 - Operator reply salah target

Operator bisa membalas escalation lama.

Mitigasi:

- lock case_id,
- strict quoted lookup,
- jangan fallback ke customer terbaru untuk approval/action penting.

## Definisi Selesai

Upgrade ini dianggap berhasil jika:

- Arthur hanya membuat agent full workflow-ready jika SOP executable dan policy cukup.
- Agent dengan SOP draft otomatis intake-safe.
- Agent WhatsApp bisa menangani user yang keluar jalur lalu kembali ke step workflow.
- Final action tidak bisa terjadi sebelum required slots dan approval terpenuhi.
- Operator bisa masuk, approve/reject, dan agent resume workflow dengan benar.
- Setiap keputusan penting bisa diaudit dari trace.

## Prioritas Teknis yang Disarankan

Urutan paling pragmatis:

1. Buat registry final action tools dan aktifkan SOP hard gate.
2. Buat `AgentWorkflowState` dan simpan `current_step/filled_slots`.
3. Implement off-script answer-then-resume untuk satu workflow prioritas.
4. Buat `WorkflowPolicyDecision` dan simpan policy trace.
5. Upgrade Arthur agar SOP punya `steps`, `slots`, dan `approval_gates`.
6. Perluas ke semua agent buatan Arthur.

Dengan urutan ini, manfaat compliance bisa mulai terasa cepat tanpa menunggu semua arsitektur selesai.
