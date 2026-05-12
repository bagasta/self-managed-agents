# Subscription Tiers — Managed Agent Platform

> Dokumen ini adalah referensi desain subscription plan untuk tim backend web dan tim produk.
> Semua agent yang dibuat user mengikuti masa aktif subscription-nya.

---

## Ringkasan 3 Plan

| Fitur | Trial | Tier 1 — Starter | Tier 2 — Pro |
|-------|-------|------------------|--------------|
| **Harga** | Gratis | *(tim bisnis)* | *(tim bisnis)* |
| **Masa Aktif** | Sekali pakai (tidak perpanjang) | 30 hari | 30 hari |
| **Jumlah Agent** | 1 | 1 | 2 |
| **Model** | `gpt-4.1-mini` | `gpt-4.1-mini` | `gpt-4.1-mini` atau `deepseek/deepseek-v4-flash` |
| **WhatsApp** | ✅ (1 nomor) | ✅ (1 nomor) | ✅ (2 nomor) |
| **Sub-agent** | ❌ | Opsional | Opsional |
| **Token Quota (shared)** | ~\$1 | ~\$5 | ~\$10 (shared 2 agent) |
| **Grace Period** | 3 hari | 3 hari | 3 hari |
| **Upgrade** | → Tier 1 / Tier 2 | → Tier 2 | — |
| **Token Top-up** | ✅ | ✅ | ✅ |

---

## Trial

| Properti | Nilai |
|----------|-------|
| **Harga** | Gratis, sekali seumur hidup per user |
| **Masa Aktif** | Tidak ada tanggal — berlaku sampai quota habis |
| **Jumlah Agent** | 1 |
| **Model** | `openai/gpt-4.1-mini` |
| **WhatsApp** | ✅ (1 nomor) |
| **Sub-agent** | ❌ tidak tersedia |
| **Token Quota** | Setara \$1 usage `gpt-4.1-mini` |
| **Grace Period** | 3 hari setelah quota habis |

### Estimasi Token Quota Trial

`gpt-4.1-mini` pricing (per 1M token, per Mei 2026):
- Input: \$0.40 / 1M token
- Output: \$1.60 / 1M token
- Asumsi rasio input:output = 3:1

Estimasi \$1 budget:
```
Input  budget = $1 × 0.75 = $0.75 → 1,875,000 input tokens
Output budget = $1 × 0.25 = $0.25 →   156,250 output tokens
Total combined ≈ 2,031,250 tokens
```

> Implementasi: `token_quota = 2_000_000`

### Aturan Trial

- Hanya bisa diambil **satu kali** per user (cek via `user.has_used_trial`)
- Tidak bisa perpanjang — habis ya habis, harus upgrade ke Tier 1 atau Tier 2
- Kalau quota habis → masuk grace period 3 hari → agent nonaktif
- Sub-agent **diblokir** di level platform saat create agent (bukan hanya instruksi Arthur)

---

## Tier 1 — Starter

| Properti | Nilai |
|----------|-------|
| **Harga** | *(ditentukan tim bisnis)* |
| **Masa Aktif** | 30 hari per siklus |
| **Jumlah Agent** | 1 |
| **Model** | `openai/gpt-4.1-mini` |
| **WhatsApp** | ✅ (1 nomor) |
| **Sub-agent** | Opsional (diaktifkan manual di config agent) |
| **Token Quota** | Setara \$5 usage `gpt-4.1-mini` (shared) |
| **Grace Period** | 3 hari setelah `expires_at` |

### Estimasi Token Quota Tier 1

```
Input  budget = $5 × 0.75 = $3.75 → 9,375,000 input tokens
Output budget = $5 × 0.25 = $1.25 →   781,250 output tokens
Total combined ≈ 10,156,250 tokens
```

> Implementasi: `token_quota = 10_000_000`

---

## Tier 2 — Pro

| Properti | Nilai |
|----------|-------|
| **Harga** | *(ditentukan tim bisnis)* |
| **Masa Aktif** | 30 hari per siklus |
| **Jumlah Agent** | 2 |
| **Model** | Pilihan: `openai/gpt-4.1-mini` atau `deepseek/deepseek-v4-flash` |
| **WhatsApp** | ✅ (per agent, maks 2 nomor total) |
| **Sub-agent** | Opsional (diaktifkan manual di config agent) |
| **Token Quota** | Setara \$10 usage (shared antara semua agent) |
| **Grace Period** | 3 hari setelah `expires_at` |

