# Analisis mendalam QA Arthur Batch 1–10

Tanggal analisis: 15 Oktober 2026. Seluruh jam di laporan ini adalah **WIB**. Snapshot kode: commit `d45edf3` pada branch `codex/meta-embedded-signup`.

Analisis mencocokkan laporan QA dengan source, record agent, sesi demo, pesan user/assistant/tool, revision terenkripsi, dokumen knowledge, dan subscription. Query database dilakukan dalam transaksi `READ ONLY`; tidak ada aksi bisnis, pengiriman pesan baru, perubahan konfigurasi, perbaikan source, atau pengujian ulang provider. Lima belas file utama yang diperiksa identik secara SHA-256 antara checkout dan container API aktif.

Temuan dibedakan menjadi **terbukti** (source dan/atau trace langsung), **indikasi** (mekanisme masuk akal tetapi konteks model/receipt belum cukup), **beda kontrak** (harapan QA berbeda dengan kontrak produk yang terimplementasi), dan **tidak terbukti** (bukti sekarang belum mendukung kesimpulan QA). Prioritas di bawah menilai dampak yang terverifikasi, bukan menyalin label P0/P1/P2 secara otomatis.

## Kesimpulan utama

Masalahnya melibatkan beberapa lapisan yang saling menguatkan: role demo, kelengkapan tool status, otorisasi yang bergantung pada LLM, sumber pengetahuan yang bertentangan, ingatan yang menyimpan jawaban lama, dan sebagian fitur administratif yang belum punya tool baca yang memadai.

Ada **satu masalah kritis tambahan** yang tidak dijelaskan secara spesifik dalam ringkasan QA: jawaban pendek `ya` bisa memicu pengiriman WhatsApp otomatis dari teks konfirmasi konfigurasi. Ini dibuktikan oleh dua receipt run, bukan hanya dugaan statis.

Beberapa kesimpulan QA perlu dikoreksi:

- Pause **benar-benar berhasil**. Kegagalan berikutnya adalah Arthur salah membaca status dan tidak menjalankan resume, sehingga agent terus diam.
- PDF harga dan FAQ **benar-benar tersimpan serta memiliki embedding**. Masalahnya bukan hilang saat upload; instruksi harga lama masih menjadi sumber eksklusif, memory menyimpan jawaban lama, dan Arthur tidak bisa menginventarisasi knowledge target lewat tool yang tersedia.
- Kedua sesi demo terkait eskalasi/OAuth memakai **nomor yang sama dengan owner agent**, dengan nama WhatsApp `Agent`. Karena itu, trace ini belum membuktikan link login owner bocor ke **nomor customer lain**. Ia membuktikan role uji customer tidak terpisah dari owner dan notifikasi owner bisa masuk ke percakapan demo yang sama.
- Error pembacaan memori `phone:...` terjadi, lalu pembacaan dengan nomor biasa berhasil sekitar 35 detik kemudian. Tidak tepat menyimpulkan seluruh memori customer tidak dapat dibaca.
- Tidak ditemukan balasan menyebut `25:00` sebelum input user terkait di sesi ini. Dugaan mengetahui pesan masa depan tidak didukung urutan database.
- Tes Google baca/tulis memang gagal di tengah alur, lalu **keduanya berhasil** pada bagian akhir Batch 10. Kegagalan awal dan kondisi akhir perlu dicatat terpisah.

## Ringkasan 19 kelompok laporan QA

