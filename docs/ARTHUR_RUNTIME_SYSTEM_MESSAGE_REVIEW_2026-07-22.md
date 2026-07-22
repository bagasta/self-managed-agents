# Review Alur Runtime dan System Message Arthur

Tanggal analisis: 22 Juli 2026
Scope: Arthur secara universal, bukan khusus BeeChat atau Minsel
Status: diagnosis dan rancangan diskusi; belum mengubah runtime

## Kesimpulan utama

Masalah pada percakapan Minsel bukan hanya karena Arthur “lupa” atau model LLM kurang pintar. Ada tiga lapisan masalah yang bertemu:

1. **Discovery belum memiliki state yang kuat dan persisten.** Fakta kebutuhan masih banyak direkonstruksi model dari histori chat. Validator baru bekerja ketika `plan_agent` atau `create_agent` dipanggil. Akibatnya, model bisa bertanya dengan kalimatnya sendiri, lalu validator mengeluarkan pertanyaan generik yang sama ketika field canonical belum terisi.
2. **Pembuatan agent dan aktivasi konektor belum diperlakukan sebagai satu transaksi selesai.** Agent dapat dinyatakan “sudah jadi” sebelum integrasi wajib, OAuth, target spreadsheet, dan uji tulis benar-benar siap.
3. **Source code, prompt di database, dan service yang sedang melayani WhatsApp dapat berbeda versi.** Mengubah `system-message-builder.md` dan push Git belum otomatis mengubah `Agent.instructions` di database atau me-restart backend produksi.

Percakapan ini sangat kuat menunjukkan runtime WhatsApp belum memakai seluruh source terbaru. Commit `58bd6bf` sudah dibuat pukul 14:01 WIB, sedangkan percakapan terjadi pukul 14:49–15:19 WIB. Source saat ini akan memblokir create jika enam grup discovery belum lengkap dan otomatis mencoba menambahkan link OAuth jika hasil create/update menandai Google aktif. Kedua perilaku itu tidak terlihat di chat. Ini masih sebuah inferensi karena log dan commit service produksi tidak tersedia dari mesin lokal; tidak ada backend lokal atau Docker daemon aktif untuk dibandingkan.

## Bedah percakapan Minsel

### 1. Kebutuhan awal berubah, tetapi Arthur belum melakukan re-framing

User awalnya mengatakan ingin CS untuk menjawab pertanyaan pelanggan, lalu memperjelas bahwa kebutuhan utamanya adalah survei kepuasan pelanggan lama. Ini perubahan workflow yang material:

- CS inbound: pelanggan memulai chat dan agent menjawab.
- Survei outbound: sistem atau operator memulai chat ke pelanggan lama, memilih target, menentukan waktu kirim, mencatat consent/opt-out, dan menyimpan hasil.

Arthur langsung menerima narasi “agent bakal chat pelanggan” tanpa memverifikasi apakah kebutuhan outbound tersebut didukung, dari mana daftar pelanggan berasal, siapa yang memicu pengiriman, dan bagaimana persetujuan pelanggan dikelola. Ini adalah janji kemampuan sebelum pemeriksaan platform.

### 2. Pertanyaan file berulang berasal dari dua pengambil keputusan berbeda

Pertanyaan pertama kemungkinan merupakan respons LLM berdasarkan rulebook:

> Apakah agent perlu menerima file atau membuat file/laporan?

Jawaban user “hasil surveynya disimpan ke spreadsheet” tidak secara eksplisit menjawab klasifikasi file. Google Sheets adalah penyimpanan cloud melalui konektor, bukan file attachment yang diterima atau dikirim melalui WhatsApp.

Setelah Arthur mencoba create, validator melihat `file_capability` belum berisi salah satu keputusan canonical berikut:

- `text_only`
- `receive_only`
- `generate`
- `both`

`create_agent` kemudian mengembalikan blocker, dan `reply_guard` mengubah blocker itu menjadi pertanyaan file generik. Jadi dugaan user benar sebagian: pertanyaan kedua bukan semata-mata improvisasi agent; ia dapat berasal dari guard deterministik sistem. Namun akar masalahnya bukan guard itu sendiri. Akar masalahnya adalah:

- jawaban discovery belum dicatat sebagai state canonical saat percakapan berlangsung;
- pertanyaan sebelumnya tidak memiliki `question_id` yang bisa dideduplikasi;
- sistem tidak menjelaskan perbedaan “Google Sheets” dengan “file attachment”;
- model sempat berkata “sedang diproses” sebelum prasyarat create lengkap.

Respons yang benar setelah user menyebut spreadsheet seharusnya kurang lebih:

