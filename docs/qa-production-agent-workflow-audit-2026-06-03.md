# QA Production Agent Workflow Audit - 2026-06-03

Scope: backend agent architecture in the current dirty worktree, with focus on Arthur, system-message creation, SOP/Agent Operating Manual, subagents, tool calling, and launch-readiness behavior.

## Verdict

The project is not production-ready for launch until Arthur's SOP/system-message contract is hardened.

The strongest part of the current architecture is that SOP is no longer just text inside `instructions`: it is loaded by runtime, injected into the system prompt, and checked by `verify_agent`. The weak part is that SOP readiness is still partly prompt-driven and can be satisfied by automatic fallback generation. That means Arthur can still create an agent that looks operationally mature while the actual business workflow is generic, inferred, stale, or missing critical owner-confirmed rules.

## Highest-risk production failures

### P0 - Arthur can still create a production-looking business agent with an auto/fallback SOP

Evidence:
- `system-message-builder.md:181-190` says `compose_agent_operating_manual()` is mandatory for business/custom agents and that the result must be passed into `create_agent/update_agent`.
- `app/core/tools/builder_tools.py:2904-3015` can infer a semantic operating manual when Arthur skipped the manual/blueprint flow, and defaults missing maturity to `usable`.
- `app/core/tools/builder_tools.py:3379-3417` has a fallback path that builds a manual from blueprint or fallback blueprint.
- `app/core/tools/builder_tools.py:4897-4900` says `create_agent` can make a draft from context if `operating_manual` is empty.
- `app/core/domain/agent_sop_service.py:654-680` marks blueprint-derived manuals as `usable` when context length is not sparse.

Why this breaks in production:
- Arthur may skip a real interview, skip explicit SOP composition, or generate a thin blueprint, but `create_agent` can still produce an SOP-like artifact.
- `verify_agent` sees an operating manual and may return `launch_ready` if `maturity=usable`.
- The customer-facing agent then has enough text to act confident, but not enough validated business procedure to handle payment, refund, booking, order, approval, file delivery, or escalation safely.

Required launch fix:
- Treat `compose_agent_operating_manual` as a hard builder state, not just a prompt instruction.
- For business/custom/WhatsApp-customer agents, block `create_agent` or force `maturity=draft` unless the manual came from the explicit SOP tool result or an owner-approved artifact.
- Add deterministic SOP validation beyond length and workflow count: required inputs, allowed decisions, prohibited actions, escalation/handoff, final-output definition, and missing-context policy.

### P0 - File-generation delivery contract is inconsistent after parent-delivery routing

Evidence:
- `system-message-builder.md:209-210` now teaches the correct flow: subagent writes final file to `/workspace/shared/<filename>`, returns `SIAP_DIKIRIM_PARENT`, and the parent sends via `send_whatsapp_document` or `send_whatsapp_image`.
- `app/core/tools/builder_tools.py:1842-1846` still requires file-delivery instructions to mention `send_whatsapp_document`.
- `app/core/tools/builder_tools.py:3732` still treats missing `send_whatsapp_document` as weak generated-file/payment instructions.
- `app/core/tools/builder_tools.py:4761-4765` still errors in `validate_agent_config` if instructions do not mention `send_whatsapp_document`.
- `app/core/tools/builder_tools.py:5061-5065` still applies the same critical check during `create_agent`.

Why this breaks in production:
- The runtime contract is parent delivery, but the validator only checks for one parent media tool name.
- Arthur can be pushed into writing confusing instructions just to satisfy validation.
- The validator does not require the actual safe handoff markers: `/workspace/shared`, `SIAP_DIKIRIM_PARENT`, and "subagent must not send WhatsApp".

Required launch fix:
- Replace "must mention `send_whatsapp_document`" with a parent-delivery contract validator.
- Required terms should include `/workspace/shared`, `SIAP_DIKIRIM_PARENT`, no WhatsApp send from subagent, and parent media send after artifact return.
- Keep `send_whatsapp_document`/`send_whatsapp_image` as parent tool options, not the whole contract.

### P1 - SOP table drops important manual fields

Evidence:
- `app/core/domain/agent_sop_service.py:673-690` builds manuals with `knowledge_plan`, `memory_plan`, `state_plan`, `human_approval_points`, `escalation_rules`, and `validation_checklist`.
- `app/models/agent_operating_manual.py:24-32` stores only source/domain/maturity/missing_context/assumptions/workflows/created_by.
- `app/core/domain/agent_sop_service.py:861-876` reconstructs runtime artifacts only from the fields stored in the row.
- `app/core/domain/agent_sop_service.py:900-933` upserts only the limited row fields.
- `alembic/versions/018_agent_operating_manuals.py:67-93` backfills only the same limited set from `tools_config.operating_manual`.

Why this breaks in production:
- The first full artifact may exist in `tools_config`, but once runtime reads the row source it gets a narrower SOP.
- Validation checklist, state plan, human approvals, and explicit escalation rules are exactly the parts needed to keep business agents from improvising.

Required launch fix:
- Store the full normalized SOP artifact in a JSONB `artifact` column, or add explicit JSONB columns for the omitted sections.
- Make runtime prompt formatting read the full artifact, not a lossy projection.
- Backfill existing full artifacts from `agents.tools_config->operating_manual`.

