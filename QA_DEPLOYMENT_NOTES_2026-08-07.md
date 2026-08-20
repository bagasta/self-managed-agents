# QA dan Deployment Notes — 7 Agustus 2026

Dokumen ini merekam perbaikan, pengujian, dan deployment yang dilakukan pada project `managed-agents`. Tidak ada secret, token OAuth, nomor telepon pribadi, isi email, atau data percakapan pengguna yang dicatat di sini.

## Status deployment terakhir

- API berjalan pada 5 replica dan seluruhnya sehat.
- `wa-service` dan `wa-dev-service` berhasil di-build ulang serta di-recreate pada 7 Agustus 2026.
- Health check setelah deployment:
  - `wa-service`: `{"status":"ok"}`
  - `wa-dev-service`: `{"status":"ok"}`

Command untuk melihat log realtime production:

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml logs -f --tail=100
```

Log satu service saja:

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml logs -f --tail=100 wa-service wa-dev-service
```

## Perbaikan observability API

- Menyalakan access log dan level log `info` untuk API di `deploy/docker-compose.prod.yml`.
- Merekreasi 5 replica API dan memverifikasi semuanya healthy.

Hasil: command `docker compose logs -f` kini menampilkan lifecycle dan request API secara realtime.

## Perbaikan Arthur v2 dan RAG

- Memperbaiki konfigurasi agent yang dibuat Arthur agar capability RAG tersimpan sebagai aktif.
- Verifikasi agent yang dibuat dari Arthur dapat mengambil knowledge base dan mengembalikan jawaban yang sesuai dengan data uji.

File utama: `arthur_v2/plugin.py`.

## Perbaikan Google MCP

### Penyebab

OAuth Google sudah sukses, tetapi `langchain-mcp-adapters` yang terpasang tidak kompatibel dengan `mcp` major version 2. Adapter masih mengimpor API yang telah dihapus pada MCP 2.x. Error ini sebelumnya tampil menyesatkan seolah dependency belum ter-install.

### Perbaikan

- Mengunci dependency ke pasangan yang kompatibel:
  - `langchain-mcp-adapters==0.3.2`
  - `mcp>=1.28.0,<2.0.0`
- Memperjelas error MCP agar membedakan dependency yang tidak ada dari dependency yang tidak kompatibel.
- Build dan rollout image API yang memuat perbaikan tersebut.

File utama: `requirements.txt`, `app/core/tools/mcp_tool.py`.

### Verifikasi

- Import MCP client berhasil di container API.
- Token OAuth Google berhasil diinjeksi saat agent berjalan.
- Tool Gmail MCP berhasil dipanggil dari agent. Isi email pengguna tidak ditampilkan atau disimpan pada laporan QA.

## QA Arthur v2 dan CRUD agent

Pengujian memakai akun QA terpisah dan agent dengan prefix `QA`.

- CRUD agent: create, read, update, session, dan chat lulus.
- Arthur v2 membuat beberapa agent nyata melalui percakapan.
- Capability yang diverifikasi:
  - RAG / knowledge base
  - reminder / scheduler
  - escalation ke human
  - memory lintas session
  - Google Gmail MCP setelah autentikasi
  - sandbox execution
  - subagent / task

Catatan temuan: agent yang memiliki konfigurasi `deploy` dan `subagents` bersamaan saat ini men-strip deploy tools di parent run. Tidak ada deployment publik atau container baru yang dibuat selama QA. Ini perlu ditangani sebagai pekerjaan lanjutan jika dua capability tersebut memang harus aktif bersamaan.

## Stress test API / Arthur: 100 user

Simulasi memakai 100 user sintetis berbeda yang mengirim request nyata ke endpoint session/message Arthur dengan concurrency 30.

Hasil:

| Metrik | Hasil |
| --- | --- |
| User disiapkan | 100 / 100 |
| Agent berhasil dibuat oleh Arthur | 100 / 100 |
| Smoke test agent baru | 100 / 100 |
| Durasi pembuatan total | 118.45 detik |
| Durasi smoke test total | 61.55 detik |
| Latensi build maksimum | 87.018 detik |
| Latensi smoke maksimum | 24.115 detik |
| Kegagalan | 0 |

Selama stress test, lima replica API tetap sehat.

## QA dan stress test WhatsApp

### Arsitektur concurrency yang diverifikasi

- `wa-service` membatasi forward webhook ke Python API melalui `WEBHOOK_MAX_IN_FLIGHT`.
- `wa-dev-service` (nomor demo) membatasi forward ke API melalui `AGENT_MAX_IN_FLIGHT`.
- Konfigurasi production saat ini untuk keduanya adalah **48** request in-flight.
- `wa-dev-service` juga menserialisasi pesan dari sender/chat yang sama supaya turn pengguna tidak saling membatalkan. Sender yang berbeda tetap dapat berjalan paralel.

### Test 100 user bersamaan

Ditambahkan test yang memakai 100 user/chat berbeda dan backend HTTP sintetis yang sengaja ditahan. Metode ini menguji semaphore dan antrean sebenarnya tanpa mengirim pesan ke nomor WhatsApp eksternal.

Hasil:

| Jalur | Request | Batas paralel | Hasil |
| --- | ---: | ---: | --- |
| `wa-service` → Python webhook | 100 | 48 | Lulus; tidak pernah melampaui 48, seluruh antrean selesai |
| `wa-dev-service` → API agent | 100 | 48 | Lulus; tidak pernah melampaui 48, seluruh antrean selesai |

Interpretasi: saat 100 user mengirim chat berdekatan, maksimal 48 run diteruskan bersamaan pada masing-masing jalur dan sisanya menunggu slot; request tidak dijatuhkan oleh limiter tersebut.

### Perbaikan dari QA WhatsApp

- Kode test mode `wa-service` tidak sinkron dengan `DeviceManager`, sehingga suite tidak dapat dikompilasi. Sinkronisasi diperbaiki.
- Target outbound yang diblokir oleh test mode sekarang mengembalikan HTTP `403 Forbidden`, bukan `500 Internal Server Error`.
- Forward webhook diekstrak ke method terpisah agar jalur bounded yang sama dapat diuji tanpa event WhatsApp hidup.

File utama:

- `wa-service/device_manager.go`
- `wa-service/handlers.go`
- `wa-service/http_client_test.go`
- `wa-dev-service/http_client_test.go`

### Validasi yang dijalankan

```bash
(cd wa-service && go test -count=1 ./...)
(cd wa-service && go test -race -count=1 ./...)
(cd wa-dev-service && go test -count=1 ./...)
(cd wa-dev-service && go test -race -count=1 ./...)
```

Seluruh command di atas lulus.

## Command deployment WhatsApp

Untuk rebuild dan redeploy dua service WhatsApp:

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml up -d --build wa-service wa-dev-service
```

Untuk cek status:

```bash
docker compose --env-file deploy/.env.prod \
  -f deploy/docker-compose.prod.yml ps
```

## Batasan QA

- Stress test WhatsApp tidak mengirim 100 pesan ke nomor manusia atau nomor demo yang terhubung. Ini sengaja agar tidak menimbulkan spam dan tidak memengaruhi percakapan pengguna.
- Yang diuji adalah jalur HTTP forwarding, limit concurrency, antrean, error handling, health service, dan race detector.
- Untuk uji end-to-end melalui jaringan WhatsApp sungguhan, diperlukan nomor target QA khusus dan test mode ingress yang eksplisit; keduanya sebaiknya dipisahkan dari nomor demo production.
