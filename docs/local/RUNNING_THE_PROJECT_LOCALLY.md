# Menjalankan Managed Agents Project Secara Lokal

Panduan ini menjalankan backend, database, UI-DEV, WhatsApp service dedicated, dan service nomor demo.

## Prasyarat

- Python dan `pip`
- Docker dan Docker Compose
- Go (untuk `wa-service` dan `wa-dev-service`)
- API key OpenRouter

## Menyalakan Docker

Di mesin ini Docker sudah terpasang, tetapi status servicenya saat panduan ini dibuat masih `inactive` dan belum otomatis menyala saat boot. Sebelum menjalankan `make db-up`, nyalakan Docker:

```bash
sudo systemctl start docker
```

Verifikasi Docker siap dipakai:

```bash
sudo systemctl status docker --no-pager
docker ps
```

Agar Docker otomatis menyala setiap komputer dinyalakan:

```bash
sudo systemctl enable docker
```

Jika `docker ps` memberi error permission denied, jalankan sekali lalu logout/login kembali (atau reboot):

```bash
sudo usermod -aG docker $USER
```

Untuk menghentikan Docker saat benar-benar tidak dipakai:

```bash
sudo systemctl stop docker
```

## 1. Siapkan environment

```bash
cd /home/bagas/managed-agents-project
cp .env.example .env
```

Edit `.env`. Nilai minimal untuk local development:

```env
DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5432/managed_agents
OPENROUTER_API_KEY=isi-api-key-kamu
API_KEY=buat-random-secret-sendiri
WA_SERVICE_URL=http://localhost:8080
WA_DEV_SERVICE_URL=http://localhost:8081
```

`API_KEY` dipakai saat membuka UI-DEV. Jangan memakai nilai contoh di `.env.example` untuk environment yang dapat diakses orang lain.

## 2. Jalankan semua service

Gunakan terminal terpisah untuk tiap service agar log mudah dibaca.

### Terminal 1: PostgreSQL

```bash
cd /home/bagas/managed-agents-project
make db-up
```

### Terminal 2: dependency Python dan migrasi database

Jalankan saat setup awal, atau ulangi `make upgrade` ketika ada migration baru.

```bash
cd /home/bagas/managed-agents-project
make install-dev
make upgrade
```

### Terminal 3: Backend API dan UI-DEV

```bash
cd /home/bagas/managed-agents-project
make dev
```

Untuk memakai port `8001` seperti dashboard yang sedang dibuka di mesin ini:

```bash
make dev HOST=127.0.0.1 PORT=8001
```

Target development mengawasi Python dan berkas UI (`.html`, `.js`, `.css`). Saat ada perubahan, server dan dashboard lokal akan reload otomatis.

Backend tersedia di `http://localhost:8000` dan Swagger di `http://localhost:8000/docs`.

UI-DEV tidak memiliki server frontend terpisah; FastAPI menyajikannya di:

```text
http://localhost:8000/ui
```

Startup project hanya melakukan seed Arthur V2. Arthur legacy dinonaktifkan secara default melalui `ARTHUR_LEGACY_ENABLED=false`.

### Terminal 4: WhatsApp dedicated service

Service ini diperlukan untuk QR dan nomor WhatsApp dedicated milik agent, termasuk Arthur V2.

```bash
cd /home/bagas/managed-agents-project
make wa
```

Service berjalan di `http://localhost:8080`.

### Terminal 5: WhatsApp nomor demo/shared trial (opsional)

Ini terpisah dari WhatsApp dedicated Arthur V2. Jalankan hanya bila ingin menguji nomor demo/shared trial.

```bash
cd /home/bagas/managed-agents-project
make wa-dev
```

Service berjalan di `http://localhost:8081`.

## 3. Konfigurasi UI-DEV

Buka `http://localhost:8000/ui`, lalu isi konfigurasi dashboard:

| Field | Nilai local |
| --- | --- |
| Base URL | `http://localhost:8000` |
| API Key | Nilai `API_KEY` dari `.env` |
| WA Service URL | `http://localhost:8080` |

## 4. Membuat dan menghubungkan Arthur V2 ke WhatsApp

1. Di UI-DEV, buka bagian **Arthur V2** dan klik **Refresh**.
2. Jika belum ada, klik **Buat Arthur V2**. Ini membuat record dengan plugin `arthur_v2`, tidak memakai Arthur legacy.
3. Pada panel **WhatsApp Arthur**, klik **Connect / Generate QR**.
4. Scan QR dari WhatsApp pada nomor yang akan menjadi nomor dedicated Arthur.
5. Tunggu status dashboard berubah menjadi **Connected**.

Arthur V2 menggunakan `wa-service` pada port `8080`. Jangan memakai `wa-dev-service` untuk nomor dedicated Arthur; service itu khusus shared trial number.

## 5. Sandbox, sub-agent, dan tool file (opsional)

Jika Arthur V2 akan memakai sandbox, sub-agent, atau tool file, build image sandbox satu kali:

```bash
cd /home/bagas/managed-agents-project
make sandbox-build
make sandbox-check
```

Docker daemon harus aktif untuk fitur tersebut.

## Ringkasan perintah cepat

```bash
# Terminal 1
make db-up

# Terminal 2 (setup awal)
make install-dev
make upgrade

# Terminal 3
make dev

# Terminal 4, agar QR WhatsApp Arthur V2 berfungsi
make wa

# Terminal 5, opsional untuk nomor demo/shared trial
make wa-dev
```

## Troubleshooting singkat

- UI tidak terbuka: pastikan `make dev` aktif dan akses `http://localhost:8000/ui`, bukan membuka file `UI-DEV/index.html` langsung.
- Dashboard mendapat error autentikasi: periksa nilai API Key di UI-DEV sama dengan `API_KEY` di `.env`.
- QR Arthur tidak muncul: pastikan `make wa` aktif dan `WA_SERVICE_URL=http://localhost:8080`.
- Database error: cek Docker aktif, lalu ulangi `make db-up` dan `make upgrade`.
- Nomor demo tidak bekerja: service yang dibutuhkan adalah `make wa-dev`, bukan `make wa`.
