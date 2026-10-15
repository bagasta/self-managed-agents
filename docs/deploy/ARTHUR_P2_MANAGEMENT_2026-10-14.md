# Arthur P2 management — 14 Oktober 2026

P2 sudah dideploy pada **20:27 WIB** ke API dan scheduler dengan image `managed-agents-app:arthur-p2-20261014`. Arthur memiliki **32 tool inti**: 16 tool awal, enam P1, dan sepuluh P2. Implementasi serta signature tool lama tetap tersedia.

## Tool baru

| Tool | Kemampuan dan batasnya |
|---|---|
| `set_assistant_paused` | Jeda/aktifkan layanan tanpa menghapus agent atau melepas koneksi. Jeda membatalkan antrean balasan dan menjeda jadwal aktif. Aktivasi default hanya mengaktifkan layanan. Owner dapat memilih `resume_schedules=true` untuk jadwal yang dijeda oleh tindakan ini, dengan waktu berikutnya di masa depan; jadwal yang dijeda manual tidak ikut aktif dan backlog tidak dikirim ulang. |
| `get_assistant_access` | Membaca owner, operator, operator eskalasi, akses pelanggan, dan versi konfigurasi. |
| `configure_assistant_access` | Menambah/menghapus operator dan memilih akses pelanggan `all`, `allowlist`, atau `operators_only`. Membutuhkan versi terkini serta konfirmasi. Owner tidak dapat dihapus. Penghapusan operator eskalasi memperbarui kedua sumber hak akses agar nomor tersebut tidak diam-diam tetap menjadi operator. |
| `manage_assistant_google_connection` | Memeriksa status akun Google, melepas integrasi agent tertentu, atau memulai koneksi ulang melalui OAuth owner. Integrasi dan koneksi agent lain tidak diubah. |
| `set_assistant_google_spreadsheet` | Memeriksa metadata spreadsheet dengan koneksi owner, lalu mengganti target yang dikonfigurasi. Referensi ID target lama pada instruksi diganti secara spesifik. Tidak membuat atau menulis spreadsheet. Resource dengan kontrak customer append-only khusus ditolak untuk ditinjau terpisah. |
| `inspect_assistant_memory` | Membaca satu scope nomor pelanggan atau global, dengan pagination, preview, serta etag nilai lengkap. Tidak mencampurkan pelanggan atau membuka ruang arsip administrasi. |
| `edit_assistant_memory` | Koreksi/hapus satu item berdasarkan ID dan etag, setelah konfirmasi. Nilai lama diarsipkan; data yang sudah berubah tidak ditimpa. |
| `reset_assistant_context` | Mengarsipkan memori satu pelanggan dan mereset konteks aktif sesi terkait, dengan pemeriksaan jumlah item. Riwayat chat tetap tersimpan; dokumen, koneksi, shared soul/global memory, dan pelanggan lain dipertahankan. Reset massal global tidak tersedia. |
| `list_assistant_revisions` | Membaca revision konfigurasi yang mulai direkam oleh rilis ini, tanpa mengarang riwayat sebelum pengarsipan tersedia. Credential dan konfigurasi rahasia tidak ditampilkan. |
| `rollback_assistant_revision` | Preview lalu mengembalikan field yang dipilih dari revision milik agent tersebut, setelah konfirmasi dan pemeriksaan versi. Rollback menghasilkan versi baru; tidak memundurkan counter versi. |

## Kontrol layanan dan akses

Flag kontrol disimpan terpisah pada `tools_config.arthur_service_controls`. Agent tanpa flag baru mempertahankan perilaku admission yang sebelumnya berlaku. Jeda berlaku terhadap semua pengguna agent tersebut, termasuk owner; pengelolaannya dilakukan dari Arthur yang tidak dapat menjadi sasaran tool ini.

Guard ditambahkan pada ingress WhatsApp/generic/Meta, runtime sebelum model, scheduler, dan antrean outbound. Runtime me-refresh konfigurasi kontrol dari database agar request yang tertunda tidak memakai snapshot ORM lama. Jadwal yang dibuat ketika agent sedang dijeda juga ditahan scheduler. Reminder tidak dapat melewati admission pelanggan yang dikelola melalui P2.

