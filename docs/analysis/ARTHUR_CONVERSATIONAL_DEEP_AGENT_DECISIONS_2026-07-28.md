# Arthur Conversational Deep Agent — Decisions and Findings

**Date:** 2026-07-28
**Status:** Working decision record. This captures the product/architecture discussion so a future session can continue from the same premises.

## Executive decision

Arthur should remain a **Deep Agent**: the LLM orchestrates the conversation and selects tools. It must not become a rigid form wizard. The system must still deterministically control state, authorization, validation, persistence, idempotency, and external side effects.

The intended boundary is:

```text
LLM: consultation, wording, exploration priority, summaries, tool choice
System: facts/state, permissions, validation, persistence, side effects
Guard: last-resort safety only, never routine copywriting
```

Deep discovery is a product requirement. The goal is not to reduce the amount of information needed to make a good agent. The goal is to collect it conversationally, accept several facts in one user message, and ask only the highest-impact unresolved question next.

## What was observed in production

### Raw LLM response can differ from WhatsApp response

For run `68b9f96e-21fb-4dd1-ba65-1e4be40085ab`, logs showed:

1. `agent_step.llm_response` contained a natural, longer reply.
2. `plan_agent` returned `needs_clarification`.
3. `agent_run.final_reply_overridden_by_non_empty_guard` replaced that draft with a shorter canonical question.
4. `wa_incoming.final_reply_sent` and `wa-service` sent the shorter final text successfully.

This was **not** a WhatsApp transport or Markdown conversion issue. `agent_step.llm_response` is a draft; `wa_incoming.final_reply_sent` is the delivered response.

### Latency evidence

Observed discovery runs showed that the main bottleneck is repeated LLM orchestration with large prompts, not WhatsApp delivery or the deterministic planner.

| Run | Total duration | Prompt tokens | Important observation |
|---|---:|---:|---|
| `17e5e72e-263f-4427-afbd-75076161420c` | 32.2 s | 63,732 across calls | First LLM response did not call `plan_agent`; runtime forced another LLM pass, then a final response pass. |
| `b74f3719-07c1-4c7c-a2dd-8b997d65d21f` | 56.1 s | 48,251 across calls | Two large LLM passes plus planning; final WhatsApp delivery took about 0.6 s. |

`plan_agent` itself completed in roughly 0.1 s in the observed runs. The expensive part is repeatedly sending ~20k–25k-token contexts to the model.

## Reply guard decision

### Why it exists

The reply guard should protect users from objective failure states:

- empty/model-malformed replies;
- internal implementation leakage (`plan_agent`, state/evidence/tool details);
- premature success claims not supported by state/tool results;
- unsupported capability claims;
- duplicate outbound WhatsApp delivery.

### What it must not do

The guard must not decide whether a natural question is semantically good by looking for language-specific keywords. That makes UX robotic and fails for English, mixed language, slang, typos, and other languages.

It must not routinely replace the LLM's wording with a planner's canonical question. The planner is authoritative about **state**, not user-facing copy.

### Implemented local change

The local change in `app/core/engine/reply_guard.py` removes the topic/keyword matcher and changes planner fallback behavior to:

- keep any non-empty, non-internal, non-premature-success model reply;
- use the planner's canonical question only for an empty or unsafe reply.

Regression coverage was added/updated in `tests/test_reply_guard.py`, including a multilingual natural reply. A smoke test with a temporary container passed. Full `pytest` was unavailable on the host at the time of the change.

This change has been rebuilt and deployed to API replicas, but it is still **uncommitted local work**.

### Remaining guard concern

`agent_runner.py` still runs `guard_single_discovery_question` and `guard_repeated_questions` after the final reply. These are directionally useful, but must be treated as safety/quality checks. They should not become another language-dependent rewriting layer.

## Discovery product principles

### Required information

Current deterministic validation requires, for a business agent:

1. problem/pain point;
2. usage context (personal or business);
3. agent name;
4. audience;
5. main tasks;
6. capabilities, including an explicit file decision;
7. prohibited actions;
8. unknown/fallback handling;
9. escalation target (for business: conditions, recipient, WhatsApp number);
10. integrations;
11. final explicit confirmation of the summary.

For personal agents, escalation target is not required, so there are nine core fields plus final confirmation. Optional fields improve quality but should not block creation by default.

### Desired interview behavior

Arthur must act as a requirements consultant for an inexperienced user:

- start from the real workflow and pain point, not configuration labels;
- accept multiple volunteered facts from one message;
- reflect understanding before asking a gap question;
- ask one high-impact question at a time;
- translate technical choices into business/workflow consequences;
- never invent material permissions or external actions;
- show a concise factual brief and request explicit confirmation before create.

If a user does not answer a question, Arthur should preserve all new facts they did provide and rephrase the remaining material gap in context. For optional details it may offer a safe default for confirmation. It must not infer permissions, integrations, payments, external messaging, escalation recipients, or sensitive-data policy.

## Architectural contradictions to resolve

### 1. Static questionnaire order versus adaptive consultation

