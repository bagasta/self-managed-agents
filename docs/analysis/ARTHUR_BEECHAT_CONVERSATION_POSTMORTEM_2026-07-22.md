# Postmortem Percakapan Arthur — BeeChat

Tanggal analisis: 22 Juli 2026  
Percakapan yang dianalisis: 21 Juli 2026, 12.29–12.38 WIB

## Kesimpulan

Arthur **belum menjalankan tugasnya dengan baik** pada percakapan ini. Agent memang akhirnya dibuat dan link demo terkirim, tetapi hasil akhirnya tidak membuktikan bahwa BeeChat memahami kebutuhan universitas dengan aman dan cukup dalam.

Masalahnya bukan sekadar gaya bertanya yang terlalu singkat. Ada konflik deterministik di runtime builder:

1. `plan_agent` dapat mengembalikan `needs_clarification` ketika discovery belum lengkap.
2. `_needs_builder_create_completion()` sebelumnya menganggap hampir setiap pemanggilan `plan_agent` sebagai rencana yang siap dibuat.
3. Runtime kemudian menjalankan continuation internal dengan instruksi: **“Kalau ada detail yang belum lengkap, pakai asumsi wajar.”**
4. Instruksi itu bertentangan langsung dengan discovery gate dan aturan “dilarang mengisi jawaban sendiri”.
5. Ketika continuation tetap tidak mencapai `create_agent`, `reply_guard` mengubah kegagalan tersebut menjadi pesan generik “kendala sistem, coba kirim lagi”.

Ini menjelaskan dua gejala sekaligus: Arthur tidak menggali lebih dalam dan pembuatan pertama gagal, tetapi berhasil setelah user mengetik “coba lagi”.

Ada indikasi tambahan bahwa runtime yang melayani percakapan tidak sama dengan source checkout saat ini. Pesan statis “Masih saya proses ya. Saya akan kirim hasilnya begitu selesai.” sudah sengaja dilarang/dihapus dari jalur source saat ini, branch lokal sudah memiliki hardening discovery sejak 16 Juli, tetapi percakapan tanggal 21 Juli masih menunjukkan perilaku lama. Tidak ada backend lokal atau Docker daemon aktif saat analisis, sehingga versi produksi yang benar-benar melayani chat ini tidak dapat dibuktikan dari mesin ini.

## Penilaian Percakapan

### Yang dilakukan dengan benar

- Arthur mengenali intent utama: membuat CS WhatsApp untuk universitas.
- Arthur meminta nama agent, nama universitas, dan nomor eskalasi.
- Arthur mengecek plan/slot Trial sebelum membuat agent.
- Arthur akhirnya mengirim kode dan link demo BeeChat.

Poin-poin itu cukup untuk menghasilkan objek agent, tetapi belum cukup untuk menghasilkan agent CS universitas yang dapat dipercaya.

### Yang dilakukan kurang baik

#### 1. Perkenalan menjelaskan produk, bukan proses konsultasi

Arthur berkata bahwa ia “spesialis bantu bikin AI Agent untuk WhatsApp”, tetapi tidak menjelaskan bahwa ia akan menggali workflow, sumber kebenaran, batas wewenang, aturan eskalasi, integrasi, lalu menguji hasilnya. Akibatnya user tidak mendapat ekspektasi bahwa proses ini adalah perancangan operasional, bukan sekadar mengisi nama dan URL.

Source saat ini sudah mempunyai wording intro yang lebih baik di `app/core/engine/prompt_builder.py`, tetapi perilaku live harus diverifikasi setelah deploy.

#### 2. Discovery berhenti pada identitas agent

Arthur hanya mengumpulkan:

- jenis agent: CS universitas;
- nama: BeeChat;
- organisasi: Universitas Bina Nusantara;
- sumber umum: `https://binus.ac.id/`;
- nomor tujuan: “nomer ini”;
- file input/output: tidak diperlukan.

Informasi kritis yang tidak digali:

- pain point nyata: pertanyaan apa yang lambat/sulit dijawab sekarang;
- audience: calon mahasiswa, mahasiswa aktif, orang tua, alumni, atau semua;
- scope: admisi, program studi, biaya, beasiswa, lokasi, kalender akademik, layanan mahasiswa, atau kombinasi tertentu;
- sumber kebenaran dan prioritasnya: halaman mana, bahasa mana, apakah boleh menggunakan hasil pencarian umum, dan bagaimana menangani informasi berbeda/kedaluwarsa;
- batas wewenang: hal apa yang boleh dijawab, tidak boleh dijanjikan, dan tidak boleh diputuskan;
- contoh 2–3 percakapan ideal dan red line;
- tone, bahasa, dan penggunaan emoji;
- perlakuan data pribadi calon mahasiswa;
- skala nomor WhatsApp dan estimasi volume chat;
- kondisi eskalasi yang presisi, penerima berdasarkan role, serta format informasi yang dikirim;
- siapa reviewer yang menyetujui BeeChat sebelum go-live;
- rangkuman akhir dan konfirmasi eksplisit user.

#### 3. URL website diperlakukan seperti brief lengkap

“Ambil jawabannya dari sini” hanya menjawab **dari mana informasi berasal**, bukan:

- informasi apa yang termasuk scope;
- halaman mana yang authoritative;
- apakah BeeChat mengambil informasi live atau memakai knowledge yang diindeks;
- seberapa sering informasi perlu diperbarui;
- apa yang dilakukan jika jawaban tidak ditemukan atau saling bertentangan.

Untuk use case universitas, ini berisiko tinggi karena biaya, jadwal, syarat, dan program dapat berbeda berdasarkan kampus, periode, dan jalur pendaftaran.

#### 4. Eskalasi hanya mengumpulkan nomor, bukan workflow

User sudah memberikan kebutuhan penting: jika BeeChat tidak tahu, BeeChat harus menghubungi user untuk mendapatkan jawaban yang benar. Arthur seharusnya mengubahnya menjadi kontrak operasional:

1. BeeChat berhenti menebak.
2. BeeChat memberi tahu penanya bahwa informasi sedang dikonfirmasi.
3. BeeChat mengirim ke operator: pertanyaan asli, konteks/ringkasan percakapan, identitas customer yang relevan, dan lampiran terakhir bila ada.
4. Operator menjawab melalui jalur yang benar.
5. BeeChat meneruskan jawaban yang telah dikonfirmasi dan, jika disetujui Owner, menyimpan pengetahuan baru agar pertanyaan sama tidak selalu dieskalasikan.

Arthur tidak menanyakan kondisi pemicu, role penerima, mekanisme balasan operator, maupun apakah jawaban operator boleh dipelajari untuk penggunaan berikutnya.

#### 5. Pertanyaan kapabilitas file muncul terlalu dini dan tidak kontekstual

Pertanyaan file berasal dari capability gate generik, bukan dari pemahaman khusus tentang CS universitas. Dalam alur yang benar, pembahasan file/vision berada setelah scope, workflow, knowledge, dan eskalasi jelas. Pertanyaannya juga seharusnya kontekstual, misalnya apakah calon mahasiswa akan mengirim brosur, screenshot, atau dokumen pendaftaran untuk dibaca—bukan daftar generik PDF/Excel/CSV/grafik.

#### 6. Tidak ada konfirmasi sebelum create

Arthur seharusnya menunjukkan ringkasan faktual dan meminta user menyatakan “sudah sesuai”. Pada percakapan ini, jawaban “tidak perlu, dia hanya CS aja” diperlakukan sebagai sinyal untuk mulai membuat agent, padahal itu hanya jawaban atas pertanyaan file.

## Root Cause Teknis

### A. Auto-continuation tidak memeriksa status rencana

Sebelum patch ini, `app/core/engine/agent_followups.py::_needs_builder_create_completion()` hanya memeriksa bahwa `plan_agent` pernah dipanggil, belum ada `create_agent`, dan tidak ada blok entitlement. Fungsi tersebut tidak mensyaratkan `plan_status == "ready"`.