Perubahan administrasi yang memerlukan kondisi tenang ditolak saat agent memiliki run aktif; perubahan koneksi/jeda juga memeriksa pekerjaan/pengiriman berjalan. Jeda tidak berjanji membatalkan aksi provider yang sudah dikirim sebelum pemeriksaan tersebut.

**Pengguna non-owner tetap bisa memakai tool Google sesuai workflow agent.** Admission menentukan siapa yang dilayani; membership operator menentukan siapa yang mengelola. Memilih akses pelanggan `all` tidak mengubah izin Google, tidak memberi hak operator, dan tidak mengganti akun Google owner.

## Google

Disconnect mengirim permintaan agent-scoped ke integration service yang ada, menonaktifkan record integrasi agent tersebut, dan melepas connector lokal. Ini **bukan pencabutan seluruh grant akun di Google**. Connector lain serta resource spreadsheet tetap tersimpan; backup kebijakan connector disimpan terenkripsi di ruang administrasi untuk reconnect. Jika layanan remote gagal, konfigurasi lokal tidak diklaim telah berubah.

Reconnect mengembalikan kebijakan connector yang tersimpan dan menghasilkan link OAuth melalui mekanisme resmi yang sudah ada. Owner tetap memilih akun di Google; link bukan bukti koneksi aktif.

Pergantian spreadsheet memverifikasi ID dan metadata resource dengan koneksi yang ada, kemudian menyimpan ID/URL/judul target serta versi baru. Tool ini tidak menjamin bahwa susunan tab/header spreadsheet baru cocok dengan SOP lama; owner perlu meninjau workflow apabila struktur target berbeda. Tidak ada perluasan scope/layanan Google otomatis.

## Memori dan revision

- Ruang arsip `__arthur_control_v2__` terpisah dari scope global/nomor pelanggan yang dimuat oleh runtime sebagai recall. Memori administratif tidak ikut menjadi bias percakapan.
- Arsip memakai enkripsi channel yang sudah tersedia; kunci enkripsi produksi telah diverifikasi tersedia tanpa menampilkan nilainya.
- Variasi identitas nomor umum (`62…`, `+62…`, `08…`, serta JID telepon) diperlakukan sebagai satu pelanggan saat inspeksi/reset. Opaque LID tidak ditebak menjadi nomor.
- Reset memberi batas waktu konteks baru. Pengambilan history memfilter batas tersebut **sebelum limit**, sehingga pesan lama dengan step index lebih tinggi tidak mengalahkan percakapan baru. Ekstraksi memori selanjutnya memakai history setelah batas reset; cached summary lama tidak dipakai kembali.
- Riwayat pesan tetap tersimpan untuk audit. Item memori yang dikoreksi/dihapus diarsipkan terlebih dahulu. Item global dapat dikoreksi satu per satu; reset satu pelanggan tidak menghapus global persona atau pengetahuan RAG.
- Listener SQLAlchemy merekam snapshot sebelum/sesudah perubahan konfigurasi pada transaksi yang sama dengan perubahan agent. Perubahan kuota/token saja tidak menghasilkan revision.
- Counter versi dinaikkan monoton untuk perubahan konfigurasi, termasuk jalur runtime yang sebelumnya belum menaikkannya. Caller yang sudah menaikkan versi tidak dikenai kenaikan ganda.
- Snapshot didasarkan pada row database yang dikunci, dengan perubahan field aktual, agar dua updater yang membawa snapshot ORM lama tetap menghasilkan revision yang tepat.
- Rollback hanya menerima field eksplisit: nama, tujuan/deskripsi, instruksi, model, temperatur, batas output, atau tools configuration. Ownership, API key, kuota, binding/kredensial channel, operator/allowlist, dan memori tidak dipulihkan oleh rollback konfigurasi.
- Saat tools configuration dikembalikan, credential connector saat ini dan state kontrol jeda/admission dipertahankan. Policy agent yang berlaku juga diperiksa sebelum mengembalikan instruksi lama.

