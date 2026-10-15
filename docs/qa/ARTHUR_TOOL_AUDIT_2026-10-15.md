# Audit tugas, tool, dan rencana QA Arthur

Tanggal: 15 Oktober 2026 (WIB). Cakupan: source checkout saat audit, konfigurasi Arthur aktif di database melalui transaksi read-only, dan perakitan tool tanpa menjalankan aksinya. Checklist eksekusi: [ARTHUR_QA_CASES_2026-10-15.csv](ARTHUR_QA_CASES_2026-10-15.csv).

## Hasil utama

- Arthur aktif memakai `system_plugin=arthur_v2`, capability `builder`, dan `max_tokens=16384`. Legacy tersedia di source tetapi default `arthur_legacy_enabled=False`.
- **32 tool inti V2**, terdiri dari 16 tool awal, 6 tool operasi P1, dan 10 tool pengelolaan P2.
- **19 tool pendukung** ikut masuk ke setup percakapan WhatsApp pada konfigurasi produksi yang diperiksa. Total hasil konstruksi: **51 tool**. Angka 32 saja belum mencakup seluruh permukaan QA Arthur.
- Kelompok aktif: `memory`, `skills`, `escalation`, `tavily`, `wa_notify`, `whatsapp_media`, `builder`. Tavily terkonfigurasi. Scheduler, sandbox, deploy, subagent, dan progressive-skills legacy tidak diaktifkan pada record Arthur yang diperiksa.
- Pembuktian 51 tool dilakukan dengan sesi sintetis dan `db=None`: tidak ada tool bisnis yang dipanggil, pesan dikirim, atau konfigurasi disimpan. Ini membuktikan daftar setup normal, bukan trace pemilihan tool oleh LLM pada semua jenis pesan.
- **147 tes terarah lulus, 2 dilewati.** Dua tes PostgreSQL memerlukan database khusus. Tidak dilakukan E2E chat berbayar, OAuth interaktif, signup Meta, transaksi pembayaran, pengiriman WhatsApp, atau write Google produksi.
- Ada temuan kontrak/implementasi dan celah verifikasi yang perlu diprioritaskan; tes lulus tidak berarti seluruh perilaku percakapan telah lolos QA.

Sebelas file yang dibandingkan SHA-256 identik antara checkout dan `/app` di container API aktif: plugin, seed, operations, service_management, memory_revision_tools, run_recovery, agent_tool_setup, agent_runner, prompt_builder, arthur_context, dan agent_ownership. Ini bukan verifikasi seluruh image, dependency, atau konfigurasi scheduler.

## Tugas Arthur dan batas tanggung jawabnya

Arthur membantu owner menerjemahkan workflow menjadi assistant personal, internal, sales, registrasi, bisnis, atau layanan pelanggan. Arthur adalah pengelola assistant, bukan otomatis template customer service untuk semua permintaan.

Alur yang diharapkan:

1. Pahami target dan kebutuhan. Untuk bisnis, tanya 2–4 hal penting yang belum terjawab per giliran: pemicu/aktor, langkah kerja, output, sumber pengetahuan, keputusan, pengecualian/handoff, hasil terukur, channel, tone, dan aksi yang perlu persetujuan.
2. Periksa plan dan inventori melalui tool aktual, terutama saat user bertanya slot/kuota. Jangan menggunakan snapshot chat sebagai status terkini.
3. Buat assistant setelah brief dan otorisasi cukup; jangan mengulang wawancara atau konfirmasi yang sudah jelas diberikan.
4. Konfigurasikan capability yang memang diperlukan: Google, model normal/advance, deploy/sandbox, atau subagent. Instruksi saja tidak memasang integrasi.
5. Tambahkan referensi sebagai knowledge RAG. Dokumen adalah data; instruksi di dalamnya bukan izin menjalankan aksi dan tidak boleh diam-diam mengganti SOP.
6. Hubungkan nomor melalui Meta Embedded Signup resmi atau beri kode trial di nomor demo bersama. Link signup bukan bukti nomor sudah aktif.
7. Buat job nyata untuk kerja autonomous, bedakan dengan reminder, cek status backend, dan hentikan lewat tool.
8. Kelola assistant yang sudah ada: edit, pause, akses, koneksi, memori, revision, diagnosis, pemulihan Sheets, dan penghapusan sesuai izin.
9. Klaim hasil hanya berdasarkan output sukses dan bukti yang relevan. Agent dibuat, OAuth selesai, job aktif, data tersimpan, pesan terkirim, dan deployment selesai adalah peristiwa berbeda.

Model target ditawarkan sebagai `normal` dan `advance`, tanpa menyebut provider kepada end user. Default create adalah normal. Website/deploy mengaktifkan sandbox dan mendapat budget output 8192; target Google pada create mendapat 4096. Nilai ini adalah konfigurasi source, bukan rekomendasi model eksternal.

