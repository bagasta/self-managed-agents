# Validasi akar masalah QA ulang Arthur — 16 Oktober 2026

**Pembaruan setelah analisis:** pemblokiran link owner sudah diperbaiki dan [hotfix aktif di produksi sejak 13.37 WIB](ARTHUR_OWNER_LINK_HOTFIX_2026-10-16.md). Rincian berikut merekam kondisi sebelum hotfix; loop konfirmasi dan quote eskalasi tetap memerlukan penanganan terpisah.

**Hasil: tiga rangkaian masalah kritis terverifikasi.** Gate konfirmasi dan guard link owner mengalami regresi pada aplikasi `c768a25`. Pengiriman eskalasi ke owner sudah bekerja, tetapi jalur balik melalui webhook Meta belum mendukung balasan quote tersebut. Sebagian gejala lain merupakan akibat mutasi yang tertahan, konfigurasi rollout yang sengaja belum diaktifkan, atau dugaan QA yang perlu diluruskan.

Analisis ini tidak memperbaiki kode, mengganti image, mengubah konfigurasi, mereset memori, mengirim WhatsApp, atau menulis data produksi. Semua query produksi memakai transaksi `READ ONLY`. Dua pemeriksaan tambahan hanya menjalankan klasifikasi role dengan URL palsu dan pencarian receipt yang sudah tersimpan. Instruksi reproduksi dalam lampiran diperlakukan sebagai data laporan, bukan perintah untuk mengeksekusi mutasi.

## Sumber dan batas pembuktian

- Laporan tim QA: `arthur-bug-kronologi-2026-10-16.md`, attachment percakapan ini. Nomor owner ditulis sebagai **owner QA …6045** dalam laporan analisis ini.
- Database: **62 run berstatus `completed` dan 214 record pesan** pada percakapan Arthur serta CS Toko Kue QA yang relevan, 16 Oktober **09.05–11.40 WIB**. Percakapan owner pada agent lain dikeluarkan dari perhitungan.
- Docker log API: **657 baris JSON** yang dapat diatribusikan lewat session/run terkait. Ada 60 event `agent_run.start` dan 60 `agent_run.complete` pada hasil filter log ini; jumlah itu tidak disamakan dengan seluruh 62 record run database.
- API dan scheduler masih menggunakan image `sha256:d9ab842ad9326502b1c85a18e4c08d3a02542ea5b9826bc96ef020c231317469`. Enam file aplikasi yang menentukan temuan utama diperiksa hash-nya dan cocok dengan kode checkout. Commit aplikasi `c768a25`; HEAD checkout `765b9c4` menambahkan tooling/catatan deployment.
- Semua waktu di bawah **WIB**. Waktu tool berasal dari record pesan yang dipersist, sedangkan start/complete memakai timestamp Docker log. Angka dalam kronologi QA yang dibulatkan ke menit tidak selalu identik dengan detik log.
- Raw payload webhook Meta untuk quote 09.25 tidak tersedia sebagai bukti tersimpan yang diperiksa. Nilai `message.context.id` asli tidak diklaim sudah terverifikasi. Perilaku handler dan jalur run yang dipakai dapat diverifikasi.

## Ringkasan penilaian

