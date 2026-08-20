# Arthur Conversational Deep Agent — Remediation Plan

**Date:** 2026-07-28
**Status:** Core implementation completed; production rollout pending verification
**Source decision record:** [Arthur Conversational Deep Agent Decisions](../analysis/ARTHUR_CONVERSATIONAL_DEEP_AGENT_DECISIONS_2026-07-28.md)

## Objective

Make Arthur a reliable conversational requirements consultant that can deeply understand an inexperienced user's workflow, create agents only from validated/confirmed requirements, and remain fast enough for WhatsApp.

The plan intentionally preserves two properties:

1. **Deep discovery:** material requirements are not skipped merely to reduce turns.
2. **Deep Agent orchestration:** the LLM continues to choose how to investigate, explain, synthesize, and use tools.

The change is to move deterministic correctness out of conversational copywriting.

## Target architecture

```text
User message
  → compact build state + recent evidence
  → LLM-led conversational/tool loop
  → deterministic discovery state validation
  → semantic learning goal (not mandatory wording)
  → LLM natural reply in user's language
  → deterministic action/authorization gate
  → thin delivery safety checks
  → persist exactly the delivered reply and state transition
```

### Ownership boundary

| Concern | Owner |
|---|---|
| Conversational wording, examples, clarifying strategy, summaries | LLM + selected Arthur skill |
| Required facts, confirmation, evidence, capability and permission validation | Deterministic domain services |
| Tool availability by workflow state | Runtime skill/tool scope |
| Database writes, idempotency, authorization, external side effects | Tool implementation and services |
| Empty/internal/unsupported/duplicate delivery protection | Thin delivery guard |

## Success criteria

### Product behavior

- Arthur accepts a rich free-form brief and does not re-ask facts already supplied.
- Arthur asks one high-impact unresolved question per turn, in the user's language.
- Arthur may conduct a long interview when the workflow is genuinely complex.
- Arthur never creates an agent until required facts and a final confirmed brief are present.
- English, Indonesian informal, and code-switched conversations do not fall back to language-specific template copy merely because wording differs.

### Reliability

- No user-facing internal tool/state/evidence text.
- No success claim unsupported by completed tool results.
- No reseed can leave a partially activated skill bundle.
- Every delivered reply is traceable to draft, guard decision, final delivery, and WhatsApp message ID.

### Performance targets

- Standard discovery turn without research: p50 <= 8 seconds, p95 <= 15 seconds.
- Avoid a forced second LLM pass solely because `plan_agent` was omitted by the first pass.
- Creation flow exposes clear phase progress; core agent creation has a bounded and observable critical path.

## Phase 0 — Stabilize the deployed baseline

**Goal:** make the current deployment reproducible before wider refactoring.

### Work

1. Review, run in CI, and commit the currently deployed local changes:
   - `app/core/engine/reply_guard.py`;
   - `tests/test_reply_guard.py`;
   - `arthur-skills/arthur-discovery/runtime.yaml`.
2. Ensure the `arthur-discovery` skill version/checksum is represented by a deliberate commit and release note.
3. Add a deployment preflight that reports:
   - Git SHA and dirty worktree status;
   - selected image digest;
   - active skill bundle/version/checksums;
   - API replica count and health.
4. Restore a reproducible test runner in the repository/CI. The host lacked `pytest` during this session, so test availability cannot remain implicit.

### Acceptance criteria

- Clean worktree after a documented commit.
- CI runs `tests/test_reply_guard.py`, `tests/test_arthur_skill_runtime.py`, and discovery/create regression tests.
- Deployed image can be mapped to an exact Git SHA and skill bundle.

## Phase 1 — Make reply handling a thin safety net

**Goal:** eliminate robotizing copy rewrites without weakening action safety.

### Work

1. Keep the current removal of language-keyword semantic matching in `reply_guard.py`.
2. Formalize guard decisions as an enum/reason code, for example:
   - `pass_through`;
   - `fallback_empty_reply`;
   - `fallback_internal_leak`;
   - `fallback_premature_success`;
   - `blocked_unsupported_capability`;
   - `suppress_duplicate_outbound`.
