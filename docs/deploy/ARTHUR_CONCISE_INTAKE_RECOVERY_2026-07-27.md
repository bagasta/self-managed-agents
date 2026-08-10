# Pemulihan Intake Arthur yang Ringkas — 27 Juli 2026

## Tujuan

Mengoreksi regresi rilis progressive disclosure sebelumnya: Arthur bersikap seperti formulir, terlalu lama membuat agent, dan kadang gagal menyelesaikan pilihan nomor demo menjadi link `wa.me` beserta kode.

## Perubahan

- Discovery sekarang memblokir create hanya pada brief inti: tujuan, pengguna, nama, tugas konkret, batas material, eskalasi, dan integrasi.
- Contoh percakapan, tone, volume chat, output yang sudah tersirat dalam tugas, vision yang sudah tersirat dari foto/struk, sumber knowledge yang akan diunggah kemudian, dan approver menjadi penyempurna opsional.
- Foto struk/bukti pembayaran yang disebut pada tugas kini ikut menjadi bukti capability `receive_only`; Arthur tidak perlu menanyakan vision dengan istilah lain.
- Kernel, skill discovery, prompt builder, dan tool description memakai kontrak yang sama. Ini menghapus konflik lama yang masih memaksa enam grup dan dua–tiga contoh percakapan.
- Runtime memaksa planning gate juga dari state `idle`, sehingga respons janji seperti “saya panggil perencanaan” tanpa eksekusi tidak menjadi jawaban akhir.
- Pilihan onboarding `1` dan `2` sekarang dipetakan sebagai pilihan eksplisit nomor demo dan nomor khusus. Jalur demo tetap harus memanggil `create_wa_dev_trial_link`, lalu reply guard mengirim link `wa.me` dan kode persis dari hasil tool.
- Seed/config dinaikkan ke `arthur-progressive-v3`, `arthur-kernel-v13`, dan bundle `arthur-skills-2026-07-27-v15`.

## Alur yang diharapkan

Arthur mengumpulkan satu detail berdampak tinggi per pesan, tanpa mengulang fakta yang sudah ada. Setelah ringkasan disetujui, Arthur membuat dan memverifikasi agent pada giliran yang sama. Bila Google Sheets diperlukan, Arthur menyatakan setup yang masih pending secara jujur dan tidak mengklaim Sheet atau OAuth sudah siap tanpa hasil tool.

Setelah agent dibuat, Arthur menawarkan dua pilihan WhatsApp. Jika user memilih nomor demo—termasuk membalas `1`—Arthur mengirim link `wa.me` dan kode trial hasil tool pada giliran yang sama.

## Verifikasi lokal

- `python3 -m compileall` lulus untuk modul dan test yang diubah.
- `git diff --check` lulus.
- Test runtime penuh belum dijalankan dari shell kerja ini karena environment Python sistem menyediakan SQLAlchemy lama yang tidak kompatibel dengan aplikasi (`mapped_column` tidak ada). Jalankan suite pada virtualenv/container proyek sebelum deploy.

## Deploy

Seed ulang Arthur agar kernel dan skill yang berubah masuk ke database, kemudian lakukan rollout API/scheduler sesuai prosedur deployment proyek. Tidak ada deployment produksi yang dilakukan oleh perubahan ini.