> Siap, berarti Google Sheets wajib. Sheets bukan file attachment. Saya pastikan satu hal: Minsel cukup chat teks dan menyimpan jawaban ke Sheets, tanpa menerima atau mengirim file di WhatsApp—betul? Selain itu, surveinya dimulai otomatis dari daftar pelanggan atau pelanggan membuka chat lebih dulu?

Dengan respons itu, pertanyaan tidak terasa berulang dan dua keputusan penting diselesaikan sekaligus.

### 3. Google Sheets dikenali dalam percakapan, tetapi tidak dijadikan postcondition

Arthur berkata bahwa hasil survei akan masuk ke Google Sheets. Itu seharusnya membuat integrasi Google menjadi requirement eksplisit, bukan opsi tambahan. Alur wajibnya:

1. plan mengaktifkan Google Workspace;
2. create menyimpan konfigurasi Google pada agent;
3. verify membaca ulang konfigurasi dari database;
4. generate OAuth link;
5. user login;
6. verifikasi status auth;
7. pilih atau buat spreadsheet;
8. lakukan uji append satu baris;
9. baru nyatakan fitur Sheets siap.

Di chat, Arthur baru menyebut OAuth setelah demo diberikan dan masih menawarkannya sebagai pilihan. Untuk agent yang requirement utamanya menyimpan hasil ke Sheets, OAuth bukan upsell opsional; OAuth adalah prasyarat penyelesaian.

### 4. Balasan “Minsel sudah saya edit” adalah terminal state yang salah

Ketika user menjawab “mau” atas tawaran link OAuth, intent yang benar adalah `CONNECT_GOOGLE`, bukan `EDIT_AGENT` secara umum.

Jika Google belum aktif, Arthur boleh menjalankan `update_agent(enable_google_workspace=true)`, tetapi itu hanya langkah internal. Dalam turn yang sama ia harus melanjutkan:

`update_agent -> verify_agent/get_agent_detail -> generate_google_auth_link -> kirim auth_url`

Balasan terminal harus berisi link atau blocker konkret. “Minsel sudah saya edit” hanya melaporkan efek tool perantara dan tidak memenuhi permintaan user.

Source terbaru sebenarnya mempunyai auth-link guard setelah reply guard. Guard tersebut mencari `needs_google_auth`, `google_workspace_enabled`, atau hasil readback Google pada langkah create/update; jika ditemukan, runtime mencoba mengambil link secara otomatis. Karena chat tidak menunjukkan link maupun pesan kegagalan link, kemungkinan terkuat adalah:

- service live belum menjalankan source terbaru; atau
- update live tidak mengembalikan penanda Google/agent ID yang diperlukan; atau
- prompt/database Arthur live belum di-seed ulang sehingga model mengikuti kontrak lama.

## Alur kerja Arthur yang sebenarnya

```text
Pesan WhatsApp
  -> wa-service / wa-dev-service
  -> endpoint channel backend
  -> resolve Agent + Session + identitas owner/customer
  -> simpan pesan inbound
  -> muat histori, summary, memory, dan attachment
  -> susun tool berdasarkan policy dan tools_config Arthur
  -> susun system prompt gabungan
  -> jalankan LLM + tool loop
  -> auto-followup bila plan siap tetapi create belum dilakukan
  -> post-processing dan reply guards
  -> append OAuth link jika create/update membutuhkan Google
  -> simpan pesan outbound
  -> kirim balasan ke WhatsApp
```

Titik pentingnya: output akhir bukan hanya hasil model. Balasan dapat diubah oleh tool result, auto-followup, task guard, WhatsApp guard, builder reply guard, dan Google auth guard.

## Lapisan instruksi yang memengaruhi Arthur

### 1. Soul Arthur

`ARTHUR_SOUL` mendefinisikan identitas konsultan/arsitek, prinsip tidak menebak, enam grup discovery, demo-first, dan cara bicara. Soul disimpan sebagai memory agent.

### 2. Rulebook Arthur di database

`system-message-builder.md` adalah sumber teks rulebook, tetapi file tersebut tidak dibaca langsung pada setiap chat. `scripts/seed_arthur.py` membaca file lalu menulisnya ke `Agent.instructions` di database. Artinya:

- edit file saja tidak mengubah Arthur live;
- push Git saja tidak mengubah Arthur live;
- deploy backend tanpa menjalankan seed/migrasi prompt masih dapat memakai rulebook lama.

### 3. Directive runtime hardcoded

`prompt_builder.py` menambahkan context agent, waktu, layered memory, tool contract, aturan builder, aturan Google, keamanan, attachment, dan berbagai directive situasional. Directive ini dapat menduplikasi atau mengoreksi rulebook database.