3. Separate two kinds of protection:
   - **delivery safety:** empty/internal/duplicate/unsupported claim;
   - **conversation quality:** repeated or multiple questions.
4. For conversation quality failures, prefer an LLM repair retry with a compact instruction over deterministic text replacement. Use template fallback only if the retry fails or times out.
5. Audit `guard_single_discovery_question` and `guard_repeated_questions` in `agent_runner.py`; retain only behavior that is language-agnostic and observable.

### Tests

- Natural replies in Indonesian, English, Spanish, mixed Indonesian-English, slang, and typo-heavy input remain unchanged.
- Empty/internal/premature-success replies use safe fallback.
- A tool already sending WhatsApp outbound content still suppresses duplicate final delivery.
- Every override emits a machine-readable guard reason and before/after lengths.

### Acceptance criteria

- No keyword lists are used to determine whether a user-facing question is semantically valid.
- At least 95% of normal discovery replies are `pass_through` in staging conversation evals.

## Phase 2 — Separate semantic discovery state from conversational copy

**Goal:** preserve deep requirements gathering while removing the hidden questionnaire behavior.

### Work

1. Refactor `builder_discovery.py` to expose a semantic result object:

   ```json
   {
     "state": "needs_clarification",
     "known_facts": {},
     "unresolved_material_fields": [],
     "learning_goal": "understand_failure_handling",
     "risk_if_unresolved": "agent may invent an answer or take an unsafe action",
     "must_confirm_before_create": false
   }
   ```

2. Preserve `required_fields` as an action gate, but replace fixed `group_missing[:1]` question selection with a priority policy:
   - start from real workflow/pain point;
   - choose the gap with highest design/safety impact;
   - defer optional polish;
   - honor facts volunteered across any group;
   - avoid asking the same semantic question twice.
3. Treat current `_QUESTIONS` as emergency fallback copy only, not normal response text.
4. Store semantic facts with evidence and confidence:
   - direct user statement;
   - derived low-risk inference;
   - unresolved/ambiguous;
   - explicit delegation for safe presentation details.
5. Keep material permission fields strict: integration, payment, external message, escalation target, capability/file decision, and sensitive-data handling cannot be inferred from a weak signal.
6. Change the prompt/skill contract so Arthur receives `learning_goal` and known facts, then writes a natural one-question response in the user's language.

### Migration approach

- Add the new semantic result alongside current fields first.
- Run both selectors in shadow mode and log their proposed next goal.
- Review divergences against curated conversations before switching behavior.
- Keep the old canonical-question fallback until the new evaluator passes agreed thresholds.

### Acceptance criteria

- A rich one-message brief completes all matching fields without re-questioning them.
- A user who ignores a question but provides another fact receives a contextual rephrase of only the still-material gap.
- Creation remains blocked until all material requirements and final confirmation validate.

## Phase 3 — Make Deep Agent tool orchestration efficient

**Goal:** retain LLM tool choice while removing unnecessary full graph passes.

### Work

1. At turn entry, derive the active workflow contract from persisted build state.
2. Include an explicit compact instruction in the first agent prompt:
   - discovery state + current learning goal;
   - whether a planning tool call is mandatory before final reply;
   - available tools for this state;
   - no internal-process wording in the final response.
3. Remove the normal-path repair sequence in `agent_runner.py` that first permits a draft and later forces a second LLM call just to call `plan_agent`.
4. Keep recovery continuation only for malformed provider/tool outcomes, with bounded retry count and reason-coded telemetry.
5. For a ready plan, transition to the create skill/tool scope once, deterministically, and prevent return to discovery unless validation identifies a real missing material fact.
6. Distinguish phases in the user experience when creation is long:
   - validating brief;
   - preparing configuration;
   - creating agent;
   - optional WhatsApp/integration setup.

### Acceptance criteria

- Discovery requests normally use one planned tool loop, not a draft + forced-plan recovery loop.
- No `builder_plan_completion_required` event appears for correctly prompted normal discovery turns.
- Recovery loops are observable, bounded, and below an agreed error budget.

## Phase 4 — Reduce context and creation latency

**Goal:** improve speed without losing business context.

### Work

