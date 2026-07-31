# Cara Pakai Arthur WhatsApp Test — Local Only

Panduan ini digunakan untuk mengetes Arthur melalui jaringan WhatsApp asli
menggunakan nomor WhatsApp khusus testing.

> **Local only:** jangan commit, push, deploy, atau memasukkan sistem testing ini
> ke Docker Compose production.

## Gambaran alur

```text
Nomor WhatsApp testing
        ↓
wa-test-service lokal
        ↓
Nomor WhatsApp Arthur
        ↓
Balasan Arthur ditangkap test runner
        ↓
Report PASS/FAIL
```

Session testing hanya boleh mengirim pesan ke nomor Arthur yang telah dikunci.
Pesan ke nomor lain akan ditolak.

## 1. Persiapan

Pastikan tersedia:

- nomor WhatsApp khusus testing;
- nomor WhatsApp Arthur yang sebenarnya, bukan nomor demo;
- Go sesuai versi project;
- Python 3;
- virtual environment project sudah tersedia;
- port lokal `8082` tidak sedang digunakan.

Masuk ke repository:

```bash
cd /home/bagas/managed-agents-project
```

## 2. Isi konfigurasi lokal

Export konfigurasi pada terminal yang akan dipakai:

```bash
export ARTHUR_TEST_TARGET_PHONE=62XXXXXXXXXXX
export WA_TEST_API_KEY='ganti-dengan-random-secret-minimal-16-karakter'
export WA_TEST_SERVICE_URL=http://127.0.0.1:8082
export WA_TEST_DEVICE_ID=watest_arthur
```

Keterangan:

- `ARTHUR_TEST_TARGET_PHONE`: nomor WhatsApp Arthur dalam format `62...`.
- `WA_TEST_API_KEY`: secret lokal untuk melindungi endpoint tester.
- `WA_TEST_SERVICE_URL`: alamat service tester lokal.
- `WA_TEST_DEVICE_ID`: ID session WhatsApp khusus testing.

Jangan menaruh API key ke file yang akan di-commit.

## 3. Jalankan service tester

```bash
make wa-test-up
```

Service akan:

- membangun binary lokal;
- berjalan pada `127.0.0.1:8082`;
- menyimpan session WhatsApp di `.local/arthur-wa-test/store`;
- menulis log ke `.local/arthur-wa-test/service.log`;
- menulis PID ke `.local/arthur-wa-test/service.pid`.

Melihat log:

```bash
tail -f .local/arthur-wa-test/service.log
```

## 4. Hubungkan nomor WhatsApp testing

Jalankan:

```bash
make wa-test-connect
```

QR akan disimpan di:

```text
artifacts/wa-test/arthur-tester-qr.png
```

Buka gambar QR:

```bash
xdg-open artifacts/wa-test/arthur-tester-qr.png
```

Pada ponsel nomor testing:

1. Buka WhatsApp.
2. Masuk ke **Perangkat tertaut / Linked devices**.
3. Pilih **Tautkan perangkat / Link a device**.
4. Scan QR yang dihasilkan.

Session disimpan secara lokal. Restart biasa tidak memerlukan scan ulang selama
session belum logout atau dihapus.

Jika QR kedaluwarsa, ambil QR terbaru:

```bash
python arthur/scripts/wa_e2e.py qr
```

## 5. Periksa koneksi

```bash
make wa-test-status
```

Session siap dipakai jika hasilnya:

```json
{
  "device_id": "watest_arthur",
  "status": "connected",
  "phone_number": "+62XXXXXXXXXXX"
}
```

Jika status masih `waiting_qr`, scan QR terlebih dahulu.

## 6. Jalankan smoke test Arthur

```bash
make wa-test-run
```

Runner akan:

1. memastikan target runner sama dengan target yang dikunci service;
2. memastikan session tester berstatus `connected`;
3. membersihkan inbox tester;
4. mengirim `/reset` ke Arthur agar state percakapan testing bersih;
5. mengirim pesan scenario satu per satu;
6. menunggu balasan WhatsApp asli dari Arthur;
7. memeriksa kualitas setiap balasan;
8. menghasilkan report PASS/FAIL.

Scenario default:

```text
tests/e2e/scenarios/arthur_interview_smoke.json
```

Scenario default hanya mengetes wawancara dan pergantian bahasa. Scenario
tersebut berhenti sebelum konfirmasi pembuatan agent sehingga tidak membuat
agent baru.

## 7. Lihat hasil test

Report disimpan di:

```text
artifacts/wa-test/arthur-e2e-report.json
```

Membaca report:

```bash
python -m json.tool artifacts/wa-test/arthur-e2e-report.json
```

Setiap turn berisi:

- pesan yang dikirim nomor tester;
- balasan WhatsApp Arthur;
- WhatsApp message ID;
- status `passed`;
- alasan kegagalan jika ada.

Tester otomatis memeriksa:

- balasan kosong atau timeout;
- kebocoran `plan_agent`, state, evidence, atau tool call;
- pertanyaan yang terlalu banyak;
- konteks wajib yang hilang;
- frasa terlarang;
- balasan yang terlalu panjang;
- pertanyaan pribadi/bisnis yang seharusnya sudah dapat diinferensikan.

