# Plan: Google Workspace MCP Integration

## Overview

Integrasi `google_workspace_mcp` sebagai layanan MCP terpusat di platform ini, sehingga user/agent bisa akses Gmail, Calendar, Drive, Docs, Sheets, dll — tanpa user perlu setup OAuth sendiri.

Platform owner daftar **1 Google OAuth App** sekali. User cukup klik "Connect Google Account" dari UI.

---

## Arsitektur Target

```
[Managed Agents Platform]
        |
        |-- Agent (tools_config.mcp.google_workspace)
        |        |
        |        v
        |   [google_workspace_mcp] ← 1 instance, HTTP + OAuth 2.1
        |        |
        |        v
        |   [Redis/Valkey] ← simpan token per-user (encrypted)
        |        |
        |        v
        |   [Google APIs] ← Gmail, Drive, Calendar, dll
        |
        |-- POST /v1/integrations/google/connect  ← trigger OAuth flow
        |-- GET  /v1/integrations/google/callback ← OAuth callback
        |-- GET  /v1/integrations/google/status   ← cek status koneksi user
        |-- DELETE /v1/integrations/google/disconnect
```

---

## Fase 1 — Deploy google_workspace_mcp

### 1.1 Tambah ke docker-compose.yml

```yaml
services:
  workspace-mcp:
    image: ghcr.io/taylorwilsdon/google_workspace_mcp:latest
    # atau build dari source jika perlu custom
    ports:
      - "8002:8000"
    environment:
      GOOGLE_OAUTH_CLIENT_ID: ${GOOGLE_OAUTH_CLIENT_ID}
      GOOGLE_OAUTH_CLIENT_SECRET: ${GOOGLE_OAUTH_CLIENT_SECRET}
      MCP_ENABLE_OAUTH21: "true"
      WORKSPACE_MCP_OAUTH_PROXY_STORAGE: valkey
      WORKSPACE_MCP_OAUTH_PROXY_VALKEY_URL: redis://redis:6379/2
      FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY: ${WORKSPACE_MCP_JWT_KEY}
    command: ["uv", "run", "main.py", "--transport", "streamable-http", "--tool-tier", "extended"]
    depends_on:
      - redis
    restart: unless-stopped

  redis:
    image: valkey/valkey:8-alpine
    volumes:
      - redis_data:/data
    restart: unless-stopped

volumes:
  redis_data:
```

### 1.2 Tambah env vars baru ke .env.example

```env
# Google Workspace MCP
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
WORKSPACE_MCP_URL=http://localhost:8002/mcp
WORKSPACE_MCP_JWT_KEY=   # random secret, min 32 chars
```

### 1.3 Setup Google OAuth App (dilakukan platform owner, sekali saja)