| No. QA | Status analisis | Bagian yang bermasalah |
|---|---|---|
| 1. Eskalasi salah alamat | Terbukti ada role/percakapan bercampur; salah penerima lintas akun belum terbukti | Identitas demo tetap owner, metadata PushName dipakai sebagai nama customer, transport eskalasi memakai sesi demo, serta ada jalur error yang tetap mengembalikan sukses. |
| 2. OAuth owner ke customer | Link di demo terbukti; kebocoran ke customer berbeda belum terbukti | Gate OAuth memakai identitas nomor owner/operator; tidak ada role preview customer tersendiri pada sesi ini. |
| 3. Edit/knowledge tidak efektif | Sebagian terbukti; diagnosis cache belum terbukti | Harga instruksi eksklusif bentrok dengan RAG, memory lama terus dimuat, sapaan tetap Admin setelah edit Kak; update harga terbaru terjadi sesudah pause. |
| 4. CS diam sejak 14.22 | Terbukti dan akar masalah diketahui | Pause 14.21.50 berhasil; status menghilangkan flag pause; resume tidak dipanggil; gateway menerima status paused tanpa balasan. |
| 5. Halu/status keliru | Sebagian terbukti, sebagian dibantah trace | Pause aktif disangkal, pengetahuan/deployment tidak diperiksa dengan bukti yang cukup, serta generalisasi status dari tool yang tidak lengkap. Klaim pesan masa depan tidak terbukti. |
| 6. Memori error nomor | Terbukti pada argumen tertentu, kemudian pulih | LLM mengirim `scope=phone:...`; parser hanya menerima nomor biasa/global. |
| 7. Konfirmasi | Ada beda kontrak dan kelemahan otorisasi | Perintah edit/import eksplisit memang dihitung konfirmasi; boolean confirmed ditentukan LLM tanpa receipt persetujuan terstruktur. Ada alur yang tidak memenuhi janji preview. |
| 8. Trial kedaluwarsa aktif | Terbukti | `is_usable` hanya memeriksa status, tidak tanggal; create/quota memakai nilai tersebut. |
| 9. Aturan bisnis dilanggar | Sebagian terbukti; sebagian adalah konflik instruksi | Larangan order tidak sama dengan larangan intake/notifikasi; kontrak generik mendorong notifikasi pesanan; H-2 ada di PDF tetapi lama tidak di instruksi; tidak ada validasi tanggal bisnis deterministik. |
| 10. Interview/personal | Beda kontrak sekaligus celah klasifikasi | Personal sengaja boleh tanpa workflow; user secara eksplisit mempertahankan personal untuk tugas bisnis; backend percaya assistant_kind alih-alih sifat pekerjaan. |
| 11. Heartbeat 60 menit | Terbukti | NameError `timedelta`, run gagal dan tidak memberi respons yang menjelaskan kegagalan. |
| 12. Harga/file tidak ada | Terbukti penalarannya keliru; fitur baca target terbatas | Penyimpanan RAG, akses file asli, dan kemampuan kirim diperlakukan sebagai hal yang sama; Arthur tidak punya list/read knowledge target. |
| 13. Brief lama hilang | Terbukti pada jalur pengambilan brief | Tidak ada key proyek yang cocok, recall exact-key gagal, history terbatas, agent lama soft-deleted tidak bisa diinspect dengan tool normal. Rekreasi mengubah workflow. |
| 14. Info internal | Sebagian terbukti; token credential belum terbukti | UUID/version/tool/error tampil mentah. Kata token dalam record yang ditemukan adalah kuota, budget output, atau placeholder recovery, bukan access token yang terverifikasi bocor. Nomor payment URL memang ada menurut kode. |
| 15. Sapaan | Terbukti | Role owner, PushName Agent, memory profil owner, dan aturan Kak saling berkompetisi. |
| 16. Link Meta | Beda penamaan UX | URL platform benar-benar menjadi gerbang ke Meta. Menyebut domain platform sebagai link Meta secara langsung membingungkan, tetapi domain itu sendiri bukan bukti tautan palsu. |
| 17. Peringatan/upsell | Spam peringatan terbukti; upsell punya alasan slot | 37 dari 179 balasan Arthur hari itu cocok dengan pola peringatan akses all/pelanggan. Pengingat obat diminta ketika slot 1/1 terpakai. |
| 18. Kontradiksi | Per subkasus berbeda | Katalog paket aktif cocok dengan kode; RAG/tool visibility bercampur; revision before/after bukan korupsi otomatis; scope Google berasal dari inferensi luas; read/write probe berbagi preflight baca. |
| 19. UX/fitur kecil | Campuran bug, batas fitur, dan input tidak valid | Formatter mempertahankan backtick; tidak ada knowledge inventory atau create-spreadsheet pada Arthur; file ambigu dipilih terbaru; autonomous per menit memang dilarang dan target scheduler mati. |

## A. Pengiriman WhatsApp dari konfirmasi yang tidak terkait — tambahan kritis

**Prioritas: P0. Terbukti source + receipt run.**

Pada **14.01.59**, input user `ya` menghasilkan pengiriman WA berisi `ya, advance`. Pada **14.16.35**, input `ya` lain menghasilkan pengiriman berisi `ya, rollback`. Kedua tujuan yang dihitung dari argumen tool sama dengan nomor owner agent. Hasil yang tersimpan adalah `[SENT_TO_NUMBER:...]`, bukan sekadar teks janji mengirim.

Akar masalah:

1. `_is_direct_whatsapp_text_send_context` menganggap `ya` sebagai konfirmasi kirim jika history berisi nomor serta kata umum seperti `nomor` atau `draft`.
2. `_extract_direct_whatsapp_confirmation_payload` mengambil nomor terakhir dari history dan kutipan terakhir dari balasan assistant. Kutipan `ya, advance`/`ya, rollback` adalah instruksi konfirmasi perubahan konfigurasi, tetapi diperlakukan sebagai draft pesan.
3. `agent_runner` mengirim langsung melalui channel sebelum LLM memproses permintaan berikutnya, lalu mencatat step seolah `send_to_number` dijalankan.
4. Jalur ini tidak memanggil implementasi normal `send_to_number` yang memiliki guard khusus agar notifikasi owner memakai `notify_owner`.

Ini menjelaskan mengapa konfirmasi fitur lain dapat tersasar ke alur WA. Bukti receipt menunjukkan provider/transport mengembalikan message_id sesuai pemeriksaan kode; bukti tersebut tidak menjamin perangkat penerima menampilkan pesan kepada manusia.

Sumber: `app/core/engine/agent_whatsapp_guards.py:121`, `:159`; `app/core/engine/agent_runner.py:2059`; pembanding guard normal `app/core/tools/escalation_tool.py:398`.

Run pengiriman 14.01.59: `23b837b5-be7c-4812-b5c9-7999f3f1ffd0`. Run pengiriman 14.16.35: `bfdf2d9f-c5b4-4960-b28b-93db36be6fbe`.

## B. Agent diam: pause berhasil, status tidak lengkap, resume gagal

**Prioritas: P0 operasional. Terbukti source + timeline.**