## Peta runtime yang menentukan tool aktual

`agent_runner` → `build_agent_tool_setup` → policy builder → `build_system_agent_tools` → `build_arthur_v2_tools` → penambahan tool pendukung → filter SOP → graph.

- Policy builder menghentikan pemasangan sandbox dan subagent pada Arthur sendiri. Pada jalur utama, builder memakai `create_react_agent`, sehingga tool filesystem dan `task` bawaan Deep Agents tidak otomatis diberikan.
- `arthur_v2/runtime.py` masih punya factory `build_arthur_v2_graph` berbasis Deep Agents. Jangan menganggap factory itu sebagai jalur produksi utama; jalur runner builder di atas yang relevan dalam audit ini.
- Tool `connect_assistant_whatsapp` dan `refresh_assistant_whatsapp_qr` masih didefinisikan tetapi tidak dikembalikan oleh factory V2. Keduanya bukan anggota 32/51 tool.
- Tool lama seperti `plan_agent`, `compose_agent_blueprint`, `create_agent`, `verify_agent`, `renew_agent`, dan `send_agent_wa_pairing_code` milik legacy; bukan kontrak V2 aktif.
- Percakapan bertanda operator dapat mengubah daftar tool: builder tools tidak ditambahkan pada `operator_turn`, sementara `disable_ai`/`enable_ai` dapat ditambahkan. Verifikasi asal envelope dan trace untuk jalur ini secara terpisah.
- Setiap giliran dapat berbeda karena channel, flags, kunci layanan, admission/pause, dan guard runtime. Angka 51 berlaku untuk konstruksi giliran WhatsApp normal dengan konfigurasi yang diperiksa.

## Katalog 32 tool inti

Notasi: `confirmed` default `false`; default argumen lain disebut ketika penting. Kolom bukti menjelaskan apa yang harus diperiksa QA, bukan hasil E2E yang telah dijalankan.

### Inventori, plan, create, edit, dan runtime — 7 tool

| Tool dan input penting | Fungsi / prasyarat | Bukti dan uji negatif utama |
|---|---|---|
| `list_managed_assistants()` | Daftar assistant aktif yang lolos helper ownership; Arthur sendiri dikecualikan. | Cocokkan nama/ID dan is_deleted; owner asing tidak muncul; uji operator karena helper menerima operator sebagai ownership. |
| `get_current_plan()` | Plan, status subscription, slot terpakai/sisa, token, expiry, fitur marketing. Identifier memakai nomor owner/default target. | Bandingkan subscription aktual; unlimited direpresentasikan `null`; error jika identitas subscription tidak ditemukan. |
| `inspect_managed_assistant(agent_id)` | Nama, tujuan, instruksi, version, model_level dan status Google dari integration service jika connector terpasang. | UUID salah/asing/deleted/self ditolak; Google outage menghasilkan status unknown, bukan klaim putus. |
| `create_assistant(name,purpose,instructions,assistant_kind='personal',workflow?,enable_deploy=false,google_workspace_services=[],google_spreadsheet_url?,confirmed)` | Validasi isi/policy, workflow bisnis, plan aktif dan slot. Simpan agent, ownership, audit pembuat, RAG, kontrak tool; infer scheduler; mulai OAuth bila Google diminta. | `ok=true` dan ID tersimpan; bisnis wajib 5 field workflow; link Sheets hanya jika sheets aktif; OAuth bisa gagal setelah agent berhasil dibuat, jangan create duplikat. |
| `update_assistant(agent_id,purpose='',instructions='',confirmed,name='')` | Mengganti field non-kosong; instruksi adalah pengganti **seluruh teks**; nama maksimal 255 karakter; version bertambah. | Inspect sebelum/sesudah; instruksi tak terkait tetap dipertahankan oleh caller; edit tidak otomatis mengaktifkan automation. |
| `configure_assistant_runtime(agent_id,model_level?,enable_sandbox?,enable_deploy?,subagent_ids?,google_workspace_services?,mcp_servers?,confirmed)` | Normal/advance; merge flag; deploy membutuhkan sandbox; subagent custom harus assistant yang diizinkan. Google `None` mempertahankan, `[]` melepas connector. MCP kustom non-kosong ditolak. | Field omitted tetap; `subagent_ids=[]` memilih system subagents, bukan mematikan; jangan memperluas service Google; cek prompt/model/budget setelah perubahan. |
| `delete_assistant(agent_id,confirmed)` | Soft-delete agent. Implementasi hanya menandai deleted dan updated_at. | Tidak terlihat di inventori; cek job/binding/queue terpisah karena cleanup tidak sama dengan API delete; lihat F-03. |

