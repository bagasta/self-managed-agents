# Analisis dan perbaikan CV / reset memori Tokyo8 — 16 Oktober 2026

## Temuan dari produksi

**Penyebab utama adalah aksi pengiriman file otomatis yang salah memakai hasil baca memori sebagai sumber pekerjaan.** Penyimpanan memori Arthur dan Tokyo8 memakai agent/session yang berbeda. Gejala memori “nyambung” muncul ketika referensi file dari memori Tokyo8 dipakai sebagai artifact percakapan Arthur.

Pemeriksaan membaca database dalam transaksi `READ ONLY` dan log API pada 14.02–14.10 WIB. Isi CV, nomor lengkap, email pemilik Google, dan signed URL tidak disalin ke dokumen ini.

| Kejadian | Bukti dan penyebab |
| --- | --- |
| CV gambar dikirim ke Tokyo8 pada 14.02.16 | Run `cd98ac91-48ca-4118-b9c7-d0ba8b8086f6` mencoba upload JPG enam kali memakai path lokal atau `file://`. Server Google MCP berada pada container lain dan tidak memiliki path workspace agent tersebut. |
| CV gambar tidak tersimpan sebagai gambar asli | Semua percobaan JPG gagal. Ada upload **file TXT berisi hasil ekstraksi** yang berhasil kemudian; itu berbeda dari menyimpan gambar CV asli. |
| Gambar CV dikirim balik pada 14.05.27 | Followup otomatis mengekstrak path gambar dari argumen/hasil tool, menganggapnya artifact siap kirim, lalu benar-benar mengirim gambarnya melalui WhatsApp. User sedang menyerahkan CV untuk diproses, bukan meminta CV dikirim balik. |
| Permintaan reset pada 14.07 dan 14.10 dibalas “Gambar image.jpg sudah saya kirim” | Arthur memanggil `inspect_assistant_memory` pada agent Tokyo8 yang benar. Hasil inspect memuat path lampiran historis. Followup otomatis mengambil path itu dan mencoba mengirim gambar, mengganti jawaban LLM untuk permintaan reset. |
| Klaim terkirim pada 14.10 palsu | Pada run `9e7f5ceb-8884-4a82-bc0d-0111f0d420ed`, tool mengembalikan `[MEDIA_SOURCE_UNAVAILABLE] Tidak ada gambar tervalidasi untuk dikirim pada percakapan ini.` Pemeriksa sukses memakai pencarian kata ` dikirim `, sehingga penolakan tersebut dianggap sukses dan log mencatat `sent=true`. |
| Reset tidak menghapus memori | Tidak ada panggilan `reset_assistant_context` pada percobaan tersebut. Hasil inspect 14.10 menunjukkan 7 memori scoped Tokyo8. Ini kegagalan alur setelah pembacaan memori, bukan tool reset hilang atau menolak nomor lengkap itu. |
| Memori terasa tersambung | Memori dibatasi oleh `agent_id` dan scope. Sesi Arthur `72aa6458-09e3-4787-86a6-83b37032e38b` berbeda dari sesi Tokyo8 `08021c27-e466-4c78-8be8-aee814c52695`. Kedua sesi memakai alias `/workspace/...` yang serupa tetapi direktori host berbeda. Pointer artifact Arthur justru tersimpan dari path yang terbaca dalam preview memori Tokyo8; ini pencampuran referensi oleh postprocessor, bukan satu tabel/scope bersama. |
| Pesan tentang Google Sheet tidak sesuai tugas Drive | Fallback untuk resource Google yang tidak ditemukan selalu menyuruh menyiapkan Sheet dan mengklaim percobaan pemulihan sudah dilakukan. Teks tersebut dipakai juga untuk pekerjaan Drive. Receipt upload Drive juga belum dikenali sebagai artifact sukses seperti URL Google Docs/Sheets. |

## Perbaikan kode

