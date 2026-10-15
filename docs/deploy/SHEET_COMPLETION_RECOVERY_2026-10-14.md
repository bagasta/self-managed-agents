# LapiBot gagal menyimpan saat penutup percakapan

Perbaikan sudah dideploy pada 14 Oktober 2026 pukul 18:59 WIB. API internal dan publik merespons HTTP 200; pemeriksaan awal setelah deploy tidak menemukan error atau HTTP 500. Tiga file runtime di container cocok dengan patch yang diuji.

## Penyebab yang terbukti

Run `3a2939f1-53f1-4254-a84d-66674bc11836` (18:42–18:43 WIB) dan `f76bda50-2e12-4127-b277-c2d6118badbd` (18:44–18:45 WIB), session `94502f96-3090-4d68-8770-f6b9b93bcfb3`, berhasil membaca spreadsheet. Keduanya menggunakan `deepseek/deepseek-v4-flash` dengan batas output 4.096 token.

Pada generasi terakhir kedua run, `completion_tokens=4096` dan `reasoning_tokens=4096`: seluruh anggaran output terpakai untuk penalaran, tanpa tool tulis dan tanpa jawaban akhir. Runtime kemudian mengirim fallback “Proses belum menghasilkan jawaban akhir yang terverifikasi…”. Ini bukan penolakan akses non-owner atau kegagalan autentikasi Google. Tidak ada penulisan spreadsheet oleh kedua run tersebut.

Jalur pemulihan sebelumnya dibatasi untuk builder Arthur. LapiBot adalah agent operasional, sehingga tidak mendapat pemulihan yang sama.

## Perubahan

- Minimum output untuk agent Google yang memakai DeepSeek menjadi 16.384 token; konfigurasi lebih besar tetap dihormati.
- Generasi terpotong dideteksi dari metadata/usage generasi terakhir, bukan jumlah token seluruh langkah.
- Agent dapat melanjutkan sekali dengan anggaran lebih besar, maksimal 32.768 token, menggunakan hasil tool yang sudah selesai. Instruksi pemulihan meminta penyelesaian tindakan tersisa dan verifikasi penyimpanan.
- Hasil tulis yang sudah selesai dipertahankan. Tool call tanpa hasil yang status eksekusinya tidak diketahui memblokir pemulihan, untuk menghindari pengulangan tindakan yang tidak aman.
- Pemulihan dibatasi waktu. Agent dengan konfigurasi persetujuan manusia `interrupt_on` tidak dialihkan ke executor yang tidak membawa middleware persetujuannya.

## Validasi

109 tes unik lulus, mencakup anggaran model, pemulihan, parser hasil, akses Google untuk pelanggan non-owner, guard Sheets, dan regresi Arthur. Tes graph memastikan hasil tulis yang sudah selesai dibaca ulang untuk verifikasi tanpa mengeksekusi tool tulis lagi.

Replay dengan model asli menggunakan percakapan kasus ini, hasil baca spreadsheet, dan seluruh mutasi/notifikasi disimulasikan. Replay akhir menghasilkan tiga penulisan simulasi: `Jawaban`, `Laporan`, `Peserta`; kemudian satu panggilan baca verifikasi, notifikasi owner simulasi, dan jawaban akhir 247 karakter dengan `finish_reason=stop`.

Replay pertama memakai snapshot audit yang ternyata terpotong pada 2.000 karakter untuk tab Laporan. Uji kelengkapan belum lolos pada replay itu. Setelah snapshot dibaca ulang secara read-only, replay lengkap di atas lulus. Data dan pesan produksi tidak diubah selama pengujian.

## Deployment

- Image aktif API: `managed-agents-app:tool-workflow-recovery-20261014`.
- Rollback: `managed-agents-app:rollback-tool-workflow-recovery-20261014`.
- Base image: `managed-agents-app:whatsapp-binding-20261014`, mempertahankan perbaikan yang sudah ada.
- API direcreate setelah pemeriksaan menemukan nol run aktif; shutdown graceful tetap berlaku.
- Scheduler, layanan WhatsApp, dan database tidak direstart; tidak ada migrasi.

## Batas verifikasi dan jawaban yang tertunda

Patch aktif untuk permintaan berikutnya. Dua run lama yang gagal belum otomatis dijalankan ulang atau ditulis ke spreadsheet. Untuk melanjutkan kasus itu, kirim “lanjutkan simpan jawaban tadi” pada percakapan LapiBot yang sama; tidak perlu mengulang wawancara.

Validasi model memakai mutasi simulasi, sehingga tidak boleh diartikan sebagai bukti bahwa jawaban lama sudah tersimpan di produksi. Belum ada run pengguna baru yang diamati selesai setelah deployment pada saat laporan dibuat. Pemeriksaan kesehatan awal bukan jaminan tidak akan pernah ada kegagalan provider atau tool di masa depan.
