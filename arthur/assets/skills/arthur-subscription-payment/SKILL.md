---
name: arthur-subscription-payment
description: Inspect and handle agent subscription, slot, quota, renewal, upgrade, and payment-link workflows. Use when a user asks about plan limits, cannot create due to capacity, requests an upgrade, needs renewal, or asks for payment.
---

# Arthur Subscription and Payment

Resolve subscription identity from the current WhatsApp-linked user; never assume a similarly named database row is the same owner.

## Workflow

1. Distinguish a plan-catalog question from the user's current-plan question.
2. Resolve the exact current user ID and retrieve their live subscription.
3. Explain the relevant limit or entitlement in plain language.
4. For upgrade/payment intent, confirm the target tier. State price/period only when returned by an approved live source; otherwise say those details are shown on checkout.
5. When the user selects Starter, Pro, or Enterprise, generate a real payment link through the approved payment tool in that same turn.
6. Return the verified link and what activation will change.
7. Re-read subscription status after a confirmed payment/activation event.

## Rules

- Never fabricate plan, quota, price, expiry, payment URL, or activation status.
- Current authoritative agent capacity is Starter = 1 agent, Pro = 2 agents, and Enterprise = unlimited agents. Do not replace these with invented 2/3/5+ values.
- A user who already uses one slot and wants a second agent needs at least Pro, not Starter.
- Never say the payment tool is unavailable unless the current tool result proves it failed or the runtime explicitly omits it.
- Never upgrade a different user ID than the WhatsApp-linked owner.
- Slot exhaustion is a recoverable blocker; preserve the build draft.
- Do not treat Arthur's own control-plane quota as the user's plan quota.
