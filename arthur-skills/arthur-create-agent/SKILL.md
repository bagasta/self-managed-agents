---
name: arthur-create-agent
description: Create, validate, and verify a new AI agent from confirmed requirements. Use after discovery is complete and confirmed, when a ready build must be composed, created, checked, and advanced to demo or integration setup.
---

# Arthur Create Agent

Create only from confirmed evidence and finish the whole required transaction.

## Preconditions

- Runtime build state belongs to the current owner and session.
- Required discovery facts are confirmed.
- Agent name, target users, core jobs, behavior boundaries, knowledge source, escalation/fallback, and required integrations are known.
- Subscription capacity and duplicate-name constraints have been checked.

If any precondition fails, return to discovery with one precise question. Never call create with placeholders or invented defaults.
`Trial` is a valid plan state, not a blocker by itself. Continue when the verified entitlement says creation is allowed; never require dashboard linking merely because the plan label is Trial.

## Workflow

1. Read platform capabilities and relevant preset data; do not infer internal capabilities.
2. Compose blueprint, operating manual, instructions, and soul only from the evidence ledger.
3. Include explicit capability contracts for files, browsing, integrations, escalation, and WhatsApp delivery.
4. Validate the full configuration.
5. Present a concise final summary if confirmation is still pending.
6. Call the create tool once with an idempotency key after confirmation.
7. Read the created agent back and verify name, model, instructions, required tools, escalation, and ownership.
8. If integrations are required, continue into their setup skill. Creation alone is `agent_created`, not `production_ready`.
9. When setup permits, prepare the WhatsApp demo and return the verified trial link/code.

After an explicit confirmation such as “sesuai” or “sudah sesuai”, execute the create workflow in the same turn. Do not ask the user to open a dashboard, connect WhatsApp, or send a code unless a verified tool result for this build explicitly requires that exact step.
Treat a missing business/brand name as optional copy context: use “bisnis ini”, never emit a placeholder, and never interrupt a confirmed build solely to ask for it.
Never report creation or advance build state from a failed/unknown tool result; require `success=true` and a valid created agent identifier.

## Postconditions

- `agent_created`: database record exists and matches the confirmed config.
- `setup_pending`: a required connector, OAuth, resource, or demo step remains.
- `demo_limited`: demo exists but limitations are stated.
- `production_ready`: all required integrations and core smoke tests pass.

Never say “selesai”, “siap”, or “sudah jadi” unless the corresponding postcondition is verified.

## Recovery

- On transient create failure, read by idempotency key/name before retrying.
- On validation failure, repair only the reported field; do not restart discovery or ask an unrelated stock question.
- On provider/tool outage, report the concrete blocker and safe retry state. Never tell the user to “coba lagi” without preserving progress.
