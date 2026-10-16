# Hotfix link koneksi owner Arthur — 16 Oktober 2026

## Penyebab dan perubahan

Link Meta Embedded Signup Lapi Bot berhasil dibuat pada 13.13.45 WIB, tetapi guard balasan menganggap pemilik assistant sebagai customer agent operasional. Pemilik assistant tidak otomatis menjadi operator internal agent sistem Arthur. Guard lalu mengganti balasan yang mengandung link dengan pesan kendala internal. Kejadian ini juga terbukti pada owner di luar cohort konfirmasi QA.

Guard sekarang memakai kelas runtime yang sama dengan orkestrator: percakapan pengelolaan pada agent builder seperti Arthur dapat menyampaikan link penyiapan akun. Pemeriksaan kepemilikan assistant dan konfirmasi pada tool tetap menentukan apakah link boleh dibuat. Tidak menambahkan owner tenant ke daftar operator internal, mengubah klasifikasi customer global, atau memberi akses ke assistant tenant lain.

Agent operasional tetap memblokir link login/penyiapan owner dari balasan customer, termasuk ketika owner menguji agent sebagai customer demo. Log membedakan penambahan link Google dari pemblokiran link untuk customer; sebelumnya kedua kondisi memakai nama event yang sama.

## Verifikasi

- Sebelum perbaikan: tes regresi baru menghasilkan **9 failed, 8 passed**; sembilan kegagalan mereproduksi pemblokiran link management.
- Setelah perbaikan: **17 passed** pada tes khusus link owner. Mencakup konfigurasi/capability builder, Cloud/demo, Meta/Google, akses lintas owner, persetujuan yang belum diberikan, customer operasional, serta customer preview demo.
- Suite terkait: **134 passed** (20,15 detik), mencakup regresi QA Arthur, kontrak OAuth, routing Google, guard balasan, serta preservasi environment/rollback hotfix.
- Helper rollout menerima image dan commit kandidat baru serta mode mempertahankan seluruh environment aktual API dan scheduler. Mode ini tidak memilih ulang cohort QA atau mengubah policy flags.
- Image hotfix dibangun dengan `deploy/Dockerfile.owner-link-hotfix` di atas image produksi `d9ab842ad932…`, hanya mengganti dua modul aplikasi dan menambahkan/memperbarui helper. Tes dijalankan dari checkout terpisah. Dependency Python/OS tetap berasal dari image produksi sebelumnya.
- Probe `scripts/arthur_owner_link_probe.py` membaca sesi dan ownership aktual dengan transaksi read-only dan menggunakan URL placeholder. Probe tidak membuat token signup, mengirim WhatsApp, atau mengubah konfigurasi agent.
- Probe pada salinan sesi Lapi Bot lulus: ownership sesuai, link Meta/Google owner lolos, link yang sama tetap diblokir pada customer agent operasional.
- Salinan database baru memuat **210 agent, 846 chunk knowledge, 502 sesi, dan 10.477 pesan**. Knowledge/brief dapat dibaca, preview belum menulis, approval menerapkan perubahan pada salinan, dan approval duplikat tidak mengulang perubahan.
- Semua **114 distribusi Python identik** dengan produksi lama; layer dasar image dipertahankan dan hash empat file aplikasi/helper sesuai checkout yang diuji.

## Status rilis

**Sudah aktif di produksi sejak 16 Oktober 2026, 13.37.00 WIB**, mengganti API dan scheduler. Commit aplikasi `c11190a` memuat perbaikan guard pada `b8fc0df`. Image immutable:

| Peran | Image |
| --- | --- |
| Hotfix aktif | `sha256:02977cec95b16abf813a3ba8e2b9177a13122ea71a430724b1ed326c75d63613` |
| Versi tepat sebelum hotfix, tersedia untuk rollback | `sha256:d9ab842ad9326502b1c85a18e4c08d3a02542ea5b9826bc96ef020c231317469` |

Tag aktif `managed-agents-app:arthur-owner-link-hotfix-20261016-overlay`. Pointer `deploy/.env.prod` sudah mengikuti tag dan commit aktif. Environment aktual API dan scheduler masing-masing dibandingkan dengan snapshot: hanya `APP_COMMIT_SHA` yang berubah. Cohort konfirmasi satu owner, customer preview satu agent, credential dan penegakan expiry tetap sesuai konfigurasi sebelumnya.

Urutan **hotfix → versi sebelum hotfix → hotfix** lulus pada staging terisolasi: health, pembacaan API dengan autentikasi, dan worker. Backup database berhasil direstorasi; archive dua image lolos pemeriksaan gzip CRC. Resource staging kemudian dibersihkan.

Pemeriksaan produksi setelah penggantian:

- HTTPS publik `/health` HTTP 200, status `ok`, commit `c11190a`.
- Database, scheduler, dan `wa_service` berstatus `ok`; API Docker `healthy` dan scheduler running pada image yang sama.
- Probe read-only pada **sesi Lapi Bot aktual** lulus: ownership benar, link management Meta/Google tidak diganti, dan link yang sama tetap diblokir pada customer agent operasional.
- Hash dua modul aplikasi di container produksi sama dengan source yang diuji.
- Tidak mengirim WhatsApp, menghasilkan token Meta tambahan, mengubah SOP/knowledge/memori agent, atau menjalankan migrasi schema selama hotfix. Tidak merestart gateway WhatsApp.

**Batas validasi:** koneksi sampai selesai di halaman Meta dan percakapan ulang WhatsApp nyata masih memerlukan percobaan user. User dapat meminta ulang link koneksi Lapi Bot lalu mengikuti konfirmasi Arthur. Hotfix ini menangani pemblokiran link owner; loop konfirmasi dan routing quote eskalasi pada laporan QA tetap pekerjaan terpisah.

Selama observasi, startup mencatat timeout warmup embedding namun health berhasil. Terdapat satu validasi `create_assistant` yang menolak argumen tambahan `purpose_detail` pada alur lain (run `8565c2a4-4535-4940-b3d5-e7650c0b9ba9`); schema tool tersebut tidak diubah oleh hotfix ini dan temuan itu belum diperbaiki di rilis ini. Tidak ditemukan event log level `error`/`critical` pada window pemeriksaan awal pasca-hotfix.

## Backup dan rollback

Backup privat: `/home/clevio/backups/arthur-qa-20261016-063159`, di luar Git, direktori mode `0700` dan state/config mode `0600`.

- `database.dump`: **11.630.145 byte**, 137 entry archive, berhasil direstorasi pada staging.
- `images.tar.gz`: **301.451.729 byte**, dua image, pemeriksaan CRC lulus.
- `state.private.json`, snapshot environment, dan dua Compose privat menyimpan konfigurasi rollback serta kandidat. Nilai credential tidak disalin ke laporan/repository.

Rollback ke versi tepat sebelum hotfix:

```bash
python3 scripts/arthur_qa_release.py rollback-production \
  --state /home/clevio/backups/arthur-qa-20261016-063159/state.private.json
```

Rollback mengganti API dan scheduler beserta environment sebelumnya; tidak memulihkan database atau membatalkan aksi eksternal. Setelah rollback, pointer `deploy/.env.prod` perlu dikembalikan ke `IMAGE_TAG=arthur-qa-remediation-20261015-r1` dan `APP_COMMIT_SHA=c768a25` sebelum deployment berikutnya. Backup sebelum keseluruhan rilis QA juga tetap tersedia pada lokasi yang dicatat di [laporan deployment awal](ARTHUR_DEPLOYMENT_2026-10-16.md).