Akibatnya output berikut tetap bisa masuk continuation create:

```json
{
  "plan_status": "needs_clarification",
  "discovery_progress": {
    "next_group": {"id": "agent_behavior"}
  }
}
```

Padahal respons yang benar adalah menanyakan `next_questions`, bukan membuat agent.

### B. Continuation secara eksplisit mengizinkan asumsi

Directive internal sebelumnya memuat instruksi:

> Kalau ada detail yang belum lengkap, pakai asumsi wajar dan tandai untuk direview nanti.

Ini membatalkan aturan discovery enam grup. Model diberi dua instruksi yang berlawanan: jangan mengarang, tetapi pada recovery justru diminta mengarang agar create selesai. Instruksi recovery biasanya lebih dekat dengan giliran terakhir, sehingga sangat mungkin lebih dominan.

### C. Fallback menyembunyikan state sebenarnya

Jika chain sudah memanggil tool builder tetapi tidak mencapai `create_agent`, `app/core/engine/reply_guard.py` mengirim pesan generik “kendala sistem”. User tidak tahu apakah penyebabnya:

- kebutuhan belum lengkap;
- compose berhenti di tengah;
- entitlement;
- tool error;
- runtime timeout.

Dalam kasus ini, `needs_clarification` semestinya tidak menjadi error sama sekali.

### D. Hanya ada satu recovery internal

Untuk rencana yang benar-benar `ready`, runner sebelumnya hanya melakukan satu continuation. Jika model kembali berhenti setelah compose/validate, user harus mengetik “coba lagi”. Ini cocok dengan pola percakapan BeeChat.

### E. Kemungkinan runtime/deploy stale

Checkout saat ini berada di branch `agent/guard-arthur-media-sources`, tujuh commit di depan `main`, dan hardening discovery sudah ada sejak commit `0e5faee` tanggal 16 Juli. Percakapan tanggal 21 Juli masih menunjukkan marker progres dan pola lama. Karena tidak ada runtime lokal yang aktif dan Docker daemon lokal tidak tersedia, ini belum dapat dipastikan tanpa log/deploy produksi.

## Perbaikan yang Diimplementasikan

### 1. Hanya rencana `ready` yang dapat auto-create

`app/core/engine/agent_followups.py` sekarang:

- mewajibkan output `plan_agent` berbentuk JSON terstruktur;
- mewajibkan `plan_status == "ready"`;
- menolak continuation untuk `needs_clarification`, policy block, dan entitlement block;
- tidak lagi menganggap output legacy seperti `"ok"` sebagai bukti discovery selesai.

### 2. Recovery dilarang menambah asumsi

Directive continuation sekarang menyatakan bahwa rencana sudah `ready` dan hanya boleh memakai `discovery_answers` yang dikonfirmasi. Kalimat “pakai asumsi wajar” dihapus dan diganti dengan larangan eksplisit menambahkan detail yang tidak pernah diberikan user.

### 3. Recovery internal maksimal dua kali untuk rencana siap

`app/core/engine/agent_runner.py` sekarang memberi maksimal dua continuation internal hanya ketika rencana sudah `ready` tetapi belum mencapai `create_agent`. Tujuannya agar kegagalan model berhenti di tengah tidak dibebankan kepada user melalui instruksi “coba lagi”. Loop berhenti segera setelah create berhasil, status tidak lagi eligible, atau terjadi exception nyata.

### 4. Regression tests ditambahkan

Tes baru memastikan:

- `needs_clarification` tidak memicu auto-create;
- output plan yang tidak terstruktur tidak dianggap siap;
- entitlement block tetap tidak diterobos;
- directive recovery tidak mengandung “pakai asumsi wajar”;
- directive secara eksplisit melarang detail buatan.

## Alur BeeChat yang Seharusnya

Berikut bentuk percakapan yang lebih benar; bukan script kaku, tetapi urutan informasi yang harus tercapai.

