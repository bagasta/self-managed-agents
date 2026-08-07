# Recap Perbaikan Arthur V2 dan Agent Bisnis

Tanggal: 4 Agustus 2026

Dokumen ini merangkum perubahan yang dilakukan untuk kasus agent toko sembako dan fondasi SaaS yang dipakai oleh agent-agent buatan Arthur V2.

## Prinsip Arsitektur yang Ditetapkan

1. **Arthur V2 adalah control plane.** Arthur hanya menggali kebutuhan, membuat konfigurasi, workflow, dan instruksi agent milik user. Arthur tidak boleh membaca, menulis, atau menghubungkan OAuth akun Google user.
2. **Agent target adalah runtime worker milik user.** Setelah Owner menghubungkan Google Workspace pada agent target, agent tersebut dapat memakai kredensial Owner untuk melayani customer mana pun. Customer tidak diberi OAuth link atau akses Google.
3. **Runtime tool contract adalah sumber kebenaran.** Instruksi tidak menciptakan akses. Agent hanya boleh menggunakan tool yang benar-benar diinjeksi pada run itu dan hanya boleh mengklaim aksi selesai setelah tool berhasil.
4. **Identitas Owner berasal dari data platform.** Owner/operator tidak boleh disimpulkan dari nama tampilan WhatsApp. Runtime memakai `owner_external_id` dan/atau `operator_ids`.
5. **Eskalasi adalah case, bukan pesan WhatsApp biasa.** Case harus menyimpan customer target dan ID pesan WhatsApp agar balasan Owner dapat dirutekan kembali dengan benar.

## Masalah Awal yang Ditemukan

- Arthur membuat workflow dan instruksi terlalu umum; tidak mendefinisikan tool, urutan tool, atau kegagalan tool dengan cukup jelas.
- Arthur awalnya dapat mengurus OAuth Google. Hal ini melanggar pemisahan control plane dan runtime agent target.
- Link Spreadsheet yang diberikan saat pembuatan hanya menjadi teks dalam instruksi; agent tidak memiliki resource target yang terstruktur.
- Agent customer bisa dianggap sebagai Owner karena nama WhatsApp sama/serupa.
- Agent Google Sheets memuat 122 tool MCP pada setiap pesan, lalu menyaring mayoritas tool setelahnya.
- Tool Sheets penting seperti `append_table_rows` dapat ikut terbuang oleh filter nama tool.
- Notifikasi Owner untuk order/komplain dikirim memakai `send_to_number` biasa. Pesan ini tidak memiliki case, nomor customer, atau ID pesan yang bisa dipakai untuk quote routing.
- Bila Owner me-reply notifikasi biasa, agent tidak tahu customer tujuan dan mencoba mencari nomor di Spreadsheet.
- `notify_owner`/eskalasi sempat gagal pada agent buatan Arthur karena `escalation_config` legacy belum ada, walaupun agent telah memiliki `owner_external_id` yang valid. LLM lalu fallback ke `send_to_number` biasa.
- ID pesan WhatsApp eskalasi sebelumnya berisiko tidak tersimpan karena persistence dilakukan di luar lifecycle DB session awal.

## Perubahan Arthur V2

File utama: `arthur_v2/plugin.py`.

### Pemisahan OAuth

- Tool OAuth Google tidak lagi diekspos ke Arthur V2.
- Arthur tidak dapat memulai maupun mengecek OAuth Google milik user melalui tool Arthur.
- Arthur mengonfigurasi Google Workspace hanya pada agent target dalam status `auth_required`.
- Agent target yang memberikan link OAuth kepada Owner/operator saat runtime, dan hanya bila tool auth link memang tersedia.

### Konfigurasi Google Workspace Target

- `CreateAssistantInput` sekarang menerima `google_workspace_services`, misalnya `['sheets']`.
- Hanya service Google yang didukung platform dan diperlukan workflow yang dipasang pada agent target.
- Agent bisnis dengan Google Workspace memakai `delegated_runtime_access=True`: customer dapat memicu workflow, tetapi kredensial yang dipakai tetap milik Owner.
- Konfigurasi MCP kustom/arbitrary URL tidak lagi diterima Arthur. Saat ini connector produk yang didukung hanya Google Workspace.
- Konfigurasi runtime tidak menghapus MCP Google yang sudah dipasang sebelumnya.

### Resource Spreadsheet Terstruktur

- `CreateAssistantInput` sekarang menerima `google_spreadsheet_url`.
- URL divalidasi harus berupa `https://docs.google.com/spreadsheets/d/<id>`.
- Spreadsheet ID disimpan di `tools_config.google_workspace_resources` sebagai resource Owner-configured.
- Resource dari URL Owner **bukan** otomatis dianggap terverifikasi. Agent harus melakukan read pada runtime untuk memverifikasi akses, tab, dan header sebelum membaca stok atau menulis data.