| Temuan QA | Hasil validasi | Penyebab / koreksi penting |
| --- | --- | --- |
| 1a–1d: edit, impor, pause terus meminta konfirmasi | **Terbukti, kritis** | Frasa yang diminta Arthur tidak diterima gate; payload juga berubah antargiliran. Ada 19 panggilan tool, semuanya ditahan sebelum mutasi. |
| 1e: link Meta tidak dibuat | **Gejala terbukti; dugaan proses perlu dikoreksi** | Tool akhirnya berhasil membuat link. Guard jawaban menganggap user Arthur sebagai customer dan menyembunyikan link owner. |
| 2: quote owner tidak sampai ke customer | **Terbukti, kritis** | Webhook Meta meneruskan teks ke LLM tanpa penanganan quoted escalation. Lookup lama juga dibatasi ke agent yang sedang menerima pesan, sedangkan receipt ada pada agent CS. |
| 3: identitas/kartu eskalasi | **Campuran data nyata dan masalah penyajian** | Nama `Agent` berasal dari identitas WhatsApp; nomor sama karena penguji memakai nomor yang sama. Isi `Pesan` memang berasal dari ringkasan LLM. Case ID pada kartu owner dipakai untuk korelasi routing, bukan bukti bocor ke customer. |
| 4: klaim saling bertentangan | **Sebagian terbukti** | Penyangkalan 09.44.59 bertentangan dengan tool trace sebelumnya. Akan tetapi versi 1 dan knowledge kosong memang sesuai agent baru yang dituju. |
| 5: trial, payment, kode refund, sapaan | **Perlu dipisahkan** | Expiry enforcement memang mati; Growth/tier_1 benar; nomor pada URL pembayaran memang dibuat kode; kode refund berulang pada owner, belum terbukti bocor ke customer; instruksi “Kak” belum tersimpan. |

## 1. Konfirmasi: UI, payload, dan state machine tidak memiliki kontrak yang sama

### Bukti mutasi tertahan

Dalam window yang diperiksa, hasil tool adalah:

| Tool | Panggilan | Argumen | Hasil |
| --- | ---: | --- | --- |
| `update_assistant` | 6 | `confirmed=true` | Seluruhnya `ok=false`, `needs_confirmation=true` |
| `add_assistant_knowledge` | 10 | `confirmed=true` | Seluruhnya `ok=false`, `needs_confirmation=true` |
| `set_assistant_paused` | 3 | `confirmed=true` | Seluruhnya `ok=false`, `needs_confirmation=true` |

Jadi kalimat “tool impor tidak pernah dipanggil” kurang tepat secara teknis: **permintaan tool tercatat, tetapi wrapper persetujuan mengembalikan preview dan tidak menjalankan fungsi impor asli**. Tidak ada bukti OCR atau penyimpanan knowledge gagal pada 10 panggilan ini, karena fungsi itu belum dijalankan.

### Penyebab pertama: frasa persetujuan tidak cocok

Daftar `_APPROVALS` di [confirmation_gate.py](/home/clevio/stack/managed-agents-meta-embedded-signup/arthur_v2/confirmation_gate.py:27) menerima beberapa literal seperti `ya`, `setuju`, dan `ya, terapkan`. Ia **tidak menerima** frasa yang benar-benar diminta/dibalas dalam QA berikut:

| Waktu hasil tool | Balasan user | Tool | Status |
| --- | --- | --- | --- |
| 09.14.29 | `simpan` | edit instruksi, dua panggilan | Meminta konfirmasi lagi |
| 09.15.03 | `simpan` | edit instruksi | Meminta konfirmasi lagi |
| 09.15.33 | `setuju simpan` | edit instruksi, dua panggilan | Meminta konfirmasi lagi |
| 09.16.38 | `setuju simpan FAQ` | impor FAQ | Meminta konfirmasi lagi |
| 09.44.45 | `ya simpan` | impor TXT, dua panggilan | Meminta konfirmasi lagi |
| 10.15.36 / 10.16.06 | `ya simpan` | impor TXT | Meminta konfirmasi lagi |
| 11.08.01 | `kirim` | impor TXT | Meminta konfirmasi lagi |
| 11.19.58 / 11.20.16 | `ya pause` | pause, tiga panggilan | Meminta konfirmasi lagi |

Kondisi [gate baris 94](/home/clevio/stack/managed-agents-meta-embedded-signup/arthur_v2/confirmation_gate.py:94) mensyaratkan `text in _APPROVALS`. Balasan persis sesuai permintaan Arthur tetap ditolak jika frasanya tidak ada di himpunan itu. Daftar frasa yang sah tidak dikembalikan dalam preview, sehingga Arthur bebas menciptakan instruksi konfirmasi yang backend tidak pahami.

