---
name: arthur-edit-agent
description: Safely update an existing AI agent while preserving unrelated configuration. Use when a user asks to change behavior, knowledge, tools, integrations, model, escalation, prompt, or other settings on an existing agent.
---

# Arthur Edit Agent

Edit the correct agent with a minimal verified patch.

## Workflow

1. Resolve the target by explicit agent ID or unambiguous owner-scoped name.
2. Read the current configuration before proposing a patch.
3. Clarify only changes whose intent or permission is unresolved.
4. Show the material before/after impact and confirm destructive or externally visible changes.
5. Validate the merged configuration, not just the patch fragment.
6. Apply once with idempotency/version protection.
7. Read back and verify every requested field while confirming unrelated fields were preserved.
8. If a newly required integration is not authorized, mark `setup_pending` and continue into its setup skill.

## Rules

- Never replace omitted fields with defaults.
- Never target “latest agent” when the user named a different agent.
- Never claim an integration works because its flag was enabled.
- Never expose OAuth tokens, credentials, or internal tool protocol.
- If the target is ambiguous, ask for the agent name; do not guess.

## Completion

Report exactly what was changed, what was verified, and any remaining setup action. A generic “agent sudah saya edit” is not a valid completion response.
