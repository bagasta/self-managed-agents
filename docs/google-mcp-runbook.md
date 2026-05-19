# Google MCP Runbook (QA / Onboarding)

Dokumen ini adalah panduan operasional ringkas untuk tim saat:
- melakukan re-auth Google,
- menjalankan smoke test live,
- dan troubleshooting cepat saat ada error MCP.

---

## 1) Prasyarat

Pastikan service ini aktif:
- Integration API: `http://localhost:8003`
- Google MCP server: `http://localhost:8002/mcp`
- Managed agents API (jika perlu end-to-end WA flow)

Pastikan virtualenv project tersedia:
- `/home/bagas/managed-agents-project/.venv/bin/python`

---

## 2) Command Utama (Paling Sering Dipakai)

### A. Lihat langkah onboarding tester
```bash
make mcp-smoke-live-onboard
```

### B. Generate link re-auth Google fresh
```bash
make mcp-smoke-live-reauth
```

### C. Jalankan live smoke suite (safe mode)
```bash
make mcp-smoke-live
```

### D. Jalankan live smoke suite strict mode
```bash
make mcp-smoke-live-strict
```

---

## 3) Alur Standar Buat QA

1. Jalankan:
   ```bash
   make mcp-smoke-live-reauth
   ```
2. Buka `auth_url` hasil command, selesaikan consent Google.
3. Jalankan:
   ```bash
   make mcp-smoke-live
   ```
4. Pastikan hasil test lulus.

Jika butuh gating lebih ketat (opsional service tidak boleh skip), jalankan strict:
```bash
make mcp-smoke-live-strict
```

---

## 4) Override Target User / Agent

Kalau mau test user/agent lain:

```bash
GOOGLE_MCP_EXTERNAL_USER_ID=<external_user_id> \
GOOGLE_MCP_AGENT_ID=<agent_id> \
make mcp-smoke-live
```

Override endpoint jika environment beda:

```bash
GOOGLE_MCP_INTEGRATION_URL=http://localhost:8003 \
GOOGLE_MCP_URL=http://localhost:8002/mcp \
GOOGLE_MCP_EXTERNAL_USER_ID=<external_user_id> \
GOOGLE_MCP_AGENT_ID=<agent_id> \
make mcp-smoke-live
```

---

## 5) Jalankan Tanpa Makefile (Direct Pytest)

Safe mode:
```bash
RUN_GOOGLE_MCP_LIVE_SMOKE=true \
GOOGLE_MCP_INTEGRATION_URL=http://localhost:8003 \
GOOGLE_MCP_URL=http://localhost:8002/mcp \
GOOGLE_MCP_EXTERNAL_USER_ID=62895619356936 \
GOOGLE_MCP_AGENT_ID=46ed1c39-c343-4d42-a5ff-2559f43efa0e \
/home/bagas/managed-agents-project/.venv/bin/python -m pytest -q tests/test_google_mcp_live_smoke.py
```

Strict mode:
```bash
RUN_GOOGLE_MCP_LIVE_SMOKE=true \
GOOGLE_MCP_LIVE_SMOKE_STRICT=true \
GOOGLE_MCP_INTEGRATION_URL=http://localhost:8003 \
GOOGLE_MCP_URL=http://localhost:8002/mcp \
GOOGLE_MCP_EXTERNAL_USER_ID=62895619356936 \
GOOGLE_MCP_AGENT_ID=46ed1c39-c343-4d42-a5ff-2559f43efa0e \
/home/bagas/managed-agents-project/.venv/bin/python -m pytest -q tests/test_google_mcp_live_smoke.py
```

---

## 6) Cakupan Smoke Suite

Smoke suite live saat ini mencakup operasi aman/non-destruktif:
- Sheets: create + edit values
- Slides: create + batch update
- Docs: create + modify text
- Drive: create + update metadata
- Calendar: create + update event
- Gmail: draft only (tidak kirim email)
- Tasks: create list + create task
- Forms: create + batch update
- Contacts: create + update

---

## 7) Troubleshooting Cepat

### Kasus: `Auth link not found or expired`
Penyebab: short-link disimpan in-memory dan punya TTL.
Solusi: generate ulang link.
```bash
make mcp-smoke-live-reauth
```

### Kasus: `504 Gateway Timeout` ke URL tunnel MCP
Solusi:
- pastikan runtime MCP call pakai localhost (`WORKSPACE_MCP_RUNTIME_URL=http://localhost:8002/mcp`)
- restart service managed-agents agar env terbaru terbaca.

### Kasus: `insufficient authentication scopes`
Solusi:
- jalankan re-auth lagi (`make mcp-smoke-live-reauth`)
- pastikan user klik consent sampai selesai.

### Kasus: Forms / Contacts error API disabled
Solusi:
- enable API di Google Cloud project OAuth client:
  - Google Forms API
  - People API
- lalu re-auth ulang.

### Kasus: Tool error karena argumen salah
Contoh yang sering:
- `modify_sheet_values` pakai `range_name` (bukan `range`)
- `draft_gmail_message.to` berupa string tunggal
- `manage_event` update waktu sertakan `start_time` + `end_time`

---

## 8) Safety Rules

- Jangan jalankan operasi destructive untuk smoke test.
- Jangan kirim email beneran saat validasi: pakai draft (`draft_gmail_message`).
- Hindari delete operation untuk data user.
- Gunakan nama resource bertanda `Smoke`/`Test` agar mudah dikenali.

---

## 9) Referensi Teknis

- Live smoke test suite:
  - `tests/test_google_mcp_live_smoke.py`
- Re-auth helper script:
  - `scripts/generate_google_mcp_reauth_link.py`
- Makefile targets:
  - `Makefile`
- Recap perubahan lengkap:
  - `docs/recap.md`
