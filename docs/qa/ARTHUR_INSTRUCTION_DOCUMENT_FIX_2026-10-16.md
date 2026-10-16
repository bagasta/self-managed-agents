# System message Markdown dan penerimaan dokumen — 16 Oktober 2026

## Perubahan

`anthropic/claude-sonnet-5.5` melalui OpenRouter khusus menyusun `Agent.instructions` saat pembuatan dan revisi instruksi penuh Arthur V2. Model percakapan Arthur, pemetaan normal/advance, OCR dan vision tidak diganti. Agent yang sudah ada tidak ditulis ulang otomatis.

Brief yang dikirim ke penulis berisi nama, tujuan, instruksi yang disetujui, workflow dan batas kemampuan tools; tidak mengirim seluruh percakapan/memori. Penulis tidak diberi tools untuk melakukan aksi. Output harus berupa Markdown lengkap dengan judul nama agent dan bagian bernomor, mengikuti gaya contoh LapiBot tanpa menyalin data proyek contoh. Backend menambahkan aturan tools otoritatif secara verbatim. Output terpotong, salah nama, tanpa struktur atau gagal dari provider membatalkan penyimpanan. Receipt menyimpan model, format dan hash brief. Perubahan versi saat penyusunan juga menyebabkan edit dibatalkan.

## Penyebab masalah dokumen

1. **Metadata lampiran hilang di WhatsApp Cloud.** `meta_webhooks._process` menerima hasil `process_wa_media` tetapi mengabaikan metadata sumber file. Tools upload/knowledge memerlukan sumber terdaftar pada sesi; keberadaan file di disk saja tidak cukup. Jalur Cloud kini memakai helper registrasi yang sama dengan nomor demo, sebelum agent/tools dijalankan.
2. **MIME tidak diteruskan dan dokumen menjadi `.bin`.** File PDF/DOCX tanpa ekstensi tidak diproses walaupun WhatsApp menyediakan MIME yang benar. MIME diteruskan dari kedua channel dan dipakai untuk nama/ekstensi, termasuk parameter MIME. PDF generik juga dikenali dari signature. Byte asli tetap disimpan.
3. **Ketergantungan OCR dan batas layanan.** Log API produksi sejak 07.00 UTC memuat dua event retry OCR dan satu ekstraksi dokumen gagal dengan HTTP 429. PDF digital sekarang dibaca lokal lebih dulu, tanpa menghabiskan kuota OCR. PDF scan/campuran tetap memerlukan OCR dan parser lokal tidak boleh diam-diam membuang halaman scan. DOCX/PPTX pada jalur OCR dapat memakai parser lokal saat provider gagal, selama file tidak mengandung gambar tertanam yang dapat menyebabkan hasil tidak lengkap.
4. **CV gambar yang dikirim sebagai dokumen.** JPG/PNG/WebP kini masuk jalur vision, dengan sumber asli tetap bertipe lampiran dokumen agar dapat diunggah sebagai byte asli.
5. **Penanda lampiran tertinggal.** Kedua penanda current dihapus pada turn teks berikutnya. Kode lama memakai `pop(...) or pop(...)`, sehingga penanda kedua dapat tidak terhapus. Metadata file terakhir tetap tersedia untuk permintaan eksplisit memakai lampiran sebelumnya; bukan artifact baru yang otomatis dikirim.

Dokumen kosong atau format yang belum didukung tidak dianggap selesai dibaca. File asli tetap tersedia untuk diteruskan/diunggah sesuai workflow. Pembacaan otomatis didukung untuk PDF, DOCX, PPTX, TXT, MD dan CSV; gambar dikirim ke vision. OCR outage pada file scan/gambar tertanam masih dapat menjadi blocker nyata dan harus dijawab jujur. Tidak mengklaim semua format dokumen sudah didukung.

## Validasi

Pengujian menggunakan data sintetis dan layanan tiruan untuk memastikan format/model khusus, tidak ada eksekusi tools dari writer, konfirmasi/ownership, kegagalan tanpa write, pembuatan/revisi dengan model runtime tetap, registrasi Cloud sebelum tools, file tanpa ekstensi/MIME, pembacaan PDF lokal, fallback Office, dokumen kosong/tidak didukung, image-as-document, serta lifecycle lampiran. Regresi CV/reset/transfer file lama juga disertakan.

Tes prompt legacy `test_builder_prompt_blocks_repeated_continue_questions` dikecualikan karena sudah gagal pada image produksi sebelum hotfix, sebagaimana dicatat pada rilis sebelumnya.

Suite final: **453 passed, 1 deselected** dalam 34,96 detik. Percobaan awal pembuatan menggunakan SQLite memerlukan penyesuaian fixture filter JSON PostgreSQL; tidak ada perubahan query produksi untuk menyesuaikan tes.

Smoke nyata ke OpenRouter pada 08.25.42 UTC berhasil menggunakan ID model persis `anthropic/claude-sonnet-5.5`: dokumen 3.900 karakter, sembilan bagian bernomor, 3.080 token total. Input hanya brief daftar belanja sintetis. Tidak membuat agent, mengubah database, atau mengirim WhatsApp.

Status kandidat: pengujian kode selesai; bukti staging, deployment dan rollback dicatat setelah validasi rilis selesai.

Percobaan staging pertama tertahan saat restore: `pg_isready` berhasil pada server inisialisasi sementara database clone belum dibuat. Produksi tidak berubah. Helper rilis diperketat agar menunggu query `SELECT 1` pada database tujuan; state staging yang gagal dibatalkan sebelum percobaan baru.

## Batas dan kompatibilitas

Tidak ada migrasi schema, reset memori pelanggan, penulisan ke Google, atau pengiriman WhatsApp selama pemeriksaan ini. Format Markdown disimpan sebagai teks instruksi seperti sebelumnya. Field receipt baru ada di JSON config dan aman diabaikan oleh versi lama. Rollback aplikasi tidak otomatis mengembalikan instruksi yang kelak telah diedit owner.

Referensi model resmi: [OpenRouter model catalog](https://openrouter.ai/api/v1/models), diperiksa untuk ID persis yang diminta. Tidak ada penggantian nama model secara diam-diam.
