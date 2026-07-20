# Rebuild produksi dan replika API

Dokumen ini mencatat prosedur rebuild stack `managed-agents` dan pembuatan beberapa replika untuk menangani lebih banyak user secara paralel.

## Topologi replika

- `api`: 3 replika secara default, diatur oleh `API_REPLICAS` dan dibagi bebannya oleh Traefik.
- `scheduler`: 1 standalone worker. Klaim job terkoordinasi dengan advisory lock/row lock database; jangan menambah worker tanpa meninjau mekanisme ini.
- `wa-service` dan `wa-dev-service`: masing-masing 1 instance karena menyimpan state/sesi WhatsApp pada volume persisten.
- `redis` dan `pgbouncer`: masing-masing 1 instance sebagai layanan bersama untuk seluruh replika API.

Replika API memakai Redis, database, volume sandbox/upload, dan konfigurasi environment yang sama. Compose tidak memakai `container_name`, sehingga nama container replika dapat dibuat otomatis (`deploy-api-1`, `deploy-api-2`, dan seterusnya).

## Langkah rebuild yang dilakukan

Jalankan dari root repository:

```bash
git status -sb
git pull --ff-only
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml config
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml up -d --build \
  api scheduler wa-service wa-dev-service redis pgbouncer
```

`deploy/docker-compose.prod.yml` menetapkan 3 replika API secara default. Untuk mengganti jumlahnya pada satu deployment tanpa mengubah file:

```bash
API_REPLICAS=5 docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml up -d --build api
```

Pilih jumlah replika sesuai kapasitas CPU, RAM, batas koneksi database, dan batas provider LLM. Tick scheduler yang ikut hidup pada proses API memakai row lock dan `SKIP LOCKED` saat mengklaim job. Jangan mereplikasi standalone `scheduler` atau service WhatsApp tanpa meninjau kembali mekanisme koordinasi/partitioning terlebih dahulu.

> **Penting:** jangan gunakan `--remove-orphans` pada host ini. Beberapa stack pernah memakai nama project Compose `deploy`, sehingga penghapusan orphan dapat menyentuh container di luar `managed-agents`. Selalu sebutkan service yang ditargetkan secara eksplisit seperti pada perintah di atas.

## Verifikasi setelah rebuild

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml ps
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml ps --status running api
docker inspect --format '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' \
  $(docker compose --env-file deploy/.env.prod \
    -f deploy/docker-compose.prod.yml ps -q api)
curl -fsS https://managed-agent.chiefaiofficer.id/health
```

Hasil yang diharapkan:

- Terdapat 3 container `api` dengan status `running` dan health `healthy`.
- Hanya ada 1 instance untuk setiap service stateful/scheduler.
- Endpoint publik `/health` mengembalikan respons sukses melalui Traefik.

Jika salah satu replika gagal sehat, periksa log seluruh replika API:

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml logs --tail=200 api
```

## Rollback jumlah replika

Kurangi kembali ke satu replika tanpa menghapus volume persisten:

```bash
API_REPLICAS=1 docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml up -d api
```

Perubahan kode dapat di-rollback melalui Git, lalu jalankan ulang perintah rebuild dengan commit yang dipilih. Hindari `docker compose down -v` karena opsi `-v` menghapus volume Redis dan sesi WhatsApp.
