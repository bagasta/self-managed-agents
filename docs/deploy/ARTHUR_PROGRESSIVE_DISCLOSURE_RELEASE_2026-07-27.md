# Rekap Perbaikan Arthur dan Rilis Produksi — 27 Juli 2026

## Hasil akhir

Arthur sekarang memakai alur discovery dan pembuatan agent yang lebih deterministik, berbasis skill progressive disclosure, serta memiliki release gate untuk autentikasi dan kesiapan resource. Perbaikan ini menutup kegagalan yang terlihat pada percakapan JULEHAI/JuleAI dan Coding Joy CS: konfirmasi berulang, klaim tool tidak tersedia yang tidak akurat, Google OAuth tanpa Sheet siap pakai, halusinasi Google Tasks API, dan konfigurasi file/vision yang tidak konsisten.

Rilis produksi sudah dilakukan pada commit `ba9570a586fb869a6252dc4730437e533c4fc82a` dengan lima replika API sehat. Hanya service proyek ini yang diubah.

## Masalah yang diperbaiki

| Kejadian sebelumnya | Akar masalah | Perbaikan yang diterapkan |
| --- | --- | --- |
| Arthur meminta konfirmasi/rangkuman berulang atau menyatakan discovery belum lengkap padahal user sudah setuju. | Status discovery dan konfirmasi tidak memiliki manifest kanonis yang stabil. | Manifest requirement kanonis sekarang di-hash dan disimpan bersama bukti konfirmasi. Konfirmasi hanya berlaku untuk manifest yang sama; perubahan nyata saja yang membuka kebutuhan konfirmasi baru. |
| Arthur berkata `create_agent`/`update_agent` tidak tersedia, walau kemampuan builder sebenarnya seharusnya dapat dijalankan. | Prompt/runtime tidak membedakan tool yang benar-benar eligible dari tool yang tidak relevan pada state saat itu. | Daftar tool eligible dan state-gated sekarang diinjeksi secara otoritatif ke runtime. Guard menolak klaim progres builder yang tidak terverifikasi. |
| OAuth Google selesai, tetapi agent demo diluncurkan tanpa Google Sheet/resource yang siap digunakan. | Autentikasi dianggap selesai, padahal setup artefak dan verifikasi tulis belum selesai. | State integrasi kini menyimpan autentikasi, Spreadsheet yang dibuat/dipilih, ID/URL, hasil verifikasi tulis, dan binding agent. Auth/setup dianggap pekerjaan create yang belum selesai sampai resource tervalidasi. |
| Agent meminta user menghubungi Arthur untuk membuat Sheet, atau meminta ID placeholder. | Tidak ada bootstrap Sheet proaktif untuk alur pencatatan keuangan. | Saat owner terautentikasi meminta pencatatan dan belum ada Sheet terverifikasi, runtime melakukan bootstrap: buat/pilih Sheet, buat header, tulis data uji, baca kembali, lalu simpan binding valid. |
| Agent mengarahkan user mengaktifkan Google Tasks API untuk kebutuhan Google Sheets. | Scoping Google terlalu luas dan instruksi fallback tidak dibatasi produk yang dikonfirmasi. | Allowlist layanan Google diterapkan. Kebutuhan `Google Sheets` hanya mendapat Sheets dan Drive yang diperlukan; tool Tasks/Calendar/Gmail tidak diekspos tanpa requirement eksplisit. |
| Kebutuhan menerima foto struk bisa berubah menjadi kemampuan membuat file, atau model/kapabilitas media tidak konsisten. | Ekstraksi capability file tidak tahan terhadap negasi dan routing media kurang eksplisit. | Frasa seperti `tidak perlu membuat file` diperlakukan sebagai `receive_only`, bukan generate. Runtime menyertakan routing capability persisten dan komposisi mixin Google + file sekaligus. |
| Arthur membaca terlalu banyak instruksi atau kehilangan instruksi penting di tengah proses. | Skill loading belum benar-benar progressive disclosure. | Runtime sekarang memuat katalog metadata seluruh skill terlebih dahulu, lalu isi penuh hanya untuk skill yang dipilih oleh state/tugas saat ini. Kernel, discovery, create, Google, dan file skill diperbarui untuk hand-off eksplisit. |

## Perubahan kode utama

- `app/core/engine/arthur_skill_runtime.py`
  - Engine Arthur dinaikkan ke v2 dan prompt kernel ke v12.
  - Katalog skill metadata-first dan pemuatan isi skill terpilih.
  - Mixin Google dan file dikomposisikan bersama.
  - Routing capability, tool eligibility, dan state integrasi disuntikkan secara eksplisit.

- `app/core/domain/agent_build_state_service.py`
  - Manifest requirement kanonis, hash, versi, dan bukti konfirmasi.
  - Status autentikasi Google, artefak Sheet, verifikasi tulis, serta binding agent dipersistenkan.
  - Sanitasi pertanyaan Markdown agar wrapper tidak bocor atau meninggalkan format rusak.

- `app/core/engine/agent_followups.py` dan `app/core/engine/agent_runner.py`
  - Retry `plan_agent` yang terkontrol jika discovery/create berhenti sebelum perencanaan.
  - Tidak memaksa re-plan ketika yang tertinggal hanya setup integrasi setelah agent dibuat.
  - Bootstrap Google Sheet proaktif dan scoping Google MCP yang ketat.