### 4. Deskripsi dan schema tool

LLM memilih tool dari nama, description, dan schema argumen. Pada Arthur ada 24 builder tools. Deskripsi tool yang panjang ikut menjadi instruksi operasional dan dapat bertentangan atau tumpang tindih dengan rulebook.

### 5. Prompt pembuat artefak agent

Agent baru tidak ditulis oleh satu prompt tunggal. Ada prompt/fallback terpisah untuk:

- blueprint workflow;
- operating manual/SOP;
- instructions/system message agent;
- soul/personality;
- validasi konfigurasi.

Jika input faktualnya lemah, writer prompt yang bagus tetap dapat menghasilkan detail yang tampak meyakinkan tetapi sebenarnya asumsi.

### 6. Validator dan reply guard deterministik

Discovery gate, entitlement gate, file-capability gate, create verification, completion followup, serta OAuth append bukan system message. Bagian ini harus diperbaiki dengan state dan kode, bukan prompt saja.

## Kapabilitas tool Arthur

Arthur dikonfigurasi sebagai agent `system + builder`. Ada 24 builder tools yang tersedia pada runtime builder:

### Pemeriksaan platform dan user

- `get_self_config` — membaca konfigurasi Arthur sendiri.
- `get_platform_capabilities` — membaca kemampuan dan keterbatasan platform.
- `get_user_subscription` — mengecek paket, entitlement, dan slot agent.
- `link_dashboard_account` — menghubungkan identitas WhatsApp dengan akun dashboard.
- `get_presets` — membaca katalog preset agent.

### Discovery, desain, dan penulisan agent

- `plan_agent` — memvalidasi discovery dan membuat recommended config.
- `compose_agent_blueprint` — membuat workflow/state/approval blueprint.
- `compose_agent_operating_manual` — membuat SOP operasional.
- `compose_agent_instructions` — membuat system instructions agent.
- `compose_agent_soul` — membuat identitas dan gaya agent.
- `validate_agent_config` — memvalidasi konfigurasi sebelum penyimpanan.

### Lifecycle agent

- `create_agent` — membuat agent di database.
- `verify_agent` — membaca ulang readiness agent setelah create/update.
- `update_agent` — mengubah agent yang sudah ada.
- `delete_agent` — soft-delete agent.
- `get_agent_detail` — membaca konfigurasi satu agent.
- `list_my_agents` — membaca daftar agent milik user.
- `renew_agent` — memperpanjang masa aktif agent.

### Knowledge dan memory

- `set_agent_memory` — menyimpan memory/soul/blueprint target agent.
- `add_agent_knowledge` — memasukkan attachment sebagai knowledge base agent.

### WhatsApp dan demo

- `list_available_wa_devices` — membaca device WhatsApp yang tersedia.
- `create_wa_dev_trial_link` — membuat kode/link demo pada nomor trial.

### Google dan pembayaran

- `generate_google_auth_link` — membuat URL OAuth Google untuk agent.
- `get_payment_link` — membuat link pembayaran/upgrade paket.

Selain 24 tool tersebut, konfigurasi Arthur saat ini juga mengaktifkan kelompok runtime:

- memory dan heartbeat;
- skills;
- escalation;
- Tavily untuk riset eksternal bila API key tersedia;
- WhatsApp notify/media;
- WhatsApp agent manager.

Yang sengaja dimatikan pada Arthur sendiri:

- scheduler;
- sandbox/deploy;
- tool creator;
- RAG;
- HTTP internal;
- MCP pada Arthur;
- subagents.

`mcp=false` pada Arthur tidak berarti Arthur tidak bisa membuat child agent yang memakai Google Workspace. Arthur mengonfigurasi MCP child agent melalui builder tools internal dan menghasilkan OAuth lewat integration service. Pemisahan ini benar secara arsitektur, tetapi harus dijelaskan jelas dalam kontrak orchestration.

## Aturan utama yang saat ini dimiliki Arthur

- Panggil kemampuan platform dan subscription sebelum create.
- Selesaikan enam grup discovery dan minta konfirmasi rangkuman.
- Jangan menebak detail yang belum diberikan user.
- Jangan menanyakan jam aktif/jam operasional saat discovery.
- Untuk bisnis, definisikan pemicu eskalasi, penerima, dan nomor WhatsApp.
- Putuskan kemampuan file secara eksplisit.
- Untuk permintaan Google eksplisit, aktifkan Google dan buat link auth setelah create/update.
- Gunakan demo Arthur sebelum memasang nomor user, kecuali user meminta langsung.
- Jangan menjanjikan sukses sebelum tool result membuktikannya.
- Tolak use case politik manipulatif dan use case berbahaya.