### Detail Token Quota Tier 2

Token quota **shared** — total 20 juta token untuk semua agent milik user:

| Model | Token Quota Shared |
|-------|--------------------|
| `openai/gpt-4.1-mini` | 20,000,000 token |
| `deepseek/deepseek-v4-flash` | 20,000,000 token |

> `deepseek-v4-flash` jauh lebih murah, jadi \$10 sebenarnya membeli lebih banyak token.
> Untuk simplifikasi UX: samakan angka quota-nya, bukan nilai dollar-nya.
>
> Implementasi: `token_quota = 20_000_000` di level subscription (bukan per agent)

---

## Grace Period — Aturan Detail

Grace period berlaku untuk semua plan setelah kondisi "habis":

| Kondisi | Trial | Tier 1 & 2 |
|---------|-------|------------|
| Trigger | `tokens_used >= token_quota` | `now() >= expires_at` |
| Durasi | 3 hari | 3 hari |
| Selama grace period | Agent masih aktif, tidak bisa tambah pesan baru (readonly) | Agent masih aktif, tidak bisa tambah pesan baru |
| Setelah grace period | Agent nonaktif, data tetap ada | Agent nonaktif, data tetap ada |
| Reaktivasi | Upgrade ke Tier 1/2 | Renew subscription |

> **Catatan:** "Tidak bisa tambah pesan baru" artinya endpoint `POST /v1/agents/{id}/sessions/{session_id}/messages`
> mengembalikan `403 Subscription expired — please renew to continue`.

---

## Panduan Implementasi untuk Tim Backend Website

> Bagian ini adalah referensi lengkap yang bisa langsung digunakan tim backend website
> untuk membangun sistem user management dan subscription.

---

### Gambaran Relasi Antar Tabel

```
users
  │
  ├── user_subscriptions (1 aktif per user)
  │     ├── subscription_plans (referensi plan)
  │     └── token_topups (riwayat top-up)
  │
  └── [agents] ← tabel ini ada di platform Python
        owner_user_id → users.id
```

---

### Tabel `users`

Tabel utama identitas user. `external_id` adalah penghubung ke platform agent — dikirim sebagai `external_user_id` saat buat session Arthur.