Workflow bisnis yang divalidasi backend: `trigger`, `steps`, `outputs`, `knowledge_sources`, `exceptions_handoff`. Jenis yang memicu kewajiban ini: business/internal/sales/registration/customer. Decision rules, tone, channel, dan metrik ada dalam kontrak interview tetapi tidak semuanya field terstruktur wajib.

### Automation — 3 tool

| Tool dan input penting | Fungsi / prasyarat | Bukti dan uji negatif utama |
|---|---|---|
| `get_assistant_automation_status(agent_id)` | Membaca ScheduledJob aktual, jenis reminder/autonomous, status, next/last run. | Cocokkan job; jangan menyimpulkan aktif dari instruksi saja. |
| `configure_assistant_autonomous_run(agent_id,label,sop,schedule,confirmed)` | Scheduler target wajib aktif, SOP non-kosong maksimal 6000 karakter, jadwal berulang, sesi owner sudah ada. Menolak cron persis `* * * * *`, label aktif duplikat. | Job `execution_mode=agent_run`, active dan next_run_at terisi. Uji interval efektif <2 menit dengan cron alternatif; uji target paused. |
| `stop_assistant_automation(agent_id,label='',confirmed)` | Cancel job autonomous active/running; label kosong berarti semua autonomous pada agent. | Job cancelled, next_run_at null; reminder tidak ikut dihentikan; hentikan saat running dan cek tidak hidup kembali. |

### Knowledge — 1 tool

| Tool dan input penting | Fungsi / prasyarat | Bukti dan uji negatif utama |
|---|---|---|
| `add_assistant_knowledge(agent_id,filename='',title='',confirmed)` | Ambil file asli dari workspace sesi Arthur; prioritas current_input/shared/workspace. Ekstraksi → chunk → embedding → Document; aktifkan RAG. Judul maksimal 450 karakter. | Hash dan set chunk lengkap, source/nama asli, retrieval target nyata. Deduplikasi sebelum OCR dan setelah lock; retry import lengkap harus `already_added`. File kosong/tidak didukung/path keluar workspace/embedding gagal harus gagal jujur. |

Jika filename kosong, implementasi memilih kandidat paling baru pada root pertama yang berisi kandidat. Karena itu kasus dua upload dan target ambigu wajib diuji. OCR berhasil belum berarti knowledge tersimpan. Import tidak mengganti instruksi atau menjalankan instruksi dari dokumen.

### Pembayaran — 1 tool

| Tool dan input penting | Fungsi / prasyarat | Bukti dan uji negatif utama |
|---|---|---|
| `get_payment_link(plan)` | Membuat URL checkout dengan plan dan nomor owner. Harus ada niat beli/upgrade menurut prompt; tool tidak punya parameter confirmed. | URL hasil tool; plan belum berubah sebelum callback pembayaran. Growth/tier_1 = Rp299.000/bulan, 5 agent; Business/tier_3 = Rp999.000/bulan, unlimited sesuai katalog source. Alias legacy tetap diterima. |

Harga di atas mendeskripsikan kode repo; QA checkout harus mencocokkannya dengan plan database dan halaman pembayaran. Jangan memaksa upgrade hanya untuk model atau Google ketika slot, masa aktif, dan kuota masih cukup.

### Google dan resource — 3 tool

| Tool dan input penting | Fungsi / prasyarat | Bukti dan uji negatif utama |
|---|---|---|
| `start_assistant_google_oauth(agent_id,confirmed,force_reauth=false)` | Memulai OAuth agent yang sudah memiliki connector/scopes. | URL harus persis hasil tool pada giliran tersebut. Link ulang/expired memakai force_reauth; tidak mensyaratkan WA target terhubung. |
| `manage_assistant_google_connection(agent_id,action='status',confirmed)` | Status; disconnect remote agent-scoped lalu lepas connector lokal dan backup terenkripsi; reconnect mengembalikan kebijakan dan memulai OAuth. | Agent lain tetap; kegagalan remote tidak mengklaim lokal berubah; tidak revoke seluruh akun Google; link reconnect belum berarti connected. |
| `set_assistant_google_spreadsheet(agent_id,spreadsheet_id,expected_version,confirmed)` | Memverifikasi metadata resource dengan koneksi sekarang lalu mengganti target ID/URL/title dan referensi ID lama di instruksi. | Versi stale ditolak; tidak ada write Sheet. Kontrak append-only khusus ditolak. Header baru belum otomatis cocok dengan SOP. |

Layanan Google yang diterima konfigurasi: sheets, drive, docs, forms, slides, calendar, gmail, tasks, contacts, chat. Daftar ini tidak membuktikan setiap operasi provider tersedia; filter MCP, scope OAuth dan tool runtime target tetap menentukan akses.

### WhatsApp — 5 tool

