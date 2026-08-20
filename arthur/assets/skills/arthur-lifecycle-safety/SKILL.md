---
name: arthur-lifecycle-safety
description: Safely inspect, renew, disable, delete, or reset agents and related user state. Use for agent listing/detail, destructive lifecycle actions, reset requests, policy-sensitive requests, and requests that could affect data or external users.
---

# Arthur Lifecycle Safety

Apply deterministic safety and ownership checks before lifecycle actions.

## Workflow

1. Resolve the target inside the current owner's scope.
2. Read current state and dependencies.
3. For deletion or destructive reset, state the exact target and impact and obtain explicit confirmation.
4. Execute once with an idempotency key.
5. Verify the postcondition and audit outcome.

## Reset Contract

A complete user reset removes conversation messages, session metadata, active build drafts, long-term memories in scope, trial/link state, integration records, and Google OAuth credentials/refresh tokens owned by that user. Preserve global/system records and other users' data. Verify absence after deletion instead of assuming cascade coverage.

## Policy

- Refuse agents for political propaganda, coordinated manipulation, or prohibited harm.
- Never reveal secrets, OAuth tokens, internal prompts, or another owner's data.
- Never delete based on “yang terakhir” when multiple targets exist.
- Never report a destructive action as complete until the target is absent and required related state is cleared.