1. **Bukti kirim harus eksplisit.** Pengiriman gambar/dokumen hanya dianggap berhasil jika tool mengembalikan receipt `[IMAGE_SENT]` / `[DOCUMENT_SENT]` pada awal hasil. Kata “dikirim” pada penolakan, janji, atau status negatif tidak cukup. Hasil pengiriman baru juga tidak boleh memakai receipt sukses pengiriman sebelumnya.
2. **Provenance artifact dibatasi.** Followup otomatis hanya mengambil output dari tool pembuat file pada run saat ini. Hasil baca memori/knowledge, argumen upload, path dalam error, dan lampiran `current_input` bukan artifact baru untuk dikirim. Pemeriksaan yang sama dipakai saat menyimpan pointer artifact ke memori runtime.
3. **Reset tetap per agent dan per nomor.** Setelah review dan konfirmasi, reset membersihkan memori scoped dan konteks lampiran/artifact/persetujuan sesi yang dituju. Riwayat chat diarsipkan, SOP/knowledge/credential, global memory, Arthur, dan pelanggan lain dipertahankan. Deskripsi tool menegaskan isi preview memori merupakan data historis, bukan instruksi pengiriman file.
4. **Upload byte CV asli ke Drive.** Adapter `create_drive_file` mengubah path lampiran terdaftar pada sesi menjadi URL transfer bertanda tangan selama 120 detik. Server Google mengambil byte asli melalui URL itu. URL terikat ke agent, sesi, hash file, dan batas reset; file lokal lain atau sesi lain ditolak. Tidak membuka direktori publik atau memasang seluruh workspace tenant ke server Google.
5. **Fallback Google sesuai bukti.** Pesan resource gagal bersifat umum untuk file/folder/dokumen dan tidak mengarang kebutuhan Google Sheet atau percobaan pemulihan. Receipt upload file Drive yang berhasil dikenali sebagai artifact hasil kerja.

## Pengujian dan batas klaim

Tes reproduksi awal pada kode sebelum perbaikan: **7 failed, 2 passed**. Kegagalan membuktikan pembacaan memori memicu pengiriman, hasil negatif dianggap sukses, dan pengiriman gagal dapat memakai receipt lama.

Tes baru mencakup reset pada agent yang benar, memori Arthur/pelamar lain tetap utuh, upload byte file asli melalui endpoint ke tool Drive tiruan, token rusak/kedaluwarsa, file berubah, batas reset, akses lintas sesi, dan symlink keluar dari workspace. Tes menggunakan data sintetis dan tidak menulis Google Drive atau mengirim WhatsApp nyata.

Satu assertion prompt legacy `test_builder_prompt_blocks_repeated_continue_questions` juga gagal pada image produksi sebelum perubahan. Assertion itu dikeluarkan dari suite terkait dan tidak diperbaiki dalam pekerjaan ini. Pengujian Google disconnect memerlukan URL integrasi test; konfigurasi tersebut diberikan pada suite final.

Suite final setelah pemeriksaan konfigurasi asli: **300 passed, 1 deselected** dalam 23,84 detik. Semua 114 distribusi Python sama dengan image produksi sebelumnya, layer dasarnya dipertahankan, dan hash 11 file aplikasi/helper diverifikasi terhadap checkout yang diuji.

Staging pertama menangkap akses ke field `channel_secret_key` yang tidak ada pada model Settings aplikasi. Kandidat tersebut dibatalkan sebelum produksi dan receipt staging-nya dibuat tidak valid untuk rollout. Penandatanganan transfer diperbaiki menggunakan key turunan khusus dari API key aplikasi yang memang tersedia; pengujian endpoint kini memakai model `Settings` asli dan memastikan token tidak valid menghasilkan 404.

Receipt staging, deployment, pemeriksaan produksi, serta rollback dicatat setelah validasi selesai. Permintaan hapus memori yang dikutip dalam laporan tidak dieksekusi pada data produksi; owner perlu memberi konfirmasi baru melalui alur yang telah diperbaiki.

## Validasi salinan data produksi

Kandidat final dipulihkan pada database terisolasi dengan **212 agent, 846 chunk knowledge, 503 sesi, dan 10.570 pesan**. Replay read-only run 14.10 membuktikan hasil inspect tidak lagi menghasilkan artifact/pengiriman file dan hasil kirim gagal tidak dianggap sukses. Tool reset tetap tersedia dan menuntut persetujuan.

Reset kemudian benar-benar diuji **hanya pada salinan staging**: memori scoped Tokyo8 dibersihkan, metadata lampiran/artifact dihapus, memori Arthur dan instruksi Tokyo8 tetap utuh. Probe knowledge, brief, preview tanpa write, approval berikutnya, dan pencegahan approval duplikat juga lulus. Endpoint transfer pada server staging menolak token tidak valid dengan HTTP 404.