### Penyebab kedua: draft diregenerasi, bukan dieksekusi dari payload yang ditinjau

Gate mengikat persetujuan ke SHA-256 nama tool + seluruh payload setelah normalisasi schema. Syarat `same` menjadi salah ketika LLM menulis ulang instruksi/judul pada giliran persetujuan, meskipun maksud bisnisnya sama.

Pada edit instruksi, payload yang benar-benar dikirim berubah:

| Waktu | Panjang instruksi | Awalan SHA-256 isi instruksi |
| --- | ---: | --- |
| 09.13.53 | 3.462 karakter | `b10e1ed61b84` |
| 09.14.29 | 2.925 karakter | `564ce2fbebbf` |
| 09.15.03 | 3.519 karakter | `4d5984b7ab97` |
| 09.15.33 | 3.320 karakter | `62f6e9d20ac0` |

Pada impor, filename TXT tetap `info_toko_kue_qa.txt`, tetapi title berubah dari `Info Toko Kue QA` ke `info_toko_kue_qa` pada 09.44.45, lalu berubah lagi pada 10.16.06. Perubahan title saja sudah mengubah digest.

Ketika pemeriksaan gagal, gate menyimpan preview baru dengan `prepared_message_id` = pesan user **yang sedang diproses**. Memanggil tool dua kali pada giliran itu tidak menyelesaikan approval, karena syarat berikutnya tetap memerlukan pesan user baru yang bersebelahan. Ini menjelaskan percobaan berulang dalam satu run yang hasilnya identik.

Jadi menambahkan kata `simpan` saja belum menyelesaikan seluruh akar masalah. Hubungan antara draft yang ditinjau, action yang menunggu persetujuan, dan payload yang akhirnya dijalankan belum stabil.

### TTL, cohort, dan dugaan yang tidak terbukti

- Gate memang **aktif untuk owner QA ini** pada environment produksi. Default kode `[]` tidak menggambarkan konfigurasi runtime. Cohort satu owner tersebut sudah dicatat pada rollout 16 Oktober.
- Gate memiliki TTL **10 menit** dan syarat approval pada pesan user berikutnya. Balasan 11.07 setelah preview 10.16 juga sudah melewati TTL, terlepas dari masalah frasa `kirim`. Meminta review baru dalam kasus kedaluwarsa sendiri merupakan perilaku yang wajar; loop frasa tetap merupakan bug.
- Pesan `ya` sekitar 09.44.52 tidak menghasilkan panggilan mutasi pada run berikutnya. Kasus itu bukan bukti backend menolak literal `ya`; model justru berhenti mencoba tool dan memberikan penjelasan yang bertentangan.
- Seluruh 19 hasil adalah `needs_confirmation`, bukan `already_applied` atau `outcome_uncertain`. Dugaan approval dianggap sudah dieksekusi tidak didukung trace ini.
- Trial kedaluwarsa bukan penghalang 19 operasi tersebut: wrapper mengembalikan preview sebelum fungsi bisnis dijalankan dan enforcement tanggal memang mati.
- Preview hanya mengembalikan pesan generik. Tidak ada alasan terstruktur yang membedakan frasa salah, digest berubah, TTL habis, atau giliran tidak bersebelahan. Ini membuat Arthur menebak frasa baru tanpa mengetahui penyebab penolakan sebenarnya.

### Mengapa aksi lain masih jalan

`create_demo_whatsapp_trial`, `enable_heartbeat`, dan `remember` tidak masuk daftar `_ACTIONS` gate baru. Pembuatan demo masih memakai boolean konfirmasi pada tool aslinya; heartbeat dan remember memakai kontrak tool dukungan. Jadi cakupan kontrol konfirmasi memang parsial.

