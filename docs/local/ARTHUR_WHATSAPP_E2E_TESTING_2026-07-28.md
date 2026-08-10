# Arthur WhatsApp E2E Testing

## Tujuan

Menjalankan pengujian black-box ke nomor WhatsApp Arthur menggunakan nomor
testing khusus. Pesan benar-benar melewati jaringan WhatsApp. Session tester
tidak menjadi agent, tidak menggunakan nomor demo, dan tidak menggantikan
session WhatsApp Arthur.

## Batas layanan

- `wa-service`: session agent production, termasuk session khusus Arthur.
- `wa-dev-service`: nomor shared trial/demo untuk agent buatan user.
- `wa-test-service`: session nomor customer khusus testing yang hanya boleh
  mengirim ke nomor Arthur.

`wa-test-service` memakai volume, port, inbox, dan linked-device session
terpisah. Semua pesan masuk selain dari chat target Arthur diabaikan dan tidak
diteruskan ke endpoint agent.

## Scope lokal

Sistem ini hanya untuk workspace lokal. Jangan commit atau push file perubahan
tester, jangan menambahkannya ke `deploy/docker-compose.prod.yml`, dan jangan
menjalankannya sebagai service production.

Binary, PID, log, dan store session lokal tersimpan di:

```text
.local/arthur-wa-test/
```

## Konfigurasi

Tambahkan ke environment production atau export pada shell:

```bash
export ARTHUR_TEST_TARGET_PHONE=62XXXXXXXXXXX
export WA_TEST_API_KEY='generate-random-secret-minimum-16-chars'
export WA_TEST_SERVICE_URL=http://127.0.0.1:8082
export WA_TEST_DEVICE_ID=watest_arthur
```

`ARTHUR_TEST_TARGET_PHONE` wajib berisi nomor WhatsApp Arthur yang sebenarnya,
bukan nomor shared trial/demo. API key jangan disimpan di Git.

## Menyalakan service

```bash
make wa-test-up
```

Service berjalan sebagai proses lokal pada loopback `127.0.0.1:8082`. Tidak
memakai Docker Compose production dan tidak memiliki route Traefik/public.

## Menghubungkan nomor testing

```bash
make wa-test-connect
```

Perintah menyimpan QR ke:

```text
artifacts/wa-test/arthur-tester-qr.png
```

Scan QR tersebut dari WhatsApp nomor testing melalui menu Linked devices.
Session tersimpan di volume `wa_test_store`, sehingga tidak perlu scan ulang
setelah restart biasa.

Cek status:

```bash
make wa-test-status
```

Status yang siap dipakai adalah `connected`.

## Menjalankan smoke test

```bash
make wa-test-run
```

Scenario default:

```text
tests/e2e/scenarios/arthur_interview_smoke.json
```

Sebelum scenario, runner mengirim `/reset` ke Arthur agar state discovery untuk
nomor testing dibersihkan. Scenario default berhenti pada wawancara dan tidak
membuat agent baru.

Report disimpan di:

```text
artifacts/wa-test/arthur-e2e-report.json
```

Setiap turn memuat pesan tester, balasan Arthur, message ID, status PASS/FAIL,
dan alasan kegagalan. Guard bawaan memeriksa kebocoran nama tool/state internal,
pertanyaan berlebihan, konteks wajib, teks terlarang, dan panjang balasan.

## Menjalankan scenario lain

```bash
python arthur/scripts/wa_e2e.py run \
  --scenario tests/e2e/scenarios/custom.json \
  --report artifacts/wa-test/custom-report.json
```

Format turn:

```json
{
  "send": "Pesan dari tester",
  "expect": {
    "must_include_any": ["kata A", "kata B"],
    "must_include_all": ["konteks wajib"],
    "must_not_include": ["teks terlarang"],
    "max_questions": 1,
    "max_length": 500
  }
}
```

## Safety

- Semua endpoint selain `/health` membutuhkan header `X-Test-Key`.
- Setiap outbound text, contact, image, document, dan typing event ditolak jika
  targetnya bukan `ARTHUR_TEST_TARGET_PHONE`.
- Target JID/LID hasil resolusi WhatsApp diikat setelah pesan berhasil dikirim.
- Hanya balasan dari phone/JID/LID target yang masuk ke inbox test.
- Pesan test tidak pernah diteruskan oleh `wa-test-service` ke main API.
- Jangan memakai nomor personal utama sebagai session tester.