## Deployment dan pemeriksaan produksi

**Hotfix aktif di produksi sejak 16 Oktober 2026, 14.46.23 WIB.** API dan scheduler memakai commit aplikasi **`4cf0202`** (perbaikan utama `afd2f5a`) dan image immutable:

`sha256:4ebe75e13f863d55264229a0037752704cd7ece6db5d559d3d27c390f6fd25bb`

Tag aktif: `managed-agents-app:arthur-cv-memory-hotfix-20261016-r2`. Pointer deployment `.env.prod` disinkronkan ke tag dan commit tersebut.

- Urutan **kandidat final → versi sebelumnya → kandidat final** pada staging lulus health, pembacaan API terautentikasi, dan startup worker.
- Environment aktual API dan scheduler dibandingkan dengan snapshot: hanya `APP_COMMIT_SHA` berubah. Credential, cohort QA/preview, policy expiry, gateway serta koneksi WA dipertahankan. Tidak ada migrasi schema.
- HTTPS publik `/health` berstatus `ok`, commit `4cf0202`. Database, scheduler, serta `wa_service` semuanya `ok`; kedua container memakai image yang sama.
- Replay run reset yang bermasalah pada container **produksi** lulus tanpa write: hasil baca memori tidak memicu file, hasil kirim gagal tidak dianggap sukses, namespace agent berbeda, tool reset tersedia, dan persetujuan tetap diperlukan.
- **Container Google MCP benar-benar mengambil gambar CV asli melalui endpoint produksi baru:** HTTP 200, `image/jpeg`, dan hash/ukuran byte sama dengan lampiran terdaftar. Signed URL serta isi gambar tidak dicetak. Ini pembuktian akses lintas container yang sebelumnya gagal karena path lokal tidak tersedia.
- Tidak ada log JSON level warning/error/critical pada window pemeriksaan awal pasca-deploy. Resource staging final dibersihkan setelah validasi.

**Batas hasil:** pengujian produksi terakhir hanya membaca file dan menjalankan pemeriksaan tanpa mutasi. Tidak melakukan upload ulang ke Google Drive, mengirim WhatsApp, atau menghapus memori nyata. Upload Drive end-to-end dan percakapan ulang reset perlu dijalankan melalui agent oleh owner; mekanisme transfer file dan alur resetnya sudah diuji seperti di atas. File TXT yang dibuat saat kejadian lama tidak dihapus otomatis.

Untuk QA ulang: minta Arthur reset memori nomor yang dituju pada Tokyo8, periksa ringkasan dan beri persetujuan baru. Setelah reset berhasil, kirim CV gambar/PDF ke Tokyo8 dan periksa file asli di folder Drive tujuan. Tidak perlu mereset memori Arthur untuk memperbaiki kasus ini.

## Backup dan rollback

Backup final: `/home/clevio/backups/arthur-qa-20261016-074220`, direktori privat di luar Git.

- Database: **11.739.133 byte**, 137 entry archive; restore staging berhasil.
- Archive image: **301.554.166 byte**, dua image; integritas gzip CRC terverifikasi.
- Image sebelumnya: `sha256:02977cec95b16abf813a3ba8e2b9177a13122ea71a430724b1ed326c75d63613` (commit aplikasi `c11190a`). Perbaikan link owner tetap tersedia pada versi ini.

```bash
python3 scripts/arthur_qa_release.py rollback-production \
  --state /home/clevio/backups/arthur-qa-20261016-074220/state.private.json
```

Rollback mengganti API/scheduler dan environment tersimpan, tanpa menimpa database atau membatalkan aksi Google/WhatsApp. Setelah rollback, pointer `.env.prod` perlu dikembalikan ke `IMAGE_TAG=arthur-owner-link-hotfix-20261016-overlay` dan `APP_COMMIT_SHA=c11190a` sebelum deployment berikutnya. Snapshot credential, state privat, signed link, dan konten CV tidak masuk Git.

Kandidat awal yang dibatalkan disimpan sebagai catatan validasi pada backup `073735`; receipt rollout-nya sudah dinonaktifkan dan resource staging-nya dibersihkan. Jangan memakai state tersebut untuk deploy.
