---
name: arthur-whatsapp-demo-channel
description: Prepare, verify, and explain an agent's WhatsApp demo or channel installation. Use when a user asks to try an agent, requests a demo code/link/contact, or wants to attach the agent to their own WhatsApp number.
---

# Arthur WhatsApp Demo and Channel

Keep the shared demo flow separate from installing the user's own WhatsApp device.

When the user asks generally how to use or install the agent, present exactly two WhatsApp choices: the Arthur demo number, or a dedicated number the user already owns. Do not choose for them. Never direct the user to a dashboard, Settings page, or another UI; Arthur completes channel setup inside this WhatsApp conversation.

## Demo Workflow

1. Verify the target agent exists and is at least `agent_created`.
2. Check required integrations; state demo limitations if setup is pending.
3. Generate a trial code/link for the explicitly selected agent.
4. Send the shared-number vCard from Arthur's dedicated session when configured.
5. Return the exact verified link/code and short test instructions.

## Own-number Workflow

1. Proceed only when the user explicitly asks to install on their own WhatsApp number or has completed and approved the demo.
2. Resolve the correct agent and device ownership.
3. Generate the QR through the dedicated channel tool and deliver it to the verified owner identity.
4. Verify connection status before saying the number is connected.

## Rules

- Do not offer a user-owned number before demo approval unless the user requests it.
- Do not confuse WhatsApp channel setup with Google OAuth.
- Never fabricate trial codes, links, QR status, contact delivery, or phone numbers.
- Never send a QR to a typed or WhatsApp LID value that is not the verified owner destination.
- A confirmed demo choice must call the trial-link tool in the same turn and return its exact wa.me link and code.
- A confirmed dedicated-number choice must call the QR tool in the same turn and report only its verified delivery/connection result.
