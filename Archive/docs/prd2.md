# PRD2: Platform Improvements — Hasil Diskusi Analisis

> Dokumen ini merangkum temuan analisis dan arah perbaikan yang disepakati dari diskusi review arsitektur platform.
> Tujuan: membuat platform benar-benar **general-purpose dan config-driven**, tidak bias ke agent tertentu.

---

## 1. Latar Belakang

Platform saat ini sudah berjalan, namun setelah dianalisis ditemukan beberapa masalah struktural:

- Kode implisit bias ke agent tertentu (Arthur — agent yang bisa membuat agent lain)
- Agent tidak memiliki self-awareness terhadap platform dan identitasnya sendiri
- Isolasi session/memory di WhatsApp belum ketat
- Default tool terlalu permisif untuk semua agent
- WhatsApp integration belum lengkap untuk semua tipe pesan

---

## 2. Temuan yang Janggal (Issues)

### 2.1. `send_agent_wa_qr` Selalu Aktif untuk Semua WA Session

**Masalah:** Tool `send_agent_wa_qr` di-load otomatis untuk semua agent yang punya WhatsApp session, tanpa kontrol via `tools_config`. Padahal tool ini sangat spesifik — hanya relevan untuk agent seperti Arthur yang tugasnya membuat agent lain.

**Dampak:** Semua agent WA punya tool yang tidak relevan, memperpadat tool list, dan berpotensi membingungkan LLM.

**Solusi:** Jadikan opt-in via `tools_config`, misalnya dengan flag `"wa_agent_manager": true`. Hanya agent yang butuh fitur ini yang mengaktifkannya.

---

### 2.2. Default Tool Terlalu Permisif

**Masalah:** Di `agent_runner.py`, tool seperti `sandbox`, `tool_creator`, `scheduler` default ON untuk semua agent. Fungsi `_is_enabled()` mengembalikan `True` jika key tidak ada di `tools_config`.

**Dampak:** Agent customer service biasa punya akses ke bash execution dan tool creator — ini attack surface yang tidak perlu. Kalau agent di-jailbreak, user bisa abuse tool ini.

**Solusi:** Ubah default menjadi lebih konservatif. Hanya `memory` dan `skills` yang masuk akal default ON. Tool powerful seperti `sandbox`, `tool_creator`, `scheduler` harus opt-in eksplisit.

---

### 2.3. Behavior Arthur Bergantung pada Memory, Bukan Instruksi

**Masalah:** Arthur "pintar" (bikin agent → langsung kirim QR) hanya ke nomor operator karena ada memory dari interaksi sebelumnya. User baru dapat Arthur yang blank — tidak tahu harus melakukan apa setelah bikin agent.

**Dampak:** Behavior tidak konsisten antar user. Platform terkesan bias ke satu nomor tertentu.

**Solusi:** Pindahkan SOP Arthur ke `agent.instructions` di DB. Memory untuk fakta dinamis, instruksi untuk behavior tetap.

---

### 2.4. Sandbox Di-init Meski Tidak Dipakai

**Masalah:** `DockerSandbox(session.id)` dibuat di baris awal `run_agent()`, sebelum cek apakah sandbox diaktifkan di `tools_config`. Kalau sandbox di-disable, container tetap ter-create lalu tidak dipakai.

**Dampak:** Waste resource — container spin up sia-sia setiap request.

**Solusi:** Inisialisasi sandbox hanya jika `_is_enabled(tools_config, "sandbox")` bernilai True.

---

### 2.5. Dokumentasi Internal Tidak Konsisten dengan Behavior

**Masalah:** Comment di `agent_runner.py` baris 750 menyebut RAG default ON, tapi implementasinya tidak begitu. Beberapa komentar tidak mencerminkan kondisi kode aktual.

**Dampak:** Membingungkan developer yang membaca kode.

**Solusi:** Sinkronkan comment dengan behavior aktual saat refactor.

---

### 2.6. Isolasi Session WhatsApp Belum Ketat

**Masalah:** Belum ada jaminan bahwa session di WhatsApp di-scope secara konsisten per pengirim. Pesan dari grup dan pesan personal bisa tercampur, dan memory bisa bocor antar user.

**Dampak:** User A bisa mendapat konteks dari user B. History tidak terisolasi.

**Solusi:** Lihat bagian 4.3.

---

## 3. Perbaikan yang Disepakati

### 3.1. `send_agent_wa_qr` → Opt-in via `tools_config`

```json
// tools_config agent Arthur
{
  "wa_agent_manager": true
}
```

Di `agent_runner.py`, load `send_agent_wa_qr` hanya jika flag ini aktif — pisahkan dari `whatsapp_media` yang umum.

---

### 3.2. Revisi Default Tool Config

| Tool | Default Sekarang | Default Baru |
|------|-----------------|--------------|
| `memory` | ON | ON |
| `skills` | ON | ON |
| `sandbox` | ON | **OFF** (opt-in) |
| `tool_creator` | ON | **OFF** (opt-in) |
| `scheduler` | ON | **OFF** (opt-in) |
| `escalation` | ON | ON |
| `http` | OFF | OFF |
| `wa_agent_manager` | (tidak ada) | **OFF** (opt-in baru) |

---

### 3.3. Agent Context Block di System Prompt

Setiap run, inject blok konteks otomatis berisi informasi platform:

