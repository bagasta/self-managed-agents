# Arthur Progressive Skills — Production Deploy Runbook

Tanggal: 22 Juli 2026
Target: `managed-agent.chiefaiofficer.id`
Release: Arthur progressive runtime, persistent build state, DeepSeek V4 Flash, Mistral document route, dan GPT-4.1 Mini image route

## Prinsip wajib

- Deploy commit dari `main`, bukan sekadar branch yang sudah di-push.
- Migration `023`, seed Arthur, API, dan scheduler adalah satu release unit.
- Push Git bukan bukti production sudah berubah. Bukti final berasal dari commit container, database Arthur, health endpoint, dan canary WhatsApp.
- Jangan mencampur channel: `wa-service` tetap untuk dedicated device/session; `wa-dev-service` tetap untuk shared trial number.
- Jangan menyebut rollout `production_ready` sebelum text, document, image, Google OAuth/Sheets, dedicated WhatsApp, dan shared demo canary lulus.
- Jangan mencetak `.env.prod`, API key, OAuth token, atau database credential ke log/tiket.

## 0. Release record dan rollback point

Sebelum menyentuh production, catat:

- commit production saat ini;
- image tag API/scheduler saat ini;
- model, prompt version, dan `tools_config.arthur_runtime` Arthur saat ini;
- Alembic revision saat ini;
- status dan jumlah device pada `wa-service` serta shared session `wa-dev-service`;
- snapshot/backup PostgreSQL sebelum migration dan seed.

Simpan `PREVIOUS_IMAGE_TAG` dan pastikan image tersebut belum dihapus. Migration `023` bersifat additive; rollback aplikasi tidak memerlukan downgrade database.

## 1. Pastikan release sudah masuk main

Jalankan dari checkout production yang benar:

```bash
git fetch origin
git checkout main
git pull --ff-only origin main
git status --porcelain
git log -1 --oneline --decorate
```

Gate:

- `git status --porcelain` harus kosong;
- commit refactor harus menjadi ancestor `HEAD` main;
- reviewer harus mencatat full SHA yang akan dideploy.

```bash
DEPLOY_SHA="$(git rev-parse HEAD)"
IMAGE_TAG="${DEPLOY_SHA:0:12}"
export IMAGE_TAG
echo "DEPLOY_SHA=${DEPLOY_SHA}"
echo "IMAGE_TAG=${IMAGE_TAG}"
```

## 2. Production environment

Pastikan `deploy/.env.prod` berisi nilai nyata, bukan placeholder:

```dotenv
OPENROUTER_API_KEY=...
MISTRAL_API_KEY=...
ARTHUR_PRIMARY_MODEL=deepseek/deepseek-v4-flash
ARTHUR_DOCUMENT_MODEL=mistral-ocr-latest
ARTHUR_IMAGE_MODEL=openai/gpt-4.1-mini
ARTHUR_ENGINE_VERSION=arthur-progressive-v1
ARTHUR_PROMPT_VERSION=arthur-kernel-v1
LLM_REQUEST_TIMEOUT_SECONDS=120
LLM_MAX_RETRIES=1
APP_COMMIT_SHA=<full DEPLOY_SHA>
```

Selain itu verifikasi `DATABASE_URL`, `API_KEY`, `OPENROUTER_API_KEY`, `MISTRAL_API_KEY`, Redis, Google MCP/OAuth, dan key lain yang sudah diwajibkan production. `API_KEY` juga dipakai `wa-dev-service` sebagai fallback `MAIN_API_KEY`; jangan mengubahnya tanpa merotasi kedua sisi.

Jangan bergantung pada default model di source untuk production. Nilai eksplisit di `.env.prod` membuat health/version drift mudah dideteksi.

## 3. Build image satu kali

```bash
PROD_COMPOSE=(docker compose -f deploy/docker-compose.prod.yml)
"${PROD_COMPOSE[@]}" config --quiet
"${PROD_COMPOSE[@]}" build api
```

`api` dan `scheduler` memakai image yang sama: `managed-agents-app:${IMAGE_TAG}`. Jangan menjalankan `make deploy-api-fast` untuk release ini karena scheduler juga harus memakai model/schema/runtime code yang sama.

## 4. Preflight dengan image baru

```bash
"${PROD_COMPOSE[@]}" run --rm --no-deps api alembic current
"${PROD_COMPOSE[@]}" run --rm --no-deps api python scripts/seed_arthur.py --dry-run
```