Trace: kode demo berhasil 09.17.04; `remember` berhasil 09.20.46; heartbeat 60 menit berhasil 09.21.18. Catatan “tutup Senin” disimpan oleh tool memori yang terikat pada **Arthur**, dengan teks hasil yang juga menyatakan belum masuk instruksi CS. Itu bukan bukti memori atau SOP agent CS sudah berubah. Lihat binding [agent_tool_setup.py](/home/clevio/stack/managed-agents-meta-embedded-signup/app/core/engine/agent_tool_setup.py:167) dan [tool_builder.py](/home/clevio/stack/managed-agents-meta-embedded-signup/app/core/engine/tool_builder.py:101).

## 2. Link Meta: dibuat, tetapi guard customer memblokir owner

Run **`b71dbada-5d98-4bab-a0b0-00a0a51fda22`**, sekitar 11.31, memuat urutan berikut:

1. Model awalnya memakai ID agent Arthur sendiri pada `connect_assistant_whatsapp_cloud`; tool menolak (`ok=false`).
2. Model mengoreksi target ke CS Toko Kue QA (`a2ec2077-4dbd-4b29-b895-997b8517472c`). Tool berikutnya menghasilkan **`ok=true` dan `signup_url`**.
3. Jawaban akhir ke owner diganti menjadi teks “Pelanggan tidak perlu melakukan login akun”. Link bertanda tangan tidak dicantumkan dalam laporan ini.

Docker log pada **11.31.04,898** mencatat event `agent_run.final_reply_appended_google_auth_link`, dengan panjang jawaban **429 → 133 karakter**. Nama event menyesatkan untuk kasus ini: pada lokasi tersebut guard juga dapat menghapus link Meta, bukan hanya menambahkan link Google.

Akar masalah ada pada [guard_customer_auth_links](/home/clevio/stack/managed-agents-meta-embedded-signup/app/core/engine/agent_google_routing.py:225) yang memanggil [klasifikasi customer](/home/clevio/stack/managed-agents-meta-embedded-signup/app/core/engine/agent_identity.py:72). Klasifikasi melihat apakah pengirim termasuk owner/operator dari **agent Arthur itu sendiri**. User yang memiliki assistant melalui Arthur bukan otomatis operator internal agent sistem Arthur. Tidak ada pengecualian builder pada guard ini.

Probe read-only dengan objek Session/Agent produksi dan **URL palsu** memberikan:

```text
builder = true
classified_customer = true
demo_preview = false
fake_link_was_replaced = true
```

Ini false positive guard di percakapan pengelolaan owner. Bukan kegagalan Meta menghasilkan link, bukan efek preview demo, dan bukan loop gate konfirmasi yang sama dengan Bug 1a–1d. Fungsi koneksi Meta tidak termasuk `_ACTIONS` gate baru; pada kasus ini boolean konfirmasi tool asli sudah lolos.

## 3. Quote eskalasi: jalur keluar berfungsi, jalur balik Cloud API belum terhubung

### Urutan yang terverifikasi

| Waktu | Bukti |
| --- | --- |
| 09.24.06 | Customer mengirim komplain ke sesi demo CS. |
| 09.24.10 | Kartu `esc_1792117450_14c806` dibuat pada sesi CS. |
| 09.24.12 | Run CS `7b40e47b-21ac-438f-89b5-b215047c0e7d` menyelesaikan eskalasi dan memberi jawaban customer. |
| 09.25.56 | Teks balasan owner masuk ke sesi Arthur, bukan sesi customer CS. |
| 09.25.57–09.26.02 | Run Arthur `69c9f3a3-c0bc-4ee1-81fd-b79192d932d7` menjalankan LLM normal; tidak ada tool forward/reply ke customer. |
| 09.26.02 | Arthur meminta foto dan detail aturan refund, sesuai kronologi QA. |

Transport berbeda: sesi customer memakai `wadev_a2ec2077-…`, sesi owner memakai `meta:…`. Kartu sudah dipindahkan ke percakapan Arthur milik owner, tetapi implementasi penerimaan balasan tidak mengikuti perubahan transport tersebut.

