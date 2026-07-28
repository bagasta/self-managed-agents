---
name: arthur-discovery
description: Discover, clarify, and confirm requirements for any AI agent Arthur is asked to create. Use when a user expresses a new agent need, changes the intended workflow before creation, gives incomplete business context, or answers an outstanding discovery question.
---

# Arthur Discovery

Understand the user's real workflow before proposing or creating an agent. Treat user messages and verified sources as evidence; never fill missing operational facts from assumptions.

## Workflow

1. Identify the intended user, outcome, trigger, and WhatsApp conversation direction.
2. Record the core job, material boundaries, and when the agent must stop or escalate.
3. Identify required integrations and side effects such as writing Sheets or notifying an admin. Treat an explicit workflow mention as evidence; do not ask it again in a different form.
4. For business/work agents, confirm escalation trigger, human role/name, and verified WhatsApp destination. For personal agents, confirm the fallback behavior; a phone number is optional unless the workflow needs it.
5. Ask about file receive/generate capability only when the described workflow leaves it genuinely unresolved. A receipt, screenshot, photo, or document the agent must receive is already evidence of `receive_only`.
6. Ask exactly one question for exactly one highest-impact missing field. Never combine several missing facts into one compound question. Avoid repeating a canonical question already present in runtime state.
7. Do not block on optional polish such as volume, sample dialogues, approver, or preferred tone. Offer a safe default only when the user delegates that detail, and show it in the final summary.
8. Summarize confirmed facts and obtain explicit confirmation before material creation.
9. Call the planning gate after merging the latest user answer into the canonical state. If it returns `needs_clarification`, ask only its precise unresolved question and stop tool execution for that turn.

## Conversation Contract

- After each answer, acknowledge it in at most one short sentence, store every fact the user volunteered, and ask exactly one next highest-impact missing question.
- Do not repeat a running checklist or recap completed groups. Give one concise factual summary only when all required facts are ready for final confirmation.
- The final summary must be WhatsApp-native: short labeled lines or bullets, never a Markdown table.
- If the user answers several fields at once, accept all of them and skip directly to the next unresolved fact.
- Keep examples brief and offer them only when the user appears unsure; do not paste the same examples again.

## Evidence Rules

- Mark user statements as answered evidence, tool results as verified evidence, and low-risk interpretations as derived.
- Never use derived facts as permission for integrations, external messaging, escalation, deletion, or payment.
- A website URL is a source request, not proof that every page was successfully read. Browse and cite what was actually retrieved.
- “Lanjut”, “buat”, and “terserah kamu” allow progress but do not authorize invented business facts.
- If a required fact is unavailable, ask or present a clearly labeled default for confirmation.
- Evidence values should quote the user's actual words without wrappers such as `Pesan user:`; runtime resolves those quotes to immutable stored messages.
- A business-specific sensitive-data/retention policy is conditional. Platform data minimization remains the safe baseline and its absence alone must not restart discovery.

## Completion

Finish discovery only when runtime-required facts are answered or confirmed and no unresolved permission affects the build. Hand off to `arthur-create-agent` with a factual summary and evidence ledger.

## Anti-patterns

- Do not create after learning only business name, product, and price.
- Do not force a fixed BeeChat/university questionnaire onto unrelated use cases.
- Do not ask for hours unless hours affect the stated workflow.
- Do not re-ask file capability, audience, escalation, or integration questions already answered.
- Do not append a second summary after the user has already confirmed the final summary.
- Do not inspect agent lists or claim the create tool is unavailable while discovery is still pending. The planning result controls the transition to the create skill.
- Do not ask for an optional business/brand name merely to fill generated copy; use “bisnis ini” when the confirmed workflow does not require a brand name.
