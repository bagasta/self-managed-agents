# 08 — Agent Builder (Platform Meta-Agent)

> **Status:** 🟡 Planning  
> **Tanggal:** 2026-04-28  
> **Prioritas:** High — Pintu masuk utama platform sebagai layanan SaaS berbasis WhatsApp

---

## Latar Belakang & Motivasi

Saat ini, membuat agent baru di platform ini membutuhkan developer untuk:
- Menulis system prompt secara manual
- Memahami format `tools_config` (JSONB)
- Mengisi data via API atau langsung ke database
- Mengetahui daftar skill, MCP server, dan channel yang tersedia

Ini menciptakan bottleneck: setiap agent baru bergantung pada developer, bukan pada operator bisnis.

**Solusi:** Sebuah **Agent Builder** — agent khusus (meta-agent) yang bertugas memandu **siapapun** (bukan hanya developer) melalui proses pembuatan dan konfigurasi agent baru via percakapan WhatsApp biasa. Mirip konsep yang diterapkan oleh **Claude Managed Agents** (Anthropic): infrastruktur dikelola platform, pengguna cukup mendeskripsikan kebutuhan mereka dalam bahasa natural.

> **Model Bisnis:** Agent Builder adalah **pintu masuk utama** platform ini sebagai layanan SaaS. User biasa (pemilik bisnis, UMKM, operator) bisa mendaftar dan membuat AI agent mereka sendiri hanya dengan chat WhatsApp — tanpa coding, tanpa form web, tanpa developer.

---

## Visi & Tujuan

```
User Baru (chat WA pertama kali)
    → Agent Builder (identifikasi nomor WA = identitas user)
        → Registrasi akun (auto-create tenant)
            → Tanya kebutuhan agent
                → Buat & deploy agent
                    → Agent siap digunakan oleh user tersebut
```

Agent Builder berperan sebagai **"Admin Panel yang bisa diajak ngobrol"**. User cukup menjelaskan kebutuhan dalam bahasa natural, dan Agent Builder akan:
1. Mendaftarkan user baru (menggunakan nomor WA sebagai identitas unik)
2. Menggali kebutuhan secara interaktif
3. Meracik konfigurasi agent yang optimal sesuai tier/paket user
4. Menyimpan ke database (di bawah tenant user tersebut)
5. Menginformasikan cara menghubungkan agent ke channel WA milik user

---

## Prinsip Desain

| Prinsip | Detail |
|---------|--------|
| **Zero Architectural Shift** | Tidak mengubah arsitektur inti. Agent Builder adalah agent biasa dengan tools tambahan. |
| **Platform-Aware** | System prompt Agent Builder berisi "Platform Rulebook" — pengetahuan mendalam tentang kapabilitas, batasan, dan best practices platform ini. |
| **Multi-Tenant by Design** | Setiap user yang chat ke Agent Builder memiliki "tenant" sendiri. Agent yang dibuat terisolasi per nomor WA (owner). |
| **Tier-Aware** | Agent Builder tahu paket apa yang dimiliki user (Free/Pro/Business) dan membatasi fitur yang bisa dikonfigurasi sesuai paket. |
| **Open Registration** | Siapapun bisa chat ke nomor WA Agent Builder untuk mendaftar dan membuat agent. Tidak ada `allowed_senders` restrictor. |
| **Dogfooding** | Agent Builder menggunakan `agent_runner.py` yang sama, membuktikan maturity sistem. |

---

## Komponen yang Dibutuhkan

### 1. Database: Flag `is_system_agent` pada Model `Agent`

**File:** `app/models/agent.py` & `app/schemas/agent.py`

Tambahkan flag untuk membedakan agent sistem dari agent biasa:
```python
# app/models/agent.py
is_system_agent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
```

Flag ini digunakan oleh `agent_runner.py` untuk mengizinkan pemuatan "System Tools" yang tidak tersedia untuk agent biasa.

> **Keamanan:** Hanya agent dengan `is_system_agent = True` yang dapat memuat `builder_tools`. Ini mencegah agent biasa memanipulasi platform.

---

### 2. New Tool Group: `builder_tools.py`

**File:** `app/core/tools/builder_tools.py`  
**Pemuat:** `app/core/tool_builder.py` → fungsi baru `build_builder_tools()`

Kumpulan tools yang hanya tersedia untuk Agent Builder:

#### a. `ListAvailableSkillsTool`
Membaca database untuk mengembalikan daftar skill/tool aktif di platform.
```python
# Output contoh:
{
  "native_tools": ["memory", "skills", "escalation", "scheduler", "http", "mcp", "sandbox"],
  "custom_tools_count": 3,
  "mcp_servers": ["notion", "google-calendar", "custom-crm"]
}
```