### Penyebab pertama: handler Meta tidak membaca konteks quote

Handler [meta_webhooks._process](/home/clevio/stack/managed-agents-meta-embedded-signup/app/api/meta_webhooks.py:263) hanya mengambil `message.text.body`, membuat/mengambil sesi dengan **`is_operator=False`**, lalu memanggil `run_agent`. Ia tidak membaca atau merutekan `message.context.id` ke receipt eskalasi. Jalur operator/draft reply yang tersedia di `channels.py` tidak digunakan oleh handler ini.

Karena itu teks “Maaf ya Kak, kirim foto kuenya…” menjadi input percakapan Arthur biasa. Ini sesuai pesan yang tersimpan dan log run LLM normal. Tidak ada draft forward operator yang dibuat.

### Penyebab kedua: helper lookup lama hanya mencari pada agent yang menerima pesan

[find_session_by_quoted_message_id](/home/clevio/stack/managed-agents-meta-embedded-signup/app/api/wa_helpers.py:106) membatasi pencarian dengan `Session.agent_id == agent.id`. Receipt kartu berada pada sesi CS; balasan owner datang ke Arthur. Fallback pencarian case juga memiliki batas agent yang sama.

Probe SELECT-only memakai receipt eskalasi yang saat ini tersimpan memberikan:

```text
stored_receipt_exists = true
lookup_under_customer_agent_matches = true
lookup_under_Arthur_matches = false
same_qa_phone = true
different_transport = true
```

Probe ini membuktikan batas lookup saat ini, bukan mengklaim telah merekonstruksi ID quote 09.25 yang raw payload-nya tidak tersedia. Nomor owner sama dengan nomor customer **bukan penyebab utama**: lookup di bawah agent CS tetap berhasil untuk receipt yang sama. Walaupun nomor penguji dipisah, handler Cloud yang tidak membaca quote tetap bermasalah.

Ada pula perbedaan kontrak UX: jalur operator legacy menyiapkan draft dan meminta `kirim` sebelum meneruskan teks, sedangkan kartu menjanjikan cukup reply kartu lalu tulis jawaban. Pada insiden ini keduanya belum terjadi; handler langsung masuk LLM. Keputusan apakah quote menjadi persetujuan kirim atau perlu konfirmasi tambahan harus konsisten di kartu dan handler.

## 4. Klaim Arthur: pisahkan kontradiksi nyata dari status yang sebenarnya benar

### Kontradiksi 09.44–09.45 memang nyata

- **09.44.45**, run `af71a8aa-46b4-4ac2-8d8b-58abffb50ff0`: ada dua panggilan impor tertahan dan satu `escalate_to_human` yang mengembalikan `[error] Agent tidak memiliki Owner/operator terverifikasi untuk menerima eskalasi.` Pernyataan “penyimpanan tertahan dan eskalasi gagal” memiliki bukti tool.
- **09.44.59**, run `118759dd-3deb-4411-8de6-f550c2c86b77`: tidak ada panggilan tool, tetapi model menyatakan belum pernah mencoba impor/eskalasi dan sistem tidak menolak. Penyangkalan ini bertentangan dengan run sebelumnya.
- Kartu **09.50.58** dan **10.14.14** berasal dari agent **CS**, dengan case berbeda. Keduanya bukan bukti eskalasi dukungan dari Arthur pada 09.44 berhasil. Ada dua actor dan dua tujuan eskalasi yang berbeda.

Percakapan ini memiliki `reply_guard_reason=pass_through`, sehingga tidak ada bukti guard balasan utama mengganti isi penyangkalan itu. Riwayat untuk model hanya memuat user/agent; record tool tidak dimuat oleh [load_history](/home/clevio/stack/managed-agents-meta-embedded-signup/app/core/engine/context_service.py:111), dan [db_messages_to_lc](/home/clevio/stack/managed-agents-meta-embedded-signup/app/core/engine/context_service.py:152) hanya membentuk HumanMessage/AIMessage. Bukti hasil aksi terdahulu hilang dari konteks terstruktur model.