Masalahnya bukan ketiadaan aturan. Masalahnya adalah jumlah dan pengulangan aturan. Rulebook saat ini sekitar 57 ribu karakter/711 baris, `prompt_builder.py` sendiri 93 ribu karakter/1.313 baris, Arthur memakai model `openai/gpt-4.1-mini`, output maksimal 2.048 token, dan harus memilih di antara banyak tool. Ini menciptakan risiko instruction dilution: aturan kritis ada, tetapi tidak selalu menjadi keputusan berikutnya yang paling kuat.

## Rancangan alur target

```text
INTENT
  -> DISCOVERY_CONTEXT
  -> DISCOVERY_WORKFLOW
  -> DISCOVERY_BEHAVIOR
  -> DISCOVERY_KNOWLEDGE_AND_FALLBACK
  -> DISCOVERY_DATA_INTEGRATION
  -> DISCOVERY_GOLIVE
  -> SUMMARY_CONFIRMATION
  -> PLAN
  -> COMPOSE
  -> VALIDATE
  -> CREATE
  -> VERIFY
  -> CONNECT_REQUIRED_SERVICES
  -> CONNECTOR_SMOKE_TEST
  -> DEMO
  -> USER_REVIEW
  -> ACTIVATE_OWN_NUMBER
```

### Prinsip state yang diperlukan

Setiap build harus memiliki record persisten, misalnya `agent_build_drafts`, dengan:

- `build_id`, `session_id`, `owner_id`, dan target agent;
- fakta discovery per field;
- sumber/evidence pesan user untuk setiap fakta;
- status `unknown / asked / answered / derived / confirmed`;
- `question_id` dan kapan terakhir ditanyakan;
- integrasi wajib dan status setup-nya;
- tahap orchestration saat ini;
- idempotency key untuk create, OAuth, dan demo.

LLM bertugas memahami bahasa dan merumuskan pertanyaan. Runtime menjadi otoritas perpindahan state. Dengan begitu, LLM tidak boleh melompati fase atau mengulang pertanyaan yang sudah `answered/confirmed`.

### Aturan deduplikasi pertanyaan

- Jangan merender pertanyaan dengan `question_id` yang sama dua kali berturut-turut.
- Jika jawaban user terkait tetapi ambigu, jelaskan perbedaan istilah dan tanyakan versi yang lebih spesifik.
- Validator tidak mengeluarkan copy generik jika assistant baru saja menanyakan topik yang sama; ia mengeluarkan follow-up kontekstual.
- “Sedang diproses” hanya boleh dikirim setelah semua precondition terpenuhi dan tool benar-benar mulai berjalan.

### Kontrak integrasi wajib

Setiap integrasi memiliki status:

`not_required -> required -> configured -> auth_pending -> authorized -> verified`

Agent tidak boleh disebut “siap” jika integrasi wajib masih `auth_pending`. Ia boleh disebut “agent dasar sudah dibuat, setup Google belum selesai”, tetapi bukan selesai penuh.

Untuk Google Sheets, readiness minimal harus mencakup:

- Google Workspace aktif pada config agent;
- OAuth authorized;
- spreadsheet target dipilih atau dibuat;
- struktur kolom disepakati;
- append test berhasil.

## Struktur system message yang disarankan

### A. Arthur Constitution — singkat dan stabil

Hanya berisi identitas, batas kewenangan, larangan mengarang, keamanan, bahasa, dan prinsip bahwa runtime state adalah sumber kebenaran. Targetnya jauh lebih pendek dari rulebook sekarang.

### B. State-specific directive — dibuat runtime setiap turn

Berisi:

- state build saat ini;
- fakta yang sudah confirmed;
- fakta yang belum ada;
- maksimal tiga pertanyaan yang boleh ditanyakan;
- tool yang boleh dipanggil pada state itu;
- terminal condition turn tersebut.

Contoh: pada `CONNECT_REQUIRED_SERVICES`, Arthur tidak diberi instruksi discovery/create lagi. Ia hanya boleh verify config, generate auth, dan menyampaikan link/blocker.

### C. Tool contract — dekat dengan tool, tidak diduplikasi di rulebook

Precondition, postcondition, dan error recovery diletakkan pada schema/description tool serta orchestration code. Prompt cukup menyebut tujuan user-facing.

### D. Composer prompts — menerima evidence ledger

Blueprint, SOP, instructions, dan soul hanya boleh memakai fakta dengan evidence. Detail default yang memang diizinkan user harus ditandai sebagai `proposed_default`, lalu terlihat dalam rangkuman sebelum create.

