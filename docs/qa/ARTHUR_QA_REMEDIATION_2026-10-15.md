# Arthur QA Batch 1–10: perbaikan dan batas rilis

Status: kandidat kode untuk staging/canary, **belum diterapkan ke produksi**. Perbaikan dibuat di branch `codex/meta-embedded-signup`, di atas baseline `e5468b4`. Tidak ada perubahan database produksi, reseed Arthur, reset percakapan, pengaktifan kembali agent, atau pengiriman pesan customer selama pengujian ini.

Analisis awal tetap tersedia di [laporan akar masalah](ARTHUR_BATCH_1_10_ROOT_CAUSE_ANALYSIS_2026-10-15.md). Temuan QA adalah laporan gejala; perbaikan berikut membedakan bug yang terbukti, kontrol yang memerlukan aktivasi bertahap, dan perilaku percakapan yang masih perlu diuji.

## Perubahan terhadap hasil QA

| QA | Perbaikan kode | Batas pembuktian / tindak lanjut |
| --- | --- | --- |
| 1. Eskalasi | Demo wajib memakai percakapan Arthur milik owner yang terverifikasi dan terpisah dari transport demo. Jika tidak tersedia, tidak mengirim kartu ke demo dan mengembalikan kegagalan. Sukses WhatsApp wajib punya `message_id`. | Uji penerima dan quoted reply nyata masih wajib. Nama WhatsApp ditandai sebagai nama pengirim; kode tidak mengarang nomor customer ketika owner sendiri melakukan tes. |
| 2. Link login owner | Guard jawaban terakhir berjalan setelah penambahan link otomatis. Tool balas customer dan kirim ke nomor lain menolak link otorisasi akun. Customer tidak mendapat tool koneksi owner. | Untuk owner yang sedang menyamar sebagai customer di demo, aktifkan customer preview pada agent canary. Akses owner di koneksi bisnis biasa tetap tersedia. |
| 3. Edit / knowledge | Snapshot instruksi, nama, model, dan konfigurasi dibaca ulang sebelum run. Tool pencarian knowledge tersedia pada agent dengan RAG aktif. Fallback pencarian mengabaikan metadata transport dan tidak mewajibkan semua kata pertanyaan muncul. Memori lama ditandai sebagai riwayat, bukan aturan. | Tidak menulis ulang SOP/harga lama. Konflik harga perlu keputusan owner; tidak otomatis memilih harga dari dokumen tanpa persetujuan. Akurasi jawaban LLM perlu replay QA. |
| 4. CS diam | Semua pembacaan status penting membedakan record aktif dari layanan dijeda. Demo mengirim pemberitahuan pause tanpa menjalankan LLM; gateway menerima fallback pause. | CS asli terbukti dijeda sebelum berhenti membalas, lalu dihapus. Tidak direstorasi atau di-resume otomatis. |
| 5. Klaim Arthur | Shortcut pra-LLM yang memasangkan nomor dan kutipan dari history dihapus. Klaim sukses kirim memerlukan hasil tool yang benar-benar terverifikasi, termasuk pada pertanyaan status. Exception pengiriman tidak lagi menjadi hasil sukses. Bukti brief/deployment dan knowledge kini bisa dibaca langsung. | Prompt mendorong verifikasi pause/resume dan melarang klaim tanpa bukti. Tidak ada jaminan semua kalimat LLM bebas halusinasi. Dugaan mengetahui pesan masa depan tidak terbukti pada audit awal. |
| 6. Memori customer | Scope menerima nomor biasa, awalan `phone:`, dan suffix WhatsApp yang sah. LID tidak diperlakukan sebagai nomor telepon. | Akses global dan lintas owner tetap dibatasi. |
| 7. Konfirmasi | Gate opsional menyimpan digest tool+payload, mengikat persetujuan ke pesan user berikutnya, serta menolak payload berubah, jeda percakapan, approval kedaluwarsa, dan pengulangan aksi pada pesan approval yang sama. Status sedang dieksekusi/hasil tidak pasti tidak boleh ditimpa perubahan lain. | Default mati; aktifkan per owner canary. Selama belum diaktifkan, kontrak konfirmasi lama tetap berlaku. Hasil tidak pasti perlu pemeriksaan aktual, bukan retry otomatis. |
| 8. Trial kedaluwarsa | Tanggal berlaku dinilai dengan timezone dan grace period. Snapshot menampilkan validitas term secara terpisah dari status lama. Enforcement dapat diaktifkan per user setelah audit. | Default hanya observasi. Akun lama tidak tiba-tiba diputus. Script audit hanya membaca; aktivasi cohort belum dilakukan. |
| 9. Aturan order / PO | Kontrak runtime menegaskan SOP terkini, batas pencatatan vs penerimaan pesanan, lead time, dan penghentian jawaban harga yang saling bertentangan. | Ini penguatan instruksi, bukan validasi transaksi khusus toko kue. SOP dan jawaban nyata harus ditinjau owner dalam canary. |
| 10. Interview | Jenis assistant yang tidak dikenal ditolak. Tujuan dengan indikator tugas customer/order yang jelas tetap membutuhkan lima field workflow meski diberi label personal. | Deteksi frasa tidak mencakup semua maksud bahasa alami. Interview percakapan tetap harus diuji; assistant personal sederhana tetap didukung. |
| 11. Heartbeat | Alias `timedelta` diperbaiki; 60 menit membuat jadwal yang benar. Interval yang tidak dapat direpresentasikan tepat ditolak, tidak diam-diam dibulatkan. | Pengiriman heartbeat terjadwal nyata belum dijalankan. |
| 12. Daftar harga | Arthur mendapat tool list/read knowledge yang tersimpan, dengan pagination dan pemeriksaan ownership. Tidak dapat mengirim file asli tidak berarti isi knowledge tidak ada. | Penyimpanan teks/chunk tidak menjamin PDF asli tersedia. Fitur ekspor/kirim ulang PDF asli tidak ditambahkan. |
| 13. Brief lama | Tool inspeksi membaca brief/workflow terakhir dan bukti URL tersimpan milik owner, termasuk agent yang soft deleted. | Tidak merestorasi agent. Bukti URL kosong berarti belum diketahui, bukan belum pernah deploy. |
| 14. Info internal | Kontrak Arthur melarang ID, nama tool, versi, kuota teknis, dan error mentah pada jawaban biasa. Error pengiriman baru menyembunyikan detail provider; log penambahan OAuth tidak merekam preview jawaban/link. | Bukan redaksi deterministik untuk semua respons. QA kebocoran informasi dan formatting tetap wajib. |
| 15. Sapaan | Nama tampilan generik Agent/Admin/User tidak dijadikan sapaan otomatis. Instruksi tone/sapaan terbaru didahulukan. | Nama WhatsApp asli tetap data pengirim, bukan identitas yang diciptakan sistem. Konsistensi bahasa perlu QA percakapan. |
| 16. Link Meta | Instruksi membedakan halaman penghubung milik platform dari domain resmi Meta. | Domain dan flow aktual tidak diubah. |
| 17. Peringatan berulang | Instruksi melarang mengulang peringatan akses dan upsell yang tidak relevan di tengah workflow. | Perlu replay percakapan panjang; tidak mengubah paket/billing customer. |
| 18. Info kontradiktif | Readback status/knowledge/brief tersedia. Hak Google yang sudah disimpan secara eksplisit tidak diperluas oleh prosa runtime. Penambahan layanan mempertahankan hak layanan lama; Sheets probe menerima hasil terstruktur dan menolak payload error. | Katalog paket tidak diubah karena audit menemukan kode dan data katalog sesuai. Scope baru perlu OAuth yang sesuai; tool filter bukan pencabutan token OAuth lama. |
| 19. Fitur kecil | Impor dengan beberapa file meminta pilihan, tidak menebak file terbaru. Scheduler bisa diaktifkan lewat konfigurasi terkonfirmasi, dan tidak bisa dimatikan saat ada job aktif. Bentuk cron yang ekuivalen satu menit ditolak untuk autonomous run. | Pembuatan spreadsheet baru oleh Arthur belum ditambahkan; jawaban diarahkan untuk jujur tentang kemampuan itu. Perapian semua format output belum dianggap lolos QA. |