Dry-run harus menunjukkan:

- kernel 4.235 karakter;
- model `deepseek/deepseek-v4-flash`;
- document model `mistral-ocr-latest`;
- image model `openai/gpt-4.1-mini`;
- bundle `arthur-skills-2026-07-22-v1` berisi 8 skill.

Stop deploy bila import gagal, database tidak dapat diakses, model berbeda, skill kurang dari delapan, atau dry-run menampilkan secret.

## 5. Migration dan seed

Urutannya wajib migration dahulu, baru seed:

```bash
"${PROD_COMPOSE[@]}" run --rm --no-deps api alembic upgrade head
"${PROD_COMPOSE[@]}" run --rm --no-deps api alembic current
"${PROD_COMPOSE[@]}" run --rm --no-deps api python scripts/seed_arthur.py
```

Gate:

- Alembic harus `023 (head)`;
- seed selesai sekali tanpa checksum/trust error;
- Arthur database memakai DeepSeek V4 Flash;
- tepat 8 active system skills, semuanya immutable dan checksum-valid.

Jangan menjalankan seed berulang sebagai respons pertama terhadap error. Baca error dan state database dahulu; seed sudah dibuat idempotent, tetapi retry buta menyulitkan audit.

## 6. Restart API dan scheduler

```bash
"${PROD_COMPOSE[@]}" up -d --no-deps api scheduler
"${PROD_COMPOSE[@]}" ps api scheduler redis wa-service wa-dev-service
```

Release Python ini tidak mengubah binary Go, sehingga jangan rebuild/reset volume WhatsApp. Khususnya:

- jangan menghapus volume `wa_store` atau `wa_dev_store`;
- jangan memindahkan dedicated Arthur device ke `wa-dev-service`;
- jangan login ulang device bila session lama masih sehat;
- vCard/link shared demo tetap dipicu dari flow Arthur yang benar, sementara shared trial traffic tetap diisolasi oleh `wa-dev-service`.

## 7. Health dan version gate

```bash
curl -fsS https://managed-agent.chiefaiofficer.id/health | python -m json.tool
curl -fsS https://managed-agent.chiefaiofficer.id/health/detailed | python -m json.tool
```

Expected `/health`:

- HTTP 200;
- `commit` sama dengan `DEPLOY_SHA`;
- `arthur_runtime.primary_model=deepseek/deepseek-v4-flash`;
- `document_model=mistral-ocr-latest`;
- `image_model=openai/gpt-4.1-mini`;
- `engine_version=arthur-progressive-v1`;
- `prompt_version=arthur-kernel-v1`.

Expected `/health/detailed`:

- HTTP 200, bukan degraded 503;
- `checks.database=ok`;
- `checks.scheduler=external`;
- `checks.wa_service=ok`;
- `arthur_runtime.active_system_skills=8`;
- `arthur_runtime.skill_bundle_version=arthur-skills-2026-07-22-v1`;
- database primary model dan version sama dengan source health.

Jika `/health` 200 tetapi `/health/detailed` 503, release belum lulus. Periksa dependency yang disebut endpoint; jangan menutupinya dengan hanya memonitor endpoint sederhana.

## 8. Log gate

```bash
"${PROD_COMPOSE[@]}" logs --since=15m api scheduler wa-service wa-dev-service
```

Cari dan selesaikan sebelum canary:

- migration/ORM/schema error;
- missing skill atau checksum mismatch;
- provider 400/401/403/429;
- attachment route salah;
- database pool timeout;
- webhook/auth mismatch antara API dan `wa-dev-service`;
- restart loop atau WhatsApp reconnect loop.

Warning yang memang dipahami harus dicatat bersama dampaknya; jangan menganggap semua warning aman.

## 9. Canary wajib

Gunakan user/operator test terisolasi. Jangan memakai customer production pertama sebagai test.

### Text discovery

1. Minta agent universal untuk use case non-BeeChat, misalnya survey klinik atau admin properti.
2. Berikan beberapa requirement pada turn kedua.
3. Pastikan Arthur menggali kebutuhan bertahap, tidak mengulang canonical/semantic topic, dan belum memanggil `create_agent` sebelum confirmation.
4. Pastikan run metadata menunjukkan DeepSeek, engine, prompt, dan skill version yang benar.

### Create dan verify

