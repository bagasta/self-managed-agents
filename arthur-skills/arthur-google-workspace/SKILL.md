---
name: arthur-google-workspace
description: Configure and verify Google Workspace capabilities for an agent. Use when a workflow requires Google Sheets, Drive, Docs, Forms, Slides, Calendar, Gmail, OAuth, reconnect, or a Google resource link.
---

# Arthur Google Workspace

Treat Google setup as a transaction, not a boolean capability.

## State Sequence

`required -> configured -> auth_pending -> authorized -> resource_ready -> verified`

## Workflow

1. Confirm the exact Google product, operation, data schema, trigger, and write/read permission.
2. Update or create the agent with only the required Google capability.
3. Read the agent back and verify the connector configuration.
4. Check authorization for the current owner identity.
5. If authorization is missing or expired, generate and return the real OAuth/reconnect URL in the same turn.
6. After authorization, create or select the intended resource. Never invent a spreadsheet ID, URL, tab, or column.
7. Inspect the resource structure before writing.
8. Run a non-destructive smoke test in an owned sandbox spreadsheet or isolated temporary worksheet.
9. Report `production_ready` only after the functional smoke test succeeds.

## Google Sheets Survey Contract

For survey agents, confirm columns such as timestamp, customer identity, purchase reference, each answer, score, consent if applicable, escalation status, and notes. The agent must know whether it initiates outbound messages or only responds inbound; do not infer permission to contact customers.

## Failure Handling

- If OAuth is required, never ask an unrelated file-capability question.
- Never substitute sandbox code, local files, or non-Google tools for a required Google action.
- Preserve the created agent and return `setup_pending` with one concrete next action.
- If OAuth succeeds but resource creation fails, do not generate another auth link unless the error is actually auth-related.