| Tool dan input penting | Fungsi / prasyarat | Bukti dan uji negatif utama |
|---|---|---|
| `connect_assistant_whatsapp_cloud(agent_id,confirmed)` | Buat signup_url Meta Embedded Signup singkat untuk target. | User menyelesaikan flow resmi; status target dibaca lagi. Jangan menawarkan QR/pairing untuk own-number. |
| `get_assistant_whatsapp_status(agent_id)` | Status Cloud API/coexistence atau legacy device. | Bedakan konfigurasi credential dan live connection; jangan menganggap field nomor saja sebagai bukti provider sehat. |
| `create_demo_whatsapp_trial(agent_id,confirmed,force_new_code=false)` | Kode reusable untuk shared demo dan wa.me URL jika nomor tersedia. | Kode milik target yang benar; reuse kecuali diminta baru; `/stop` mengakhiri sesi trial; nomor demo kosong menghasilkan warning. |
| `disconnect_assistant_whatsapp(agent_id,confirmed)` | Lepas binding, cancel antrean queued, pause job, detach transport sesi. Tolak run/pengiriman/job sedang berjalan. | Idempotent; legacy delete device 404 diterima; Cloud disconnect lokal bukan Meta deregistration. History tetap. |
| `transfer_assistant_whatsapp(source_agent_id,target_agent_id,confirmed)` | Pindah binding dua agent yang diizinkan secara transaksional dengan urutan lock konsisten. Target harus kosong; routing n8n ditolak. | Binding hanya satu pemilik; source dilepas sebelum flush target; queue cancel/job pause kedua agent; history tidak dipindah. |

### Diagnosis dan recovery — 4 tool

| Tool dan input penting | Fungsi / prasyarat | Bukti dan uji negatif utama |
|---|---|---|
| `diagnose_assistant(agent_id)` | Ringkas kemampuan, Google/WA, 5 run terakhir, status queue/job. | Tidak membuat OAuth, tidak mengirim, tidak membocorkan credential/isi customer; outage diberi unverified. |
| `test_assistant_google_tools(agent_id,range_name,mode='read',confirmed=false,include_values=false)` | Baca Sheet configured; write_test hanya satu sel kosong tab `Arthur_Test*`, dengan izin write dan konfirmasi. | Default nol write, values hanya bila diminta; marker dibaca ulang dan hanya marker sendiri yang dibersihkan. Periksa hasil cleanup ketika timeout. |
| `prepare_assistant_run_recovery(run_id,updates?)` | Periksa run gagal/fallback terbaru dan trace. Dengan update eksplisit: baca preimage, simpan plan, beri token untuk review. | Tanpa Sheet write/pesan; plan TTL 10 menit; 1–5 range, masing-masing maksimal 100 sel, total JSON maksimal 16000 karakter, range tidak overlap dan ukuran nilai tepat. Menyimpan plan berarti metadata Run berubah meskipun tidak ada write eksternal. |
| `recover_assistant_run(run_id,plan_token,confirmed)` | Jalankan plan Sheets yang disetujui; cek token/TTL/version/pesan/data terbaru dan baca balik. | Retry tidak mengulang write yang sudah terverifikasi; status uncertain dipertahankan. Tidak replay LLM, tidak mengirim pesan. Run dengan tool selain metadata/read/update Sheets ditolak untuk recovery ini. |

### Pause dan admission — 3 tool

| Tool dan input penting | Fungsi / prasyarat | Bukti dan uji negatif utama |
|---|---|---|
| `set_assistant_paused(agent_id,paused,confirmed,resume_schedules=false)` | Pause service tanpa delete/disconnect; cancel queued, pause job aktif. Resume default hanya layanan. | Tidak ada runtime/scheduler/outbound baru ketika paused; opt-in resume hanya job yang dijeda tindakan ini, waktu masa depan, tidak replay backlog. |
| `get_assistant_access(agent_id)` | Owner, operator, operator eskalasi, mode admission, allowed customers, version. | Cocokkan seluruh sumber role; daftar pelanggan tidak memberi hak admin. |
| `configure_assistant_access(agent_id,customer_access?,allowed_customers?,add_operators?,remove_operators?,expected_version?,confirmed)` | Mode all/allowlist/operators_only; tambah/hapus operator; versi wajib. | Owner tak boleh dihapus; allowlist harus non-kosong; normalisasi nomor; menghapus operator eskalasi membersihkan sumber role kedua. Customer admitted tetap dapat memakai workflow Google target. |

### Memori target dan revision — 5 tool