### Kontrak Instruksi Target Agent

Arthur menyisipkan aturan tool yang terstruktur ke instruksi agent target:

- Tool harus dipanggil sebelum klaim baca/tulis/kirim berhasil.
- Resource, tab, header, stok, harga, record ID, dan nomor pihak ketiga tidak boleh ditebak.
- Untuk Sheets: baca struktur dahulu; append transaksi/komplain hanya setelah data lengkap; update stok hanya ke range yang sudah diverifikasi; read-back bila memungkinkan.
- Customer memakai workflow dengan kredensial delegasi Owner; OAuth link hanya untuk Owner/operator.
- Notifikasi terkait order, komplain, stok habis, atau persetujuan Owner memakai `notify_owner` atau `escalate_to_human`, bukan `send_to_number` ke Owner.
- `send_to_number` dikhususkan untuk pihak ketiga seperti supplier setelah nomor dan tujuan diverifikasi.

## Perubahan Runtime Google Workspace

File utama: `app/core/engine/agent_runner.py`, `app/core/engine/agent_google_routing.py`, dan `app/core/engine/google_mcp_support.py`.

### Delegated Runtime Access

- Customer session dapat menjalankan tool Google pada agent bisnis yang memang dikonfigurasi `delegated_runtime_access=True`.
- Runtime hanya mencari token Google pada identitas Owner/operator yang terverifikasi, bukan identitas customer.
- Customer tidak menerima tool `get_google_workspace_auth_link`; hanya Owner/operator yang dapat menyelesaikan koneksi.

### Resource dan Filter Sheets

- Runtime menyuntikkan resource Spreadsheet Owner-configured ke prompt sebagai resource yang harus dibaca terlebih dahulu.
- Filter Google service sekarang memiliki allowlist eksplisit untuk tool Sheets bernama generik, termasuk `append_table_rows`, `list_sheet_tables`, `modify_sheet_values`, dan `read_sheet_values`.
- Log filter sekarang hanya mencatat `removed_count` dan sampel nama tool, bukan daftar lengkap puluhan tool.

### Cache MCP Google

File utama: `app/core/tools/mcp_tool.py`.

- Sebelumnya setiap request menjalankan `MultiServerMCPClient.get_tools()` dan memuat 122 schema tool, baru kemudian menyaring tool sesuai service.
- Sekarang client MCP Google dan hasil `tools/list` di-cache selama 90 detik pada masing-masing API worker.
- Cache key berupa identitas agent + Owner + endpoint/transport; key tidak berisi bearer token.
- Cache memakai lease counter. Client tidak ditutup saat sedang dipakai run aktif.
- Cache hanya berlaku pada connector Google Workspace yang dipercaya dan dibatasi maksimum 32 entry per worker.
- Log baru:
  - `mcp_tools.cache_miss_loaded`: load pertama pada worker.
  - `mcp_tools.cache_hit`: request berikutnya memakai client/schema yang sama.

Catatan operasional: karena deployment memakai 5 API replica, request pertama yang masuk ke tiap replica masih dapat mengalami cold start satu kali. Cache ini menghilangkan load 122 tool pada request berikutnya yang masuk ke replica yang sama. Latensi run masih mencakup model LLM dan call baca/tulis Spreadsheet.

## Perubahan Eskalasi dan Notifikasi Owner

File utama: `app/core/tools/escalation_tool.py`, `app/api/wa_helpers.py`, `app/api/channels.py`, dan `app/core/engine/prompt_builder.py`.

### Tool Baru dan Guard

- Ditambahkan tool `notify_owner(reason, summary)`.
- `notify_owner` menghasilkan notifikasi yang dapat di-reply seperti eskalasi, tetapi agent tetap melanjutkan workflow order normal dan tidak memberi tahu customer bahwa order sedang dieskalasi bila tidak ada masalah handoff.
- `send_to_number` memblokir target nomor Owner/operator dan mengarahkan agent ke `notify_owner` agar case/routing tidak hilang.
- Guard tersebut menentukan Owner dari `escalation_config` bila tersedia, atau fallback aman ke `owner_external_id`/`operator_ids` bila agent Arthur tidak memiliki konfigurasi legacy.

### Isi Notifikasi Case

Notifikasi Owner standar memuat:

- ID kasus;
- nomor customer/user;
- nama customer bila tersedia;
- alasan;
- ringkasan/pesan customer;
- instruksi agar Owner me-reply pesan WhatsApp tersebut.

Nomor customer diambil dari metadata channel/session (nomor telepon riil bila ada, lalu JID/identitas yang tersedia), bukan dari Google Sheet.

### Quote Routing dan Draft

Alur yang didukung:

```text
Customer → agent membuat case/notifikasi Owner
          → ID pesan outbound WhatsApp disimpan pada metadata session
Owner reply pesan tersebut
          → quoted stanza/message ID dicocokkan ke customer session + case
          → agent membuat draft balasan untuk customer
Owner memilih Kirim / revisi / batal
          → `reply_to_user` mengirim ke customer yang terkunci pada case
```

- ID pesan outbound kini dipersist dalam DB session baru setelah channel send sukses, sehingga tidak bergantung pada DB context yang sudah ditutup.
- Draft pesan Owner yang quote eskalasi ditangani deterministik sebelum LLM agar model tidak sekadar menjawab “baik, saya catat”.
- Tidak ada lagi kebutuhan membaca Spreadsheet untuk mencari nomor customer saat Owner membalas eskalasi.

## Perbaikan Prompt Runtime

Prompt WhatsApp sekarang secara eksplisit menjelaskan:

- `notify_owner` untuk order/komplain/stok habis/update yang terkait customer;
- `send_to_number` hanya untuk pihak ketiga;
- `escalate_to_human` sudah mengirim notifikasi sendiri dan tidak perlu pesan manual tambahan ke Owner;
- ketika Owner me-reply quoted escalation, target customer telah terkunci dan alurnya draft → konfirmasi → kirim.

## Validasi yang Dilakukan

- `python3 -m compileall` pada file yang diubah.
- `git diff --check`.
- Smoke test image untuk tool `notify_owner` dan parsing Spreadsheet URL.
- Smoke test cache MCP dengan client palsu di container: dua context identik memanggil `get_tools()` tepat satu kali dan context kedua menghasilkan `mcp_tools.cache_hit`.
- Health check deployment: 5 API replica dan Redis sehat.
- Arthur V2 direseed dengan ID `df7ed4d2-e42c-4c60-a625-267e2a05c8f1`.

`pytest` tidak tersedia di host maupun production image, sehingga suite pytest tidak dieksekusi. Tidak ada dependency test yang dipasang ke production hanya untuk menjalankan test.

## Deployment Terakhir

- Image: `managed-agents-app:arthur-v2-google-mcp-cache-20260804-r4`.
- Service yang di-update: API project `deploy` dan reseed Arthur V2.
- Project/container lain di luar stack `deploy` tidak diubah.

## Migrasi Database Google Workspace MCP

Google Workspace MCP semula menyimpan `google_integrations` dan
`oauth_states` di PostgreSQL container terpisah (`google-workspace-postgres`,
database `google_workspace_mcp`). Hal ini membuat reset user pada database
`managed_agents` tidak menghapus token OAuth lama.

Migrasi yang dilakukan:

- Logical backup dua tabel OAuth dari database lama.
- Restore schema, constraint, index, dan data ke database utama
  `managed_agents`; hasil copy: 63 koneksi Google dan 419 OAuth state.
- Integration API Google diarahkan melalui `host.docker.internal` ke
  PostgreSQL utama.
- Google Alembic memakai tabel revision sendiri,
  `google_workspace_alembic_version`, agar tidak berbenturan dengan
  `alembic_version` milik managed-agents. Schema OAuth hasil migrasi ditandai
  pada revision Google `005`.
- Migration Google berhasil dijalankan sebagai no-op setelah perpindahan.
- Integration API sehat dan terverifikasi membaca `managed_agents`.

Database/container Google lama dipertahankan sementara sebagai sumber rollback;
integration API tidak lagi menggunakannya. Setelah periode verifikasi selesai,
database lama dapat didekomisioning secara terpisah.

Konsekuensi: reset user sekarang cukup memakai
`scripts/delete_user_google_oauth.sql` pada database `managed_agents`; script
ini kembali menghapus `oauth_states` dan `google_integrations` dalam transaksi
yang sama.

## Cara Verifikasi Manual Berikutnya

1. Chat sebagai customer dan pesan produk yang tidak tersedia.
2. Pada request pertama di satu replica, log dapat berisi `mcp_tools.cache_miss_loaded`.
3. Kirim request Sheets berikutnya dalam waktu kurang dari 90 detik; log pada replica yang sama harus berisi `mcp_tools.cache_hit`, bukan `mcp_tools.loaded` dengan count 122.
4. Buat kondisi yang memerlukan Owner (misalnya minta nomor rekening yang belum dikonfigurasi).
5. Pastikan notifikasi Owner memuat `ID Kasus` dan `Nomor customer/user`.
6. Owner me-reply notifikasi tersebut. Pastikan agent menampilkan draft balasan customer dan tidak meminta nomor customer atau mencari nomor itu di Sheet.
7. Owner mengetik `kirim`; pastikan pesan dikirim ke customer pada case yang sama.
