# Agent Architecture Audit — Context/Attachment Binding (Arthur / WhatsApp)

Date: 2026-06-10 · Skill: `ecc:agent-architecture-audit`

> **Status (2026-06-10):** Fixes 1–4 below implemented via TDD — see `tests/test_context_binding.py` (8 tests, green; 163 passing across related suites). Not yet committed. LLM-authored error text inside a *completed* run is mitigated (Fix 2 directive) but not fully eliminated without classification.

## Executive verdict
- **Overall health:** high_risk (production)
- **Primary failure mode:** Memory/history contamination — stale agent text and old file bodies are replayed verbatim every turn, with no deterministic "latest message/file = source of truth" enforcement.
- **Most urgent fix:** Quarantine failed/error agent replies from history re-injection + strong current-attachment binding.

## Scope
- Target: `run_agent` pipeline (WhatsApp entrypoint), config-driven agents (Arthur + generated agents).
- Model stack: OpenRouter LLMs via `langchain-openai`, LangGraph / Deep Agents.
- Layers audited: 2 (session history), 6/8 (tool selection/interpretation), 11 (hidden repair), 12 (persistence).

---

## Findings (severity-ranked)

### CRITICAL 1 — Stale assistant-error replayed as history
- **Symptom:** Arthur keeps answering "Gagal mengirim laporan.xlsx…" on later, unrelated turns ("hah?", "laporan apa?").
- **Mechanism:** Error replies are persisted as `role="agent"` rows. `db_messages_to_lc` re-injects **every** agent message as `AIMessage` (`app/core/engine/context_service.py:106-107`), up to `MAX_PRIOR_MESSAGES=30` (`app/core/engine/agent_input.py:12`). The model reads its own past failure as conversation history and continues it.
- **Why the existing guard misses it:** the `tail_dirty` HARD OVERRIDE (`agent_input.py:54-77`) only fires when the last user message has **no** agent reply after it. An error reply counts as `has_final_ai_reply_after_user=True` (`agent_input.py:47-54`) → override not triggered → stale error bleeds forward.
- **Source layer:** 2 (history) + 12 (persistence)
- **Evidence:** `context_service.py:96-108`, `agent_input.py:30-77`
- **Confidence:** 0.85

### CRITICAL 2 — No current-attachment binding (old file wins)
- **Symptom:** After uploading `dummy test.pdf` and asking for a viz, agent charts the earlier `titanic.txt`.
- **Mechanism:** Each uploaded doc's **full extracted text** is inlined into that turn's user message (`app/api/wa_helpers.py:635-638`) and that text stays verbatim in history (user rows are re-injected by `db_messages_to_lc`). On the next upload, history still holds the OLD file's full body; nothing marks the NEW file as the single source of truth. Both files also coexist under `/workspace/shared/` (`wa_helpers.py:597-601`), so a glob/sandbox read can pick the older one. Model anchors on the richer/earlier Titanic block.
- **Source layer:** 2 (history) + 6/8 (tool routing/interpretation)
- **Evidence:** `wa_helpers.py:597-643`, `channels.py:1730`, `context_service.py:98-105`
- **Confidence:** 0.80

### HIGH 3 — Reply guard runs AFTER history is persisted
- **Symptom:** User corrects the agent, but the old/raw output keeps coming back.
- **Mechanism:** Parsed `db_messages` (carrying the **raw** `final_reply`) are added to DB at `agent_runner.py:2065-2066` (and sibling branches). Reply guards/overrides mutate the local `final_reply` **for delivery only** later at `agent_runner.py:2267-2290`. The ORM rows already staged are not updated → **DB history stores the pre-guard text**. The corrected version the user saw ≠ the version replayed next turn. Corrections never stick.
- **Source layer:** 11 (hidden repair) + 2 (history)
- **Evidence:** `agent_runner.py:1877-1898, 2065-2066, 2267-2290`
- **Confidence:** 0.80

### MEDIUM 4 — Full document bodies bloat history
- **Mechanism:** Up to 30 messages each carrying up to `media_doc_max_chars` of inlined file text → large duplicated context that degrades attention and directly amplifies Finding 2.
- **Source layer:** 2/3
- **Evidence:** `wa_helpers.py:632-638`, `agent_input.py:12,30-36`
- **Confidence:** 0.70

---

## Ordered fix plan (code-first, not prompt-first)

1. **Quarantine failed/error agent replies from re-injection.**
   `load_history` already drops `user` rows from failed runs (`context_service.py:67-75`). Extend the same run-status filter to **agent** rows (drop agent content from `failed/abandoned/cancelled/timed_out` runs), and/or tag error replies at persist time and skip them in `db_messages_to_lc`. Kills Finding 1.

2. **Deterministic current-attachment binding.**
   When the current turn carries an attachment, prepend a SYSTEM line: *"CURRENT ATTACHMENT (single source of truth for this turn): `<filename>` at `<path>`. Use only this file unless the user explicitly names a previous one."* In `db_messages_to_lc`, **elide old inline document bodies** from history (replace the ```…``` body with `[file <name> — bukan lampiran turn ini]`). Kills Finding 2.

3. **Persist the post-guard reply.**
   Move agent-message persistence to after the guard/override stage, or update the staged `Message` ORM row's content once `final_reply` is finalized. Fixes Finding 3.

4. **Stop inlining full doc text into persisted history.**
   Persist only `path + short summary` in the user row; keep the full extracted body ephemeral to the live turn (or fetch on demand via RAG/workspace read). Shrinks context, reinforces Findings 2 & 4.

5. **(Defense) "latest file wins" in workspace tooling.**
   Write each new upload to a stable `current` pointer and have file tools prefer it, so even if the model is ambiguous, the tool layer resolves to the newest file.
