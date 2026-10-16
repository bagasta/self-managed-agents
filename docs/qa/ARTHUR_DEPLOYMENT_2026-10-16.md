# Deployment Arthur QA remediation — 16 Oktober 2026

**Catatan deployment awal; produksi kini memakai [hotfix penulis Markdown/dokumen `20055cc`](ARTHUR_INSTRUCTION_DOCUMENT_FIX_2026-10-16.md), aktif 16 Oktober 2026 pukul 15.33 WIB.** Versi ini mempertahankan [hotfix CV/reset memori](ARTHUR_TOKYO8_CV_MEMORY_FIX_2026-10-16.md) dan [perbaikan link owner](ARTHUR_OWNER_LINK_HOTFIX_2026-10-16.md). Rincian berikut mencatat deployment kandidat `c768a25`: API dan scheduler diganti bersama pada 16 Oktober 2026, pukul **08:54:46 WIB**. Tidak ada migrasi schema, reseed Arthur, pemulihan database produksi, penghapusan memori, atau pengaktifan kembali agent yang dijeda/dihapus.

## Versi yang disimpan

| Peran | Identitas immutable |
| --- | --- |
| Produksi baru: API dan scheduler | `sha256:d9ab842ad9326502b1c85a18e4c08d3a02542ea5b9826bc96ef020c231317469` |
| Produksi sebelumnya, tersedia untuk rollback | `sha256:cf2828fd2584a700993166e2a146573c5bc9c874bae92edbf70e5145b81aabd7` |

Tag kandidat: `managed-agents-app:arthur-qa-remediation-20261015-r1`. Tag rollback tambahan: `managed-agents-app:rollback-qa-20261016-014350`. Image sebelumnya juga tetap tersedia sebagai `managed-agents-app:arthur-knowledge-contract-20261015`.

Hanya service `api` dan `scheduler` yang diganti. Gateway WhatsApp, koneksi/auth store WhatsApp, Redis produksi, PgBouncer, dan deployment agent lain tidak direstart oleh rollout ini. Perubahan kode gateway Go pada commit sebelumnya belum diterapkan; API baru mengirim pemberitahuan pause demo langsung sehingga tidak memerlukan upgrade gateway serentak.

## Backup dan pembuktian rollback

Backup privat berada di `/home/clevio/backups/arthur-qa-20261016-014350` pada host ini, di luar Git. Direktori menggunakan mode `0700`, berkas konfigurasi/state privat menggunakan `0600`.

- `database.dump`: archive PostgreSQL konsisten, 10.791.779 byte, 137 entry archive. Hanya database aplikasi yang dicadangkan; tidak mengubah database sumber.
- `images.tar.gz`: kedua image aplikasi, 301.397.804 byte. Integritas gzip/tar dan dua entry image terverifikasi.
- `production-containers.private.json`: snapshot konfigurasi aktual dua container, termasuk environment. Berkas ini mengandung secret dan tidak boleh dipublikasikan.
- `configuration/`: salinan konfigurasi deployment sebelum rollout.
- `production.rollback.private.json` dan `production.candidate.private.json`: Compose lengkap, memakai image immutable dan konfigurasi aktual. Juga mengandung secret; tidak masuk repository.
- `state.private.json`: checksum, hasil pemeriksaan, dan receipt operasional. Mengandung kredensial staging privat.

Database berhasil dipulihkan ke PostgreSQL 16 + pgvector pada jaringan Docker internal yang terpisah. Hasil pemulihan: **210 agent, 846 chunk knowledge, 501 sesi, 9.738 pesan**. Worker staging memakai Redis sendiri. Jadwal/outbound pada salinan dikarantina, credential provider produksi tidak dimasukkan ke environment staging, tidak ada Docker socket produksi, dan jaringan staging tidak memiliki jalur keluar.

Pada salinan ini, urutan **kandidat → image lama → kandidat** berhasil. Setiap versi lulus health, pembacaan API dengan autentikasi, dan startup worker. Probe integrasi pada data QA membuktikan:

- Schema 35 core tools berhasil dimuat.
- Knowledge dan brief tersimpan dapat dibaca oleh owner yang sesuai.
- Perubahan belum terjadi saat preview dikembalikan.
- Approval pada pesan berikutnya menerapkan perubahan.
- Pengulangan approval yang sama mengembalikan receipt tanpa mengulang perubahan.

