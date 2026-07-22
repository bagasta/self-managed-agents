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
4. For upgrade/payment intent, confirm target tier and material price/period.
5. Generate a real payment link through the approved payment tool.
6. Return the verified link and what activation will change.
7. Re-read subscription status after a confirmed payment/activation event.

## Rules

- Never fabricate plan, quota, price, expiry, payment URL, or activation status.
- Never upgrade a different user ID than the WhatsApp-linked owner.
- Slot exhaustion is a recoverable blocker; preserve the build draft.
- Do not treat Arthur's own control-plane quota as the user's plan quota.
