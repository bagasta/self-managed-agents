# 09 — Monetization: Platform sebagai Layanan Berbayar (SaaS)

> **Status:** 🟡 Planning  
> **Tanggal:** 2026-04-28  
> **Prioritas:** High — Fondasi bisnis jangka panjang platform ini

---

## Konsep Utama

Platform ini akan beroperasi sebagai **SaaS berbasis WhatsApp** — model yang unik karena seluruh interaksi (onboarding, konfigurasi, penggunaan agent) dilakukan via WhatsApp, bukan via web dashboard. Pintu masuk utamanya adalah **Agent Builder** (`08-agent-builder.md`).

```
User Baru → Chat WA ke Agent Builder → Daftar → Pilih Paket → Buat Agent → Pakai
```

Target segmen: **UMKM, operator bisnis, startup Indonesia** yang ingin AI agent untuk WhatsApp tanpa harus coding.

---

## Struktur Tier / Paket Langganan

### Tier 1: Free (Gratis — Percobaan)
Untuk mengurangi friction onboarding dan membiarkan user merasakan value sebelum bayar.

| Fitur | Limit |
|-------|-------|
| Jumlah agent | 1 agent |
| Pesan per bulan | 200 pesan |
| Memory/history | 10 turns |
| Akses tools | Memory saja |
| Model LLM | Model hemat (misal: GPT-4o Mini) |
| Channel WA | Pakai nomor dev platform (shared) |
| Support | Self-service via Agent Builder |
| Watermark | ✅ "Powered by Clevio AI Staff" di akhir pesan |

### Tier 2: Starter (Rp 99.000 / bulan)
Untuk UMKM kecil dengan 1 use case spesifik.

| Fitur | Limit |
|-------|-------|
| Jumlah agent | 2 agent |
| Pesan per bulan | 2.000 pesan |
| Memory/history | 30 turns |
| Akses tools | Memory, Escalation, HTTP Tool |
| Model LLM | GPT-4o Mini |
| Channel WA | Nomor WA sendiri (sambungkan via wa-service) |
| Support | Via WA (response 1x24 jam) |
| Watermark | ❌ Tidak ada |

### Tier 3: Pro (Rp 299.000 / bulan)
Untuk bisnis dengan kebutuhan lebih kompleks.

| Fitur | Limit |
|-------|-------|
| Jumlah agent | 5 agent |
| Pesan per bulan | 10.000 pesan |
| Memory/history | 100 turns |
| Akses tools | Memory, Escalation, HTTP, Scheduler, MCP |
| Model LLM | GPT-4o / Claude Sonnet (pilihan) |
| Voice Note | ✅ Transkripsi otomatis |
| Channel WA | Hingga 3 nomor WA sendiri |
| Skill Library | ✅ Akses custom skills |
| Support | Priority via WA (response 4 jam) |

### Tier 4: Business (Rp 799.000 / bulan)
Untuk bisnis menengah dengan banyak agent dan volume tinggi.

| Fitur | Limit |
|-------|-------|
| Jumlah agent | 20 agent |
| Pesan per bulan | 50.000 pesan |
| Memory/history | Unlimited |
| Akses tools | Semua tools (termasuk Sandbox, Tool Creator) |
| Model LLM | Pilihan bebas (GPT-4o, Claude, Gemini) |
| Voice Note | ✅ |
| Channel WA | Unlimited nomor |
| MCP Servers | Custom MCP server sendiri |
| White-label | ✅ Nama agent bisa dikustom sepenuhnya |
| Laporan | Ringkasan percakapan & analytics via WA |
| Support | Dedicated support WA group |

### Tier 5: Enterprise (Custom Pricing)
Untuk perusahaan besar dengan kebutuhan khusus.
- On-premise deployment
- SLA tertulis
- Integrasi custom ke sistem internal (ERP, CRM)
- Dedicated instance (tidak shared)
- Training & onboarding tim

---

## Arsitektur Teknis untuk Monetisasi

### 1. Multi-Tenancy: Model `User` / `Organization`

**File baru yang perlu dibuat:** `app/models/user.py`

Saat ini platform tidak memiliki konsep "pemilik" agent. Kita perlu menambahkan entitas `User` yang terhubung ke setiap agent.