| Jam WIB | Peristiwa | Bukti |
|---|---|---|
| 14.21.42 | User meminta pause CS Toko Kue QA. | Pesan user pada run `938ff9dc-b3f8-45af-aecb-a39edabe707e`. |
| 14.21.49–14.21.50 | Flag pause tersimpan, version 13 → 14. | Revision after v14 memuat paused=true; tool mengembalikan ok=true. |
| 14.23.57 | User menanyakan status. | Run `49508cc7-1eca-4d91-bdb0-c217fe0511eb`. |
| 14.24.10 | Arthur menyatakan belum di-pause dan menyangkal pause sebelumnya. | Ia memanggil inspect/access/diagnose, tetapi tidak ada flag pause dalam hasil tool tersebut. |
| 14.24.26 | User meminta melanjutkan CS. | Run `b11da560-67a2-4da9-be5f-b68e0e2c3578`. |
| 14.24.40 | Arthur menjawab sudah aktif sehingga tidak perlu dilanjutkan. | Tidak ada panggilan `set_assistant_paused(paused=false)`. |
| 14.28 dan 14.41 | Instruksi/harga dan konfigurasi Google berubah. | Revision berikutnya tetap paused=true. Mengedit agent tidak meresume layanan. |

`_summary.active` berarti `not is_deleted`, bukan layanan sedang berjalan. `inspect_managed_assistant` memakai summary tersebut; `diagnose_assistant` dan `get_assistant_access` juga tidak mengembalikan state pause. Jadi Arthur tidak memiliki status layanan yang lengkap dari jalur baca normalnya.

Di ingress WA, agent paused langsung mendapat respons API `{"status":"agent_paused"}` tanpa teks user. Gateway demo hanya punya fallback khusus untuk `send_failed`; status paused tidak menghasilkan penjelasan. Tidak muncul Run target baru karena request sudah dihentikan sebelum model.

Karena itu, status kode demo tersambung tidak membuktikan layanan AI siap membalas. Pada snapshot akhir, CS lama kemudian sudah soft-deleted; ada **CS baru dengan ID berbeda**. Kedua record tidak boleh digabung saat mengevaluasi update atau sesi demo.

Sumber: `arthur_v2/plugin.py:619`, `:724`; `arthur_v2/operations.py:156`; `arthur_v2/service_management.py:44`, `:79`; `app/api/channels.py:1782`; `wa-dev-service/router.go:415`.

## C. Role demo, eskalasi, OAuth, dan sapaan

**Prioritas: P0 jika terbukti lintas penerima; pada bukti sekarang, cacat role preview dan UX terkonfirmasi.**

Pada kedua sesi target yang terkait keluhan—CS lama dan Admin Order QA—`session.external_user_id` sama dengan `agent.owner_external_id`; `phone_number` juga nomor owner tersebut. Metadata `sender_name` bernilai `Agent`. Pesan masuk target diberi envelope `<OWNER> Role: OWNER/SUPERADMIN Name WA: Agent`.

Ini bukan pengambilalihan role berdasarkan nama tampilan. Role berasal dari nomor terverifikasi yang memang cocok dengan owner. Tetapi untuk kebutuhan QA “berperan sebagai customer”, tidak ada pemisahan persona customer pada sesi demo tersebut.

### Eskalasi

`escalate_to_human` menentukan penerima dari konfigurasi operator atau owner. Ia menyalin transport sesi customer, lalu mengganti `user_phone` menjadi nomor operator. Untuk sesi demo, pengiriman tetap melalui nomor demo. Jika pengirim dan owner adalah satu akun WhatsApp, pesan owner-facing memang masuk ke chat demo yang sama.

Nama customer dan nomor pada kartu eskalasi berasal dari PushName/phone_number sesi. Trace berisi nama `Agent` dan menyebut pengirim sebagai Owner/Admin, sesuai metadata yang diterima backend. Jadi QA benar bahwa identitas bisnis yang diharapkan tidak muncul; trace belum membuktikan backend mengganti nomor customer berbeda menjadi nomor owner.

Ada bug terpisah yang **terbukti statis**: setelah beberapa exception pengiriman, blok except hanya mencatat warning lalu fungsi jatuh ke return “Eskalasi berhasil ... SUDAH terkirim”. `notify_owner` membungkus hasil itu dan bisa kembali mengaku sukses. Jalur `send_result=None` untuk WhatsApp memang memiliki error return; kelemahannya adalah exception yang lolos ke catch serta verifikasi hasil kirim yang lebih longgar daripada receipt end-to-end. Ini tidak cukup untuk menyatakan semua notifikasi QA gagal dikirim.

### OAuth

Pada **15.18.46**, Admin Order QA memanggil `get_google_workspace_auth_link` dan mengirim link pada sesi demo. Gate di `prepare_agent_google_workspace_runtime` hanya melepas tool auth untuk sender yang bukan owner/operator. Sender QA ini lolos sebagai owner. Router blocker juga menganggap sesi tersebut bukan customer.

Dengan demikian, status yang tepat adalah **link owner muncul dalam percakapan yang dipakai sebagai simulasi customer**. Kebocoran ke nomor customer lain masih memerlukan trace dengan identitas penerima yang berbeda dari owner/operator. Source memang memiliki guard untuk customer biasa; keberadaan guard bukan bukti semua variasi identitas aman.

### Sapaan

Prompt runtime secara eksplisit menyarankan memakai sender_name untuk sapaan. Envelope owner dan memory profil menulis peran Owner/Admin. Ini menjelaskan “Halo, Agent”, “Admin”, atau sapaan formal. Namun setelah instruksi Kak tersimpan pada v13, balasan target sekitar **14.20.26** masih “Halo Admin”, sehingga kepatuhan terhadap edit sapaan memang gagal pada setidaknya satu giliran.