| Tool dan input penting | Fungsi / prasyarat | Bukti dan uji negatif utama |
|---|---|---|
| `inspect_assistant_memory(agent_id,scope,offset=0,limit=20)` | Scope nomor customer atau global; limit 1–50, offset nonnegatif; preview 2000 karakter dan etag isi penuh. | Tidak mencampur customer; scope administrasi ditolak; pagination benar. |
| `edit_assistant_memory(agent_id,memory_id,expected_etag,action,value?,confirmed)` | Set/delete item tepat; set maksimal 16000 karakter; arsip sebelum perubahan. | Etag stale ditolak; admin archive terlindungi; tidak mengganti knowledge/instruksi. |
| `reset_assistant_context(agent_id,scope,expected_count,confirmed)` | Arsip/hapus memori scoped dan reset konteks sesi nomor itu. | Count stale/global ditolak; history audit tetap, summary lama terbuang, global/RAG/customer lain tetap. |
| `list_assistant_revisions(agent_id,limit=10)` | Arsip revision, limit 1–30; tidak mengarang history sebelum pencatatan tersedia. | Version/stage/tanggal tepat; tanpa credential/raw config. |
| `rollback_assistant_revision(agent_id,revision_id,expected_version,fields,confirmed)` | Preview lalu restore field eksplisit; false memberi preview; true menerapkan; version tetap naik. | Stale/revision asing/field duplikat ditolak; token/credential kini, pause/access, ownership, quota, binding dan memori tidak dipulihkan dari snapshot lama. |

Field rollback: name, description, instructions, model, temperature, max_tokens, tools_config. Arsip disimpan terenkripsi pada scope `__arthur_control_v2__`; tidak boleh masuk recall customer.

## Katalog 19 tool pendukung

Ini tool Arthur sendiri, berbeda dari tool untuk mengelola target assistant. Sebagian tidak memakai `confirmed`; otorisasi berasal dari konteks percakapan dan guard masing-masing.

| # | Tool / input | Efek dan fokus QA |
|---|---|---|
| 33 | `remember(key,value)` | Upsert memori Arthur pada scope sender; pisahkan brief per proyek; jangan simpan credential/link OAuth sementara. |
| 34 | `recall(query)` | Baca key atau daftar key yang tersedia dalam scope; histori tidak boleh menggantikan status live. |
| 35 | `forget(key)` | Hapus item memori Arthur; bukan reset agent target. |
| 36 | `update_daily(content)` | Append catatan harian pada scope Arthur; uji tanggal/WIB dan isolasi sender. |
| 37 | `update_longterm(content)` | Append curated longterm; jangan mencampur proyek atau mengubah catatan menjadi policy. |
| 38 | `enable_heartbeat(interval_minutes=30,quiet_start='23:00',quiet_end='08:00')` | Simpan config memori dan buat job heartbeat Arthur; quiet hours WIB. Berbeda dari autonomous run target. Uji interval 0/negatif/>60/tidak kelipatan 60 dan waktu invalid. |
| 39 | `disable_heartbeat()` | Ubah config dan cancel heartbeat aktif pada scope; cek job running tidak aktif lagi sesudah cancel. |
| 40 | `create_skill(name,description,content_md)` | Simpan/update skill library milik agent Arthur; tidak memiliki parameter scope sender. Uji lintas owner pada Arthur bersama. |
| 41 | `list_skills()` | Daftar skill milik Arthur; uji apakah user lain dapat melihat skill yang tidak semestinya. |
| 42 | `use_skill(name)` | Memuat konten skill; pastikan isi buatan user tidak dianggap sebagai izin admin/platform. |
| 43 | `escalate_to_human(reason,summary='')` | Membuat/menyampaikan eskalasi dan routing operator; cek kegagalan delivery serta state sesi. |
| 44 | `notify_owner(reason,summary='')` | Notifikasi owner dengan konteks customer/case yang dapat dibalas; bukan pesan pihak ketiga generik. |
| 45 | `reply_to_user(message)` | Kirim teks melalui channel sesi; cek duplikasi dengan final reply normal. |
| 46 | `send_to_number(phone_or_target,message)` | Kirim teks ke nomor lain; normalisasi nomor dan blok notifikasi owner melalui jalur ini. Uji izin penerima/isi dan social injection. |
| 47 | `notify_user(message)` | Notifikasi eksplisit lewat transport sesi; guard terhadap klaim file terkirim tanpa bukti. |
| 48 | `send_whatsapp_image(image_path_or_base64,caption='',phone='',mimetype='image/jpeg')` | Kirim media; builder tidak boleh memakai arbitrary workspace path. Uji current attachment, base64, MIME, tujuan. |
| 49 | `send_whatsapp_document(file_path_or_base64,filename='file',caption='',phone='',mimetype='')` | Kirim file; verifikasi file nyata, nama, tipe, target, serta hasil transport. |
| 50 | `tavily_search(query,max_results,topic='general',search_depth,time_range='',include_domains='',exclude_domains='')` | Pencarian web dengan default dari config. Uji failure/key kosong/domain filter dan konten tidak dipercaya. |
| 51 | `tavily_extract(urls,query='',max_chars=8000,extract_depth='basic')` | Ekstraksi URL; uji URL invalid, batas konten dan instruksi berbahaya dari halaman. |