```python
# app/models/user.py
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    phone_number: Mapped[str] = mapped_column(String, unique=True, nullable=False)  # nomor WA = identitas unik
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)

    # Langganan
    tier: Mapped[str] = mapped_column(String, default="free")  # free | starter | pro | business | enterprise
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    subscription_id: Mapped[str | None] = mapped_column(String, nullable=True)  # ID dari payment gateway

    # Kuota & Usage
    monthly_message_limit: Mapped[int] = mapped_column(Integer, default=200)
    messages_used_this_month: Mapped[int] = mapped_column(Integer, default=0)
    usage_reset_at: Mapped[datetime] = mapped_column(DateTime)  # tanggal reset tiap bulan

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relasi
    agents: Mapped[list["Agent"]] = relationship("Agent", back_populates="owner")
```

### 2. Update Model `Agent`: Tambah `owner_id`

```python
# app/models/agent.py — tambahan kolom
owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
owner: Mapped["User | None"] = relationship("User", back_populates="agents")
is_system_agent: Mapped[bool] = mapped_column(Boolean, default=False)
```

Agent dengan `is_system_agent = True` (seperti Agent Builder) tidak memiliki `owner_id`.

### 3. Usage Tracking: `UsageLog`

Setiap pesan yang diproses harus dicatat untuk keperluan billing & analytics.

```python
# app/models/usage_log.py
class UsageLog(Base):
    __tablename__ = "usage_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"))
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    message_count: Mapped[int] = mapped_column(Integer, default=1)
    llm_tokens_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_calls: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

### 4. Quota Enforcement di `agent_runner.py` / `channels.py`

Sebelum menjalankan agent, cek apakah user masih punya kuota:

```python
# Di app/api/channels.py, sebelum memanggil run_agent()
async def _check_user_quota(user: User, db: AsyncSession) -> bool:
    if user.messages_used_this_month >= user.monthly_message_limit:
        return False  # Trigger pesan "kuota habis, upgrade paket"
    return True
```

### 5. Payment Gateway Integration

**Rekomendasi untuk pasar Indonesia: Midtrans**  
Midtrans mendukung virtual account bank, QRIS, GoPay, OVO, dan kartu kredit — sangat sesuai target pasar UMKM Indonesia.

**Alternatif:** Stripe (untuk pasar internasional), Xendit (Indonesia).

**Alur Pembayaran via WhatsApp:**
```
User: "Saya mau upgrade ke Pro"
Agent Builder: "Baik! Berikut link pembayaran untuk paket Pro (Rp 299.000/bulan): [link Midtrans]
               Setelah pembayaran berhasil, akun Anda akan otomatis diupgrade."
[User klik link, bayar]
[Midtrans webhook → endpoint kita → update tier user di DB]
Agent Builder otomatis kirim konfirmasi: "Akun Anda berhasil diupgrade ke Pro! ✅"
```

**File baru:** `app/api/payment_webhook.py` — endpoint untuk menerima notifikasi dari Midtrans/Stripe.

---

## Alur Onboarding User Baru (End-to-End)

```
1. User chat ke nomor WA Agent Builder
   ↓
2. Agent Builder cek: nomor ini sudah terdaftar?
   - Belum → Mulai flow registrasi
   - Sudah → Lanjut ke menu utama
   ↓
3. Registrasi:
   Agent Builder: "Halo! Selamat datang di [Nama Platform] 👋
                  Saya akan bantu Anda membuat AI Agent pertama Anda.
                  Boleh saya tahu nama Anda?"
   ↓
4. Kumpulkan: nama, email (opsional)
   → Buat record User di DB (tier: "free")
   ↓
5. Tanya kebutuhan agent:
   "Anda ingin AI agent untuk keperluan apa?
   Misalnya: CS pelanggan, booking appointment, tanya-jawab produk..."
   ↓
6. Buat agent (sesuai limit Free tier)
   ↓
7. Agent Builder kirim ringkasan + cara test:
   "Agent 'CS Toko Anda' sudah siap! 🎉
   Untuk sementara berjalan di nomor dev kami.
   Upgrade ke Starter (Rp 99k/bln) untuk sambungkan ke nomor WA Anda sendiri."
   ↓