Itu faktor pendukung kuat terhadap jawaban status yang tidak andal, tetapi bukan pembuktian deterministik mengapa model memilih kalimat tertentu. Yang terverifikasi adalah kontradiksinya, tidak adanya receipt tool dalam history injection, dan tidak adanya tool verifikasi pada run penyangkalan.

Kegagalan eskalasi dukungan Arthur sendiri juga nyata: tidak ada owner/operator internal yang terverifikasi pada agent sistem untuk tujuan itu. Owner pengguna suatu assistant tidak boleh otomatis dianggap operator internal Arthur. Ini celah konfigurasi/alur handoff dukungan yang terpisah dari eskalasi bisnis CS ke owner toko.

### Versi 1 dan knowledge kosong justru benar untuk agent yang dituju

| Record agent | Status | Versi | Dokumen |
| --- | --- | ---: | ---: |
| CS yang dibuat ulang 15 Oktober 15.39.46 WIB, `a2ec2077-…` | Aktif, tidak dijeda | 1 | 0 |
| CS lama, `7c730188-…` | Sudah dihapus, flag pause masih tersimpan | 18 | 3 |

Seluruh edit baru tertahan, sehingga versi 1 pada agent baru masuk akal. Tiga dokumen agent lama tidak otomatis menjadi knowledge agent yang dibuat ulang. Ini bukan bukti cache mengabaikan instruksi yang sudah berhasil disimpan.

Harga lama dan sapaan `kamu` pada demo dapat dijelaskan oleh instruksi “Kak”/harga baru yang belum pernah lolos mutasi. Penyimpangan gaya bahasa tambahan tetap perlu QA setelah penyimpanan benar-benar berhasil; belum bisa divonis sebagai kegagalan pembaruan konfigurasi runtime pada retest ini.

## 5. Isi kartu, expiry, pembayaran, dan kode refund

| Butir | Validasi dan penilaian |
| --- | --- |
| Nama WhatsApp `Agent` | Nilai `sender_name` memang tersimpan dari pengirim WhatsApp. Bukan bukti backend menukar identitas customer dengan owner. Nama generik masih kurang baik untuk UX kartu, tetapi berbeda dari pemalsuan identitas. |
| Nomor customer sama dengan owner | Sesi demo dan sesi Arthur sama-sama memiliki nomor owner QA ini. Backend menampilkan nomor penguji yang nyata; jangan menggantinya dengan nomor rekaan agar terlihat seperti customer lain. |
| ID kasus pada kartu | Berada di kartu owner dan dipakai korelasi reply. Bukan bukti kebocoran internal ke customer; laporan QA sendiri menyatakan kartu tidak lagi masuk chat customer. |
| Isi `Pesan` ditulis ulang | Benar. [escalation_tool.py](/home/clevio/stack/managed-agents-meta-embedded-signup/app/core/tools/escalation_tool.py:229) memilih `summary`/`reason` dari argumen LLM, bukan teks user asli. Label ini menyamarkan ringkasan sebagai pesan asli; ekspektasi kutipan verbatim belum dipenuhi. |
| Trial kedaluwarsa tetapi masih usable | Tool 09.21.40, 11.16.58, dan 11.20.31 menunjukkan `subscription_active=true`, `term_valid=false`, `expiry_enforced=false`. Deadline UTC 29 Agustus 21.57.28 = **30 Agustus 2026 04.57.28 WIB**. Ini konfigurasi rollout yang belum diaktifkan, sebagaimana [laporan deployment](ARTHUR_DEPLOYMENT_2026-10-16.md), bukan penyebab gate macet. |
| Growth = tier_1 | Benar: kode dan hasil tool 11.31.40 sama-sama Growth, Rp299.000/bulan, maksimal 5 assistant. Tidak ada mismatch paket yang terbukti di kasus ini. |
| Nomor WA pada URL pembayaran | Benar dan berasal dari kode, bukan karangan model. [payments.py](/home/clevio/stack/managed-agents-meta-embedded-signup/arthur_v2/payments.py:23) secara eksplisit membuat parameter `plan`, `wa`, dan `request`. Ini paparan identifier melalui URL yang perlu keputusan desain privasi, bukan kegagalan nama paket. Nilai lengkap URL tidak disalin ke laporan. |
| `REFUND-QA-7781` berulang | Ditemukan **6 balasan Arthur ke owner** yang memuat kode ini pada window analisis; **0 balasan CS ke customer** memuatnya. Repetisi ke owner terbukti; kebocoran kode ke customer belum terbukti. |

