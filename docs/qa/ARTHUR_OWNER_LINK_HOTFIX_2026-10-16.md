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
- Probe `scripts/arthur_owner_link_probe.py` membaca sesi dan ownership aktual dengan transaksi read-only dan menggunakan URL placeholder. Probe tidak membuat token signup, mengirim WhatsApp, atau mengubah konfigurasi agent.

## Status rilis

Perbaikan kode siap divalidasi pada suite terkait dan salinan database terisolasi. Receipt deployment, identitas image, hasil probe produksi, serta perintah rollback dicatat setelah langkah tersebut selesai. Belum mengklaim proses Meta atau QA percakapan WhatsApp nyata telah lulus.