### P1 - SOP DB read failures are silently ignored

Evidence:
- `app/core/domain/agent_sop_service.py:879-897` catches all exceptions in `get_latest_agent_operating_manual` and silently falls back to `tools_config`.
- `app/core/engine/agent_runner.py:1420-1425` depends on this function before prompt assembly.

Why this breaks in production:
- A migration issue, malformed row, DB permission problem, or relationship bug can be hidden at runtime.
- Agents may run with stale embedded SOP while `verify_agent` and runtime appear healthy.

Required launch fix:
- Log the exception with `agent_id`.
- Surface a readiness blocker for SOP-load failure.
- Do not silently turn DB failures into normal fallback unless the failure is a known "table not migrated yet" dev-mode path.

### P1 - Draft/needs_review SOP restriction is prompt-level, not a runtime gate

Evidence:
- `app/core/domain/agent_sop_service.py:840-858` produces blockers for missing/draft/needs_review SOP.
- `app/core/engine/prompt_builder.py:199-214` instructs the model to only intake, clarify, summarize, and escalate when SOP is draft or missing.
- Tool exposure itself is not reduced there; the restriction is injected as instructions.

Why this breaks in production:
- A model can still call tools that perform irreversible or customer-visible actions.
- The platform needs deterministic gating for high-risk workflows, not only prompt compliance.

Required launch fix:
- If SOP maturity is `draft` or `needs_review`, runtime should remove or block high-risk tools for that run: payment/admin approval, final delivery, booking/write actions, external account mutation, and generated-file send.
- Keep intake, search/read-only, memory notes, and escalation.

### P1 - Full test suite did not complete

Evidence:
- Focused suites from the current changes previously passed:
  - `tests/test_whatsapp_progress.py tests/test_whatsapp_direct_send.py tests/test_deploy_path.py`: 66 passed.
  - `tests/test_builder_tools.py`: 105 passed.
- Full suite command `PYTHONPATH=. .venv/bin/python -m pytest -q` hung with no result and had to be killed.

Why this breaks in production:
- A launch branch with hanging tests is not releasable.
- Given the number of changed runtime files and new migrations, a full green suite or known isolated timeout is required.

Required launch fix:
- Run the suite with timeout and failure isolation, for example per directory or with `-vv --maxfail=1`.
- Identify the hanging test/process and either fix it or quarantine it with a tracked issue and a smaller mandatory release gate.

### P1 - Migration chain and live DB state must be verified before deploy

Evidence:
- New migrations exist for created-by metadata and operating manuals: `017_agent_created_by_metadata.py`, `018_agent_operating_manuals.py`.
- The current worktree has many modified and untracked runtime/model files.
- `018_agent_operating_manuals.py:16-18` depends on `017`.

Why this breaks in production:
- If production `alembic_version` is not exactly compatible, SOP rows may not exist or may be partially backfilled.
- Runtime will then depend on fallback behavior at the same time production traffic starts relying on SOP as a contract.

Required launch fix:
- Query live `alembic_version` before deploy.
- Dry-run migration on a staging copy.
- Verify a newly created Arthur agent gets both `agents.tools_config.operating_manual` and an `agent_operating_manuals` row with matching version/source/maturity.

### P2 - Arthur rulebook/seed drift remains a release risk

Evidence:
- `scripts/seed_arthur.py:23-43` reads `system-message-builder.md` and seeds Arthur's instructions.
- `scripts/seed_arthur.py:114-137` updates Arthur when the script is run.
- Prompt/rulebook fixes do not affect a live Arthur until seeded into the DB.

Why this breaks in production:
- Code can be correct while the live Arthur keeps using old instructions.
- This repo has already had drift between direct builder tools and old HTTP/ngrok-style prompting.

Required launch fix:
- Make `seed_arthur.py --dry-run` and real seed part of deployment.
- Add a test that checks the seeded Arthur instructions contain the current SOP and parent-file-delivery contract.

## Launch gate checklist

Block launch until all P0 items are fixed and verified.

Minimum release gate:
1. Business/custom Arthur creation cannot reach `launch_ready` without an explicit, validated operating manual.
2. Generated-file agents validate the parent-delivery contract: `/workspace/shared` plus `SIAP_DIKIRIM_PARENT` plus parent media send.
3. SOP persistence is not lossy, or runtime intentionally reads the full artifact from a canonical source.
4. SOP DB load failures are visible and block readiness.
5. Draft/needs_review SOP disables high-risk runtime actions deterministically.
6. Full test suite completes, or the release gate has a documented isolated timeout with focused green coverage.
7. Staging migration and Arthur reseed are verified before production traffic.

## Recommended fix order

1. Patch file-delivery validators first. This is narrow, high-confidence, and directly related to the WhatsApp/subagent bug already observed.
2. Make SOP readiness deterministic in `create_agent`, `update_agent`, and `verify_agent`.
3. Fix SOP persistence shape so runtime does not lose approval/escalation/state/checklist details.
4. Add runtime tool gating for draft/needs_review SOP.
5. Add regression tests for Arthur business agent creation, generated-file parent delivery, SOP row artifact preservation, and launch-readiness blockers.
6. Run migrations on staging, seed Arthur, then run a real WhatsApp smoke test: create business agent, create generated-file agent, update existing agent, and verify owner-facing setup status.