## 6. Mengapa pemeriksaan otomatis sebelumnya tidak menangkap ini

1. Tes positif konfirmasi menggunakan literal `ya` dan payload identik. Probe staging juga mengirim `ya` secara langsung, tanpa LLM yang memilih frasa `simpan` atau meregenerasi argumen. Lihat [tes regresi](/home/clevio/stack/managed-agents-meta-embedded-signup/tests/test_arthur_qa_regressions.py:186) dan [probe staging](/home/clevio/stack/managed-agents-meta-embedded-signup/scripts/arthur_qa_stage_probe.py:54).
2. Cakupan guard link menguji customer/owner pada agent operasional. Kasus user pemilik assistant yang berbicara dengan **builder Arthur**, tetapi bukan operator agent sistem, belum tercakup pada tes tersebut.
3. Probe staging tidak memakai koneksi WhatsApp nyata. Pengiriman kartu dan pemakaian receipt teruji pada komponen, tetapi perjalanan **demo CS → kartu pada Cloud Arthur → quote owner → kembali ke demo** tidak dibuktikan end-to-end.
4. Run berstatus `completed` berarti loop agent selesai, bukan mutasi berhasil. `needs_confirmation` merupakan hasil tool normal, bukan exception. Karena itu health API hijau dan 62 run completed tidak membuktikan fungsi pengelolaan berhasil.

## 7. Kesimpulan dan urutan penanganan berikutnya

**Bukan user QA yang salah mengikuti instruksi konfirmasi.** Regresi utama ada pada kontrak persetujuan yang tidak konsisten dan penempatan guard role. Untuk eskalasi, perbaikan tujuan pengiriman belum dilengkapi jalur balasan pada transport Cloud yang sekarang menerima kartu.

Urutan penanganan yang paling langsung berdasarkan bukti:

1. Kontrak konfirmasi: satu action/draft yang stabil, frasa UI yang benar-benar didukung, serta alasan penolakan yang bisa dibedakan. Memperluas daftar kata saja belum menyelesaikan perubahan digest.
2. Balasan eskalasi Cloud: baca konteks quote, petakan receipt ke sesi customer dengan pemeriksaan ownership yang benar, dan selaraskan aturan kirim/draft. Jangan sekadar menghapus filter agent lalu mencari semua tenant.
3. Guard link: terapkan konteks customer operasional tanpa memblokir user yang sedang mengelola assistant miliknya melalui Arthur. Perlindungan link terhadap customer tetap diperlukan.
4. Bukti aksi lintas giliran dan handoff dukungan Arthur: status tidak boleh ditebak dari teks jawaban sebelumnya atau dari kasus eskalasi agent lain.
5. Evaluasi expiry/URL pembayaran/format kartu sebagai pekerjaan terpisah setelah blocker utama selesai. Tidak mengaktifkan expiry global atau mereset memori untuk mencoba mengatasi loop konfirmasi.

