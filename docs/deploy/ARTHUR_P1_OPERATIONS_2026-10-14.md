# Arthur P1 operations — 14 Oktober 2026

Sudah dideploy ke API pada **19:52 WIB** dengan image `managed-agents-app:arthur-p1-20261014`. Registry kini berisi 22 tool inti: 16 tool lama ditambah enam tool baru.

## Tool yang ditambahkan

| Tool | Perilaku |
|---|---|
| `disconnect_assistant_whatsapp` | Melepas koneksi nomor tanpa menghapus agent. Untuk Cloud API, hanya binding lokal yang dilepas; tidak melakukan deregistrasi Meta atau mematikan WhatsApp Business App. Legacy QR menghapus perangkat di wa-service sebelum binding dilepas. |
| `transfer_assistant_whatsapp` | Memindahkan binding ke agent lain milik owner yang sama. Target harus belum terhubung. Perpindahan database atomik, menjaga keunikan nomor/device, dan mempertahankan mode Coexistence. Tidak membuat QR atau registrasi Meta baru. |
| `diagnose_assistant` | Membaca konfigurasi, koneksi Google, status WhatsApp, lima run terakhir, antrean pengiriman, dan status jadwal. Tidak mengubah konfigurasi atau membuat OAuth link. Status live Cloud API ditandai belum diverifikasi, bukan diklaim sehat hanya karena credential ada. |
| `test_assistant_google_tools` | Membaca range terbatas pada spreadsheet yang dikonfigurasi agent. Secara default tidak menulis dan tidak mengembalikan isi sel. Nilai dapat diminta owner dengan `include_values=true`. |
| `prepare_assistant_run_recovery` | Memeriksa run gagal/fallback. Dengan range dan nilai eksplisit, membaca nilai terkini dan membuat rencana before/after yang berlaku sepuluh menit untuk ditinjau owner. Tidak menulis spreadsheet. |
| `recover_assistant_run` | Menjalankan rencana yang disetujui owner dengan token rencana. Membandingkan data terbaru sebelum menulis, lalu membaca ulang hasilnya. Tidak mengulang model, append, notifikasi, atau keseluruhan workflow. |

## Perilaku koneksi dan data

- Pengelolaan hanya berlaku untuk agent operasional milik pemanggil. Agent Arthur/builder dan agent milik orang lain tidak boleh menjadi sasaran.
- Pemutusan/pemindahan membutuhkan konfirmasi yang mencakup pembatalan pesan antre dan jeda jadwal terkait. Pekerjaan, pengiriman, atau jadwal yang sedang berjalan menyebabkan operasi ditolak.
- Referensi transport dan credential WhatsApp pada sesi lama dilepas. Riwayat pesan, instruksi, tools, pengetahuan, Google OAuth, serta kepemilikan agent tetap tersimpan. Riwayat sesi tidak dipindahkan ke agent baru.
- Kedua agent pada transfer diperiksa dan dikunci dengan urutan konsisten. Binding asal dilepas dan di-flush sebelum binding target diisi; semuanya dalam satu transaksi. Kegagalan update target mengembalikan binding asal lewat rollback.
- Transfer nomor yang diarahkan ke n8n ditolak agar routing eksternal tidak dipindah tanpa konfigurasi yang tepat. Agent dengan dua transport sekaligus juga memerlukan pemeriksaan terpisah.
- Kegagalan remote disconnect legacy mempertahankan binding lokal. Karena logout provider dan transaksi database bukan satu transaksi bersama, kegagalan database setelah logout remote tetap memerlukan rekonsiliasi; tool tidak mengklaim berhasil bila hasilnya tidak terverifikasi.

## Batas dan pengamanan Google

Kemampuan pemeriksaan resource dan pemulihan pada rilis ini berfokus pada **Google Sheets**, sesuai insiden yang dilaporkan. Layanan Google lainnya tetap memakai tool lama; diagnosis dapat melaporkan koneksi/layanan yang dikonfigurasi, tetapi tidak mengklaim telah menguji baca/tulis semua produk Google.

