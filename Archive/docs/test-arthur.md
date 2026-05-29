# Test Arthur — Agent Builder

Dokumen ini berisi langkah-langkah standar untuk menguji Arthur (AI Agent Builder) secara end-to-end.

---

## Prerequisites

- API server running: `make dev` (port 8000)
- Arthur sudah ada di DB dan punya soul ter-seed (`scripts/seed_arthur.py`)
- `DOCKER_SANDBOX_IMAGE=managed-agents-sandbox:latest` di `.env` (untuk agent dengan sandbox)
- Sandbox image sudah di-build: `DOCKER_HOST=unix:///run/docker.sock docker build -f sandbox.Dockerfile -t managed-agents-sandbox:latest .`

---

## Langkah Test

### 1. Ambil Arthur ID & API Key

```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/v1/agents \
  | python3 -c "import sys,json; [print(a['id'], a['name'], a.get('api_key','')) for a in json.load(sys.stdin)['items'] if 'rthur' in a['name']]"
```

Pastikan:
- Arthur ada di list
- Punya `api_key`
- `tools_config` mengandung: `builder`, `http`, `sandbox`, `memory`, `scheduler`

### 2. Cek Soul Arthur

```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/v1/agents/{ARTHUR_ID}/memory \
  | python3 -c "import sys,json; mems=json.load(sys.stdin); [print(m['key'], m['value_data'][:100]) for m in mems if m['key']=='soul']"
```

Soul harus ada dengan scope `null`. Jika tidak ada, jalankan `python scripts/seed_arthur.py`.

### 3. Buat Session Arthur

```
POST /v1/agents/{ARTHUR_ID}/sessions
Headers: X-API-Key: ...
Body: { "external_user_id": "tester" }
```

Simpan `session_id` dari response.

### 4. Kirim Perintah Buat Agent (Test Utama)

```
POST /v1/agents/{ARTHUR_ID}/sessions/{SESSION_ID}/messages
Headers: X-API-Key: ..., X-Agent-Key: {ARTHUR_API_KEY}
Body: { "message": "<perintah natural>" }
```

**Timeout minimal 480 detik** — Arthur melakukan beberapa tool call (plan → validate → create → seed soul → update memory sendiri).

Contoh perintah natural yang sudah diuji:

| Perintah | Agent yang Dibuat | Kompleksitas |
|----------|-------------------|--------------|
| "Buatkan agent untuk bikin prototype website dan deploy via Cloudflare Tunnel" | WebBuilder | Medium |
| "Buatkan CS untuk toko fashion, eskalasi pembelian ke +6281234567890" | CS Toko Bagas | Medium |
| "Bikin agent yang bisa generate PDF, Excel, CSV, Word dan kirim filenya ke user" | DocGen | Medium |
| "Gue pengen pantau harga BTC, ETH, BBCA tiap hari, notif kalau turun >5%, laporan mingguan Excel" | MarketBot | Hard |

### 5. Verifikasi Agent Berhasil Dibuat

Cek di list agents:
```bash
curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/v1/agents \
  | python3 -c "import sys,json; [print(a['id'], a['name'], list((a.get('tools_config') or {}).keys())) for a in json.load(sys.stdin)['items']]"
```

Pastikan:
- [ ] Agent baru muncul dengan nama yang diminta
- [ ] `tools_config` sesuai dengan kebutuhan
- [ ] Soul ter-seed (cek `/v1/agents/{NEW_ID}/memory` → key `soul` ada)

### 6. Test Agent Baru

Buat session untuk agent baru, kirim pesan test, verifikasi perilaku sesuai instructions.

**WebBuilder** — test auto-deploy:
```
"Buatkan landing page untuk startup AI bernama NeuralWave"
```
Expected: agent generate HTML, deploy via Cloudflare Tunnel, tampilkan link aktif **tanpa diminta deploy**.

Update test:
```
"Tambahin section testimonial dan ganti warna CTA button jadi biru neon"
```
Expected: update file + redeploy di URL yang sama.

**CS Toko Bagas** — test eskalasi:
```
"Mau beli 2 pcs kaos, gimana cara bayarnya?"
```
Expected: agent memanggil `escalate_to_human`, muncul message role `escalation` di history, agent balas user bahwa diteruskan ke tim.

**DocGen** — test generate file:
```
"Buatkan file Excel inventaris 10 produk dan PDF laporan penjualan"
```
Expected: agent `execute` Python script di sandbox, file `.xlsx` dan `.pdf` terbuat di `/workspace/`.

**MarketBot** — test data live + scheduler:
```
"Cek harga Bitcoin sekarang"
```
Expected: agent `http_get` ke CoinGecko, return harga live dalam IDR.

```
"Aktifin alert tiap jam kalau BTC turun >5%"
```
Expected: agent `set_reminder` dengan cron `0 * * * *`, simpan baseline via `remember`.

---

## Checklist Pass/Fail

| Item | Expected | Pass? |
|------|----------|-------|
| Arthur reply dalam bahasa natural, tanya klarifikasi jika perlu | Ya | |
| Agent baru ter-create di DB | Ya | |
| Soul ter-seed via `http_post` ke memory API | Ya | |
| Arthur tulis `update_daily` setelah create agent | Ya | |
| Agent baru punya layered memory (soul inject di system prompt) | Ya | |
| WebBuilder auto-deploy tanpa diminta | Ya | |
| WebBuilder bisa update + redeploy | Ya | |
| CS eskalasi memanggil tool `escalate_to_human` (bukan hanya bilang teks) | Ya | |
| DocGen generate Excel/PDF via openpyxl + reportlab di sandbox | Ya | |
| MarketBot fetch harga live dari CoinGecko | Ya | |
| MarketBot set scheduler tiap jam + simpan baseline ke memory | Ya | |

---

## Bug yang Ditemukan Saat Test (Log)

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| `MultipleResultsFound` saat load soul | Arthur punya 3 duplikat soul record | `get_memory()` ganti `.scalar_one_or_none()` → `.scalars().first()` |
| DocGen tidak bisa generate Excel/PDF | `.env` pakai `DOCKER_SANDBOX_IMAGE=python:3.12` (image plain) | Buat `sandbox.Dockerfile` dengan openpyxl/reportlab/fpdf2/python-docx, update `.env` ke `managed-agents-sandbox:latest` |
| Agent timeout di client | Banyak tool calls, 180s tidak cukup | Gunakan timeout minimal 480s untuk request ke Arthur |

---

## Catatan Arsitektur

- Arthur **tidak bisa** langsung write memory ke agent lain via `remember()` — tool itu scoped ke `agent_id` Arthur sendiri.  
  Arthur harus pakai `http_post("/v1/agents/{new_agent_id}/memory", {"key": "soul", "value": "..."})` untuk seed soul agent baru.
- Sandbox image default di `.env` override `config.py` — selalu cek `.env` jika sandbox tidak bisa import library.
- Request ke Arthur harus pakai 2 headers: `X-API-Key` (platform key) + `X-Agent-Key` (Arthur's own api_key).