`builder_discovery.py` currently defines fixed groups and selects the first unresolved field from that order. This guarantees coverage but makes the next question form-driven rather than risk-driven.

Target: planner should produce a structured `learning_goal`, known facts, unresolved risk, and confirmation requirement. It should not produce mandatory user-facing copy or impose a fixed sequence when the conversation already supplies richer context.

### 2. Rules are duplicated across too many layers

Conversation constraints currently exist in the kernel, skills, prompt builder, follow-up directives, deterministic validator, runtime, and guards. Duplicate policy causes drift and competing instructions.

Target: one canonical domain schema and state contract; skills define workflow behavior; runtime enforces tool scope and state; guard handles only safety failures.

### 3. Evidence validation is valuable but can be too rigid

Evidence must prevent hallucinated configuration, but exact quote/schema requirements can make Arthur appear not to understand users who speak informally or in another language.

Target: retain immutable evidence and confirmation, but track semantic facts with confidence/ambiguity. Clarify only when a material fact is genuinely ambiguous; do not discard a valid understanding because it was not phrased like a schema field.

### 4. The current runtime adds avoidable LLM passes

When a discovery/create turn omits `plan_agent`, the runner adds a directive and re-invokes the graph. This is safe but expensive and can create a draft/final mismatch.

Target: expose the active state contract at the start of the turn. Require `plan_agent` as the first relevant tool step when needed, rather than allowing an initial draft and repairing it afterward.

### 5. Skill release and reseed are not sufficiently atomic

System skills are immutable by name/version/checksum. Changing a `SKILL.md` without bumping `runtime.yaml` causes reseed to fail. During this session, `arthur-discovery` required bumps through `1.2.3` as content changed.

Target: CI/preflight must reject a checksum-changing skill without a version bump, and reseed should make the Arthur configuration, soul, and all skill publications transactional or use a staged activation model.

## Target runtime model

```text
Incoming user message
  → load compact relevant state and evidence
  → LLM-led conversation / tool choice
  → deterministic planner validates facts and returns semantic state
  → LLM writes one natural response in the user's language
  → deterministic action gate authorizes create/update/integrations
  → thin delivery safety checks
  → persist exactly the response delivered to the user
```

This is still LLM-orchestrated. A normal Deep Agent tool loop remains valid:

```text
LLM decides to call a tool → tool result → LLM interprets it and replies
```

The optimization is to remove **unnecessary** loops such as:

```text
LLM draft → runtime notices required plan missing → forced LLM pass → plan tool → final LLM pass
```

For material creation, multi-step orchestration is appropriate. For a normal discovery turn, the target should be one planning/tool cycle and one natural final response.

## Performance direction

1. Measure spans separately: `context_build`, model time-to-first-token, model completion, each tool, DB, and WhatsApp delivery.
2. Compact context: current primary skill, relevant mixins, a structured discovery summary, evidence references, recent turns, and only active tool schemas.
3. Avoid forced recovery calls by establishing the state/tool contract before the first model invocation.
4. For create, use structured outputs/artifacts where possible; validate and persist deterministically instead of making every artifact a separate conversational graph continuation.
5. Keep non-critical provisioning (for example optional channel/integration setup) resumable after the core agent exists.

Target experience:

- discovery turn: roughly 3–8 seconds when no external research is needed;
- creation: a clear primary completion in the tens of seconds at most, with optional provisioning able to continue independently.

## Progressive-skill model

Arthur currently follows a useful progressive-disclosure pattern:

- reseed reads local `arthur-skills/*/SKILL.md` and `runtime.yaml`, then publishes immutable system-skill records to the database;
- runtime chooses one primary workflow skill per turn;
- discovery uses `arthur-discovery` with discovery/planning tools;
- confirmation/ready/create states use `arthur-create-agent` with compose/validate/create/verify tools;
- runtime loads full content for the selected primary skill (and relevant policy mixins), not every skill body simultaneously.

This mirrors the useful distinction in Codex:

- skill: reusable workflow instructions;
- tool: controlled ability/action;
- runtime: permission, validation, execution, and side-effect enforcement.

## Immediate backlog

1. Commit the deployed reply-guard and test changes after running a full test suite in CI.
2. Add structured timing telemetry and a dashboard/trace view that distinguishes LLM draft, guard decision, final reply, and WhatsApp message ID.
3. Add multilingual conversational evals: Indonesian informal, English, code-switching, terse answers, rich briefs, non-answers, corrections, and changed requirements.
4. Replace static next-field ordering with a risk-prioritized semantic learning-goal selector while preserving the required-fields action gate.
5. Reduce prompt/context size and remove forced-plan recovery passes from normal turns.
6. Add CI validation for skill content/version/checksum and make reseed staged/atomic.

## Non-negotiable safety constraints

- No `create_agent` before required material facts and explicit final confirmation are validated.
- Do not infer permissions for integrations, payment, external messaging, escalation, or sensitive-data handling.
- Keep tool authorization and side-effect validation outside the LLM.
- Preserve user-provided facts and the actually delivered assistant message in durable state.
