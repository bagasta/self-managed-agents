# Pemulihan balasan setelah kredit AI habis

Permintaan WhatsApp yang gagal karena kredit provider AI tidak cukup kini
disimpan dalam antrean PostgreSQL dan dicoba kembali oleh scheduler. Pengguna
tidak perlu mengirim pesan pemicu ulang setelah kredit tersedia.

## Insiden LapiBot

- Agent: `d91c36b9-9e09-4498-b549-ea3419db1db9` (LapiBot, Meta Cloud API).
- Audit 24 jam menemukan 28 run gagal dengan error kredit OpenRouter `402`,
  melibatkan 17 penerima berbeda. Semua pesan asli tersimpan dan berupa teks.
- Tidak ditemukan run sukses setelah kegagalan terakhir untuk para penerima
  tersebut, termasuk pemeriksaan lintas sesi untuk nomor dan agent yang sama.
- Pemulihan historis sudah dijalankan: 17 permintaan dimasukkan ke antrean.
- Verifikasi produksi akhir: seluruh 17 permintaan berstatus `completed` dan
  seluruh 17 balasan outbound berstatus `sent`, tanpa error. Tidak dibutuhkan
  pesan pemicu ulang dari para penerima.

## Perilaku

- Hanya kegagalan kredit sebelum token terpakai atau tool dijalankan yang
  dipulihkan otomatis. Error autentikasi, rate limit, dan kuota storage tidak
  masuk kategori ini.
- Jeda pertama 60 detik; percobaan berikutnya memakai jeda 120, 240, 480,
  lalu maksimal 900 detik. Scheduler memeriksa antrean setiap 10 detik.
- Permintaan kedaluwarsa 24 jam setelah pesan asli diterima.
- Pesan yang sudah disusul pesan/jawaban baru dibatalkan. Pemeriksaan ini
  dilakukan sebelum proses AI dan sebelum pengiriman, termasuk lintas sesi
  untuk penerima yang sama. Layanan yang dihentikan dan sesi dengan AI nonaktif
  juga tidak dipulihkan.
- Pesan asli digunakan kembali tanpa menambah baris pesan pengguna duplikat.
  Setelah berhasil, pesan tersebut diasosiasikan dengan run pemulihan.
- Hasil AI masuk ke antrean outbound secara atomik. Kegagalan pengiriman
  mengulang teks yang sudah tersimpan, tanpa menjalankan model dan tool lagi.
- Jika worker mati setelah mulai menjalankan model/tool dan hasilnya belum
  pasti, item ditandai `failed` untuk diperiksa agar aksi eksternal tidak
  terulang tanpa verifikasi.
- Status `completed` pada antrean pemulihan berarti proses AI selesai dan hasil
  diserahkan ke antrean pengiriman. Status `sent` pada outbound menunjukkan
  layanan WhatsApp menerima pengiriman, bukan konfirmasi pesan dibaca pengguna.

## Implementasi dan validasi

- Migrasi `031` menambah `pending_ai_replies`; tidak mengubah data percakapan
  yang sudah ada saat migrasi.
- Image produksi: `managed-agents-app:ai-credit-recovery-20261017`.
- Image sebelum perubahan disimpan sebagai
  `managed-agents-app:before-ai-credit-recovery-20261017`.
- Konfigurasi produksi yang berubah hanya image untuk API dan scheduler.
- API `/health` dan `/health/detailed` kembali `ok` setelah pemasangan;
  database, scheduler, dan layanan WhatsApp juga `ok`.
- 109 pengujian lolos dalam lingkungan terpisah, termasuk migrasi PostgreSQL,
  durability pesan, sesi, webhook Meta, scheduler, dan pemulihan kredit.
- 39 pengujian antrean dan scheduler diulang setelah penyesuaian akhir dan lolos.
- Ruff untuk semua file Python baru dan `git diff --check` bersih.

## Pemulihan historis tambahan

Perintah berikut hanya menampilkan jumlah calon pemulihan. Tambahkan `--apply`
untuk memasukkannya ke antrean. Menjalankan ulang tidak menambah item yang sama.

```bash
docker exec deploy-api-1 python -m scripts.recover_ai_credit_replies \
  --agent-id d91c36b9-9e09-4498-b549-ea3419db1db9
```

Image patch dibangun dengan `deploy/Dockerfile.ai-credit-recovery`, menggunakan
image produksi sebelumnya sebagai `BASE_IMAGE`. Override image berada di
`deploy/docker-compose.ai-credit-recovery.yml`. Konfigurasi dasar produksi
yang digunakan tetap
`/home/clevio/backups/arthur-qa-20261016-082944/production.candidate.private.json`.

Untuk rollback kode, jalankan Compose dengan konfigurasi dasar tersebut tanpa
override pemulihan. Tabel tambahan dapat dibiarkan; tidak perlu menghapus
volume atau data percakapan.