1. Selesaikan discovery serta confirmation.
2. Pastikan create hanya sekali.
3. Pastikan readback/verify berhasil.
4. Jika integrasi belum siap, status harus `agent_created` atau `setup_pending`, bukan `production_ready`.

### Dokumen

1. Kirim PDF valid dan satu DOCX/PPTX valid.
2. Pastikan metadata route menggunakan `mistral-ocr-latest`.
3. Kirim file corrupt/unsupported dan pastikan Arthur memberi blocker, bukan menebak isi.

### Gambar

1. Kirim PNG/JPEG valid dengan informasi yang dapat diverifikasi.
2. Pastikan evidence berasal dari `openai/gpt-4.1-mini` dan raw image tidak diberikan ke DeepSeek.
3. Kirim gambar invalid dan pastikan tidak ada klaim “sudah melihat”.

### Google Sheets/OAuth

1. Buat use case yang memang wajib menyimpan hasil ke Google Sheets.
2. Pastikan Arthur tidak mengulang pertanyaan file setelah requirement Sheets sudah jelas.
3. Jika belum authorized, OAuth link asli harus dikirim pada turn yang sama dan status tetap `setup_pending`.
4. Selesaikan OAuth, pilih/buat spreadsheet sandbox tenant, inspect struktur, lalu append smoke row terisolasi.
5. Bersihkan temporary row/worksheet secara idempotent.
6. Baru setelah readback membuktikan data masuk, status boleh `production_ready`.

### WhatsApp boundaries

1. Dedicated Arthur canary harus melewati `wa-service` dan device/session Arthur sendiri.
2. Shared trial canary harus melewati `wa-dev-service` pada nomor bersama.
3. Pastikan trial code memilih agent yang benar dan tidak membawa build state user lain.
4. Pastikan contact/vCard shared demo tetap dikirim oleh session Arthur sesuai flow, bukan oleh virtual agent yang salah.

### Reset

1. Reset user test memakai prosedur resmi.
2. Pastikan session, messages, memory, build draft, dan Google OAuth identity yang memang masuk scope terhapus.
3. Chat ulang “Halo”; Arthur harus memperlakukan user sebagai baru dan tidak menyebut agent/percakapan lama.

## 10. Observation window

Setelah canary lulus, observasi minimum satu jam sebelum memperluas rollout:

- error rate dan p95 latency;
- OpenRouter/Mistral auth, rate-limit, timeout, dan cost;
- duplicate-question guard events;
- build-state optimistic-lock errors;
- create/update retry dan duplicate agent;
- OAuth link delivery serta auth recovery;
- API/scheduler restart count;
- webhook backlog `wa-service` dan per-agent in-flight `wa-dev-service`.

Hentikan rollout jika ada false completion, fabricated link/result, attachment guessing, cross-user state leak, duplicate create, atau WhatsApp channel/session tertukar.

## 11. Rollback

Rollback aplikasi memakai image sebelumnya dan seed dari image sebelumnya. Jangan downgrade migration `023` pada rollback cepat karena downgrade menghapus build draft dan mereduksi versioned skills.

```bash
export IMAGE_TAG="${PREVIOUS_IMAGE_TAG}"
PROD_COMPOSE=(docker compose -f deploy/docker-compose.prod.yml)
"${PROD_COMPOSE[@]}" run --rm --no-deps api python scripts/seed_arthur.py --dry-run
"${PROD_COMPOSE[@]}" run --rm --no-deps api python scripts/seed_arthur.py
"${PROD_COMPOSE[@]}" up -d --no-deps api scheduler
```

Sesudah rollback:

- ulangi `/health` dan `/health/detailed`;
- verifikasi prompt/model Arthur benar-benar kembali ke release lama;
- lakukan satu dedicated Arthur text smoke;
- pastikan build drafts/audit baru tetap disimpan untuk investigasi;
- catat trigger rollback dan jangan melanjutkan rollout sampai root cause diketahui.

## Definition of done

Production deploy hanya selesai bila semua ini benar:

- exact main commit aktif pada API dan scheduler;
- Alembic `023 (head)`;
- Arthur DB model/kernel/bundle sesuai release;
- `/health` dan `/health/detailed` HTTP 200;
- dedicated `wa-service` dan shared `wa-dev-service` sehat tanpa boundary drift;
- text, create/verify, Mistral document, GPT image, Google OAuth/Sheets, demo, dan reset canary lulus;
- tidak ada false completion, fabricated result/link, duplicate requirement question, atau duplicate create;
- rollback image dan release record tersimpan.