#### b. `ListAvailableChannelsTool`
Membaca device/channel WA yang aktif dan belum di-assign ke agent lain.
```python
# Output contoh:
{
  "available_devices": [
    {"device_id": "wadev_abc123", "type": "dev", "phone": "+6281xxx"},
    {"device_id": "wa_xyz789", "type": "prod", "status": "available"}
  ]
}
```

#### c. `ValidateSystemPromptTool`
Memvalidasi draft system prompt sebelum disimpan:
- Cek panjang token (batas konteks model)
- Cek apakah ada instruksi yang bertentangan dengan platform rules
- Memberi skor kualitas dan saran perbaikan

#### d. `CreateAgentTool`
Eksekusi INSERT ke tabel `agents`:
```python
# Parameter
{
  "name": str,
  "role": str,
  "system_prompt": str,
  "tools_config": dict,          # {"memory": true, "scheduler": false, ...}
  "allowed_senders": list[str],  # nomor WA yang diizinkan
  "model": str,                  # default: model platform
  "channel_device_id": str       # optional: auto-assign ke device WA
}
```

#### e. `UpdateAgentTool`
Modifikasi agent yang sudah ada (revisi prompt, update tools).

#### f. `GetAgentDetailTool`
Membaca konfigurasi agent yang sudah ada untuk keperluan debugging atau revisi.

---

### 3. Platform Rulebook (System Prompt Agent Builder)

**File:** `system-message-builder.md` (di root project, setara `system-message-Arthur.md`)

Ini adalah komponen **terpenting**. System prompt Agent Builder harus berisi:

#### a. Identitas & Misi
```
Kamu adalah Agent Builder untuk Platform Managed Agents.
Tugasmu adalah membantu user (operator bisnis) membuat dan mengonfigurasi
AI agent yang efektif di platform ini melalui percakapan.
```

#### b. Kapabilitas Platform (Platform Awareness)
- Channel yang didukung: WhatsApp (via `wa-service` & `wa-dev-service`)
- Input yang didukung: Teks, Voice Note (audio PTT — otomatis ditranskrip)
- Fitur bawaan: Memory/History, Escalation ke operator manusia, Scheduler
- Optional tools yang bisa diaktifkan: HTTP tool, MCP servers, Custom tool creator, Sandbox

#### c. Best Practices Prompting untuk Platform Ini
```
RULES — wajib diterapkan di semua agent yang kamu buat:
1. JANGAN gunakan markdown (*bold*, # heading, dll) — WhatsApp tidak render markdown standar
2. SELALU sertakan instruksi eskalasi: kapan agent harus memanggil human operator
3. Tentukan bahasa respons secara eksplisit (Indonesia/Inggris/mixed)
4. Batasi panjang pesan 1–3 paragraf per respons — user WA tidak suka wall of text
5. Sertakan contoh percakapan di system prompt untuk few-shot learning
```

#### d. Batasan Platform (Limitations)
```
Yang BELUM bisa dilakukan platform ini saat ini:
- Mengirim gambar, video, atau dokumen (hanya menerima)
- Mengirim pesan ke banyak penerima sekaligus (broadcast)
- Scheduling pesan otomatis tanpa tool scheduler diaktifkan
- Integrasi email langsung (perlu MCP server tambahan)
```

#### e. Alur Kerja Agent Builder
```
1. Tanya kebutuhan → 2. Gali use case → 3. Buat draft konfigurasi →
4. Presentasikan ke user → 5. Revisi jika perlu → 6. Simpan ke DB →
7. Assign ke channel dev → 8. Panduan testing
```

---

### 4. Integrasi ke `agent_runner.py` & `tool_builder.py`

**File:** `app/core/agent_runner.py`  
**Perubahan:** Tambah kondisi untuk memuat builder tools jika `agent.is_system_agent == True`

```python
# Di agent_runner.py, setelah blok tool loading yang ada:
if agent_model.is_system_agent:
    from app.core.tools.builder_tools import build_builder_tools
    tools.extend(build_builder_tools(db))
    active_groups.append("builder")
```

**File:** `app/core/tool_builder.py`  
**Perubahan:** Tambah fungsi `build_builder_tools(db)`.

---

### 5. Channel & Keamanan

**Konfigurasi Agent Builder:**
```json
{
  "name": "Agent Builder",
  "is_system_agent": true,
  "allowed_senders": null,
  "tools_config": {
    "memory": true,
    "builder": true,
    "escalation": true,
    "scheduler": false
  }
}
```

