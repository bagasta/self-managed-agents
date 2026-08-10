# Arthur Progressive Skills — Implementation Report

Tanggal: 22 Juli 2026
Status: implementasi core selesai dan aktif pada database lokal; production canary belum dijalankan

## Outcome

Arthur tidak lagi bergantung pada rulebook monolitik sebagai prompt aktif. Runtime lokal sekarang memakai compact kernel, persistent build state, progressive system skills, state-scoped tools, deterministic attachment routing, dan guard runtime untuk mencegah klaim tanpa bukti serta pertanyaan kebutuhan yang berulang.

Konfigurasi Arthur yang telah di-seed ke database lokal:

| Peran | Model |
|---|---|
| Chat, discovery, orchestration, dan jawaban akhir | `deepseek/deepseek-v4-flash` |
| PDF, DOCX, dan PPTX | `mistral-ocr-latest` |
| JPEG, PNG, WebP, screenshot, foto, dan QR | `openai/gpt-4.1-mini` |

Runtime version yang aktif:

- engine: `arthur-progressive-v1`
- prompt: `arthur-kernel-v1`
- skill bundle: `arthur-skills-2026-07-22-v1`
- kernel database: 4.235 karakter
- active trusted system skills: 8

## Perubahan yang diimplementasikan

### 1. Compact kernel dan progressive skills

Source of truth berada di `arthur-skills/`:

- `arthur-discovery`
- `arthur-create-agent`
- `arthur-edit-agent`
- `arthur-google-workspace`
- `arthur-whatsapp-demo-channel`
- `arthur-files-knowledge`
- `arthur-subscription-payment`
- `arthur-lifecycle-safety`

Runtime memilih maksimal satu primary workflow skill dan satu policy mixin. Loader hanya menerima skill `system`, `immutable`, enabled, dan checksum-valid. Skill user tidak dapat menggantikan aturan sistem Arthur.

### 2. Persistent build state

Migration `023` menambahkan `agent_build_drafts` untuk menyimpan owner, session, intent, workflow state, evidence, question history, integration status, artifact status, confirmation, idempotency metadata, prompt/skill/engine version, expiry, dan optimistic `state_version`.

State tidak lagi hanya bergantung pada history LLM. Update memakai compare-and-swap sehingga dua turn yang berdekatan tidak boleh diam-diam menimpa predecessor yang sama.

Reset WhatsApp dan script reset user sekarang ikut menghapus build draft. Dengan demikian user yang di-reset tidak membawa konteks discovery lama, sementara cleanup OAuth Google tetap dicakup oleh script reset.

### 3. Anti-hallucination dan question deduplication

Kernel melarang Arthur mengarang fakta bisnis, nomor, permission, integrasi, link, kode, resource ID, hasil tool, isi attachment, atau status selesai.

Selain instruksi prompt, runtime menerapkan guard deterministik:

- pertanyaan dibandingkan berdasarkan canonical text;
- parafrasa dibandingkan berdasarkan requirement topic seperti pain point, nama agent, target user, tugas, knowledge source, eskalasi, file, integrasi, trigger, KPI, dan tone;
- evidence user yang sudah eksplisit dijawab menjadi answered requirement slot;
- seluruh pertanyaan pada balasan diperiksa, tidak hanya tiga pertanyaan pertama;
- jika provider hanya menghasilkan pertanyaan berulang, runtime mengembalikan balasan non-kosong yang menyatakan informasi lama sudah tercatat.

### 4. Tool scoping

Discovery tidak lagi melihat seluruh builder tool. Material tools seperti create, update, delete, payment, auth, dan demo hanya ditambahkan ketika primary skill atau mixin yang sesuai aktif. Safety enforcement server-side tetap dipertahankan.

### 5. Attachment routing

- Dokumen Arthur divalidasi lalu diproses lewat adapter Mistral OCR bersama. PDF selalu memakai document route; DOCX/PPTX memakai Mistral untuk Arthur.
- Gambar Arthur dikirim ke GPT-4.1 Mini untuk menghasilkan structured visual evidence. Raw image tidak diteruskan ke DeepSeek.
- Processor attachment tidak memperoleh builder tools dan tidak dapat mengubah workflow state.
- Kegagalan processor menghasilkan blocker jujur; tidak ada fallback silang yang membuat DeepSeek menebak isi dokumen atau gambar.
- Route/model/status attachment dicatat di runtime metadata.

### 6. Runtime observability

Setiap run dapat mencatat model, engine version, prompt version, loaded skill versions, primary skill, mixin, dan attachment route. `/health` menunjukkan konfigurasi source; `/health/detailed` membaca model dan bundle Arthur dari database beserta jumlah active system skills.

