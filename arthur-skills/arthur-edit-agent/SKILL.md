---
name: arthur-edit-agent
description: Safely update an existing AI agent while preserving unrelated configuration. Use when a user asks to change behavior, knowledge, tools, integrations, model, escalation, prompt, or other settings on an existing agent.
---

# Arthur Edit Agent

Edit the correct agent with a minimal verified patch.

## Workflow

1. Treat complaints, screenshots, and “kok agentnya tidak bisa” reports as diagnosis of an existing agent.
2. Resolve the target by explicit agent ID or unambiguous owner-scoped name.
3. Read the current configuration and runtime capability facts before proposing a patch.
4. Distinguish a configuration defect from a runtime/orchestration failure. Do not patch fields that are already correct.
5. Clarify only changes whose intent or permission is unresolved.
6. Show the material before/after impact and confirm destructive or externally visible changes.
7. Validate the merged configuration, not just the patch fragment.
8. Apply once with idempotency/version protection.
9. Read back and verify every requested field while confirming unrelated fields were preserved.
10. If a newly required integration is not authorized, mark `setup_pending` and continue into its setup skill.

## Rules

- Never replace omitted fields with defaults.
- Never target “latest agent” when the user named a different agent.
- Never claim an integration works because its flag was enabled.
- Never use `plan_agent`, create entitlement, or active-agent slot checks to diagnose/update an existing agent.
- `openai/gpt-4.1-mini` supports image input in this runtime.
- Incoming WhatsApp photos are accepted by the media ingestion route. `whatsapp_media` controls outbound image/document delivery; it is not the gate for reading an incoming receipt.
- Never delegate inbound receipt OCR to a subagent/file reader. The parent vision route receives trusted image evidence and the original image when supported.
- Never recommend deleting/recreating an agent or switching models until readback proves the current configuration cannot satisfy the requirement. Destructive replacement always requires explicit confirmation.
- Never say a builder tool is unavailable merely because progressive scoping selected the wrong workflow. Use the loaded edit workflow tools.
- Never expose OAuth tokens, credentials, or internal tool protocol.
- If the target is ambiguous, ask for the agent name; do not guess.

## Completion

Report exactly what was changed, what was verified, and any remaining setup action. A generic “agent sudah saya edit” is not a valid completion response.
