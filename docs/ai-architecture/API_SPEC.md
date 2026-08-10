# API Spec

Tanggal snapshot: 2026-07-02

## Base URL
- Local: `http://localhost:8000`
- Swagger: `/docs`
- ReDoc: `/redoc`
- Metrics: `/metrics`

## Authentication Requirements
- Management/admin endpoints: `X-API-Key: <settings.API_KEY>`.
- Runtime message endpoint: `X-Agent-Key: <agent.api_key>`.
- Some inbound channel webhooks currently rely on internal trust; production should add webhook auth/HMAC.

## Endpoint List
### Meta
- `GET /health`
- `GET /health/detailed`
- `GET /metrics`

### Models
- `GET /v1/models`

### Auth Keys
- `POST /v1/auth/keys`
- `GET /v1/auth/keys/me`
- `POST /v1/auth/keys/renew`
- `POST /v1/auth/keys/{key_id}/revoke`

### Users
- `POST /v1/users`
- `GET /v1/users/{user_id}`
- `PATCH /v1/users/{user_id}`
- `POST /v1/users/phone-login`

### Subscriptions
- `GET /v1/subscriptions/plans`
- `POST /v1/subscriptions/{user_id}/activate`
- `POST /v1/subscriptions/{user_id}/upgrade`
- `POST /v1/subscriptions/{user_id}/topup`
- `GET /v1/subscriptions/{user_id}`

### Agents
- `POST /v1/agents`
- `GET /v1/agents`
- `GET /v1/agents/{agent_id}`
- `PATCH /v1/agents/{agent_id}`
- `DELETE /v1/agents/{agent_id}`
- `POST /v1/agents/{agent_id}/renew`
- `GET /v1/agents/{agent_id}/whatsapp/qr`
- `GET /v1/agents/{agent_id}/whatsapp/status`
- `POST /v1/agents/{agent_id}/whatsapp/connect`
- `DELETE /v1/agents/{agent_id}/whatsapp`

### Sessions and Messages
- `POST /v1/agents/{agent_id}/sessions`
- `GET /v1/agents/{agent_id}/sessions`
- `GET /v1/agents/{agent_id}/sessions/{session_id}`
- `PATCH /v1/agents/{agent_id}/sessions/{session_id}`
- `POST /v1/agents/{agent_id}/sessions/{session_id}/messages`
- `GET /v1/sessions/{session_id}/history`
- `GET /v1/sessions/{session_id}/stream`
- `GET /v1/runs/{run_id}`

### Knowledge and Tools
- `GET /v1/agents/{agent_id}/memory`
- `POST /v1/agents/{agent_id}/memory`
- `DELETE /v1/agents/{agent_id}/memory/{key}`
- `GET /v1/agents/{agent_id}/skills`
- `POST /v1/agents/{agent_id}/skills`
- `GET /v1/agents/{agent_id}/skills/{name}`
- `DELETE /v1/agents/{agent_id}/skills/{name}`
- `GET /v1/agents/{agent_id}/custom-tools`
- `POST /v1/agents/{agent_id}/custom-tools`
- `GET /v1/agents/{agent_id}/custom-tools/{name}`
- `DELETE /v1/agents/{agent_id}/custom-tools/{name}`
- `POST /v1/agents/{agent_id}/documents`
- `GET /v1/agents/{agent_id}/documents`
- `POST /v1/agents/{agent_id}/documents/search`
- `GET /v1/agents/{agent_id}/documents/{doc_id}`
- `PATCH /v1/agents/{agent_id}/documents/{doc_id}`
- `DELETE /v1/agents/{agent_id}/documents/{doc_id}`
- `POST /v1/agents/{agent_id}/documents/upload`

### Channels and Integrations
- `POST /v1/channels/incoming/{session_id}`
- `POST /v1/channels/wa/incoming`
- `GET /v1/channels/wa-dev/operator-route`
- `POST /v1/channels/wa-dev/claim-code`
- `POST /v1/channels/wa-dev/disconnect`
- `GET /v1/integrations/google/auth-link`
- `GET /v1/integrations/google/status`

## Request Schema Highlights
### Create Agent
```json
{
  "name": "CS Agent",
  "description": "Customer support WhatsApp",
  "instructions": "SOP dan persona agent",
  "model": "anthropic/claude-sonnet-4-6",
  "temperature": 0.7,
  "max_tokens": 1024,
  "tools_config": {"memory": true, "escalation": true, "scheduler": true},
  "channel_type": "whatsapp",
  "owner_external_id": "628xxx"
}
```

### Send Message
```json
{
  "message": "Halo, saya mau cek order",
  "external_user_id": "628xxx"
}
```

## Response Schema Highlights
### Message Response
```json
{
  "reply": "string",
  "steps": [{"tool_name": "string", "summary": "string"}],
  "run_id": "uuid"
}
```

### Agent Response
Contains agent config, `api_key`, quota fields, WA device fields, version, owner metadata, and optional `qr_image` on create/connect.

## Authorization Rules
- Admin key can manage platform resources.
- Agent key can execute only that agent's message endpoint.
- Owner/operator authorization is enforced in runtime policy for certain tools, but management endpoint ownership enforcement should be strengthened.
- Google Workspace MCP actions should only be allowed for owner/operator-authorized sessions.

## Error Handling
- `401`: invalid API key or agent key.
- `402`: quota/payment required.
- `404`: missing agent/session/document/etc.
- `409`: duplicate user/subscription transaction/reference.
- `422`: validation error or unsupported upload.
- `429`: rate limit, notably message endpoint `20/minute`.
- `502`: upstream service such as WA service failure.
- `503`: detailed health degraded.

## Rate Limiting
- Runtime message endpoint uses slowapi limit `20/minute`.
- Redis-backed rate limit storage is used when `REDIS_URL` is set; otherwise in-memory behavior applies.

## Versioning Strategy
- API prefix is `/v1`.
- Breaking API changes should create `/v2` or explicit compatibility adapters.
- Agent config should use versioned `tools_config` contracts when schema changes become incompatible.