### 7. Seed dan dependency

`scripts/seed_arthur.py` sekarang:

- memuat compact kernel dari repo;
- memvalidasi ukuran kernel dan delapan skill;
- mengaktifkan model serta feature flags Arthur;
- menerbitkan versioned immutable system skills;
- tidak mencetak API key mentah.

`PyYAML` ditambahkan sebagai dependency eksplisit dan seluruh environment override model/version didokumentasikan di `.env.example`.

## Bukti verifikasi

### Database dan migration

- Alembic current: `023 (head)`
- Alembic heads: `023 (head)`
- Arthur database model: `deepseek/deepseek-v4-flash`
- max output tokens: `8192`
- system skills: `8`
- seluruh system skills immutable: `true`
- seluruh system skills memiliki checksum: `true`

### Provider smoke tests

- DeepSeek V4 Flash: tool-call smoke berhasil dan menghasilkan tepat satu tool call yang diminta.
- Mistral OCR: ekstraksi PDF valid berhasil.
- GPT-4.1 Mini: ekstraksi visual PNG valid berhasil. Fixture gambar 1x1 yang tidak layak sebelumnya ditolak provider, lalu test diperbaiki memakai PNG 64x64 valid; tidak ada fallback menebak.

### Runtime two-turn smoke

Percakapan discovery dua turn dijalankan melalui `run_agent` nyata dengan session sementara:

- model kedua turn: `deepseek/deepseek-v4-flash`
- engine kedua turn: `arthur-progressive-v1`
- primary skill kedua turn: `arthur-discovery`
- mutation tool calls: tidak ada
- overlap requirement topic antar-turn: tidak ada
- requirement slot yang sudah dijawab lalu ditanyakan lagi: tidak ada
- question history unik: `true`
- build draft tersimpan selama workflow dan terhapus setelah session test dihapus

### Automated tests

- focused Arthur/runtime tests: `50 passed`
- full maintained suite: `1009 passed, 9 skipped, 7 failed`
- ketujuh failure sama persis dengan baseline sebelum refactor, sehingga tidak ada regression failure baru:
  - payment bridge URL expectation
  - coding deploy preset expectation
  - trial expiry expectation
  - dua QR owner tests
  - dua WhatsApp spam-window event-loop tests
- compileall: lulus
- `git diff --check`: lulus
- seluruh 8 skill bundle: lulus `quick_validate.py`
- seed dry-run: lulus

### Fresh backend health

- `/health`: HTTP 200
- primary/document/image model, engine, dan prompt version: sesuai konfigurasi di atas
- database check: `ok`
- active system skills: `8`
- database skill bundle: `arthur-skills-2026-07-22-v1`
- `/health/detailed`: HTTP 503 hanya karena `wa_service=unreachable`

Backend test yang dibuat untuk verifikasi telah dihentikan; tidak ada proses Uvicorn test yang ditinggalkan.

## Release boundary yang masih terbuka

Implementasi ini belum boleh disebut production rollout selesai karena:

1. `wa-service` utama tidak sedang berjalan pada environment pengujian. Port 8080 ditempati proses VS Code, bukan health service WhatsApp. Service tidak dipaksa hidup agar dedicated Arthur device dan shared trial boundary tidak tercampur atau terkoneksi tanpa canary yang disengaja.
2. Dedicated Arthur WhatsApp canary dan shared-number `wa-dev-service` canary belum dijalankan.
3. OAuth Google -> resource selection -> isolated Sheets append smoke test belum dijalankan dengan akun Google nyata. Skill dan tool routing sudah disiapkan, tetapi status `production_ready` harus tetap menunggu bukti transaksi live ini.
4. Status commit/push dilacak oleh Git history setelah laporan ini dibuat. Git push sendiri bukan bukti runtime production sudah aktif; ikuti `docs/deploy/ARTHUR_PROGRESSIVE_SKILLS_PRODUCTION_DEPLOY_2026-07-22.md` sampai seluruh gate lulus.

## Release berikutnya yang aman

1. Bebaskan atau pindahkan port service, lalu hidupkan `wa-service` dedicated dan verifikasi `/health/detailed` menjadi 200.
2. Jalankan canary chat Arthur di dedicated session: discovery, create, verify, demo, dan reset.
3. Jalankan shared trial canary melalui `wa-dev-service` tanpa mengubah ownership/session Arthur.
4. Jalankan Google OAuth dan Sheets smoke test pada spreadsheet sandbox tenant atau worksheet temporer terisolasi.
5. Ikuti runbook production untuk deploy API dan scheduler, lalu verifikasi commit/model/prompt/bundle dari endpoint production sebelum rollout user.
