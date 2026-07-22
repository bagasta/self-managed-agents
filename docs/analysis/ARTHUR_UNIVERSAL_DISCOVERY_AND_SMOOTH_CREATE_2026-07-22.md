# Arthur Universal Discovery & Smooth Create Hardening

Tanggal: 22 Juli 2026

## Tujuan

Perubahan ini berlaku untuk seluruh pembuatan agent oleh Arthur, bukan hanya agent CS atau satu use case tertentu. Targetnya:

1. Arthur tidak dapat mengisi kebutuhan yang belum pernah diberikan user.
2. Discovery menyesuaikan jenis pekerjaan dan risikonya.
3. `needs_clarification` selalu menghasilkan pertanyaan, bukan error teknis.
4. Rencana yang belum lengkap tidak dapat masuk proses create.
5. Rencana yang sudah siap diselesaikan secara internal tanpa meminta user mengetik “coba lagi”.
6. Retry setelah commit tidak membuat agent duplikat.

## Invariant Baru

### 1. Setiap jawaban discovery harus memiliki bukti

`discovery_answers` sekarang wajib membawa `_evidence` pada runtime produksi. Setiap key menunjuk satu atau beberapa kutipan persis dari pesan user.

```json
{
  "problem": "Proses follow-up masih manual dan banyak lead terlewat.",
  "audience": "Tim sales internal.",
  "_evidence": {
    "problem": "follow-up masih manual dan banyak lead kelewat",
    "audience": "yang pakai tim sales saya"
  }
}
```

Validator membaca pesan user yang benar-benar tersimpan pada session. Kutipan yang tidak ditemukan ditolak.

### 2. Kutipan asli tetapi tidak relevan juga ditolak

Kutipan yang memang pernah dikirim user belum tentu mendukung field yang diisi. Validator membandingkan isi jawaban dengan kata-kata bermakna pada kutipan.

Contoh yang ditolak:

```json
{
  "prohibited_actions": ["Tidak boleh memberikan refund"],
  "_evidence": {
    "prohibited_actions": "Saya ingin membuat agent untuk pekerjaan bisnis"
  }
}
```

Kutipannya asli, tetapi tidak membuktikan larangan refund.

### 3. Validasi gagal secara tertutup

Jika riwayat pesan tidak dapat dibaca, Arthur tidak boleh melanjutkan create dengan asumsi. Tool mengembalikan state `temporarily_unavailable` dan `retryable=true`. Runtime mencoba ulang secara internal satu kali menggunakan payload yang sama.

User tidak diminta mengulang seluruh discovery karena gangguan pembacaan riwayat.

### 4. Konfirmasi akhir harus eksplisit dan terbaru

`user_confirmed=true` hanya sah jika:

- Arthur sudah memberikan rangkuman lengkap;
- pesan user terakhir mengandung persetujuan eksplisit seperti `sudah sesuai`;
- `_evidence.user_confirmed` mengutip pesan tersebut.

Jawaban generik seperti `iya`, `oke`, atau `tidak perlu file` tidak dapat mengonfirmasi seluruh rancangan agent.

Jika user memberikan perubahan setelah sebelumnya setuju, konfirmasi lama otomatis tidak berlaku karena bukan lagi pesan user terakhir.

### 5. Hanya `plan_status=ready` yang boleh auto-create

State machine sekarang membedakan:

| Status | Tindakan |
|---|---|
| `needs_clarification` | Tanyakan `next_questions`; jangan compose/create |
| `temporarily_unavailable` + retryable | Retry plan internal satu kali |
| `blocked_by_policy` | Tolak sesuai policy |
| entitlement blocked | Jelaskan batas plan; jangan memaksa create |
| `ready` | Lanjutkan compose, validate, create, verify, demo |

Output legacy tidak terstruktur seperti `ok` tidak lagi dianggap sebagai bukti bahwa rencana siap.

### 6. Clarification tidak boleh berubah menjadi “kendala sistem”

Jika model menghasilkan reply kosong atau hanya pesan progres setelah `needs_clarification`, reply guard menyusun pertanyaan langsung dari output `plan_agent`.

Dengan demikian state discovery normal tidak lagi tampil sebagai:

> Maaf, lagi ada kendala sistem. Coba kirim lagi.

### 7. Tidak ada izin asumsi pada recovery

Directive recovery lama yang menyuruh model memakai “asumsi wajar” telah dihapus. Recovery hanya boleh menggunakan `discovery_answers` yang sudah terkonfirmasi dan terbukti dari pesan user.

### 8. Ready plan mendapat recovery create internal

Jika rencana sudah `ready` tetapi model berhenti sebelum `create_agent`, runtime mencoba melanjutkan maksimal dua kali. Loop berhenti ketika:

- create berhasil;
- state berubah menjadi tidak eligible;
- terjadi exception nyata.

User tidak perlu mengirim “coba lagi” hanya karena model berhenti di tengah compose/validate.

### 9. Create retry bersifat idempotent

Runtime menghasilkan `_builder_creation_request_id` deterministik dari:

- session ID;
- owner;
- nama agent.

Marker disimpan di `tools_config` agent. Jika commit database berhasil tetapi respons tool hilang, retry dengan session dan nama yang sama mengembalikan agent yang sudah dibuat sebagai:

```json
{
  "success": true,
  "idempotent_replay": true,
  "agent_id": "..."
}
```

Agent tidak dibuat dua kali dan duplicate error tidak ditampilkan sebagai kegagalan user.

## Discovery Tetap Universal dan Adaptif

Validator menggunakan enam kelompok universal:

1. Konteks, masalah, nama, dan pengguna.
2. Workflow, kemampuan, wewenang, larangan, gaya, contoh ideal, dan red line.
3. Ketidaktahuan, fallback, approval, dan eskalasi.
4. Sumber kebenaran dan data sensitif.
5. Skala, integrasi, input/output, file, dan vision.
6. Reviewer sebelum go-live.

Arthur tidak harus menanyakan ulang informasi yang sudah diberikan. Namun, setiap field yang dianggap terjawab harus mempunyai bukti relevan dari pesan user.

Kedalaman pertanyaan mengikuti risiko:

- Personal sederhana: lebih ringkas; eskalasi manusia dan approver dapat dilewati.
- Percakapan/knowledge: fokus audience, sumber, batas jawaban, dan fallback.
- Operasional/transaksional: fokus state, approval, handoff, audit, dan kondisi selesai.
- Data: fokus sumber, struktur, validasi, metode, dan output.
- Konten: fokus audience, brand, review, serta izin publikasi.
- Coding/deploy: fokus spesifikasi, repo, stack, pengujian, environment, dan definisi deploy selesai.
- Agent dengan data sensitif atau keputusan berdampak tinggi: discovery dan approval lebih ketat.

## File yang Diubah

- `app/core/tools/builder_discovery.py`
- `app/core/tools/builder_planning_tools.py`
- `app/core/tools/builder_create_tools.py`
- `app/core/tools/builder_tools.py`
- `app/core/engine/agent_followups.py`
- `app/core/engine/agent_runner.py`
- `app/core/engine/reply_guard.py`
- `app/core/engine/prompt_builder.py`
- `system-message-builder.md`
- regression tests terkait Arthur.

## Verifikasi

- Regression inti anti-hallucination dan recovery: **73 passed**.
- Regression builder luas: **274 passed**.
- Full maintained suite: **987 passed, 7 failed, 9 skipped**.
- Tujuh kegagalan sama dengan baseline sebelum hardening ini: payment-link expectation, coding-preset wording, trial-expiry expectation, dua WA QR mock/event-loop, dan dua spam-window event-loop tests.
- Tidak ada kegagalan baru pada file Arthur yang diubah.
- `git diff --check`: lulus.
- Python compilation untuk seluruh source yang diubah: lulus.

## Batas Kejujuran Teknis

Tidak ada sistem yang dapat dijanjikan bebas gangguan 100%. Perubahan ini memastikan gangguan yang sudah diketahui tidak lagi dibebankan kepada user, asumsi tidak lolos secara diam-diam, retry aman, dan state normal tidak disamarkan sebagai error.

Error eksternal seperti database benar-benar down, provider LLM tidak tersedia, atau deploy lama masih aktif tetap mungkin terjadi. Dalam kondisi itu sistem harus gagal tertutup, mempertahankan jawaban user, mencatat error terstruktur, dan tidak membuat agent dari data yang belum terverifikasi.

## Deploy Wajib

Perubahan source belum mengubah perilaku live sebelum production memakai commit ini.

1. Merge/cherry-pick seluruh perubahan ke branch produksi.
2. Build ulang image API/worker.
3. Jalankan migrasi normal—perubahan ini tidak membutuhkan migration schema.
4. Karena `system-message-builder.md` berubah, jalankan dry-run lalu `scripts/seed_arthur.py` sesuai `deploy/FAST_DEPLOY.md`.
5. Restart API/worker dan pastikan image/commit produksi benar.
6. Uji dari session baru untuk minimal empat jenis agent: personal, knowledge/CS, operasional ber-approval, dan coding/deploy.
7. Verifikasi log state:
   - incomplete → `needs_clarification` → pertanyaan;
   - explicit confirmation → `ready`;
   - ready → compose/validate/create/verify/demo;
   - lost response/retry → `idempotent_replay=true` tanpa agent duplikat.