### E. Reply policy — hasil berdasarkan status, bukan nama tool terakhir

Contoh aturan terminal:

- `create success + required connector pending` -> “Agent dasar dibuat; selesaikan koneksi berikut.”
- `OAuth URL success` -> kirim URL.
- `update success tetapi tujuan user belum tercapai` -> lanjutkan tool chain, jangan berhenti.
- `connector test failed` -> beri blocker dan retry yang konkret.

## Discovery yang semestinya dilakukan untuk Minsel

Arthur sudah mengetahui:

- bisnis: Veselka, mukena premium;
- tujuan: survei pelanggan yang sudah membeli;
- nama agent: Minsel;
- operator: nomor user;
- pertanyaan survei boleh memakai draft standar;
- output: Google Sheets.

Arthur masih wajib memperoleh atau mengonfirmasi:

1. Apakah survei dikirim proaktif atau pelanggan membuka chat sendiri?
2. Dari mana daftar pelanggan dan nomor mereka diperoleh?
3. Kapan survei dikirim setelah pembelian dan apakah ada reminder?
4. Bagaimana menangani opt-out/tidak bersedia disurvei?
5. Pertanyaan final dan skala jawaban apa yang digunakan?
6. Kolom spreadsheet serta apakah memakai sheet baru atau existing?
7. Kapan jawaban negatif harus dieskalasikan dan kepada siapa?
8. Apa yang dilakukan jika agent tidak memahami jawaban bebas?
9. Tone dan contoh percakapan ideal/red-line.
10. Siapa yang menyetujui sebelum go-live?
11. Konfirmasi bahwa agent text-only di WhatsApp; Sheets bukan file attachment.

Jika user mengatakan “pertanyaannya sesuaikan saja”, Arthur boleh membuat **draft default pertanyaan survei**, tetapi tidak boleh menganggap itu izin untuk menebak sumber pelanggan, consent, trigger outbound, kebijakan eskalasi, atau struktur data.

## Prioritas perbaikan yang direkomendasikan

### P0 — sebelum mengubah wording lagi

1. Tambahkan version/commit SHA dan prompt version pada health/log setiap run.
2. Pastikan deploy backend, restart worker, dan seed Arthur menjadi satu prosedur rilis.
3. Ambil log run Minsel untuk membuktikan tool sequence dan payload create/update sebenarnya.
4. Tambahkan test transcript penuh yang memakai percakapan ini sebagai regression scenario.

### P1 — membuat flow benar-benar mulus

1. Persistenkan build state dan evidence di database.
2. Deduplikasi pertanyaan berdasarkan field/question ID.
3. Jadikan Google setup transaction wajib bila requirement eksplisit.
4. Jadikan reply terminal berbasis goal completion, bukan tool terakhir.
5. Tambahkan idempotency dan retry aman untuk create/OAuth/demo.

### P2 — perombakan system message

1. Pangkas rulebook global menjadi constitution ringkas.
2. Pindahkan aturan fase ke state-specific runtime directives.
3. Hilangkan instruksi Google/file/demo yang berulang di banyak tempat.
4. Selaraskan composer prompt dengan evidence ledger.
5. Uji prompt dengan transcript lintas use case: CS, survey, personal assistant, research, data analyst, content, coding/deploy, serta agent dengan dan tanpa Google/file.

## Keputusan yang perlu disepakati sebelum implementasi

| Keputusan | Rekomendasi |
|---|---|
| Discovery selalu panjang atau adaptif | Adaptif, tetapi field risiko tinggi tetap wajib. Tanyakan maksimal 2–3 pertanyaan per pesan. |
| Boleh memakai default saat user berkata “sesuaikan” | Boleh untuk konten draft berisiko rendah; tidak untuk permission, channel action, data source, eskalasi, dan integrasi. |
| OAuth sebelum atau sesudah demo | Sebelum demo jika integrasi merupakan fungsi inti; sesudah demo hanya jika integrasi opsional. |
| Kapan agent dinyatakan selesai | Setelah create, verify, seluruh konektor wajib authorized, dan smoke test fungsi inti lulus. |
| Siapa otoritas alur | Runtime state machine; LLM hanya memahami bahasa dan menulis respons/artefak. |
| Apakah cukup memperbaiki system message | Tidak. Prompt, state, tool contract, release process, dan observability harus diperbaiki bersama. |

Rekomendasi final: jangan menulis ulang seluruh system message terlebih dahulu. Pertama sepakati state machine dan terminal condition di atas. Setelah itu system message dapat dipangkas dan disusun mengikuti state, sehingga prompt tidak lagi menjadi satu-satunya pengendali proses.