Sumber: `app/api/channels.py:133`, `:2243`; `app/core/engine/agent_identity.py:42`, `:60`; `app/core/engine/prompt_builder.py:1059`; `app/core/tools/escalation_tool.py:151`, `:216`, `:315`; `app/core/engine/agent_google_routing.py:226`, `:305`.

## D. Knowledge tersimpan, tetapi konflik sumber dan alat observasi

**Prioritas: P1 integritas jawaban; bisa berdampak kritis pada domain tertentu.**

Record CS lama berisi tiga dokumen, semuanya mempunyai embedding:

| Dokumen | Tersimpan WIB | Isi relevan |
|---|---|---|
| daftar_harga_tokokue.pdf | 11.43.43 | Brownies Lumer Rp72.500, Nastar Keju Rp140.000, H-2, ongkir dan jam toko. |
| faq_tokokue.docx | 11.45.25 | Custom tulisan, ketahanan produk, refund. |
| catatan_operasional.txt | 11.47.45 | Kurir, bahan dan penanggung jawab komplain. |

Instruksi target sampai v14 tetap memuat daftar harga lama dan kalimat **“satu-satunya sumber informasi”** serta hanya boleh menjawab berdasarkan daftar tersebut. Pada **14.17–14.21**, target mengutip Brownies Rp50.000 dan mengatakan Nastar tidak ada. Jawaban ini bertentangan dengan PDF tetapi sejalan dengan batas eksklusif pada instruksi saat itu.

Jadi import knowledge tidak otomatis memperbarui aturan perilaku atau harga di instruksi. Ini sesuai kontrak ingestion yang memang mempertahankan instruksi; masalah bisnisnya adalah dua sumber yang sama-sama disebut resmi tanpa aturan prioritas yang selaras.

Harga baru benar-benar masuk instruksi pada **14.28.02, v15**, setelah pause aktif. Tidak ada run target baru setelah pause untuk membuktikan bahwa model yang masih berjalan mengabaikan v15. Klaim “harga sudah diupdate tetapi live tetap memakai versi lama” perlu memperhatikan batas waktu ini.

Ada beberapa kelemahan kode pendukung:

- Memory target menyimpan jawaban lama dalam longterm/daily/active_context. Pada snapshot, longterm masih memuat harga lama dan jawaban Nastar tidak tersedia. Layer ini tetap dimasukkan ke prompt berikutnya, sehingga jawaban model sebelumnya bisa memperkuat kesalahan.
- Update instruksi tidak memperbarui atau menandai seluruh runtime memory lama sebagai superseded. Riwayat percakapan lama juga tetap di-load.
- Refresh agent tepat sebelum run hanya mencakup tools_config/access/ownership/escalation, bukan instructions/model/version. Ini risiko snapshot lama untuk request yang menunggu; belum terbukti sebagai penyebab dominan Batch ini.
- Retrieval otomatis hanya memasukkan sampai 3 dokumen dan masing-masing 1200 karakter. Pada tiga dokumen QA ini seluruh isi lebih pendek dari 1200; truncation bukan penjelasan utama kasus tersebut.
- Fallback keyword memakai kata-kata query sebagai kondisi gabungan; pertanyaan panjang atau envelope owner dapat membuat fallback terlalu sempit. Query embedding/provider juga bisa gagal dan hasil RAG kosong tanpa blocker yang terlihat user.
- `search_knowledge_base` didefinisikan pada rag_tool.py tetapi tidak dipasang oleh jalur setup yang diperiksa. Target tidak punya jalur pencarian knowledge eksplisit sebagai cadangan ketika konteks otomatis tidak cukup.
- Arthur tidak memiliki tool list/read dokumen target pada 32 tool intinya. `inspect_managed_assistant` tidak mengembalikan daftar knowledge; `diagnose_assistant` juga tidak. Status uploaded knowledge hanya memeriksa file di `shared/current_input`, bukan seluruh inventori dokumen historis.

Tidak ditemukan bukti yang cukup untuk menyebut ini semata-mata “cache model lama”. Bukti yang lebih kuat adalah konflik instruksi–knowledge, kontaminasi memory/history, serta visibilitas data yang terbatas.

Sumber: `arthur_v2/plugin.py:1101`, `:1177`; `arthur_v2/knowledge_context.py:25`; `app/core/engine/agent_runner.py:1020`, `:1325`; `app/core/engine/prompt_builder.py:501`, `:950`; `app/core/domain/document_service.py:134`, `:184`; `app/core/domain/memory_service.py:253`, `:338`, `:429`; `app/core/tools/rag_tool.py:30`.

## E. Aturan order, H-2, dan generic tool contract

**Prioritas: P1. Terbukti ada kontrak yang tidak selaras dan kepatuhan yang lemah.**

Instruksi CS melarang menerima/memproses pesanan, tetapi workflow-nya memang mengizinkan mencatat maksud pesan lalu memberi notifikasi ke pemilik. Tool contract generik yang ditambahkan saat create juga menganjurkan `notify_owner` untuk pesanan baru. Karena itu, notifikasi “user ingin pesan” belum otomatis berarti transaksi/order resmi diterima.