- Menggunakan koneksi Google owner dan policy layanan/operasi yang sudah ada. Koneksi yang dinonaktifkan serta policy read-only tidak diberi izin tulis baru. Bearer runtime disuntikkan hanya pada salinan konfigurasi.
- Uji tulis membutuhkan persetujuan eksplisit dan satu sel kosong pada tab khusus berawalan `Arthur_Test`. Marker acak dibaca ulang lalu dibersihkan hanya jika isi sel masih marker yang sama. Jika tidak ada tab khusus tersebut, owner perlu menyiapkannya terlebih dahulu; tool tidak membuat atau mengubah struktur sheet secara diam-diam.
- Range harus eksplisit dengan nama tab, maksimal 100 sel per range. Probe baca default tidak menampilkan data peserta. Tidak ada notifikasi atau pesan pelanggan yang dikirim oleh tool probe/recovery.
- Pemulihan hanya menerima 1–5 range yang tidak tumpang tindih dan nilai eksplisit; owner harus meninjau nilai sebelum/sesudah terlebih dahulu. Tidak ada model yang menebak lalu mengeksekusi sendiri rencana yang belum ditinjau.
- Run harus merupakan run terbaru pada sesi, gagal atau memiliki fallback yang belum selesai. Sesi yang dijeda, eskalasi manusia, atau agent dengan approval khusus tidak dipulihkan melalui jalur ini.
- Token rencana, versi konfigurasi, resource spreadsheet, dan perubahan percakapan diperiksa lagi sebelum eksekusi. Perubahan data sejak persiapan menyebabkan penolakan, bukan penimpaan.
- Percobaan ulang memakai rencana yang sama: nilai yang sudah cocok dilewati. Jika respons tulis timeout tetapi data sebenarnya tersimpan, pembacaan berikutnya dapat memverifikasinya tanpa menulis ulang.
- Rencana/hasil verifikasi dicatat pada `Run.runtime_metadata`; status historis run asal tidak ditulis ulang menjadi seolah berhasil pada percobaan pertama. Tidak ada migrasi database.
- Operasi dibatasi 90 detik. Claim pemulihan yang tertinggal karena crash baru dapat diperiksa ulang setelah 120 detik, tetap dengan perbandingan nilai secara langsung dan rencana yang sama.

## Preservasi perilaku yang sudah ada

Perbandingan byte dengan plugin produksi sebelum deploy menunjukkan bahwa, setelah dua baris registrasi tambahan dihilangkan, `arthur_v2/plugin.py` identik dengan versi aktif sebelumnya. Semua implementasi dan urutan 16 tool lama dipertahankan.

Tiga modul baru dipisahkan dari runtime lama. Tidak ada perubahan baru pada engine agent, autentikasi Google, aturan penggunaan tool oleh pelanggan non-owner, endpoint API lama, instruksi agent tersimpan, atau seed data. Image menggunakan base aktif `tool-workflow-recovery-20261014`, sehingga seluruh perbaikan sebelumnya tetap terbawa.

## Verifikasi

**173 tes unik lulus:** 39 tes operasi P1, satu tes concurrency di PostgreSQL terpisah, dan 133 tes regresi Arthur/Google/WhatsApp yang relevan.

Cakupan mencakup owner/agent protection, konfirmasi, busy run, target sudah terhubung, Cloud/legacy transfer, rollback jika update target gagal, pembersihan transport sesi, antrean/jadwal, probe tanpa tulisan, penolakan sel uji berisi, cleanup marker setelah timeout, policy read-only, perbandingan data, token/versi/percakapan/kedaluwarsa rencana, retry tanpa tulisan ganda, serta pemulihan setelah crash.

Tes PostgreSQL mencoba dua transfer paralel dari nomor asal ke dua target berbeda. Tepat satu berhasil dan hanya satu target memiliki nomor setelahnya. Database pengujian berada di container khusus, bukan database produksi.

Sebelum dan sesudah deploy, probe baru berhasil membaca header spreadsheet LapiBot dengan `writes_executed=0`. Diagnosis membaca koneksi Google yang aktif. Pemeriksaan dari image final serta runtime produksi menunjukkan 22 tool terdaftar; empat file source cocok dengan patch yang diuji. Endpoint health publik mengembalikan HTTP 200.

Tidak ada nomor produksi diputus/dipindah, jawaban peserta ditulis ulang, atau pesan dikirim selama pengujian dan deployment fitur. Pengujian mutasi dilakukan dengan database terisolasi serta provider/tool palsu. Fitur mutasi melalui percakapan WhatsApp nyata belum dijalankan terhadap nomor pengguna pada saat laporan dibuat.

## Deployment dan rollback

- API direcreate setelah pemeriksaan menunjukkan nol run aktif, dengan graceful shutdown yang sudah dikonfigurasi.
- Scheduler serta kedua layanan WhatsApp mempertahankan waktu start sebelumnya.
- Tidak ada migrasi atau seed.
- Rollback tersedia: `managed-agents-app:rollback-arthur-p1-20261014`.

Contoh permintaan ke Arthur: “Cek kesehatan LapiBot”, “Uji baca spreadsheet LapiBot”, atau “Pindahkan nomor dari agent A ke agent B”. Untuk tindakan koneksi, Arthur perlu memastikan kedua agent serta menjelaskan efek terhadap antrean dan jadwal sebelum menjalankan tool.