- `app/core/tools/builder_discovery.py` dan `app/core/tools/builder_google.py`
  - Resolusi nomor owner dari frasa seperti `nomer ini` atau `ke nomer saya`.
  - Pengenalan konfirmasi informal yang aman dan penolakan bentuk negatif.
  - Allowlist produk/layanan Google berdasarkan requirement yang sudah disetujui.

- `app/core/engine/reply_guard.py`
  - Menolak klaim progres builder umum yang tidak punya bukti eksekusi.

- Skill Arthur yang diperbarui:
  - `arthur-skills/KERNEL.md`
  - skill discovery, create, dan Google beserta runtime YAML-nya.

## Validasi sebelum rilis

Pemeriksaan yang berhasil:

- Targeted builder suite: `142 passed`.
- Arthur/Google/reply-guard suite: `182 passed`.
- Regression state/resource suite: `117 passed`.
- Integration suite sebelumnya: `386 passed`.
- Dry-run seed Arthur berhasil: kernel dan delapan skill aktif termuat dengan bundle v14.
- `compileall`, pemeriksaan Ruff untuk file yang diubah, dan `git diff --check` berhasil.

Catatan: full suite `tests/` tidak dipakai sebagai gate bersih karena test container tidak memiliki PostgreSQL pada `localhost:5432`; kegagalan yang tersisa bersifat lingkungan database, bukan regresi aplikasi. Test yang relevan dengan perubahan ini dijalankan pada environment yang sesuai dan lulus.

## Commit dan konfigurasi rilis

- Implementasi inti: `f4f790a` — `Make Arthur progressive workflows deterministic`.
- Release gate produksi: `ba9570a` — `Update Arthur production release gates`.
- Branch yang dipush: `agent/guard-arthur-media-sources`.
- Konfigurasi rilis:
  - commit aplikasi: `ba9570a586fb869a6252dc4730437e533c4fc82a`
  - engine: `arthur-progressive-v2`
  - prompt kernel: `arthur-kernel-v12`
  - bundle skill: `arthur-skills-2026-07-27-v14`

## Deployment produksi

Urutan deployment:

1. Build image proyek ini saja: `managed-agents-app:ba9570a`.
2. Verifikasi Alembic pada revision `023 (head)`.
3. Jalankan migrasi dan seed Arthur.
   - Arthur diperbarui ke versi 33.
   - Delapan skill aktif dipublikasikan pada bundle v14.
4. Rollout terbatas pada `api` dan `scheduler`:

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml \
  up -d --no-deps --scale api=5 api scheduler
```

5. Verifikasi `/health`, `/health/detailed`, replica health, log, dan state database.

Hasil:

- Lima replika API aktif, memakai image `managed-agents-app:ba9570a`, seluruhnya `healthy`, dengan zero restart:
  - `deploy-api-49`
  - `deploy-api-50`
  - `deploy-api-51`
  - `deploy-api-52`
  - `deploy-api-53`
- `deploy-scheduler-1` aktif pada image yang sama dengan zero restart.
- Endpoint health mengembalikan commit lengkap, engine v2, prompt v12, database `ok`, scheduler eksternal `ok`, dan WhatsApp service `ok`.
- Audit database mengonfirmasi Arthur versi 33 dan delapan skill aktif. Sebagian baris skill lama masih menyimpan provenance bundle lama karena checksum skill tersebut tidak berubah; ini disengaja untuk menjaga riwayat skill yang immutable. Bundle runtime aktif tetap v14.

## Batas isolasi deployment

Tidak ada container atau volume di luar target proyek yang disentuh. Identitas dan waktu mulai service berikut tetap sama sebelum dan sesudah rollout:

- `deploy-wa-service-1`
- `deploy-wa-dev-service-1`
- `deploy-redis-1`
- `deploy-pgbouncer-1`

Satu container test sementara yang dibuat untuk validasi progressive disclosure telah dihapus setelah pengujian.

## Pemeriksaan pascarilis

- Smoke test di salah satu replika produksi memverifikasi Google dan file mixin aktif bersama.
- Capability file `receive_only` tetap dipertahankan saat user menyatakan tidak perlu membuat file.
- Scope Google Sheets tidak mengekspos `manage_task`.
- Deteksi bootstrap Sheet berjalan saat belum ada Sheet default tervalidasi.
- Pemeriksaan log lima hingga sepuluh menit setelah rollout tidak menemukan error, critical, traceback, atau error checksum skill yang relevan.

Observasi non-blocking: pada startup, tiap replika mencatat warning `startup.embedding_warmup.timeout`, lalu startup selesai dan seluruh health check lulus. Ini tidak menghambat layanan, tetapi layak dipantau sebagai pekerjaan performa terpisah.

## Dampak operasional yang diharapkan

Untuk pembuatan agent berikutnya, Arthur seharusnya:

1. Menggali kebutuhan secukupnya sesuai skill aktif, tanpa memuat seluruh instruksi sekaligus.
2. Mengunci requirement yang sudah disetujui melalui manifest stabil, tanpa meminta konfirmasi ulang bila tidak ada perubahan material.
3. Hanya menyebut atau memakai tool yang memang eligible pada state saat itu.
4. Tidak menyatakan agent siap dipakai penuh apabila OAuth, Google Sheet, atau verifikasi penulisan belum selesai.
5. Menyiapkan dan menguji Google Sheet untuk workflow Sheets, tanpa meminta Google Tasks API kecuali Tasks benar-benar menjadi requirement yang disetujui.
6. Menjaga capability media sesuai kebutuhan yang sudah dipilih dan tidak menaikkannya lewat interpretasi keliru.