Tidak ada migrasi database. Revision serta backup memakai tabel memory yang sudah ada dengan scope administrasi tersendiri. Pengarsipan historis dimulai dari rilis ini: kondisi sebelum perubahan pertama dicatat ketika perubahan tersebut terjadi.

## Validasi

**317 tes unik lulus**, terdiri dari pengujian P2, P1, engine/Google, memori, quota, Meta webhook, session/history, scheduler, dan antrean outbound. Perubahan terakhir pada guard reminder diuji ulang bersama seluruh tes P2 dan tes scheduler/outbound yang terkait.

Tes mencakup:

- Konfirmasi/ownership dan perlindungan owner dari penghapusan akses.
- Jeda/aktivasi tanpa putus koneksi, penolakan saat busy, serta resume jadwal secara opt-in tanpa menghidupkan jadwal yang dijeda manual.
- Request yang membawa ORM state lama berhenti sebelum model ketika status jeda di database sudah berubah.
- Penghentian runtime, webhook, scheduled job, reminder, dan queue delivery ketika flag baru memblokir layanan/pelanggan.
- Pelanggan admitted tetap tidak membutuhkan role owner untuk menjalankan Google workflow.
- Memory etag, arsip nilai lama, penolakan scope administrasi, isolasi pelanggan/global memory, preservasi chat, serta history filter sebelum limit.
- Snapshot terenkripsi dan rollback transaksional, version guard, serta preservasi credential/kontrol aktif.
- Google disconnect yang hanya menyasar agent tertentu dan mempertahankan connector lain; kegagalan remote tidak dianggap berhasil.
- Dua tes PostgreSQL terisolasi: transfer nomor bersamaan tetap menyisakan tepat satu pemilik, dan dua perubahan konfigurasi bersamaan menghasilkan versi monoton dengan snapshot terbaru yang memuat keduanya.

Mutation testing dilakukan di database terpisah dengan provider palsu. Pada produksi, empat tool baca baru (`get_assistant_access`, `inspect_assistant_memory`, `list_assistant_revisions`, status Google) berhasil sebelum dan sesudah deploy. Pemeriksaan sebelum deploy memastikan jumlah arsip administrasi tidak berubah akibat probe baca.

Tidak ada nomor produksi dilepas/dipindah, akun Google produksi diputus, akses pelanggan diubah, memori pelanggan direset, atau spreadsheet ditulis selama pengujian dan deployment fitur. Alur mutasi lengkap melalui chat WhatsApp pengguna nyata belum dijalankan pada saat laporan dibuat.

## Deployment

- Image aktif API/scheduler: `managed-agents-app:arthur-p2-20261014`.
- Base: `managed-agents-app:arthur-p1-20261014`, mempertahankan perbaikan sebelumnya.
- Sebelum penggantian: run running/pending = 0, scheduled job running = 0, outbound sending = 0.
- API dan scheduler direcreate dengan graceful shutdown yang tersedia. Scheduler ikut diperbarui agar aturan jeda/admission juga berlaku pada eksekusi background.
- Kedua layanan WhatsApp mempertahankan waktu start 14 September dan tidak direstart.
- Sebelas source file di API cocok dengan patch final; image final mengimpor aplikasi dan mendaftarkan 32 tool.
- API Docker health `healthy`; endpoint publik HTTP 200; `python -m app.scheduler_healthcheck` berhasil. Pemeriksaan log awal API/scheduler tidak menemukan error/traceback/HTTP 500.
- Tidak ada migrasi atau seed data.

Rollback image tersimpan sebagai `managed-agents-app:rollback-arthur-p2-api-20261014` dan `managed-agents-app:rollback-arthur-p2-scheduler-20261014`. Image lama belum memahami flag P2; bila kontrol P2 sudah digunakan, state jeda/admission perlu ditinjau sebelum melakukan downgrade binary. Ini berbeda dari rollback konfigurasi agent yang tersedia melalui tool dan tetap mempertahankan kontrol aktif.