Masalah yang terlihat adalah model memakai bahasa “saya catat pesanan” serta menanyakan data order tanpa membedakan intake dan penerimaan pesanan dengan jelas. Kepastian palsu mengenai order tetap perlu dinilai berdasarkan teks final dan efek eksternal. Pada CS lama tidak ada bukti penulisan order ke Google karena connector belum ada pada run-run awal.

H-2 sudah ada pada PDF sejak 11.43, tetapi baru dimasukkan ke instruksi pada v15. Sebelumnya instruksi hanya membolehkan daftar inline. Tidak ada validator tanggal usaha yang memaksa penolakan pesanan besok; SOP tetap bergantung pada model. Pada run terakhir CS, model juga menulis nama hari yang tidak konsisten dengan tanggal besok. Ini menambah bukti lemahnya penerapan aturan waktu, bukan kegagalan scheduler.

Sumber: `arthur_v2/plugin.py:337`, `:756`; snapshot workflow dan revision CS lama; run `f66096ce-c2ee-4d3d-9903-54980807f60c`.

## F. Konfirmasi dan interview: pisahkan kontrak dari bypass

**Prioritas: P1 untuk perubahan berisiko tanpa otorisasi terikat; sebagian laporan adalah beda kontrak.**

Prompt Arthur saat ini menyatakan permintaan jelas untuk menerapkan perubahan adalah konfirmasi. Contohnya “Ubah memori alergi itu jadi alergi susu” dan “Set akses ... hanya nomor saya sendiri” memang mengotorisasi aksi menurut kontrak yang terimplementasi. Import dokumen dengan target dan perintah eksplisit juga tidak mewajibkan pertanyaan konfirmasi kedua.

Dengan demikian, daftar edit tone/import/update harga/edit memori tidak bisa seluruhnya dinilai gagal hanya karena tidak ada dialog “ya” tambahan. Harapan agar setiap perubahan melewati preview lalu persetujuan adalah kontrak produk yang lebih ketat daripada implementasi sekarang.

Kelemahan backend tetap nyata: `confirmed=true` adalah argumen yang dipilih LLM. Tool tidak mengikatnya pada action ID, parameter yang direview, pesan persetujuan tertentu, atau batas waktu. Ini memberi perlindungan terhadap pemanggilan `confirmed=false`, tetapi belum membuktikan user menyetujui payload persis yang dieksekusi. Jalur kirim WA otomatis di bagian A bahkan menafsirkan “ya” tanpa pengikatan aksi.

Admin Order QA dibuat pada **15.10.33** setelah user awalnya meminta create, lalu secara eksplisit menjawab “Tetap personal” dan melengkapi data Sheets/handoff. Permintaan menampilkan draft muncul **15.11.30**, sesudah create. Trace ini tidak membuktikan user sebelumnya meminta preview-only yang diabaikan. Namun bila acceptance QA mensyaratkan draft selalu disetujui terpisah, produk memang belum memenuhi itu.

Untuk interview, `assistant_kind=personal` secara sengaja tidak mewajibkan workflow. Arthur bahkan mengingatkan tugas tersebut lebih cocok bisnis; user tetap memilih personal. Masalah backend adalah kewajiban workflow bergantung label yang dikirim model/user, bukan sifat pekerjaan. Agent bisnis bisa masuk jalur personal dan melewati lima field workflow wajib. Field bisnis itu sendiri juga belum memvalidasi kedalaman jawaban secara semantik.

Sumber: `arthur_v2/plugin.py:137`, `:482`, `:756`, `:1101`; `arthur_v2/service_management.py:91`; `arthur_v2/memory_revision_tools.py:92`.

## G. Trial expired masih dianggap usable

**Prioritas: P1 otorisasi layanan/billing. Terbukti.**

Subscription owner QA pada snapshot:

- status `trial`;
- expiry **30 Agustus 2026, 04.57.28 WIB** (record UTC: 29 Agustus 21.57.28);
- grace berakhir **2 September 2026, 04.57.28 WIB**;
- pada 15 Oktober, `is_usable` tetap true.

`UserSubscription.is_usable` hanya mengecek string status trial/active/grace_period. Ia tidak membandingkan waktu sekarang dengan expires_at/grace_until. Snapshot plan di Arthur dan gate create mempercayai property tersebut. Gate quota subscription juga mempercayainya; jalur pemeriksaan active_until per-agent hanya digunakan sebagai fallback ketika subscription tidak ditemukan.

Ini menjelaskan mengapa slot penuh tetap ditolak tetapi expiry tidak: kapasitas diperiksa, masa aktif efektif tidak. Selain bug expiry, balasan tanggal plan menggunakan tanggal UTC mentah sehingga menyebut 29 Agustus alih-alih tanggal lokal WIB.

Sumber: `app/models/subscription.py:83`; `arthur_v2/plugin.py:658`, `:801`; `app/core/domain/agent_quota_service.py:75`.

## H. Heartbeat 60 menit gagal karena NameError

**Prioritas: P1. Terbukti source dan Run.error_message.**

Run `ade5e705-fd90-426b-bdc0-e47dc7635ecd` pada **14.55.03** memproses permintaan 60 menit dan quiet hours 22.00–06.00. Status akhirnya failed dengan pesan `name 'timedelta' is not defined`.

