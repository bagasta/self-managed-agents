# Arthur create/edit incident — 14 October 2026

## Findings

- Production logs showed three model-provider HTTP 400 errors on a target assistant: its saved model ID had trailing whitespace. The affected assistant had already been deleted by the owner by the time of the database inspection; no active model IDs needed data repair.
- Arthur successfully created an assistant in the inspected log window. The evidence does not establish that every creation request failed.
- Arthur's edit tool could replace purpose/instructions but could not rename an assistant. The prompt lacked a specific instruction to save requested edits through that tool. The inspected window contained repeated inspections/configuration calls and no update_assistant call.
- Replacing instructions could remove the platform's tool-use guidance.
- One Google OAuth attempt failed because the connector had not been configured; a later attempt succeeded. Existing prompt guidance already requires connector configuration first.

## Changes

- Trim model IDs at API create/update validation and again at LLM construction to handle existing records. Reject whitespace-only input.
- Add ownership-scoped renaming to update_assistant, with the existing confirmation and policy checks and a 255-character limit.
- Reattach platform tool-use guidance when replacing instructions if the guidance is absent; retain current capabilities.
- Explicitly direct Arthur to inspect and save edits in the same turn when the owner clearly requests a specific change, preserve unrelated instructions, and report success only after a successful tool result.

## Validation

- 28 tests passed in an isolated container: agent LLM, Arthur edit, model selection, Google OAuth, and knowledge/runtime regressions.
- Tests cover model whitespace/provider routing, blank model rejection, persisted rename/instructions, preserved Google/scheduler capabilities, version increment, and confirmation gating.
- Candidate image booted in a separate unexposed canary with startup side effects disabled. Its health endpoint returned ok.
- Production API and public HTTPS health returned ok after deployment. API container became healthy; initial post-deployment logs contained no abandoned-run startup warning.
- No real customer assistant was created or edited as a smoke test. End-to-end conversational model behavior remains to be confirmed by normal user traffic; tests validate tool behavior without sending WhatsApp messages.

## Deployment

- Image: managed-agents-app:arthur-edit-20261014, also tagged latest after verification.
- Hotfix Dockerfile: deploy/Dockerfile.arthur-edit-hotfix. Layers only three changed runtime files onto the prior verified production image; pre-existing workspace edits were preserved and were not added to this hotfix.
- Waited until the database reported zero running agent runs before recreating only the API using Compose's configured graceful shutdown. There remains a small race between the idle check and shutdown; a zero-interruption guarantee is not implied.
- Ran python -m arthur_v2.seed to refresh Arthur's stored instructions.
- Scheduler, WhatsApp services, Redis, and PgBouncer were not restarted. No migrations or user-data cleanup ran. Temporary validation/canary containers were removed.

## Rollback

Previous image retained as managed-agents-app:rollback-arthur-edit-20261014.
Wait for active runs to finish, then run:

```sh
IMAGE_TAG=rollback-arthur-edit-20261014 docker compose -f deploy/docker-compose.prod.yml up -d --no-deps --no-build api
docker exec deploy-api-1 python -m arthur_v2.seed
docker tag managed-agents-app:rollback-arthur-edit-20261014 managed-agents-app:latest
```

The old seed restores the old Arthur prompt. Verify internal/public health and logs after rollback. Do not restart the entire stack or remove volumes.
