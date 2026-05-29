# Google Workspace MCP — Integration Guide for AI Platform Team

## Overview

```
User (browser)
    │
    ▼
POST /v1/integrations/google/connect   ← 8003 (integration API)
    │
    ▼
workspace-mcp /authorize               ← 8002 (MCP server)
    │
    ▼
Google consent screen
    │
    ▼
GET /v1/integrations/google/callback   ← 8003 (terima code, tukar token)
    │
    ▼
JWT tersimpan di DB managed_agents
    │
    ▼
Agent run → GET /token → inject JWT → POST 8002/mcp ✅
```

---

## Services

| Service | URL (prod/dev tunnel) | Fungsi |
|---------|----------------------|--------|
| workspace-mcp | `https://msj90wr2-8002.asse.devtunnels.ms` | MCP server (Gmail, Calendar, Drive, dll) |
| integration-api | `https://msj90wr2-8003.asse.devtunnels.ms` | OAuth management, token storage |

---

## Step 1 — User Connect Google Account

### Request
```bash
curl -X POST https://msj90wr2-8003.asse.devtunnels.ms/v1/integrations/google/connect \
  -H "Content-Type: application/json" \
  -d '{
    "external_user_id": "user_123",
    "agent_id": "agent_abc"
  }'
```

### Response
```json
{
  "auth_url": "https://msj90wr2-8002.asse.devtunnels.ms/authorize?client_id=...&code_challenge=...&state=..."
}
```

→ Redirect user ke `auth_url` di browser → login Google → approve → callback otomatis simpan JWT ke DB.

**Note:** `external_user_id` harus sama persis dengan yang ada di `session.external_user_id` saat agent run.

---

## Step 2 — Cek Status Koneksi

```bash
curl "https://msj90wr2-8003.asse.devtunnels.ms/v1/integrations/google/status?external_user_id=user_123&agent_id=agent_abc"
```

### Response
```json
{
  "connected": true,
  "email": "user@gmail.com",
  "scopes": []
}
```

---

## Step 3 — Ambil JWT untuk Inject ke Agent

```bash
curl "https://msj90wr2-8003.asse.devtunnels.ms/v1/integrations/google/token?external_user_id=user_123&agent_id=agent_abc"
```

### Response
```json
{
  "bearer_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

- `404` → user belum connect, trigger Step 1 dulu.
- JWT **expire dalam 1 jam**. Fetch fresh token setiap agent run (jangan cache).

---

## Step 4 — Inject ke Agent Runner

Di `agent_runner.py`, sebelum `mcp_client_context()`:

```python
import httpx

async def get_google_mcp_token(external_user_id: str, agent_id: str | None) -> str | None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "http://localhost:8003/v1/integrations/google/token",
            params={"external_user_id": external_user_id, "agent_id": agent_id},
        )
    if resp.status_code == 200:
        return resp.json()["bearer_token"]
    return None  # user belum connect

# Sebelum mcp_client_context():
token = await get_google_mcp_token(session.external_user_id, agent_id)
if token:
    tools_config["mcp"]["servers"]["google_workspace"]["headers"] = {
        "Authorization": f"Bearer {token}"
    }
```

---

## Step 5 — tools_config yang Benar

```json
{
  "mcp": {
    "enabled": true,
    "servers": {
      "google_workspace": {
        "url": "https://msj90wr2-8002.asse.devtunnels.ms/mcp",
        "transport": "streamable_http",
        "headers": {
          "Authorization": "Bearer <jwt-dari-step-3>"
        }
      }
    }
  }
}
```

⚠️ **URL HARUS pakai tunnel (`https://msj90wr2-8002.asse.devtunnels.ms/mcp`), bukan `http://localhost:8002/mcp`.**

JWT audience di-set ke tunnel URL. Kalau agent hit localhost, JWT akan rejected (audience mismatch → 401).

---

## Step 6 — Test Manual Hit MCP

```bash
TOKEN=$(curl -s "http://localhost:8003/v1/integrations/google/token?external_user_id=user_123" | python3 -c "import sys,json; print(json.load(sys.stdin)['bearer_token'])")

curl -X POST https://msj90wr2-8002.asse.devtunnels.ms/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "jsonrpc": "2.0",
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "capabilities": {},
      "clientInfo": {"name": "test", "version": "1"}
    },
    "id": 1
  }'
```

Response `200` dengan JSON → token valid, MCP siap dipakai agent.

---

## Token Expiry

| Kondisi | Yang Terjadi | Solusi |
|---------|-------------|--------|
| Token belum ada | `GET /token` → 404 | Trigger OAuth flow (Step 1) |
| Token expired (>1 jam) | MCP → 401 | Fetch token baru dari `/token` (workspace-mcp auto-refresh jika refresh_token ada) |
| User disconnect | `GET /token` → 404 | Trigger OAuth flow ulang |

**Best practice:** Selalu fetch token fresh dari `GET /token` setiap agent run, jangan cache di memori.

---

## Disconnect

```bash
curl -X DELETE "https://msj90wr2-8003.asse.devtunnels.ms/v1/integrations/google/disconnect?external_user_id=user_123&agent_id=agent_abc"
```

```json
{ "disconnected": true }
```

---

## Catatan Penting

1. **`external_user_id` harus konsisten** — sama antara saat connect dan saat agent run. Jika session tidak punya `external_user_id`, token tidak akan di-fetch.
2. **`agent_id` opsional** — kalau tidak di-set saat connect, query token juga tanpa `agent_id`.
3. **JWT expire 1 jam** — jangan simpan di memori antar request.
4. **DB shared** — `managed_agents` PostgreSQL yang sama. Tabel: `google_integrations`.