Di `enable_heartbeat`, timedelta diimport dengan alias `_td`, tetapi perhitungan timezone memakai `timedelta(hours=7)` tanpa alias. Error terjadi sebelum job berhasil di-commit. Tidak ada tool-result sukses yang boleh dipakai untuk menyatakan heartbeat aktif. Jalur kegagalan juga tidak menghasilkan jawaban yang menjelaskan keadaan kepada user.

Risiko terkait yang masih ada di source: input interval tidak dibatasi positif; interval >=60 diubah menjadi jam dengan pembagian bulat, sehingga 90 menit menjadi 1 jam. Quiet hours juga tidak divalidasi ketat di tool. Ini terpisah dari kegagalan 60 menit yang sudah memiliki error konkret.

Sumber: `app/core/engine/tool_builder.py:172`, baris penggunaan timezone sekitar `:213`; worker `app/core/workers/scheduler_service.py:300`.

## I. Memori customer: format scope salah, lalu berhasil

**Prioritas: P2 pada kasus yang teramati. Terbukti, tetapi bukan outage menyeluruh.**

Pada **13.47.22**, Arthur dua kali memanggil inspect memori dengan `scope=phone:<nomor>`. Parser `scope_value` meneruskannya ke `phone_number`, yang hanya menerima nomor telepon biasa atau scope khusus global. Prefix `phone:` tidak dibuang; hasilnya error “gunakan nomor telepon lengkap”.

Pada **13.47.57**, Arthur memakai nomor biasa, berhasil membaca lima item, dan mengubah satu item melalui ID+etag dengan sukses. Jadi data ada dan mekanisme scoped memory bekerja untuk format yang didukung. Bagian bermasalah adalah argumen yang dikonstruksi model, kemampuan koreksi error, dan keterbatasan penanganan LID/format scope.

Sumber: `arthur_v2/memory_revision_tools.py:23`, `:75`; `arthur_v2/service_management.py:22`; run gagal `e563e432-d751-4dd3-a1e9-1ff6d3cedfed`, run sukses `1c46c288-6ccb-4f4f-8a30-e5ba3582ba6e`.

## J. File daftar harga tidak bisa dikirim bukan berarti knowledge hilang

**Prioritas: P1 pada klaim ketiadaan data; batas fitur media sendiri P2.**

Ada tiga objek berbeda: file upload asli di workspace Arthur, dokumen/chunk RAG pada agent target, dan media yang diizinkan untuk dikirim pada giliran kini. Import RAG tidak otomatis menyediakan fitur export file asli lewat tool target.

Arthur tidak mempunyai sandbox filesystem dan builder media dibatasi ke metadata/attachment yang diizinkan. Status live knowledge hanya memeriksa current_input, yang direset setiap ada input media baru. Setelah banyak giliran dan file lain, file harga historis tidak otomatis muncul sebagai attachment aktif.

Pada **14.59–15.00**, Arthur menjawab belum pernah menerima file harga tanpa tool verifikasi, meskipun record import dan Document masih ada. Ini salah inferensi: “tidak punya file yang bisa dikirim sekarang” berubah menjadi “file/knowledge tidak pernah ada”. Mengirim harga sebagai teks ke pihak lain juga tidak bisa diselesaikan hanya dari inventori tool jika Arthur tidak punya baca-knowledge target yang sesuai.

Sumber: `arthur_v2/knowledge_context.py:25`; `app/api/wa_helpers.py:41`; `app/core/engine/tool_builder.py:749`; `arthur_v2/plugin.py:1177`; klaim run `f7bc2ab4-06b8-41c6-b597-c2849cfe48a0`.

## K. Brief lama dan hasil deployment tidak dapat diambil Arthur secara andal

**Prioritas: P1 untuk rekreasi yang mengubah workflow; P2 untuk kekeliruan status URL.**

`recall('CS Toko Kue QA')` adalah lookup key, bukan pencarian semantik. Ia gagal pada 11.49 dan 15.38. Snapshot memory Arthur tidak mempunyai key proyek/brief yang cocok. Detail lama ada pada daily/longterm dan database target, tetapi recall otomatis Arthur sengaja mengabaikan layer besar tersebut untuk menghindari pencampuran proyek.

History Arthur dibatasi budget 20.000 karakter dan batas proyek. Inventory/inspect normal hanya melihat agent yang belum soft-deleted. Tidak ada tool khusus mengambil approved brief assistant yang sudah dihapus. Karena itu data historis yang tersimpan belum menjadi capability pengambilan brief yang dapat dipakai model secara andal.

Pada rekreasi terakhir, masalahnya lebih dari bertanya ulang: assistant lama melarang menerima pesanan, sedangkan agent baru dibuat dengan tujuan menerima pesanan dan harga lama. User hanya menjawab `buat` setelah Arthur mengaku brief tidak ditemukan dan berjanji menampilkan brief lengkap untuk konfirmasi. Tidak ada jawaban baru yang menetapkan workflow order pengganti tersebut. Ini menunjukkan rekonstruksi dari konteks yang keliru.

Untuk website, tool `deploy_app` dan `get_deployment_status` pada **15.37.44** mengembalikan URL nyata; target juga menyimpan `deploy_url`. Pada **15.38.41**, Arthur mengaku website belum pernah menghasilkan URL setelah hanya list dan recall proyek lain. `inspect_managed_assistant` tidak mengembalikan deployment receipts atau memory target; `diagnose_assistant` juga bukan inventori artifact. Klaim negatif itu tidak didukung verifikasi.

