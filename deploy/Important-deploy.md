# Deploy WAGO dengan Docker + Traefik (wago-wms.chiefaiofficer.id)

Panduan ringkas untuk menjalankan backend + frontend di Docker dan diteruskan Traefik (sudah berjalan di host, pakai entrypoint `web`/`websecure` + certresolver `mytlschallenge`).

## Prasyarat
- DNS `wago-wms.chiefaiofficer.id` sudah mengarah ke server ini (A/AAAA).
- Traefik container aktif di network `root_default`, port 80/443 bebas ke Traefik.
- Postgres host sudah siap di host: `postgres://wago:wago_pg_pwd_123@host.docker.internal:5432/wago?sslmode=disable` (atau sesuaikan env pada compose).
- Docker Engine + docker compose.

## Struktur penting
- `deploy/Dockerfile.backend` — build Go backend (distroless), include migrations.
- `deploy/Dockerfile.frontend` — build Vite + Nginx static.
- `deploy/nginx-frontend.conf` — Nginx SPA config di container frontend.
- `deploy/docker-compose.yml` — stack Docker (backend + frontend) dengan label Traefik.

## Langkah deploy
1) Dari `deploy/`, build dan jalan stack:
   ```bash
   sudo docker compose up -d --build
   ```
   - Backend listen di container port 8080.
   - Frontend serve statis di port 80 container.
   - Labels Traefik:
     - Frontend: `Host(wago-wms.chiefaiofficer.id)`
     - Backend: `Host(wago-wms.chiefaiofficer.id)` + path prefix `/api` atau `/ws`
     - TLS: `traefik.http.routers.*.tls=true` + `certresolver=mytlschallenge`
     - Network: `traefik.docker.network=root_default`

2) Pastikan containers up:
   ```bash
   sudo docker compose ps
   sudo docker logs deploy-wago-backend-1 | tail
   sudo docker logs deploy-wago-frontend-1 | tail
   ```

3) Cek Traefik/cert:
   ```bash
   sudo docker logs root-traefik-1 | tail -n 50
   ```
   Sertifikat otomatis via TLS-ALPN (`mytlschallenge`). Pastikan DNS dan port 443 reachable.

## Konfigurasi yang bisa disesuaikan
- `DATABASE_URL`, `JWT_SECRET`, `ALLOWED_ORIGINS`, `WHATSAPP_DATA_DIR` di `deploy/docker-compose.yml`.
- Volume WA sessions: bind ke host `../backend/whatsapp-sessions:/data/whatsapp-sessions` (persisten di host).
- Jika Postgres dalam container tersendiri, ganti `DATABASE_URL` dan network sesuai topologi.

## Ports & jaringan
- Tidak ada host port mapping untuk backend/frontend; akses via Traefik (80/443 pada host).
- Containers join dua network: `wago` (internal) dan `root_default` (Traefik). Jangan hapus network `root_default`.

## Perintah rutin
- Rebuild + restart setelah perubahan kode:
  ```bash
  cd deploy && sudo docker compose up -d --build
  ```
- Stop stack:
  ```bash
  cd deploy && sudo docker compose down
  ```

## Known gotchas
- Jika port 80/443 dipakai container lain, Traefik tidak bisa terbitkan sertifikat.
- Pastikan `host.docker.internal` tersedia (Docker CE di Linux modern). Jika tidak, ganti ke IP host atau buat alias di `extra_hosts`.
- Systemd backend lama sudah dimatikan; jika diaktifkan lagi, gunakan port berbeda agar tidak bentrok dan tetap akses via Traefik.