1. Buka [Google Cloud Console](https://console.cloud.google.com)
2. Buat project baru → APIs & Services → Credentials
3. Create OAuth 2.0 Client ID (type: Web Application)
4. Authorized redirect URIs:
   - `http://localhost:8002/oauth/callback` (dev)
   - `https://your-domain.com/oauth/callback` (prod)
5. Enable APIs: Gmail, Drive, Calendar, Docs, Sheets, Forms, Tasks
6. Salin Client ID & Secret ke `.env`

---

## Fase 2 — API Integration Endpoints

Tambah router baru: `app/api/integrations.py`

### Endpoints

#### `POST /v1/integrations/google/connect`
- Terima `external_user_id` (dari agent context atau request body)
- Generate OAuth authorization URL ke `workspace-mcp`
- Return URL untuk redirect user ke Google consent screen

#### `GET /v1/integrations/google/callback`
- Handle callback dari Google OAuth
- `workspace-mcp` yang handle token exchange & storage
- Redirect user ke success page / notifikasi agent

#### `GET /v1/integrations/google/status`
- Cek apakah `external_user_id` sudah connect Google
- Query ke `workspace-mcp` health/token-status endpoint
- Return: `{ connected: bool, email: string|null, scopes: string[] }`

#### `DELETE /v1/integrations/google/disconnect`
- Revoke token user dari `workspace-mcp`
- Hapus mapping dari DB lokal

### Model DB Baru: `google_integrations`

```sql
CREATE TABLE google_integrations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_user_id VARCHAR NOT NULL,
    agent_id UUID REFERENCES agents(id),
    google_email VARCHAR,           -- email Google user setelah connect
    bearer_token TEXT,              -- token untuk hit workspace-mcp
    connected_at TIMESTAMP,
    last_used_at TIMESTAMP,
    is_active BOOLEAN DEFAULT true,
    UNIQUE(external_user_id, agent_id)
);
```

### Alembic Migration

```bash
make migrate MSG="add google integrations table"
```

---

## Fase 3 — Wiring ke Agent Runner

### 3.1 Auto-inject MCP config saat agent run

Di `app/core/agent_runner.py`, tambah logic:

```python
# Jika agent punya tools_config.mcp.google_workspace
# dan external_user_id sudah connect Google
# → inject bearer_token ke MCP HTTP headers
if "google_workspace" in tools_config.get("mcp", {}):
    integration = await get_google_integration(external_user_id, agent_id)
    if integration and integration.is_active:
        mcp_config["google_workspace"]["headers"] = {
            "Authorization": f"Bearer {integration.bearer_token}"
        }
```

### 3.2 Agent config contoh

```json
{
  "tools_config": {
    "mcp": {
      "google_workspace": {
        "url": "http://workspace-mcp:8000/mcp/",
        "transport": "http"
      }
    }
  }
}
```

### 3.3 Tool yang tersedia di agent setelah connect

Dari service default `extended` tier:
- **Gmail**: search, read, send, draft, label, archive
- **Calendar**: list, create, update, delete events
- **Drive**: list, upload, download, share files
- **Docs**: read, create, edit documents
- **Sheets**: read/write cells, format, list spreadsheets
- **Tasks**: create, update, delete tasks
- **Forms**: read responses
- **Chat**: send messages ke Google Chat spaces

---

## Fase 4 — UX Flow (untuk WhatsApp/WebChat)

### Skenario: User WhatsApp ingin connect Google

1. User kirim pesan: *"connect google account"*
2. Agent deteksi intent → call tool internal `get_google_connect_url`
3. Agent reply dengan link: *"Klik link ini untuk connect: https://platform.com/connect-google?token=xxx"*
4. User klik → Google consent screen → approve
5. Callback → token tersimpan → agent dapat notifikasi via SSE event bus
6. Agent konfirmasi: *"Google account kamu sudah terhubung! Sekarang aku bisa akses Gmail, Calendar, dll."*

### Tool baru untuk agent: `connect_google_account`

Di `app/core/tools/google_integration_tools.py`:
- `connect_google_account` → return OAuth URL
- `check_google_connection` → cek status koneksi
- `disconnect_google_account` → revoke akses

Tool ini otomatis tersedia jika `tools_config.google_integration: true`.

---

## Fase 5 — Security & Edge Cases

### Token Security
- Bearer token disimpan terenkripsi di Redis (Fernet, by workspace-mcp)
- Token di DB lokal (`google_integrations`) juga di-encrypt sebelum simpan
- Pakai env `DATABASE_ENCRYPTION_KEY` (tambah ke env vars)

### Token Expiry
- Google OAuth tokens expire. `workspace-mcp` handle refresh otomatis jika `offline_access` scope di-request
- Jika refresh gagal → set `is_active=false`, notifikasi user via agent

### Scope Management
- Default scope: Gmail read+send, Calendar, Drive read, Docs read+write, Sheets read+write
- User bisa lihat scope yang di-approve saat consent screen
- Tidak minta scope lebih dari yang dibutuhkan (principle of least privilege)

### Multi-agent per User
- 1 `external_user_id` bisa pakai koneksi Google yang sama di banyak agent
- Token di-share via `external_user_id` key, bukan per-agent

---

## Checklist Implementasi

### Fase 1 — Infra
- [ ] Tambah `workspace-mcp` service ke `docker-compose.yml`
- [ ] Tambah `redis` service ke `docker-compose.yml`
- [ ] Update `.env.example` dengan env vars baru
- [ ] Daftar Google OAuth App di Google Cloud Console
- [ ] Test `workspace-mcp` berjalan standalone

### Fase 2 — API
- [ ] Buat `app/api/integrations.py` dengan 4 endpoints
- [ ] Buat model `GoogleIntegration` di `app/models/`
- [ ] Buat `alembic` migration untuk `google_integrations` table
- [ ] Register router di `app/main.py`
- [ ] Test OAuth flow end-to-end

### Fase 3 — Agent Runner
- [ ] Update `agent_runner.py` untuk inject bearer token ke MCP config
- [ ] Buat `app/core/tools/google_integration_tools.py`
- [ ] Daftarkan tools baru ke tool loader
- [ ] Update `tools_config` schema docs

### Fase 4 — Testing
- [ ] Test manual via Postman: connect → callback → agent run
- [ ] Test agent bisa kirim email via Gmail tool
- [ ] Test agent bisa buat event Calendar
- [ ] Test token refresh flow
- [ ] Test disconnect → agent gracefully handle missing token

---

## Estimasi Effort

| Fase | Effort | Prioritas |
|------|--------|-----------|
| Fase 1 — Deploy infra | ~2 jam | P0 |
| Fase 2 — API endpoints | ~4 jam | P0 |
| Fase 3 — Agent Runner wiring | ~3 jam | P0 |
| Fase 4 — UX tools untuk agent | ~2 jam | P1 |
| Fase 5 — Security hardening | ~2 jam | P1 |
| **Total** | **~13 jam** | |

---

## Referensi

- [google_workspace_mcp repo](https://github.com/taylorwilsdon/google_workspace_mcp)
- [OAuth 2.1 docs (repo)](https://github.com/taylorwilsdon/google_workspace_mcp#oauth-21-support)
- [FastMCP OAuth Proxy Storage](https://github.com/taylorwilsdon/google_workspace_mcp#oauth-proxy-storage-backends)
- [Google Cloud Console](https://console.cloud.google.com)