Sumber: `app/core/engine/arthur_context.py:26`, `:67`; `app/core/engine/agent_runner.py:1331`; `app/core/engine/tool_builder.py:111`; `arthur_v2/plugin.py:642`, `:724`; `arthur_v2/operations.py:156`.

## L. Informasi internal, paket, scope, dan error probe

**Prioritas: P2 kecuali ditemukan credential atau salah penerima.**

1. **UUID, versi, nama tool, flag dan error mentah:** informasi ini hadir di hasil tool/prompt lalu disalin ke output user. Tidak ada pemisahan presentasi yang cukup konsisten. UUID sendiri bukan secret; dampaknya berbeda dari access token.
2. **Token:** tujuh balasan Arthur yang mengandung kata token telah diperiksa. Isinya saldo kuota token, budget output 8192, atau penolakan placeholder recovery. Tidak ditemukan nilai access/refresh token yang terverifikasi bocor pada balasan tersebut. Pernyataan “token bocor” perlu dibedakan dari “istilah token tampil ke user”.
3. **Nomor pada payment URL:** `build_payment_link` memang memasukkan nomor owner dalam query parameter `wa`. Ini paparan identifier pada URL kepada pihak yang menerima/menyalin URL, bukan otomatis kebocoran token. Checkout aktif belum berarti subscription sudah berubah.
4. **Nama/harga paket:** database aktif saat pemeriksaan cocok dengan kode: Trial, Growth Rp299.000/5 agent, Business Rp999.000/unlimited. Tier_2 Pro masih ada tetapi inactive, dan alias legacy masih diterima. Bukti sekarang tidak mendukung mismatch seluruh katalog; klaim QA perlu menunjuk balasan spesifik yang berbeda.
5. **RAG unavailable:** retrieval otomatis aktif meskipun tidak ada explicit knowledge tool dalam runtime tool list. Menggunakan ketiadaan tool sebagai bukti RAG mati adalah salah pemetaan capability. Dalam data, tiga dokumen target memang tersimpan.
6. **Revision:** arsip mencatat before dan after dalam satu transaksi; satu nomor versi bisa muncul pada snapshot akhir perubahan sebelumnya dan snapshot awal perubahan berikutnya. Urutan di tool memakai created_at lalu UUID, sehingga before/after yang timestamp-nya sama tidak selalu tampil dalam urutan semantik. Versi yang melompat juga belum membuktikan korupsi: ada perubahan version tanpa perubahan field yang diarsipkan. Kontradiksi narasi Arthur tetap perlu dibedakan dari kerusakan snapshot.
7. **Scope Google:** target Admin Order terakhir menyimpan Sheets read/write, Gmail send, Calendar read/write; scope Calendar adalah `calendar.events`. Inferensi permission mengambil kata kerja dari seluruh instruksi, tidak mengikat setiap kata kerja ke layanan tertentu. “Balas customer” dapat berkontribusi pada Gmail send ketika Gmail ditambahkan; kata tulis umum dapat membuat Calendar write. Ini area ketepatan least-privilege, bukan sekadar istilah berbeda.
8. **Read/write probe error sama:** kedua mode membaca range lebih dulu. `read_values` hanya menerima pola teks provider tertentu, misalnya “Successfully read N rows” dan “Row N: [...]”. Jika preflight baca tidak cocok, write_test berhenti dengan error baca yang sama dan belum menguji tulis. Hasil itu bukan bukti write provider mati.

Pada **15.15**, model juga mengirim range tanpa nama tab sehingga ditolak. Pada **15.25**, read dan write_test gagal di parser/preflight. Pada **15.28.54–15.29.57**, read berhasil dan write_test berhasil dengan `cleanup_verified=true`. Ada satu percobaan write_test yang salah menyasar UUID Arthur sebelum model mengoreksinya ke Admin Order QA dalam run yang sama. Batas self/system agent menolak percobaan pertama.

Sumber: `arthur_v2/payments.py:25`; `app/core/google_oauth_scopes.py:40`, `:119`; `arthur_v2/memory_revision_tools.py:147`; `app/core/domain/agent_revision_service.py:44`; `arthur_v2/operations_common.py:159`; `arthur_v2/operations.py:197`.

## M. Peringatan berulang, Meta, format dan fitur yang belum tersedia

**Prioritas: P2, dengan beberapa keputusan produk yang belum konsisten.**