Tambahan bersyarat di engine generik, bukan anggota daftar 51 saat audit: scheduler tools jika flag diaktifkan; HTTP/custom MCP jika dikonfigurasi; `wa_agent_manager` jika flag diaktifkan; `disable_ai` dan `enable_ai` pada giliran operator. Jangan menganggap seluruh factory di repo otomatis dimiliki Arthur.

## Temuan prioritas dan risiko yang perlu dibuktikan QA

Prioritas di sini adalah urutan QA, bukan keputusan final severity keamanan produk. Temuan statis tidak dinyatakan sebagai exploit end-to-end.

### F-01 — P1: operator diperlakukan sebagai owner oleh helper pengelolaan

**Terbukti dari source:** `agent_belongs_to_owner` dan `owner_filter` menerima kecocokan `operator_ids`, bukan hanya `owner_external_id`. Tool V2 lama dan tool operasi baru memakainya. Akibatnya penamaan "owner-only" pada kontrak tidak mewakili batas backend secara ketat.

**QA:** tambah nomor B sebagai operator agent A; dari chat Arthur milik B, coba list/inspect, edit, delete, akses memori, rollback, dan ubah akses. Tetapkan apakah operator memang boleh mengelola semua itu. Jika haknya hanya menangani pelanggan, ini kandidat eskalasi privilege. Reachability dan keputusan produk belum diuji E2E.

Sumber: `app/core/domain/agent_ownership.py:146`, `:178`; `arthur_v2/plugin.py:642`; `arthur_v2/operations_common.py:24`.

### F-02 — P1: skill library Arthur tidak terisolasi per sender

**Terbukti dari wiring:** `build_skill_tools` menerima `agent_id` dan `db_factory`, tanpa scope owner; create/list/use skill diaktifkan default. Arthur adalah satu system agent bersama. Tool memori berbeda karena menerima scope sender.

**QA:** A membuat skill unik; B mencoba list/use dan overwrite nama yang sama. Ukur apakah kebocoran/perubahan lintas user sesuai desain. Uji apakah skill dapat memengaruhi aksi administratif tanpa persetujuan baru. Efek lintas percakapan belum dieksekusi pada data produksi.

Sumber: `app/core/engine/agent_tool_setup.py:170`; `app/core/engine/tool_builder.py:285`.

### F-03 — P1: hapus lewat Arthur tidak memakai cleanup API delete

**Terbukti dari source:** `delete_assistant` hanya menandai soft-delete dan waktu update. API `delete_agent` melepas Cloud binding serta membatalkan job active/running/paused; legacy device juga dicoba disconnect. Tes lifecycle yang lulus menguji API, bukan tool delete V2.

**QA:** agent dengan binding, job paused/active, serta queue dihapus lewat Arthur. Periksa status job/binding/queue segera, penggunaan kembali nomor, dan worker berikutnya. Scheduler punya guard deleted yang mengurangi risiko eksekusi, tetapi tidak membuat kedua jalur cleanup identik. Jangan menyimpulkan pesan pasti tetap terkirim.

Sumber: `arthur_v2/plugin.py:1322`; `app/api/agents.py:258`; `tests/test_agent_deletion_lifecycle.py:10`.

### F-04 — P1: race pada create/quota dan perubahan konfigurasi lama

**Indikasi statis:** snapshot slot diperiksa sebelum transaksi insert; tidak tampak reservasi slot atau lock quota di `create_assistant`. `update_assistant` dan `configure_assistant_runtime` tidak menerima expected_version atau mewajibkan idle seperti tool P2. Revision tracking menjaga version/arsip, tetapi tidak otomatis mencegah lost update field yang sama.

**QA:** dua create bersamaan pada slot terakhir; dua edit instruksi dari versi sama; edit ketika run berlangsung. Hasil yang harus disepakati: satu create sukses, perubahan stale ditolak/di-merge dengan jelas, dan tidak ada status selesai palsu. Belum diuji di PostgreSQL khusus pada audit ini.

Sumber: `arthur_v2/plugin.py:801`, `:896`, `:1101`, `:1337`; `app/core/domain/agent_revision_service.py:50`.

### F-05 — P1: assistant_kind bebas bisa melewati kewajiban workflow bisnis

**Terbukti dari skema/kondisi:** assistant_kind adalah `str`, bukan enum; workflow diwajibkan hanya jika nilainya termasuk lima jenis bisnis. Nilai salah eja/tidak dikenal atau personal untuk tujuan bisnis melewati guard terstruktur tersebut. Prompt masih dapat mencegahnya, tetapi backend bukan pertahanan lengkap.

**QA:** create dengan `assistant_kind='busines'` atau nilai asing, workflow kosong, purpose bisnis lengkap. Harapannya error validasi/normalisasi eksplisit. Jangan anggap interview otomatis teruji oleh unit test tool.

Sumber: `arthur_v2/plugin.py:145`, `:784`.