1. Arthur menjelaskan perannya dan bahwa ia akan menggali kebutuhan, membuat, lalu membantu uji demo.
2. Arthur menjelaskan eskalasi secara singkat: BeeChat berhenti menebak dan dapat meneruskan konteks ke manusia.
3. Grup 1 — konteks dan tujuan:
   - masalah utama yang ingin diselesaikan;
   - pekerjaan/bisnis;
   - nama BeeChat;
   - calon mahasiswa/mahasiswa/orang tua sebagai audience.
4. Grup 2 — perilaku:
   - daftar tugas dan scope layanan;
   - boleh/tidak boleh;
   - tone dan bahasa;
   - 2–3 contoh ideal dan satu red line.
5. Grup 3 — eskalasi:
   - pertanyaan/kondisi apa yang dieskalasikan;
   - Bagas atau role lain sebagai penerima;
   - nomor WhatsApp yang terverifikasi;
   - format ringkasan dan cara jawaban kembali ke customer;
   - apakah jawaban boleh masuk knowledge setelah disetujui.
6. Grup 4 — sumber:
   - `binus.ac.id` sebagai sumber resmi;
   - halaman/scope prioritas;
   - aturan jika informasi tidak ditemukan/berbeda;
   - kebijakan data sensitif.
7. Grup 5 — skala dan integrasi:
   - satu nomor melayani banyak user;
   - estimasi volume;
   - perlu/tidak Google, CRM, database;
   - output hanya chat;
   - perlu/tidak membaca gambar/dokumen.
8. Grup 6 — reviewer go-live.
9. Arthur merangkum seluruh kontrak operasional dan meminta satu konfirmasi eksplisit.
10. Setelah disetujui, Arthur membuat agent sampai sukses dalam giliran yang sama, memverifikasi hasil, lalu mengirim link demo.

Contoh pertanyaan lanjutan pertama yang lebih tepat setelah user memberikan nama, universitas, dan nomor:

> Siap. Kalau BeeChat tidak menemukan jawaban, dia akan berhenti menebak dan meneruskan pertanyaan beserta ringkasan chat ke nomor ini. Sebelum saya susun, siapa pengguna utamanya dan topik apa saja yang harus BeeChat layani—calon mahasiswa/admission, mahasiswa aktif, orang tua, atau semuanya?

Setelah dijawab, Arthur melanjutkan grup yang sama sampai lengkap, bukan langsung berpindah ke capability file.

## Verifikasi

- Tes terarah: **57 passed**.
- Full maintained suite: **978 passed, 7 failed, 9 skipped**.
- Tujuh kegagalan full suite berada di payment-link expectation, coding preset wording, trial expiry expectation, dua WA QR routing mocks, dan dua event-loop spam-window tests; tidak menyentuh empat file patch ini.
- `git diff --check`: **lulus**.
- File yang diubah untuk patch: empat file source/test terkait continuation Arthur; dokumen ini ditambahkan sebagai laporan.

## Langkah Deploy yang Wajib

Source fix belum sama dengan live fix. Sebelum menguji ulang nomor Arthur:

1. Merge/cherry-pick perubahan dari branch aktif ke branch yang benar-benar dipakai produksi.
2. Build dan deploy ulang service API/worker yang menjalankan `agent_runner.py` dan `agent_followups.py`.
3. Jika `system-message-builder.md` atau seed Arthur ikut berubah pada deploy gabungan, jalankan `scripts/seed_arthur.py` sesuai `deploy/FAST_DEPLOY.md`.
4. Pastikan commit/image produksi tercatat dan service benar-benar restart.
5. Jalankan ulang skenario BeeChat dari sesi baru; sesi lama dapat membawa history/tool state lama.
6. Ambil log tool steps untuk memastikan urutannya: `plan_agent(needs_clarification)` → pertanyaan user, bukan continuation create; setelah konfirmasi: `plan_agent(ready)` → compose/validate/create/verify → link demo.

Tanpa verifikasi deploy ini, hasil live tanggal 21 Juli tidak boleh dianggap mencerminkan source yang sudah diperbaiki.