Arthur V2 memiliki 35 core tools: 32 tools sebelumnya dipertahankan urutannya, lalu tiga tools inspeksi ditambahkan. Kontrak operasi disisipkan saat runtime, sehingga perbaikan tidak memerlukan penggantian system prompt yang tersimpan di database.

## Kontrol perubahan bertahap

Semua pengaturan berikut memiliki default aman untuk rollout. Tidak ada `.env` produksi yang diedit.

| Pengaturan | Default | Penggunaan canary |
| --- | --- | --- |
| `ARTHUR_CONFIRMATION_OWNER_IDS` | `[]` | Daftar nomor owner terverifikasi yang memakai preview + approval pada giliran berikutnya. Bukan UUID agent. |
| `DEMO_CUSTOMER_PREVIEW_AGENT_IDS` | `[]` | Daftar UUID agent demo yang memperlakukan owner sebagai customer untuk tes. Alternatif: properti per-agent `demo_customer_preview` melalui konfigurasi runtime terkonfirmasi. |
| `SUBSCRIPTION_EXPIRY_ENFORCEMENT_USER_IDS` | `[]` | Daftar UUID user subscription yang telah ditinjau. Bukan nomor WA atau UUID agent. |
| `SUBSCRIPTION_EXPIRY_ENFORCEMENT_ENABLED` | `false` | Enforcement global; jangan aktifkan sebelum audit dampak dan canary selesai. |