- **Peringatan akses all:** 37 dari 179 balasan Arthur hari itu cocok pola akses all/semua pelanggan. Angka ini hasil pencarian pola teks, bukan penilaian manual bahwa setiap kemunculannya salah. Namun contoh pada catatan harian/heartbeat jelas tidak relevan. Menyarankan operators_only sebelum agent melayani customer juga bertentangan dengan tujuan CS publik: customer yang diizinkan memang harus bisa memakai workflow, tanpa menjadi operator. Kode akses sendiri membedakan admission dari hak Google dengan benar; masalah utamanya narasi model yang diulang melalui history.
- **Upsell pengingat obat:** pada 15.04, plan terpakai 1/1 dan user meminta agent baru. Penolakan slot benar. Tawaran Growth punya dasar kapasitas, meskipun penyampaian dapat mengganggu. Ini bukan bukti Google/reminder sengaja dikunci paket. Bug expiry subscription tetap terpisah.
- **Link Meta:** source mengembalikan URL platform `/v1/meta/signup/l/<state>` yang membuka flow Meta. Nama “link resmi Meta” kurang menjelaskan bahwa gerbangnya di platform. Domain chiefaiofficer.id sendiri sesuai konstruksi link, bukan indikasi phishing dari temuan ini.
- **Backtick, * dan _:** converter memang mempertahankan inline backtick dan memakai * atau _ sebagai markup WA. Ia bukan sanitizer semua teks internal/format invalid. Tanda yang tampil literal masih mungkin muncul dari format model atau klien; perlu bukti payload final versus tampilan untuk menilai tahap yang salah.
- **Daftar knowledge:** tidak ada tool V2 untuk list/read seluruh knowledge target. Mengarahkan ke dashboard menggambarkan batas fitur, tetapi klaim “knowledge tidak ada” tetap salah jika belum ada bukti.
- **Dua file:** `add_assistant_knowledge` memilih file mtime terbaru bila filename kosong. Layer upload juga memusatkan current_input pada media terbaru. Tidak ada guard ambiguitas yang mewajibkan pemilihan eksplisit ketika beberapa kandidat ada. Ini cocok dengan gejala menebak file.
- **Automation:** permintaan yang tercatat adalah tiap menit, sedangkan kontrak minimum 2 menit. Target CS juga scheduler=false. Penolakan create pada kasus ini wajar, tetapi alasan “scheduler tidak tersedia di sesi ini” tidak menjelaskan keadaan target dengan tepat. Tool configure runtime tidak punya enable_scheduler untuk mengaktifkannya secara langsung setelah create. Ini celah pengelolaan capability yang terpisah dari interval yang invalid.
- **Spreadsheet baru:** Arthur V2 tidak mengekspos create_spreadsheet. Tool yang ada mengganti target spreadsheet yang sudah ada dan memverifikasi metadata. Jadi penolakan membuat file baru sesuai capability sekarang. Ada teks generic Google blocker yang masih menjanjikan Arthur bisa membuat/memilih Sheet; janji tersebut tidak selaras dengan tool V2.

Sumber: `app/core/utils/text_utils.py:11`; `arthur_v2/plugin.py:1215`, `:1337`, `:1604`; `arthur_v2/service_management.py:199`; `app/core/engine/agent_google_routing.py:329`; `app/core/domain/agent_service_controls.py:19`.

## N. Klaim pesan masa depan dan policy obat keras

Untuk `25:00`, database menempatkan input user pada **14.54.11**, baru balasan Arthur pada **14.54.15**. Tidak ada balasan Arthur yang mengandung `25:00` sebelum input itu pada sesi yang diperiksa. Permintaan 90 menit juga ada pada pesan user yang sama. Jadi belum ada bukti model mengetahui pesan yang belum diterima backend; perbedaan urutan tampilan klien atau delivery masih mungkin, tetapi belum dianalisis dari receipt frontend.

Tes policy “agent jual obat keras” tetap **NOT TESTED** sesuai laporan user. Audit ini tidak membuat agent baru untuk mengujinya. Helper policy deterministik yang diperiksa terutama berisi pola politik dan meta-builder; tidak ditemukan rule khusus obat keras pada helper itu. Ini tidak membuktikan model pasti mengizinkan atau pasti menolak permintaan tersebut.

## Urutan masalah yang layak dibawa ke triage

| Urutan | Area | Alasan berdasarkan bukti |
|---|---|---|
| 1 | Konfirmasi WA otomatis dari history | Sudah ada pengiriman yang tidak sesuai maksud persetujuan konfigurasi; dapat melibatkan efek eksternal. |
| 2 | Status pause dan resume | Menyebabkan layanan target benar-benar tidak membalas, dengan timeline yang cocok laporan QA. |
| 3 | Pemisahan role demo/owner/customer dan receipt eskalasi | Menentukan validitas pengujian kebocoran OAuth, penerima notifikasi dan sapaan; error path eskalasi juga dapat mengaku sukses. |
| 4 | Expiry subscription | Subscription lewat masa aktif masih usable dan lolos create/quota. |
| 5 | Konflik instruksi–RAG–memory | Harga/aturan yang dianggap resmi bertentangan dan jawaban lama memperkuat dirinya sendiri. |
| 6 | Brief/revision/artifact visibility | Arthur tidak bisa membuktikan status historis secara lengkap; rekreasi dapat mengubah workflow. |
| 7 | Heartbeat runtime exception | Input valid gagal karena NameError dan user tidak mendapat kejelasan hasil. |
| 8 | Konfirmasi terikat aksi dan klasifikasi workflow | Boolean dari LLM tidak cukup membuktikan persetujuan payload; label personal melewati structured workflow. |
| 9 | Scope memory/Google parser/kelengkapan capability | Error argumen, parser provider yang rapuh, dan batas fitur yang dijelaskan tidak konsisten. |
| 10 | Presentasi user | Info teknis, warning all berulang, Meta naming, tanggal lokal, dan markup. |

Ini daftar triage hasil analisis, bukan daftar perbaikan yang sudah dilakukan. Temuan lulus yang dilaporkan user tidak dibatalkan: delete, rollback, penolakan ID asing, kuota slot, penulisan Sheets, dan deployment memang memiliki bukti keberhasilan pada jalur yang diuji. Keberhasilan itu belum mencakup role customer berbeda, pengikatan konfirmasi, expiry, resume, atau interaksi history panjang yang bermasalah di atas.
