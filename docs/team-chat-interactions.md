# Team Chat: komputer, aktivitas bot, dan respons interaktif

Implementasi lanjutan berdasarkan tiga screenshot pengguna. **Belum dites atau diperiksa lewat browser**, sesuai instruksi pengguna. Bukti tes dan screenshot pada laporan UI sebelumnya tidak mencakup perubahan ini.

## Komputer

Preview di panel detail mengikuti rasio framebuffer asli, tanpa bingkai 16:10 yang memaksa ukurannya. Tombol dan teks disusun ulang. Preview dapat diklik untuk membuka dialog besar; komputer juga dapat dibuka dari header percakapan. Kendali langsung tetap memakai viewer yang dikonfigurasi runtime.

## Komputer bersama di grup

Sesuai klarifikasi pengguna, semua bot memakai **satu komputer secara bergantian**. Izin komputer tetap per bot dan berlaku untuk chat pribadi maupun grup. Tidak ada pembuatan desktop pribadi atau pengaktifan izin seluruh anggota secara otomatis.

Jalur builder Arthur sebelumnya membuang tool komputer meskipun izin bot aktif. Setup dan penyaringan tool sekarang mempertahankan tool komputer yang sudah diizinkan, dengan identitas owner dari sesi Arthur. Prompt grup menyebut anggota yang memiliki izin dan mengarahkan delegasi tugas yang sudah diminta kepada anggota tersebut jika bot penerima tidak memiliki akses.

Antrean FIFO mengambil giliran pada aksi/screenshot pertama dan mempertahankannya selama run agar rangkaian navigasi tidak disela bot lain. Giliran dilepas ketika run selesai, gagal, dibatalkan, meminta takeover, atau memanggil `computer_release_turn`. Bot yang mendapat giliran berikutnya diarahkan mengambil screenshot baru. Batas tunggu default 600 detik dapat diatur melalui `computer_runtime_turn_wait_seconds`; timeout antrean dibedakan dari komputer offline. Lease giliran kedaluwarsa setelah 1.800 detik tanpa pembaruan; entri proses yang mati dibersihkan saat metadata diakses.

Panel menampilkan pemegang giliran dan jumlah bot menunggu. Pemilihan anggota di panel mengatur izin bot tersebut, bukan kepemilikan desktop. Sesi input pribadi tetap memblokir aksi bot. Antrean memakai filesystem dan identitas VNC yang sama seperti kunci input pribadi; beberapa container memerlukan direktori metadata bersama.

Perubahan ini hanya implementasi sumber, belum diuji. Backend perlu memuat ulang kode baru dan halaman perlu dimuat ulang untuk memakai UI terbaru.

## Avatar bekerja

Status roster menyertakan aktivitas run Team Chat dan langkah workforce yang sedang berjalan di workspace tersebut. Frontend menyegarkan aktivitas setiap lima detik saat tab terlihat, menggabungkannya dengan status pesan dan permintaan yang sedang dikirim.

Avatar yang aktif melompat dua kali, berkedip, melirik kiri/kanan, dan melambaikan tangan. Status bekerja juga diberi teks. Animasi berhenti ketika pekerjaan selesai atau tab tersembunyi; preferensi reduced motion tetap dihormati.

## Pilihan respons

Bot dapat menghasilkan blok `chat-actions` berisi pertanyaan, 2–4 pilihan jawaban, dan label detail tambahan opsional. UI menampilkannya sebagai tombol dan input, bukan kode JSON. Respons lama dengan pola eksplisit seperti `balas "ya, buat"` juga mendapatkan tombol respons.

Klik mengirim jawaban lewat endpoint chat biasa kepada bot pengirim pertanyaan. Draft composer utama tidak diganti. Pertanyaan yang sudah dilewati pesan owner berikutnya tidak lagi menampilkan tombol aktif. Kartu ini tidak menyetujui approval-tool secara tersembunyi.

## Input pribadi

Alur pengguna:

1. Buka komputer atau pilih **Isi data pribadi di komputer** dari kartu chat.
2. Mulai sesi **Isi data pribadi**. Runtime memblokir aksi dan screenshot dari tool bot selama sesi ini aktif, paling lama lima menit.
3. Pilih kolom tujuan pada gambar komputer. Tombol panah dan Enter juga dapat dipakai pada gambar.
4. Isi nilai pada form bertipe password, lalu klik **Isi kolom pilihan**. Opsi penggantian isi kolom tersedia.
5. Periksa hasil melalui komputer dan selesaikan login/submit sendiri. Klik **Selesai · lanjutkan akses bot** untuk melepas sesi.

Nilai dikirim melalui endpoint khusus, tidak melalui pesan, memory, atau tool model. Worker menerima nilai lewat stdin, tidak melalui argumen proses, environment, atau file. Output worker dibuang, error tidak memuat nilai, dan field browser dikosongkan setelah dikirim/ditutup. Endpoint memerlukan autentikasi owner/admin, HTTPS atau localhost, dan mengembalikan `Cache-Control: no-store`.

Pengisian menargetkan koordinat yang dipilih pengguna; tidak ada deteksi DOM, verifikasi jenis kolom, submit otomatis, atau klaim bahwa login berhasil. Isi kolom dapat terlihat pada website tujuan sesuai jenis kolomnya. Setelah sesi pribadi dilepas, bot kembali bisa melihat layar komputer. Sesi yang tabnya ditutup tanpa pelepasan akan kedaluwarsa paling lama lima menit.

Lease dan kunci proses berlaku untuk worker pada filesystem runtime yang sama, dengan identitas host/port VNC yang sama. Deployment beberapa container/host yang mengendalikan desktop yang sama perlu storage lease/lock bersama; implementasi ini mengikuti runtime lokal yang ada.

## Berkas

- UI: `UI-DEV/team.html`, `UI-DEV/team.css`, `UI-DEV/team.js`, `UI-DEV/computer-preview.css`.
- API: `app/api/team_chat.py`, `app/api/computer.py`.
- Jalur input pribadi: `app/core/infra/computer_handoff.py`, `app/core/infra/computer_private_input.py`, `app/core/infra/computer_runtime.py`.
- Header respons: `app/middleware/security_headers.py`.

Tidak menjalankan test suite, syntax check, build, browser automation, screenshot baru, aksi komputer, migrasi, atau deployment. Backend perlu memuat kode baru agar endpoint input pribadi dan aktivitas roster tersedia. Hasil runtime menunggu pengujian pengguna.
