# Deploy Notes — managed-agent.chiefaiofficer.id

## Apa yang dilakukan

1. **Upload project ke VPS** via SFTP (paramiko) sebagai tarball, di-extract ke `/home/clevio/stack/managed-agents/`
2. **Buat file deploy khusus production**:
   - `deploy/docker-compose.prod.yml` — stack terpisah dengan Traefik labels
   - `deploy/.env.prod` — env vars production
3. **Build Docker image** dari `Dockerfile` root project
4. **Jalankan stack**: API container saja (postgres pakai yang sudah ada di VPS)
5. **Jalankan migrasi** alembic (11 migration)
6. **Serve UI-DEV** sebagai static files di `/ui/` via FastAPI `StaticFiles`
7. **Verifikasi** health check dan Traefik routing

## Lokasi di VPS

```
/home/clevio/stack/managed-agents/
├── deploy/
│   ├── docker-compose.prod.yml   ← stack production
│   └── .env.prod                 ← env vars (jangan di-commit)
├── app/
│   └── main.py                   ← mount UI-DEV di sini
├── UI-DEV/
│   ├── index.html
│   ├── app.js
│   └── style.css
├── alembic/
└── ...
```

## Akses

- **API**: `https://managed-agent.chiefaiofficer.id`
- **Swagger**: `https://managed-agent.chiefaiofficer.id/docs`
- **UI Dev**: `https://managed-agent.chiefaiofficer.id/ui/`
- **API Key**: `42523db14d86f993409fba4984764be01fb169ddf7e5e401efab2f33442c9a7b`

## Infrastruktur VPS

- **VPS**: `194.238.23.242`, user `clevio`
- **Traefik** sudah jalan di network `root_default`, certresolver `mytlschallenge`
- **PostgreSQL** pakai postgres yang sudah ada di VPS, diakses via `host.docker.internal:5432`
  - Connection: `postgresql://postgres:Aiagronomists4725.@host.docker.internal:5432/managed_agents`
  - DB `managed_agents` sudah ada, tidak perlu buat baru
- Tidak ada host port yang di-expose — semua akses via Traefik (80/443)

## Container names

| Container | Image |
|-----------|-------|
| `deploy-api-1` | `deploy-api` (built dari project) |

## Update code

```bash
# SSH ke VPS
ssh clevio@194.238.23.242

# Rebuild setelah ada perubahan kode:
cd /home/clevio/stack/managed-agents/deploy
sudo docker compose -f docker-compose.prod.yml up -d --build

# Jalankan migrasi kalau ada schema baru
sudo docker exec deploy-api-1 alembic upgrade head
```

## Perintah berguna

```bash
# Lihat logs
sudo docker logs deploy-api-1 -f

# Restart API
sudo docker compose -f /home/clevio/stack/managed-agents/deploy/docker-compose.prod.yml restart api

# Status container
sudo docker compose -f /home/clevio/stack/managed-agents/deploy/docker-compose.prod.yml ps

# Stop semua
sudo docker compose -f /home/clevio/stack/managed-agents/deploy/docker-compose.prod.yml down
```

## Catatan penting

- Jangan hapus network `root_default` — dipakai semua project di VPS
- `UI-DEV/` di-serve di path `/ui/` — kalau ada perubahan file UI, upload ulang ke VPS dan rebuild
- `wa-service` belum di-deploy (WhatsApp Go microservice), `WA_SERVICE_URL` di env masih `localhost:8080`