8. Jika user tertarik upgrade → flow pembayaran Midtrans
```

---

## Komponen Implementasi

### Phase 1: Multi-Tenancy Foundation (Est. 3–4 jam)
- [ ] Buat `app/models/user.py` dengan kolom tier & kuota
- [ ] Update `app/models/agent.py` — tambah `owner_id`, `is_system_agent`
- [ ] Buat `app/models/usage_log.py`
- [ ] Alembic migration untuk semua tabel baru
- [ ] Update `app/schemas/` untuk semua model baru

### Phase 2: Quota Enforcement (Est. 2–3 jam)
- [ ] Tambah middleware/helper `check_user_quota()` di `channels.py`
- [ ] Implementasi `increment_usage()` setelah setiap pesan berhasil diproses
- [ ] Cron job / scheduled task untuk reset `messages_used_this_month` tiap bulan
- [ ] Pesan otomatis dari Agent Builder saat kuota habis atau mendekati limit (80%)

### Phase 3: Agent Builder — Tier-Aware Tools (Est. 2–3 jam)
- [ ] Update `CreateAgentTool` — cek limit agent berdasarkan tier sebelum INSERT
- [ ] Tambah `RegisterUserTool` — auto-create User record saat first contact
- [ ] Tambah `GetUserSubscriptionTool` — Agent Builder bisa lihat status langganan user
- [ ] Tambah `GeneratePaymentLinkTool` — generate link Midtrans dan kirim ke user

### Phase 4: Payment Gateway (Est. 4–6 jam)
- [ ] Integrasi Midtrans Snap API (server-side)
- [ ] Buat `app/api/payment_webhook.py` — endpoint untuk notifikasi pembayaran
- [ ] Logic upgrade/downgrade tier berdasarkan event dari webhook
- [ ] Kirim konfirmasi otomatis ke user via Agent Builder setelah pembayaran sukses

### Phase 5: Analytics & Reporting (Est. 3–4 jam)
- [ ] Dashboard sederhana untuk operator platform (bukan user biasa): total user, revenue, pesan per tier
- [ ] Agent Builder bisa kirim ringkasan bulanan ke user: "Bulan ini agent Anda menangani 1.234 percakapan"
- [ ] Alert otomatis jika usage mendekati 80% limit tier

---

## Pertimbangan Pricing

| Komponen Biaya | Estimasi |
|----------------|----------|
| LLM per 1.000 pesan (GPT-4o Mini) | ~Rp 1.500–3.000 |
| LLM per 1.000 pesan (GPT-4o) | ~Rp 15.000–25.000 |
| Infrastructure (VPS + DB) | Rp 200.000–500.000/bulan |
| WhatsApp Business API (per nomor) | ~Rp 100.000–300.000/bulan |

**Margin kesehatan:**
- Free tier: Loss-leader (biaya ditanggung, batasi 200 pesan/bulan)
- Starter (Rp 99k): Break-even di ~2.000 pesan dengan GPT-4o Mini
- Pro (Rp 299k): Target margin 40–60%
- Business (Rp 799k): Target margin 60–70%

> **Catatan:** Angka di atas adalah estimasi kasar. Harus divalidasi dengan actual usage data setelah beta launch.

---

## Risiko & Mitigasi

| Risiko | Dampak | Mitigasi |
|--------|--------|----------|
| Biaya LLM melebihi revenue di tier rendah | Tinggi | Monitoring ketat usage per user. Batasi model mahal hanya untuk tier Pro+ |
| User abuse (spam pesan untuk menghabiskan kuota orang lain) | Sedang | Rate limiting per session. Quota terikat pada user, bukan pada session. |
| Churn tinggi di Free tier (tidak upgrade) | Sedang | Watermark + pesan nilai yang jelas saat kuota mendekati habis |
| Kompleksitas billing di WA (tidak ada UI web) | Sedang | Gunakan payment link eksternal (Midtrans). Tidak perlu UI web sendiri. |
| Data user (nomor WA) harus dilindungi | Tinggi | Enkripsi di DB, GDPR/PDPA compliance, kebijakan privasi yang jelas |

---

## Referensi

- `08-agent-builder.md` — Agent Builder sebagai kanal onboarding & upselling
- `app/models/agent.py` — Model agent yang akan diupdate
- `app/api/channels.py` — Tempat implementasi quota check
- `app/core/agent_runner.py` — Tempat implementasi usage tracking