- **`allowed_senders: null`** — Agent Builder terbuka untuk siapapun. Keamanan dijamin di level tool (setiap user hanya bisa memanipulasi agent miliknya sendiri, teridentifikasi dari nomor WA pengirim).
- **Channel Dev:** Hubungkan ke `wa-dev-service` untuk testing awal
- **Channel Prod:** Nomor WA resmi/dedicated untuk Agent Builder platform (bisa nomor bisnis WA API)
- **Isolasi Tenant:** Di dalam setiap `builder_tool`, selalu filter query DB berdasarkan `owner_phone = from_phone` untuk mencegah user mengakses/memodifikasi agent milik user lain.

---

## Alur Implementasi (Step-by-Step)

### Phase 1: Fondasi Database (Est. 1–2 jam)
- [x] Tambah kolom `is_system_agent` di `app/models/agent.py`
- [x] Update `app/schemas/agent.py` (Pydantic schema)
- [x] Buat Alembic migration
- [x] Buat record Agent Builder di database via seed script atau API
*(Selesai: 2026-04-28)*

### Phase 2: Platform Rulebook (Est. 2–3 jam)
- [x] Audit seluruh kapabilitas platform (baca semua file tools yang ada)
- [x] Tulis `system-message-builder.md` dengan format yang sudah dicontohkan di atas
- [x] Review dan iterasi — ini adalah komponen paling kritis untuk kualitas output
*(Selesai: 2026-04-28 — 28/28 TDD tests passed)*

### Phase 3: Builder Tools (Est. 3–4 jam)
- [x] Buat `app/core/tools/builder_tools.py` dengan semua tool di atas
- [x] Tambah `build_builder_tools()` di `app/core/tool_builder.py`
- [x] Integrasikan ke `agent_runner.py` dengan flag `is_system_agent`
- [x] Unit test: `tests/test_builder_tools.py`
*(Selesai: 2026-04-28 — 30/30 TDD tests passed)*

### Phase 4: Testing & Iterasi (Est. 2–3 jam)
- [x] Seed script `scripts/seed_arthur.py` — setup/update Arthur di DB (dry-run tested)
- [x] Integration tests: full pipeline validate→create→list→get→update, tenant isolation, WA best practices enforcement
- [ ] Chat langsung dengan Agent Builder via `wa-service` (manual, butuh WA device aktif)
- [ ] Minta dia membuat 2–3 agent contoh dari berbagai use case
- [ ] Validasi kualitas system prompt yang dihasilkan
- [ ] Perbaiki Platform Rulebook berdasarkan hasil observasi
*(TDD selesai: 2026-04-28 — 26/26 tests passed, 116 total)*

---

## Risiko & Mitigasi

| Risiko | Dampak | Mitigasi |
|--------|--------|----------|
| User A bisa memodifikasi agent milik User B | Tinggi | Setiap builder tool wajib filter berdasarkan `owner_phone`. Validasi di service layer, bukan hanya di Agent Builder. |
| User membuat banyak agent melebihi kuota | Sedang | `CreateAgentTool` cek limit berdasarkan tier sebelum INSERT. Kembalikan pesan upgrade jika kuota habis. |
| System prompt Agent Builder terlalu panjang (context window overflow) | Sedang | Buat Rulebook yang ringkas, gunakan model dengan context window besar |
| Agent Builder salah meracik `tools_config` | Sedang | `ValidateSystemPromptTool` + default yang aman (tools mati kecuali diizinkan) |
| Spam registrasi dari nomor random | Sedang | Rate limiting per nomor WA di `channels.py`. Tambah CAPTCHA step (misal: user harus kirim kode OTP) di flow onboarding. |
| Biaya LLM Agent Builder meledak karena banyak user | Tinggi | Lihat plan monetisasi (`09-monetization.md`) — biaya LLM adalah komponen utama pricing tier. |

---

## Ekspansi di Masa Depan

Setelah Agent Builder stabil, bisa dikembangkan menjadi:

1. **Agent Evaluator** — Agent yang menguji agent lain secara otomatis dengan skenario dummy
2. **Bulk Onboarding** — Builder menerima file CSV/JSON berisi spesifikasi banyak agent, membuat semuanya sekaligus
3. **Template Library** — Builder menyimpan template agent yang pernah berhasil dan bisa di-clone untuk use case serupa
4. **Self-Improving** — Builder membaca log percakapan agent yang sudah berjalan, mengidentifikasi kelemahan prompt, dan menyarankan perbaikan

---

## Referensi

- `app/core/agent_runner.py` — mekanisme tool loading yang akan digunakan Agent Builder
- `app/core/tool_builder.py` — tempat registrasi semua tool group
- `app/models/agent.py` — model yang perlu ditambah flag
- `app/core/tools/scheduler_tool.py` — contoh struktur tool yang baik untuk diikuti
- `system-message-Arthur.md` — referensi format Platform Rulebook yang akan dibuat
- `docs/recap.md` — context bug yang sudah diselesaikan (LID, VN Transcription)