### F-06 — P2: larangan Google di prompt bertentangan dengan tool operasi baru

**Terbukti:** prompt menyatakan Arthur tidak boleh browse/read/write akun Google user. Namun probe Sheet, pergantian spreadsheet, dan recovery memang membaca atau menulis memakai delegasi koneksi target. Tidak ada pengecualian jelas untuk diagnosis/recovery di kalimat larangan itu.

**QA:** minta diagnosis Sheet, probe aman, pindah spreadsheet dan recovery yang sah. LLM harus memilih tool yang benar, bukan menolak secara mutlak, mengarang akses, atau menjalankan write di luar plan. Perlu definisi produk tentang pengecualian administratif yang diperbolehkan.

Sumber: `arthur_v2/plugin.py:500`; `arthur_v2/operations.py:197`; `arthur_v2/service_management.py:199`; `arthur_v2/run_recovery.py:144`.

### F-07 — P2: batas cadence autonomous/heartbeat belum konsisten

**Terbukti:** autonomous menolak string cron persis `* * * * *`, bukan menghitung semua jarak eksekusi. Ekspresi ekuivalen atau menit bersebelahan perlu diuji. Heartbeat memakai int tanpa batas positif dan interval >=60 dibagi bulat menjadi jam; permintaan 90 menit menghasilkan `90 // 60 = 1`, sehingga implementasi membentuk setiap 1 jam.

**QA:** autonomous `*/1 * * * *`, `0,1 * * * *`; heartbeat 0, negatif, 61, 90, 1440 menit, quiet hours invalid. Periksa validasi, waktu actual job dan pesan ke user. Source menunjukkan risiko; kasus ini belum dieksekusi dalam audit.

Sumber: `arthur_v2/plugin.py:1038`; `app/core/engine/tool_builder.py:172`.

### F-08 — P2: kontrak tool target dan budget output bisa stale setelah configure

**Terbukti dari perbandingan jalur:** create menambahkan guidance tool target dan memilih max_tokens untuk Google/deploy. Configure mengubah tools_config/model tetapi tidak memperbarui guidance instruksi atau budget tersebut. Update hanya menambahkan guidance jika heading kontrak belum ada.

**QA:** assistant FAQ diubah menjadi Google/deploy, lalu Google dilepas atau service diganti; cek instruksi, max_tokens, capability contract runtime dan kemampuan target menyelesaikan tool call/file. Kontrak runtime aktual tetap sumber otoritatif; jangan menyimpulkan semua assistant pasti gagal.

Sumber: `arthur_v2/plugin.py:829`, `:903`, `:1129`, `:1389`.

### F-09 — P2: metadata respons create berbeda dari record target Google

**Terbukti:** agent Google tanpa deploy disimpan dengan max_tokens 4096, tetapi `runtime.max_tokens` pada hasil create mengembalikan null kecuali deploy aktif. Ini bisa mengaburkan QA atau pelaporan runtime.

**QA:** bandingkan hasil tool dengan Agent.max_tokens untuk plain/Google/deploy. Jangan gunakan field respons itu sebagai bukti budget efektif sebelum diperbaiki/disepakati.

Sumber: `arthur_v2/plugin.py:903`, `:948`.

### F-10 — P2: suite legacy tidak dapat dikumpulkan

**Teramati pada eksekusi:** menambahkan `tests/test_builder_tools.py` menghentikan collection karena `app/core/tools/builder_tools.py:56` mengimpor `_is_plain_google_form_link_reference` yang tidak tersedia di `app/core/engine/google_mcp_support.py`.

Ini menghalangi suite tersebut, bukan bukti jalur V2 aktif gagal startup. Pisahkan status tes V2 dari coverage legacy yang belum bisa dijalankan. Tidak ada perbaikan source dilakukan dalam audit ini.

## Hasil pengujian yang benar-benar dijalankan

Lingkungan: container disposable dari image aplikasi aktif, source checkout bind-mount read-only ke `/src`, cwd `/tmp/qa` agar `.env` workspace tidak dibaca, database default diarahkan ke loopback port 1. pytest 8.4.2, pytest-asyncio 0.26.0, aiosqlite 0.22.1 dipasang hanya di container tes. Source aplikasi tidak diubah.