1. Instrument every run with spans:
   - context assembly;
   - skill loading;
   - each LLM request (prompt tokens, time-to-first-token, completion time);
   - each tool;
   - DB/state writes;
   - guard processing;
   - WhatsApp delivery.
2. Build a compact per-build context containing:
   - primary skill body and necessary mixins only;
   - structured canonical discovery summary;
   - evidence references/snippets needed for current action;
   - recent conversation turns;
   - active tool schemas only.
3. Avoid repeatedly embedding full history, unused skills, inactive tool schemas, and long prior summaries.
4. Refactor create workflow artifacts into a structured generation/validation contract:
   - blueprint;
   - operating manual;
   - instructions;
   - soul;
   - configuration.
5. Execute deterministic validation/persistence directly after a valid structured artifact rather than re-running separate conversational graphs for every artifact.
6. Move non-critical provisioning to resumable follow-up jobs where safe (for example optional channel or integration setup), while retaining clear user status.

### Acceptance criteria

- Prompt-token count for common discovery turns is materially lower than the observed 20k–25k per LLM call.
- p50 and p95 targets are measured in production traces, not estimated.
- WhatsApp delivery is shown separately from agent execution latency.

## Phase 5 — Harden skill publication and deployment

**Goal:** make skill changes and reseed safe, atomic, and repeatable.

### Work

1. Add a CI check that computes the content checksum of every `SKILL.md` and requires a version bump in corresponding `runtime.yaml` when content changes.
2. Validate the exact expected skill count, metadata, version uniqueness, and bundle version before deployment.
3. Change reseed to staged activation:
   - validate all source skills first;
   - publish all new immutable versions in one transaction;
   - activate the new bundle only after every record succeeds;
   - preserve prior active bundle on failure.
4. Make application config, Arthur soul, and skill bundle release metadata explicit and traceable.
5. Add post-deploy checks for active skill versions/checksums and runtime prompt/engine version.

### Acceptance criteria

- A checksum/version mismatch fails before any mutable Arthur config or active skill is changed.
- Reseed is idempotent and atomic from the operator's perspective.
- A rollback can restore the previous active bundle without editing database rows manually.

## Phase 6 — Evaluation, rollout, and product validation

**Goal:** prove behavior with representative users before broad release.

### Conversation evaluation set

Include at minimum:

- short vague request: “buat AI CS”;
- rich business brief in one message;
- personal assistant request;
- Indonesian informal/slang;
- English;
- code-switched Indonesian-English;
- user answers a different question than asked;
- user changes a prior decision;
- user says “terserah” for optional and material details;
- ambiguous integration/payment/escalation request;
- corrected user fact;
- final confirmation and create;
- failure/retry and duplicate WhatsApp delivery.

### Metrics

- turns to confirmed brief;
- user corrections per build;
- repeated-question rate;
- guard override rate by reason;
- discovery-to-create completion rate;
- create validation failure rate;
- prompt tokens and latency by phase;
- post-creation change rate within first day/week;
- delivery success and duplicate-suppression rate.

### Rollout

1. Shadow evaluate semantic selector and compact context.
2. Enable for internal Arthur testing.
3. Enable for a small private-beta cohort behind a feature flag.
4. Compare quality, latency, and correction metrics with existing flow.
5. Expand only after metrics meet success criteria and no safety regression is found.

## Sequencing and dependencies

```text
Phase 0
  ├─ Phase 1 (safe conversational delivery)
  ├─ Phase 5 (release/seed reliability)
  └─ Phase 4 instrumentation
       └─ Phase 2 (semantic discovery selector)
            └─ Phase 3 (efficient orchestration)
                 └─ Phase 6 (feature-flag rollout)
```

Phase 2 must not remove the action gate. Phase 3 must not remove tool authorization. Phase 4 must measure first, then optimize. Phase 6 is the proof that deep conversational discovery improves the product rather than merely changing its architecture.

## Explicit non-goals

- Do not turn Arthur into a short preset-only intake.
- Do not allow creation from inferred material permissions.
- Do not remove deterministic validation, evidence, confirmation, or idempotency.
- Do not expose every tool during discovery merely to reduce graph transitions.
- Do not use keyword-based language heuristics as the general solution for multilingual conversational quality.