Delete, create/interview, Google OAuth/Sheets baru, edit→demo, rollback, skill, web search, pengiriman ke nomor asing, admin palsu, serta policy “obat keras” tetap mengikuti status **belum diuji ulang/terblok** pada laporan QA. Analisis ini tidak mengubahnya menjadi PASS atau FAIL tanpa pelaksanaan yang sesuai.

**Tidak ada perbaikan atau perubahan produksi yang dilakukan pada tahap analisis ini.** Satu-satunya keluaran workspace adalah dokumen analisis ini.

## 8. Insiden tambahan: Lapi Bot gagal mendapat link koneksi WA, 13.13 WIB

Screenshot berikutnya dari user divalidasi terhadap message/tool records produksi dan log API tanggal **16 Oktober 2026**. Ini kejadian terpisah dari CS Toko Kue QA, tetapi memiliki akar masalah guard link yang sama.

| Waktu WIB | Bukti produksi |
| --- | --- |
| 13.12.59 | User meminta koneksi WA untuk Lapi Bot. |
| 13.13.19 | User memilih nomor WhatsApp Business sendiri. Arthur meminta persetujuan pembuatan link Meta Embedded Signup. |
| 13.13.39 | User membalas `ya`. Run `53f39ddf-c634-4549-af86-5c7708129b9b` memproses persetujuan tersebut. |
| 13.13.45 | `connect_assistant_whatsapp_cloud` dipanggil dengan target Lapi Bot dan `confirmed=true`. Hasilnya **`ok=true`, `connection_type=cloud_api`, serta `signup_url` terisi**. Nilai signed URL sengaja tidak disalin. |
| 13.13.45.819667 | Log API menunjukkan balasan berubah **497 → 133 karakter** lewat event `agent_run.final_reply_appended_google_auth_link`. Nama event menyesatkan: pada kasus ini guard justru mengganti balasan yang memuat link. Pesan tersimpan sama dengan teks penolakan di screenshot. |

**Pembuatan link berhasil; penyampaian link ke owner diblokir oleh aplikasi sebelum balasan dikirim.** Belum ada bukti proses koneksi di Meta selesai karena owner belum mendapat link untuk melanjutkan.

Pemeriksaan konteks sesi aktual menghasilkan `builder=true`, `classified_customer=true`, `demo_preview=false`, dan transport `meta`. User tersebut adalah pemilik assistant yang sedang mengelola Lapi Bot melalui Arthur, tetapi tidak termasuk operator internal agent sistem Arthur. `_is_customer_whatsapp_session` menganggapnya customer; `guard_customer_auth_links` mengenali `/meta/signup/` sebagai link akun owner dan mengganti seluruh balasan dengan pesan “koneksi internal perlu diperiksa pemilik”. Lihat [klasifikasi sesi](/home/clevio/stack/managed-agents-meta-embedded-signup/app/core/engine/agent_identity.py:72), [guard link](/home/clevio/stack/managed-agents-meta-embedded-signup/app/core/engine/agent_google_routing.py:225), dan [pemanggilan guard sebelum balasan akhir](/home/clevio/stack/managed-agents-meta-embedded-signup/app/core/engine/agent_runner.py:3600).

**Cakupan dampak lebih luas dari cohort QA:** owner Lapi Bot **tidak termasuk** `ARTHUR_CONFIRMATION_OWNER_IDS`, tetapi tetap terkena guard ini. Pembatasan rollout konfirmasi tidak membatasi guard balasan tersebut. Berdasarkan jalur kode, owner Arthur lain dengan klasifikasi sesi yang sama juga berpotensi terblokir; jumlah akun terdampak belum dihitung. Ini regresi guard pada rilis `c768a25`, bukan penolakan konfirmasi atau error Meta pada pemanggilan tool ini.

Produksi masih memakai image `sha256:d9ab842ad9326502b1c85a18e4c08d3a02542ea5b9826bc96ef020c231317469`. Tidak ada hotfix, rollback, pengiriman pesan, pembuatan link tambahan, atau perubahan data produksi selama pemeriksaan insiden ini.