| Eksekusi | Hasil | Interpretasi |
|---|---|---|
| Konstruksi default WhatsApp di container tanpa Tavily key | 49 tool, semua 49 schema dapat dibentuk, sandbox null, subagent kosong | Memastikan 32 + 17 pendukung tanpa Tavily; tidak mengeksekusi aksi tool. |
| Konstruksi memakai settings container API dan konfigurasi Arthur yang diverifikasi | 51 tool, 7 group | Tavily menambah search/extract; menggunakan sesi sintetis dan db=None. |
| Suite gabungan termasuk test_builder_tools | Collection error | Missing import legacy F-10; suite tidak dianggap lulus. |
| Suite V2 tanpa GOOGLE_INTEGRATION_SERVICE_URL | 146 passed, 1 failed, 2 skipped | Tes disconnect mengasumsikan base URL terkonfigurasi tetapi fixture tidak menyetelnya. Gagal sebelum mocked request karena default URL kosong. |
| Suite V2 diulang dengan GOOGLE_INTEGRATION_SERVICE_URL=http://google-integration.invalid | **147 passed, 2 skipped** dalam 23,06 detik | URL dummy memenuhi prasyarat; request disconnect tetap di-mock oleh tes. Tidak membuktikan koneksi layanan produksi. |

13 file pada suite sukses:

- test_arthur_v2_updates.py
- test_arthur_v2_google_oauth.py
- test_arthur_v2_model_selection.py
- test_arthur_p1_operations.py
- test_arthur_p2_management.py
- test_arthur_knowledge_contract.py
- test_arthur_knowledge_recovery.py
- test_arthur_v2_knowledge_tool.py
- test_arthur_context.py
- test_arthur_plan_catalog.py
- test_agent_deletion_lifecycle.py
- test_whatsapp_binding_reuse.py
- test_arthur_p1_postgres.py (2 skipped)

Dua tes PostgreSQL yang dilewati: concurrent transfer binding dan concurrent configuration revision. Tes tersebut mewajibkan `ARTHUR_P1_TEST_DATABASE_URL` menuju host/database khusus dan tidak boleh diarahkan ke produksi. Hasil SQLite tidak menggantikan pembuktian row-lock PostgreSQL.

## Cara menjalankan QA manual

Gunakan checklist CSV sebagai test run, bukan catatan bahwa semua kasus telah lulus. Seluruh baris awal berstatus `NOT_RUN`; hasil automated suite di atas terpisah.

Siapkan owner A/B, operator O, pelanggan C/D; target personal, target bisnis Google pada spreadsheet uji, target Google read-only, target deploy, serta dua agent untuk transfer. Simpan agent ID/version sebelum aksi. Pakai nomor dan resource khusus QA untuk aksi kirim/write/signup/pembayaran.

Urutan: P1 lintas-user dan isolasi → create/edit/delete → OAuth/WA → knowledge/memory → scheduler/pause → Google diagnosis/recovery → rollback → tool pendukung dan gangguan provider. Status QA harus menyebut layer: respons chat, tool call, perubahan DB, efek provider, dan verifikasi baca balik.

Untuk setiap kasus, catat run_id/session_id, role sender terverifikasi, nama tool, argumen tersanitasi, hasil tool, state sebelum/sesudah, bukti eksternal, serta apakah retry membuat duplikasi. Jangan menyalin credential, URL OAuth lengkap, plan_token, atau data customer nyata ke laporan bug.

Kriteria kelulusan: seluruh kasus prioritas tinggi yang relevan lulus; tidak ada kebocoran lintas owner/customer; setiap mutasi sesuai otorisasi; tidak ada sukses palsu; retry aman; worker menghormati pause/delete/admission; E2E provider dan concurrency PostgreSQL memperoleh bukti sendiri. Tes statis/unit yang lulus tidak menutup temuan F-01 sampai F-10 secara otomatis.

## Rujukan implementasi utama

| Area | Source |
|---|---|
| Prompt dan tool inti | `arthur_v2/plugin.py:427`, `:631`, return-list `:1678` |
| P1/Google probe | `arthur_v2/operations.py:87` |
| Recovery | `arthur_v2/run_recovery.py:97` |
| Pause/access/Google | `arthur_v2/service_management.py:41` |
| Memori/revision | `arthur_v2/memory_revision_tools.py:72` |
| Inject tool dan policy | `app/core/engine/agent_tool_setup.py:93`, `app/core/engine/agent_policy.py:26` |
| Registry V2/legacy | `app/core/system_agents/registry.py:65` |
| Graph builder utama | `app/core/engine/agent_runner.py:1759` |
| Kontrak prompt runtime | `app/core/engine/prompt_builder.py:1296`, `:1337` |
| Memori/skills/heartbeat/media | `app/core/engine/tool_builder.py:101`, `:163`, `:285`, `:570`, `:775` |
| Eskalasi/pengiriman | `app/core/tools/escalation_tool.py:111` |
| Web search/extract | `app/core/tools/tavily_tool.py:92` |
| Ownership | `app/core/domain/agent_ownership.py:146` |
| Tracking revision | `app/core/domain/agent_revision_service.py:44` |
| Seed aktif | `arthur_v2/seed.py:21` |

Nomor baris menunjuk snapshot checkout audit; perubahan berikutnya dapat menggesernya. Laporan tidak mengubah implementasi aplikasi atau menjalankan mutasi bisnis produksi.