## 8. Menjalankan scenario custom

Buat file JSON baru, misalnya:

```text
tests/e2e/scenarios/arthur_custom_local.json
```

Contoh:

```json
{
  "name": "Arthur custom interview",
  "reset_before": true,
  "turns": [
    {
      "send": "Halo",
      "expect": {
        "must_include_any": ["Arthur", "AI Staff"],
        "must_not_include": ["plan_agent", "perencanaan"],
        "max_questions": 1,
        "max_length": 350
      }
    },
    {
      "send": "Saya mau bikin agent CS untuk bisnis.",
      "expect": {
        "must_include_any": ["masalah", "kendala", "dibantu"],
        "max_questions": 1,
        "max_length": 500
      }
    }
  ]
}
```

Jalankan:

```bash
python arthur/scripts/wa_e2e.py run \
  --scenario tests/e2e/scenarios/arthur_custom_local.json \
  --report artifacts/wa-test/arthur-custom-report.json
```

Pilihan assertion:

| Field | Fungsi |
|---|---|
| `must_include_any` | Minimal salah satu teks harus ditemukan |
| `must_include_all` | Semua teks wajib ditemukan |
| `must_not_include` | Teks tidak boleh ditemukan |
| `max_questions` | Batas jumlah tanda tanya |
| `max_length` | Batas panjang balasan |
| `timeout_seconds` | Timeout khusus untuk turn tersebut |

Jangan menambahkan persetujuan seperti `sesuai`, `buat sekarang`, atau
konfirmasi akhir jika scenario tidak boleh membuat agent sungguhan.

## 9. Mengosongkan inbox tester

```bash
python arthur/scripts/wa_e2e.py reset-inbox
```

Ini hanya menghapus inbox capture lokal. Perintah ini tidak menghapus chat pada
ponsel dan tidak mereset session Arthur.

Untuk mereset state percakapan Arthur, kirim `/reset` melalui scenario. Runner
melakukannya secara otomatis jika `reset_before` bernilai `true`.

## 10. Hentikan service

```bash
make wa-test-down
```

Perintah menghentikan proses lokal dan menghapus PID file. Store session tetap
disimpan agar tidak perlu scan ulang.

## 11. Menjalankan kembali

Pada terminal baru, export ulang environment:

```bash
export ARTHUR_TEST_TARGET_PHONE=62XXXXXXXXXXX
export WA_TEST_API_KEY='secret-yang-sama'
export WA_TEST_SERVICE_URL=http://127.0.0.1:8082
export WA_TEST_DEVICE_ID=watest_arthur
```

Kemudian:

```bash
make wa-test-up
make wa-test-status
make wa-test-run
```

## 12. Troubleshooting

### `ARTHUR_TEST_TARGET_PHONE wajib diisi`

Export nomor Arthur:

```bash
export ARTHUR_TEST_TARGET_PHONE=62XXXXXXXXXXX
```

### `WA_TEST_API_KEY wajib diisi`

Export API key minimal 16 karakter:

```bash
export WA_TEST_API_KEY='random-secret-minimal-16-karakter'
```

### Status `device not found`

Session belum dibuat. Jalankan:

```bash
make wa-test-connect
```

### Status `waiting_qr`

Scan QR menggunakan nomor WhatsApp testing.

### Status `disconnected`

Coba jalankan kembali:

```bash
make wa-test-connect
```

Jika tetap gagal, cek:

```bash
tail -n 100 .local/arthur-wa-test/service.log
```

### Pesan ditolak dengan `only TEST_TARGET_PHONE`

Nomor yang diberikan runner berbeda dengan target service. Pastikan
`ARTHUR_TEST_TARGET_PHONE` tidak berubah sejak service dijalankan.

Restart setelah memperbaiki target:

```bash
make wa-test-down
make wa-test-up
```

### Arthur tidak membalas

Periksa:

1. session tester berstatus `connected`;
2. nomor Arthur benar;
3. Arthur production sedang online;
4. tester tidak diblokir oleh nomor Arthur;
5. log local tester;
6. log backend Arthur.

Jalankan dengan timeout lebih panjang:

```bash
python arthur/scripts/wa_e2e.py run --timeout 300
```

### Port `8082` sedang digunakan

Periksa proses:

```bash
ss -ltnp | grep ':8082'
```

Hentikan tester lama:

```bash
make wa-test-down
```

## 13. Aturan keamanan

- Gunakan nomor WhatsApp khusus testing.
- Jangan gunakan session Arthur sebagai tester.
- Jangan gunakan nomor shared trial/demo sebagai tester.
- Jangan memakai nomor personal utama.
- Jangan commit API key, QR, report, log, PID, atau store session.
- Jangan push perubahan sistem tester ini.
- Jangan menambahkan tester ke production Compose atau Traefik.
- Jangan menjalankan scenario yang membuat, menghapus, atau mengubah agent
  kecuali side effect tersebut memang sedang diuji dan sudah disetujui.