```
## Platform Context
- Agent ID: <uuid>
- Agent Name: Arthur
- Model: anthropic/claude-sonnet-4-6
- Active Tools: sandbox, memory, tool_creator, escalation, whatsapp_media
- Custom Tools: [nama + deskripsi singkat setiap tool yang sudah dibuat]
- Channel: whatsapp
- Current User Phone: +6281234567890
- Current User Role: OPERATOR  ← jika nomornya ada di operator_ids
- Session ID: <uuid>
```

Tujuan: agent tidak perlu diajarkan siapa dirinya — dia langsung tahu dari system prompt setiap run.

---

### 3.4. Operator Awareness

Tambah field `operator_ids: list[str]` di model `Agent` — berisi daftar `external_user_id` (nomor WA) yang punya akses operator.

Saat run, cek apakah `external_user_id` pengirim ada di `operator_ids`:
- Jika ya → inject `Current User Role: OPERATOR` ke system prompt
- Jika tidak → inject `Current User Role: user`

Dengan ini agent langsung tahu siapa yang chat tanpa bergantung pada memory.

---

### 3.5. Tool Creator → Custom Tools Masuk System Prompt Real-time

Saat ini custom tool baru ter-load sebagai LangChain tool native di turn berikutnya. Di turn yang sama, agent hanya bisa jalankan via `run_custom_tool`.

**Perbaikan:** Setelah `create_tool` berhasil, daftar custom tools (nama + deskripsi + cara pakai) diinjek ke system prompt sebagai bagian dari Agent Context Block — sehingga agent langsung aware tools apa yang tersedia tanpa menunggu turn berikutnya.

---

## 4. Fitur Baru yang Perlu Dibangun

### 4.1. WhatsApp API yang Lengkap

**Tujuan:** Agent bisa mengirim dan menerima semua tipe pesan WhatsApp, bukan hanya teks dan gambar.

**Tipe pesan yang perlu didukung (wa-service / Go):**

| Tipe | Terima | Kirim |
|------|--------|-------|
| Teks | ✅ sudah | ✅ sudah |
| Gambar | ✅ sudah | ✅ sudah |
| Dokumen | ✅ sudah | ✅ sudah |
| Voice note / Audio | ❌ | ❌ |
| Video | ❌ | ❌ |
| Sticker | ❌ | ❌ |
| Location | ❌ | ❌ |
| Contact card | ❌ | ❌ |
| Reaction | ❌ | ❌ |
| Reply/quote pesan | ❌ | ❌ |
| Link preview | ❌ | ❌ |
| Poll | ❌ | ❌ |

**Pendekatan implementasi:** Expose endpoint generic di wa-service yang cukup fleksibel sehingga agent bisa menggunakannya via `http_post` tool — tidak perlu tool baru per tipe pesan. Agent cukup tahu endpoint dan struktur payload-nya.

---

### 4.2. Isolasi Session WhatsApp yang Ketat

**Aturan session key:**

```
Pesan personal  → session_key = agent_id + sender_phone
Pesan grup      → session_key = agent_id + group_id
```

Setiap kombinasi unik = session terpisah = history dan memory terpisah.

**Yang perlu dibenahi di wa-service (Go):** Saat pesan masuk, webhook ke Python harus mengirim field terpisah:
- `sender_phone` — nomor pengirim
- `group_id` — ID grup (kosong jika pesan personal)
- `is_group` — boolean

Saat ini kemungkinan field-field ini belum terpisah dengan jelas di payload webhook.

**Yang perlu dibenahi di Python:** Logic pembuatan/lookup session di `POST /v1/channels/wa/incoming` harus menggunakan aturan session key di atas.

---

### 4.3. Memory Scoping yang Benar

Memory (long-term) harus di-scope per `external_user_id` (nomor pengirim), bukan per session atau agent saja.

- Pesan dari grup → memory di-scope ke `group_id`, bukan sender individual
- Pesan personal → memory di-scope ke `sender_phone`
- Memory operator tidak bocor ke user biasa

---

## 5. Prinsip Desain yang Disepakati

1. **Config-driven, bukan code-driven** — behavior agent harus bisa dikontrol penuh via `agent.instructions` dan `tools_config`, tanpa ada asumsi tersembunyi di kode.
2. **Tidak bias ke agent tertentu** — kode platform tidak boleh punya logika spesifik untuk Arthur atau agent lainnya.
3. **Agent harus self-aware** — setiap agent tahu identitasnya, tools-nya, channel-nya, dan siapa yang sedang chat tanpa bergantung pada memory.
4. **Default konservatif** — tool yang powerful harus opt-in, bukan opt-out.
5. **Isolasi ketat** — memory dan history tidak boleh bocor antar user, antar session, atau antar agent.

---

## 6. Prioritas Implementasi

| # | Item | Prioritas | Estimasi Kompleksitas |
|---|------|-----------|----------------------|
| 1 | Agent Context Block di system prompt | Tinggi | Rendah |
| 2 | Operator awareness (`operator_ids`) | Tinggi | Rendah |
| 3 | `send_agent_wa_qr` → opt-in | Tinggi | Rendah |
| 4 | Revisi default tool config | Tinggi | Rendah |
| 5 | Custom tools masuk system prompt real-time | Tinggi | Sedang |
| 6 | Isolasi session WA (session key) | Tinggi | Sedang |
| 7 | Sandbox lazy init | Sedang | Rendah |
| 8 | WhatsApp API lengkap (wa-service Go) | Sedang | Tinggi |
| 9 | Sinkronisasi komentar kode | Rendah | Rendah |