Probe hanya mengubah salinan staging, kemudian mengembalikan instruksi aslinya. Tidak mengirim pesan WhatsApp, tidak menulis Google Sheets, dan tidak membuat koneksi OAuth nyata. Pemeriksaan awal dari host terhalang isolasi jaringan; pemeriksaan yang digunakan untuk keputusan rilis dilakukan dari dalam staging.

Setelah validasi produksi selesai, empat container staging beserta jaringan dan volume database salinan dibersihkan. Backup privat, image kandidat, dan image rollback tetap disimpan. Staging tidak lagi menjalankan worker tambahan.

## Cakupan pengaktifan

Kode aplikasi baru melayani seluruh API produksi, sesuai persetujuan penggantian versi. Pengaturan berikut dibatasi untuk QA:

- Konfirmasi server per giliran aktif untuk **satu owner QA** yang terverifikasi dari record CS Toko Kue QA.
- Customer preview demo aktif untuk **satu agent CS Toko Kue QA yang masih aktif**. Agent lama yang sudah dihapus tidak direstorasi. Mode ini hanya berlaku di koneksi demo.
- Penegakan tanggal kedaluwarsa subscription global **tetap nonaktif**, dan cohort enforcement kosong. Validitas tanggal tetap dapat diobservasi. Tidak memperpanjang/mengakhiri subscription secara massal.

`deploy/.env.prod` disinkronkan ke tag aplikasi, commit, dan cohort yang diterapkan. Berkas itu diabaikan Git. Makefile memakai `--env-file deploy/.env.prod` agar pemilihan image Compose tidak kembali ke `latest` pada deployment berikutnya.

## Pemeriksaan produksi

- API: container running dan health Docker `healthy`.
- Scheduler: container running pada image yang sama; heartbeat baru terukur sekitar 2,8 detik setelah tick.
- HTTPS publik `https://managed-agent.chiefaiofficer.id/health`: HTTP 200, status `ok`, commit `c768a25`.
- Health terperinci: database, scheduler, dan `wa_service` semuanya `ok`.
- Pembacaan katalog subscription dengan API key berhasil.
- Tidak ada run aktif saat drain atau pada pemeriksaan pasca-rollout.
- Log API setelah pergantian tidak memuat error. Worker menemukan satu peringatan pemeriksaan kredensial Meta (`HTTPStatusError`) pada agent **Admin Tokyo8**. Kredensial tersebut tidak dirotasi/dihapus dalam rilis ini; pemeriksaan koneksi agent itu terpisah dari health API dan worker.

Tidak ada klaim bahwa Batch 1–10 sudah lulus ulang secara percakapan nyata. Tes provider WhatsApp/Google dan tes policy “agent jual obat keras” masih perlu QA terkontrol. Hasil otomatis sebelum rilis tetap **466 passed, 2 deselected** sebagaimana [laporan perbaikan](ARTHUR_QA_REMEDIATION_2026-10-15.md).

## Jika perlu rollback

Jalankan dari checkout ini pada host yang sama:

```bash
python3 scripts/arthur_qa_release.py rollback-production \
  --state /home/clevio/backups/arthur-qa-20261016-014350/state.private.json
```

Perintah mengganti API dan scheduler ke image serta environment lama yang disimpan, dengan grace period. Rollback tetap dapat dijalankan ketika health kandidat rusak. Ia tidak memulihkan database atau mengulang pengiriman. Jika mengganti kandidat gagal saat rollout, helper otomatis mencoba menjalankan konfigurasi rollback.

Setelah rollback, sinkronkan pointer deployment di `deploy/.env.prod` ke tag `arthur-knowledge-contract-20261015`, commit lama `64cf8748c806df0b701b2a1a45e1fa1cec42b1ad`, dan cohort kosong sebelum menggunakan target Makefile lagi. Untuk restart image lama secara persis, gunakan Compose rollback privat dengan `up -d --no-deps --no-build --pull never api scheduler`; jangan membangun source baru ke tag rollback.

**Rollback kode tidak membatalkan pesan yang sudah terkirim, perubahan konfigurasi/data agent saat QA, atau perubahan di Google.** Periksa efek aktual dan antrean sebelum mengulangi aksi yang hasilnya tidak pasti. Backup image/database disimpan sampai QA dan masa observasi selesai; database produksi tidak boleh ditimpa hanya untuk membatalkan deployment kode.
