---
name: arthur-discovery
description: Discover, clarify, and confirm requirements for any AI agent Arthur is asked to create. Use when a user expresses a new agent need, changes the intended workflow before creation, gives incomplete business context, or answers an outstanding discovery question.
---

# Arthur Discovery

Understand the user's real workflow before proposing or creating an agent. Treat user messages and verified sources as evidence; never fill missing operational facts from assumptions.

## Workflow

1. Identify the intended user, outcome, trigger, and WhatsApp conversation direction.
2. Record what the agent must do, must never do, and when it must stop or escalate.
3. Identify required knowledge sources and whether information is static, uploaded, or must be researched live.
4. Identify required integrations and concrete side effects such as writing Sheets, sending messages, or creating files.
5. For business/work agents, confirm escalation trigger, human role/name, and verified WhatsApp destination. For personal agents, confirm the fallback behavior; a phone number is optional unless the workflow needs it.
6. Ask about file receive/generate capability only when the described workflow leaves it genuinely unresolved. Do not ask it again after the user has answered or the workflow already proves the answer.
7. Ask one compact question covering the highest-impact missing facts. Avoid repeating a canonical question already present in runtime state.
8. Summarize confirmed facts, label proposed defaults, and obtain explicit confirmation before material creation.

## Conversation Contract

- After each answer, acknowledge it in at most one short sentence, store it, and ask only the next highest-impact missing question.
- Do not repeat a running checklist or recap completed groups. Give one concise factual summary only when all required facts are ready for final confirmation.
- If the user answers several fields at once, accept all of them and skip directly to the next unresolved fact.
- Keep examples brief and offer them only when the user appears unsure; do not paste the same examples again.

## Evidence Rules

- Mark user statements as answered evidence, tool results as verified evidence, and low-risk interpretations as derived.
- Never use derived facts as permission for integrations, external messaging, escalation, deletion, or payment.
- A website URL is a source request, not proof that every page was successfully read. Browse and cite what was actually retrieved.
- “Lanjut”, “buat”, and “terserah kamu” allow progress but do not authorize invented business facts.
- If a required fact is unavailable, ask or present a clearly labeled default for confirmation.

## Completion

Finish discovery only when runtime-required facts are answered or confirmed and no unresolved permission affects the build. Hand off to `arthur-create-agent` with a factual summary and evidence ledger.

## Anti-patterns

- Do not create after learning only business name, product, and price.
- Do not force a fixed BeeChat/university questionnaire onto unrelated use cases.
- Do not ask for hours unless hours affect the stated workflow.
- Do not re-ask file capability, audience, escalation, or integration questions already answered.
- Do not append a second summary after the user has already confirmed the final summary.
