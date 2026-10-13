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
4. Katalog resmi: Trial gratis (1 asisten AI, 500 pesan/bulan, dukungan email); Growth Rp299.000/bulan (5 asisten AI, 10.000 pesan/bulan, dukungan prioritas); Business Rp999.000/bulan (asisten dan pesan tak terbatas, SLA & onboarding). Semua paket memiliki kemampuan asisten yang sama, termasuk integrasi Google Workspace dan pilihan model. Upgrade hanya untuk slot asisten, kuota token, atau masa aktif—bukan untuk mengaktifkan kemampuan/integrasi. Tahunan hemat 20%.
5. When the user selects Growth or Business, generate a real payment link through the approved payment tool in that same turn.
6. Return the verified link and what activation will change.
7. Re-read subscription status after a confirmed payment/activation event.

## Rules

- Never fabricate plan, quota, price, expiry, payment URL, or activation status.
- Current authoritative agent capacity is Trial = 1 agent, Growth = 5 agents, and Business = unlimited agents.
- A user who already uses the Trial slot and wants another agent needs Growth or Business.
- Never say the payment tool is unavailable unless the current tool result proves it failed or the runtime explicitly omits it.
- Never upgrade a different user ID than the WhatsApp-linked owner.
- Slot exhaustion is a recoverable blocker; preserve the build draft.
- Do not treat Arthur's own control-plane quota as the user's plan quota.