```sql
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           VARCHAR(255) NOT NULL UNIQUE,
    password_hash   VARCHAR(255) NOT NULL,
    full_name       VARCHAR(255),
    external_id     VARCHAR(64) NOT NULL UNIQUE,  -- dikirim ke platform sebagai external_user_id
    has_used_trial  BOOLEAN NOT NULL DEFAULT false,
    email_verified  BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

> `external_id` bisa di-generate sebagai `usr_` + `nanoid()` atau UUID.
> Nilai ini yang dipakai Arthur untuk scope memory dan agent per user.

---

### Tabel `subscription_plans`

Data statis — di-seed sekali, tidak berubah kecuali ada plan baru.

```sql
CREATE TABLE subscription_plans (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code              VARCHAR(32) NOT NULL UNIQUE,   -- "trial", "tier_1", "tier_2", "tier_3"
    label             VARCHAR(64) NOT NULL,           -- "Trial", "Starter", "Pro", "Enterprise"
    max_agents        INTEGER NOT NULL,               -- 1, 1, 2, NULL (unlimited)
    token_quota       BIGINT NOT NULL,                -- shared quota untuk semua agent user
    period_days       INTEGER,                        -- NULL = trial (tidak ada expiry tanggal)
    grace_period_days INTEGER NOT NULL DEFAULT 3,
    allowed_models    JSONB NOT NULL DEFAULT '[]',    -- list model yang boleh dipakai
    subagents_allowed BOOLEAN NOT NULL DEFAULT false,
    wa_connect        BOOLEAN NOT NULL DEFAULT true,
    is_trial          BOOLEAN NOT NULL DEFAULT false,
    is_active         BOOLEAN NOT NULL DEFAULT true,  -- untuk disable plan lama
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Seed data:**

```sql
INSERT INTO subscription_plans (code, label, max_agents, token_quota, period_days, allowed_models, subagents_allowed, is_trial) VALUES
('trial',  'Trial',      1, 2000000,  NULL, '["openai/gpt-4.1-mini"]', false, true),
('tier_1', 'Starter',    1, 10000000, 30,   '["openai/gpt-4.1-mini"]', true,  false),
('tier_2', 'Pro',        2, 20000000, 30,   '["openai/gpt-4.1-mini","deepseek/deepseek-v4-flash"]', true, false),
('tier_3', 'Enterprise', NULL, 0,     NULL, '[]', true, false);  -- quota & models dikonfigurasi per kontrak
```

---

### Tabel `user_subscriptions`

Satu baris aktif per user. Token quota di sini adalah **shared** — semua agent milik user memakai pool yang sama, termasuk token dari sub-agent.

```sql
CREATE TABLE user_subscriptions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id),
    plan_id         UUID NOT NULL REFERENCES subscription_plans(id),
    status          VARCHAR(20) NOT NULL DEFAULT 'trial',
                    -- "trial" | "active" | "grace_period" | "expired"
    token_quota     BIGINT NOT NULL,   -- copy dari plan saat subscribe, bisa bertambah via top-up
    tokens_used     BIGINT NOT NULL DEFAULT 0,  -- dikurangi setiap LLM call (parent + sub-agent)
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,       -- NULL untuk trial
    grace_until     TIMESTAMPTZ,       -- expires_at + grace_period_days
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT one_active_sub_per_user UNIQUE (user_id)  -- hanya 1 subscription aktif
);
```

> **Catatan penting:** `tokens_used` di tabel ini adalah yang menjadi penentu quota enforcement.
> Tabel `agents` di platform Python juga punya `tokens_used` tapi itu hanya untuk statistik per-agent,
> bukan untuk enforcement.

---

### Tabel `token_topups`

Audit trail semua penambahan token. `reference_id` UNIQUE mencegah double top-up.

```sql
CREATE TABLE token_topups (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id),
    subscription_id UUID NOT NULL REFERENCES user_subscriptions(id),
    tokens_added    BIGINT NOT NULL,
    token_quota_before BIGINT NOT NULL,
    token_quota_after  BIGINT NOT NULL,
    reference_id    VARCHAR(255) NOT NULL UNIQUE,  -- ID dari payment gateway
    note            TEXT,                           -- opsional, misal "top-up manual by admin"
    topped_up_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

### Kolom yang Perlu Ditambah ke Tabel `agents` (Platform Python)

Tim backend website perlu koordinasi dengan platform Python untuk menambah satu kolom:

```sql
-- Di database platform Python (managed-agents-project)
ALTER TABLE agents ADD COLUMN owner_external_id VARCHAR(64);
CREATE INDEX ix_agents_owner_external_id ON agents(owner_external_id);
```

> `owner_external_id` diisi dari `users.external_id` saat Arthur membuat agent.
> Ini yang dipakai untuk filter `GET /v1/agents` per user.

---

### Status Machine Subscription

```
[user baru daftar]
        │
        ▼
     trial ──── quota habis ────► grace_period ──── grace_until lewat ────► expired
        │                               │                                        │
        │ upgrade/beli                  │ upgrade/beli                           │ renew
        ▼                               ▼                                        ▼
     active ──── expires_at lewat ──► grace_period ──── grace_until lewat ────► expired
        ▲                               │
        └─────── renew ─────────────────┘
```

---

### Aturan Bisnis Penting

**Saat user daftar baru:**
```
1. Buat record users
2. Buat user_subscriptions dengan plan = "trial", status = "trial"
3. Set has_used_trial = true di users
4. Kirim external_id ke platform Python untuk scope Arthur session
```

**Saat user beli/upgrade plan:**
```
1. Update user_subscriptions:
   - plan_id = plan baru
   - status = "active"
   - token_quota = plan.token_quota  (reset ke default plan)
   - tokens_used = 0  (reset)
   - started_at = now()
   - expires_at = now() + plan.period_days
   - grace_until = expires_at + plan.grace_period_days
2. Notif platform Python via POST /v1/subscriptions/{user_id}/... (jika ada endpoint renew)
```

**Saat user top-up token:**
```
1. Cek subscription status ≠ "expired"
2. Cek reference_id belum pernah dipakai (idempotency)
3. token_quota += tokens_added  (BUKAN reset, tapi tambah)
4. Jika status = "grace_period" → ubah ke "active"
5. Catat ke token_topups
6. Notif platform Python via POST /v1/subscriptions/{user_id}/topup
```

**Saat cron job harian (background job):**
```sql
-- Tandai yang masuk grace period
UPDATE user_subscriptions
SET status = 'grace_period'
WHERE status = 'active'
AND expires_at < now()
AND expires_at IS NOT NULL;

-- Tandai yang expired
UPDATE user_subscriptions
SET status = 'expired'
WHERE status = 'grace_period'
AND grace_until < now();
```

---

### Endpoint API Platform Python yang Tersedia untuk Tim Website

| Method | Endpoint | Auth | Fungsi |
|--------|----------|------|--------|
| `POST` | `/v1/auth/keys` | X-API-Key | Generate user API key |
| `POST` | `/v1/auth/keys/{id}/revoke` | X-API-Key | Cabut user API key |
| `POST` | `/v1/subscriptions/{user_id}/topup` | X-API-Key | Tambah token quota |
| `GET`  | `/v1/subscriptions/{user_id}` | X-API-Key | Cek status subscription |
| `POST` | `/v1/agents/{id}/sessions` | X-Agent-Key | Buat session Arthur (sertakan `external_user_id`) |

> Semua endpoint dengan `X-API-Key` dipanggil dari **backend website**, bukan dari browser user langsung.

---

### Aturan saat Arthur buat agent (untuk tim platform Python)

```
1. Ambil subscription aktif user dari external_user_id
2. Cek status: harus "trial", "active", atau "grace_period"
   - Kalau "expired" → tolak, minta renew
3. Hitung active_agents milik user (is_deleted = false, owner_external_id = external_user_id)
   - Kalau active_agents >= plan.max_agents → tolak, minta upgrade
4. Validasi model yang diminta ada di plan.allowed_models
   - Kalau tidak → default ke gpt-4.1-mini
5. Set agent.active_until = subscription.expires_at (atau grace_until kalau grace period)
6. Set agent.owner_external_id = external_user_id
7. Untuk trial: blokir subagents di tools_config
```

---

---

## Open Questions (Semua Sudah Dijawab)

- [x] Token quota Tier 2: **shared** antara semua agent
- [x] Grace period: **3 hari**
- [x] Trial: **1 agent, gpt-4.1-mini, \$1 quota, tanpa sub-agent**
- [x] Model Tier 2: bisa **ganti kapan saja** melalui Arthur (bukan hanya saat buat agent)
- [x] Tier 3 / Enterprise: **akan ada**, desain tabel harus extensible
- [x] Upgrade dari Trial: agent yang sudah dibuat **tetap jalan**, tidak perlu buat ulang

---

## Tier 3 — Enterprise *(placeholder, detail menyusul)*

| Properti | Nilai |
|----------|-------|
| **Harga** | Custom / kontrak |
| **Masa Aktif** | Custom |
| **Jumlah Agent** | Unlimited (atau sesuai kontrak) |
| **Model** | Semua model tersedia (termasuk GPT-4.1, Claude, Gemini, dll) |
| **WhatsApp** | ✅ unlimited nomor |
| **Sub-agent** | ✅ default aktif |
| **Token Quota** | Custom (shared, sesuai kontrak) |
| **Grace Period** | Custom (default 7 hari) |
| **Support** | Priority support |

> Detail Tier 3 akan diisi setelah ada keputusan harga dan paket dari tim bisnis.

---

## Aturan Ganti Model (Tier 2 & Enterprise)

User bisa minta Arthur untuk ganti model agent yang sudah ada kapan saja, selama model target ada di `plan.allowed_models`.

**Flow:**
```
User: "ganti model Agent X ke deepseek"
Arthur: cek plan.allowed_models user → deepseek ada → panggil update_agent(model="deepseek/deepseek-v4-flash")
```

**Aturan enforcement:**
- Arthur wajib cek `plan.allowed_models` sebelum `update_agent`
- Kalau user Tier 1 minta model selain `gpt-4.1-mini` → Arthur jelaskan perlu upgrade ke Tier 2
- Ganti model **tidak reset token quota** — hitungan tetap berjalan dari sebelumnya

---

## Token Top-Up

User bisa menambah token kapan saja tanpa harus ganti plan atau tunggu periode berikutnya.
**Pembayaran dihandle sepenuhnya oleh tim website** — platform hanya menyediakan endpoint untuk mencatat penambahan token setelah pembayaran dikonfirmasi.

### Paket Top-Up (Referensi, tim bisnis yang final)

| Paket | Token Ditambah | Estimasi Nilai |
|-------|---------------|----------------|
| Small | 2,000,000 token | ~\$1 |
| Medium | 10,000,000 token | ~\$5 |
| Large | 20,000,000 token | ~\$10 |
| Custom | bebas (input manual) | — |

> Paket ini hanya referensi. Endpoint menerima angka token bebas — tim website yang tentukan paket dan harganya.

### Aturan Token Quota — Sub-agent Ikut Dihitung

Token quota **mencakup semua usage**, termasuk sub-agent:

- Parent agent kirim pesan → token dihitung
- Parent agent panggil `task()` ke `sys_coder` / `sys_researcher` / dll → token sub-agent **juga dihitung ke quota yang sama**
- Jadi \$5 di Tier 1 adalah total budget untuk seluruh aktivitas agent + semua sub-agent yang dipakai

Contoh:
```
User minta bikin website → parent agent call sys_coder
  → sys_coder pakai 50,000 token (buat kode, iterasi, dll)
  → parent agent pakai 5,000 token (koordinasi, reply user)
  → total yang dikurangi dari quota: 55,000 token
```

---

### Aturan Top-Up

- Tersedia untuk **semua plan** termasuk Trial
- Token top-up **ditambahkan ke `token_quota`** subscription yang aktif (bukan reset)
- Top-up **tidak memperpanjang `expires_at`** — hanya nambah quota
- Top-up bisa dilakukan meski subscription dalam status `grace_period`
  - Jika dalam grace period → status otomatis kembali ke `active`
- Top-up **tidak bisa** dilakukan kalau status sudah `expired` → user harus renew dulu
- Riwayat top-up dicatat di tabel `token_topups` untuk audit dan rekonsiliasi

### Endpoint

```
POST /v1/subscriptions/{user_id}/topup
```

**Request body:**
```json
{
  "tokens": 10000000,
  "reference_id": "payment-txn-abc123"
}
```

- `tokens` — jumlah token yang ditambahkan (integer, minimum 1)
- `reference_id` — ID transaksi dari sistem pembayaran tim website (untuk audit, wajib unik)

**Response:**
```json
{
  "user_id": "uuid",
  "tokens_added": 10000000,
  "token_quota_before": 10000000,
  "token_quota_after": 20000000,
  "tokens_used": 7500000,
  "tokens_remaining": 12500000,
  "status": "active",
  "reference_id": "payment-txn-abc123",
  "topped_up_at": "2026-05-11T10:00:00Z"
}
```

**Auth:** `X-API-Key` (admin) — tim website call endpoint ini dari backend mereka setelah payment gateway konfirmasi sukses. Bukan dipanggil langsung dari browser user.

> Struktur tabel `token_topups` lengkap ada di section **"Panduan Implementasi untuk Tim Backend Website"** di atas.

### Flow Integrasi dengan Tim Website

```
User klik "Beli Token" di website
  → Checkout & payment di sistem tim website
    → Payment gateway konfirmasi sukses
      → Backend website POST /v1/subscriptions/{user_id}/topup
        → Platform tambah token_quota
          → User langsung bisa pakai token baru
```

Platform **tidak perlu tahu** soal harga, currency, atau payment method. Cukup terima konfirmasi "berapa token ditambah" dari backend website.

---

## Aturan Upgrade Subscription

Saat user upgrade (Trial → Tier 1/2, atau Tier 1 → Tier 2):

1. **Agent yang sudah ada tetap hidup** — tidak perlu buat ulang
2. `agent.active_until` di-update ke `new_subscription.expires_at`
3. Kalau upgrade membuka slot agent baru (Tier 1 → Tier 2) → user bisa langsung minta Arthur buat agent ke-2
4. Kalau upgrade membuka model baru (Tier 1 → Tier 2) → user bisa langsung minta Arthur ganti model agent yang ada
5. `tokens_used` di subscription **direset ke 0** saat upgrade (mulai periode baru)

> Detail SQL ada di section **"Panduan Implementasi untuk Tim Backend Website"** di atas.