Koneksi Google baru/reconfigurasi yang disetujui mencatat `operation_policy_explicit`. Penambahan Calendar/Gmail tidak otomatis memperluas hak Sheets lama. Operasi eksplisit dapat diberikan lewat `google_workspace_operations`. Koneksi lama tidak dimigrasikan massal. Pengurangan scope pada konfigurasi runtime tidak dengan sendirinya mencabut scope token OAuth yang pernah diterbitkan.

Audit subscription tersedia melalui `python -m scripts.audit_subscription_expiry_readonly` pada lingkungan target. Query berada dalam transaksi `READ ONLY`, tidak mengubah status/deadline, dan default hanya mencetak jumlah. `--include-user-ids` khusus pemeriksaan cohort; tidak mencetak email/nomor telepon.

## Validasi

Hasil akhir: **466 passed, 2 deselected**, satu peringatan deprecation LangGraph, dari 29 file test dalam 40,92 detik. Ini mencakup dua tes konkurensi PostgreSQL dan 36 tes API subscription. Tes memakai checkout read-only di container disposable, jaringan internal terisolasi, SQLite untuk state tools, dan PostgreSQL disposable untuk API subscription serta konkurensi. Tidak menggunakan database atau kredensial produksi.

- `go test ./...` pada `wa-dev-service` lulus.
- AST 32 file Python yang berubah valid; `git diff --check` bersih.
- Compose kandidat lolos validasi struktur tanpa resolusi environment/secret; API dan scheduler memakai referensi image yang sama, tanpa build otomatis.
- Hash seluruh 29 file aplikasi/script yang berubah cocok antara checkout dan image kandidat.
- Import aplikasi API, worker scheduler, script audit, dan schema 35 core tools berhasil di image tanpa jaringan. Seluruh flag rollout terverifikasi kosong/mati secara default.
- Script audit read-only berhasil dijalankan pada database disposable. Tidak dijalankan pada database produksi dalam tahap implementasi ini.

Lingkungan Python: image baseline aplikasi, Python 3.12, pytest 8.4.2, pytest-asyncio 0.26.0, aiosqlite 0.22.1; database tes PostgreSQL 16. Database disposable bernama `arthur_p1_test` pada host `arthur-p1-test-postgres`; tes konkurensi memiliki guard agar tidak dijalankan pada produksi. Schema subscription dan katalog default disiapkan hanya pada database disposable sebelum tes API.

Image lokal yang diverifikasi: `managed-agents-app:arthur-qa-remediation-20261015-r1`.

ID immutable kandidat: `sha256:d9ab842ad9326502b1c85a18e4c08d3a02542ea5b9826bc96ef020c231317469`.

Base image: `managed-agents-app:arthur-knowledge-contract-20261015`. Build final memakai Dockerfile remediation dengan `--network none --no-cache --provenance=false`. Tidak menambahkan dependency runtime atau menyalin `.env`; tidak mengganti image service yang sedang berjalan. Image gateway Go belum dibangun/dijalankan sebagai service baru.

Kasus lama berikut dipisahkan secara eksplisit; ini bukan klaim seluruh test suite repository hijau:

- `test_whatsapp_direct_send.py::test_builder_prompt_blocks_repeated_continue_questions`: membandingkan rangkaian kalimat prompt legacy yang sudah berbeda pada baseline produksi. Reproduksi pada image baseline gagal juga; assertion lama tidak diganti agar terlihat hijau.
- `test_subscriptions_api.py::TestActivateSubscription::test_trial_no_expiry`: mengharapkan trial tanpa expiry, sedangkan baseline sudah membuat expiry 14 hari. Reproduksi pada image produksi lama gagal sama.
- Tiga fixture legacy `test_builder_tools.py` gagal juga pada baseline: `TestCreateAgent::test_arthur_creates_minsel_after_confirmed_discovery_replay`, `TestCreateAgent::test_create_agent_with_google_workspace_config_appends_instruction`, dan `TestUpdateAgent::test_generate_google_auth_link_reports_timeout_with_public_tunnel_context`. Kegagalan terkait mock async/query lama; tidak dicampur dengan suite Arthur V2.

Impor builder legacy yang sebelumnya menunjuk helper yang sudah hilang diperbaiki agar modul dapat dikoleksi. Helper deteksi Google Forms hanya dipulihkan untuk planning; tidak mengembalikan override runtime berbasis keyword yang sudah dihapus.

## Gerbang rollout

1. Tinjau diff dan hasil otomatis. Gunakan image kandidat yang hash source-nya cocok dengan checkout yang diuji; pin ID image immutable, bukan tag `latest`.
2. Buat backup terverifikasi sesuai prosedur operasional: database, konfigurasi deployment, dan image lama. Jangan menguji pemulihan dengan menimpa database produksi.
3. Jalankan staging dengan database/Redis/WA terpisah, job/outbound produksi nonaktif, dan akun uji Google terpisah. Default flags tetap kosong/mati.
4. Review daftar subscription lewat audit read-only. Putuskan koreksi data atau grace yang sah secara eksplisit sebelum menambahkan user ke cohort expiry; jangan memperpanjang semua trial otomatis.
5. Aktifkan satu owner dan agent demo canary. Uji preview → approval → satu efek samping, perubahan payload, pembatalan, timeout, status pause/resume, dan konflik knowledge/SOP. Uji owner serta customer memakai identitas dan chat terpisah, termasuk quoted reply eskalasi.
6. Ulangi Batch 1–10 dengan bukti tool/run/penerima nyata. Tes policy “agent jual obat keras” tetap **NOT_RUN** sampai dijalankan secara eksplisit. Jangan menganggap perubahan prompt sebagai bukti lolos.
7. Setelah lolos, drain run dan antrean yang sedang berjalan sebelum mengganti API dan scheduler bersama-sama. Override kandidat berada di `deploy/docker-compose.arthur-qa-remediation.yml` dan mewajibkan `ARTHUR_QA_IMAGE` diisi. Override tidak dipakai otomatis oleh deployment yang ada. Tidak ada migrasi schema pada rilis ini.
8. Pantau penerima eskalasi, link akun pada respons customer, rasio tidak membalas/error, antrean, job ganda, dan hasil tool. Perluas cohort bertahap hanya setelah stabil.

Rollback: hentikan perluasan cohort, drain pekerjaan baru, lalu kembalikan **API dan scheduler** ke ID image sebelumnya bersama konfigurasi deployment sebelumnya. Tidak menghapus dokumen, memori, revisi, atau mengulang outbound sebagai bagian rollback. Rollback kode tidak membatalkan pesan atau perubahan Google yang sudah terjadi. Aksi dengan hasil tidak pasti harus diperiksa terhadap status aktual sebelum dicoba lagi.

Gateway Go mendapat dukungan fallback `agent_paused`; API kandidat juga mengirim notice demo langsung agar notice tidak bergantung pada upgrade gateway serentak. Receipt notice membuat reply API kosong sehingga gateway baru tidak mengirim duplikat.

Kasus manual di [checklist QA](ARTHUR_QA_CASES_2026-10-15.csv) tetap tidak ditandai lulus tanpa pelaksanaan ulang. Laporan ini tidak mengklaim keenam P0 telah lulus end-to-end atau sistem sudah aman untuk rollout global.
