# Tools agent untuk pengguna non-owner

Status: sudah deploy, 14 Oktober 2026. Pemeriksaan akhir sekitar 16:16 WIB.

## Perilaku yang berlaku

Tools yang dikonfigurasi pada agent dapat dipakai agent untuk melayani pengguna yang diterima oleh channel tersebut, termasuk nomor non-owner/non-operator. Label personal, business, atau customer tidak menjadi syarat penggunaan tools.

Kredensial integrasi tetap milik owner. Service/operation allowlist, batas resource yang dikonfigurasi, validasi argumen, dan aturan workflow tetap berlaku. Hak mengubah konfigurasi agent atau mengelola koneksi OAuth terpisah dari penggunaan tools pada workflow.

## Penyebab dan perubahan

LapiBot memiliki Google Sheets dengan izin baca/tulis, tetapi runtime sebelumnya menghapus server Google MCP dari konteks non-operator kecuali ada flag delegasi. Agent ini berlabel personal dan tidak memiliki flag tersebut. Akibatnya tool tersedia bagi owner tetapi hilang saat peserta berbicara.

- Menghapus penolakan runtime berdasarkan peran pengirim dan ketergantungan pada delegated_runtime_access. Konfigurasi lama tanpa flag atau dengan flag false mengikuti perilaku baru tanpa migrasi database.
- Memisahkan persiapan koneksi agent ke prepare_agent_google_workspace_runtime, yang mempertahankan konfigurasi tools dan menggunakan identitas OAuth owner.
- Identifikasi owner/operator hanya dipakai untuk pengelolaan koneksi; tool pembuat link autentikasi tidak ditawarkan kepada pelanggan.
- Menghapus syarat owner/operator dari recovery spreadsheet yang hilang. Recovery tetap mengikuti tool yang tersedia dan permintaan tindakan yang tervalidasi.
- Memperjelas Runtime Tool Contract agar model tidak menolak penggunaan tools hanya karena pengguna bukan owner atau agent berlabel personal.
- Arthur tidak lagi menulis flag delegasi yang bergantung pada jenis assistant.
- Pemeriksaan tools lain tidak menemukan pencabutan tools umum berdasarkan nomor non-owner seperti gate Google yang dihapus. Aturan khusus eskalasi, ownership konfigurasi, autentikasi, dan workflow bukan larangan umum terhadap penggunaan tools oleh pelanggan.

## Validasi

**157 tes lulus**, mencakup Google MCP, tools Sheets/Drive, batas service/operation, kredensial owner, OAuth, capability contract, serta regresi Arthur, memory, context, dan edit.

Tes khusus menggunakan sesi non-owner pada tiga jenis agent dengan flag delegasi tidak ada, false, dan true. Semuanya mempertahankan tools baca/tulis, memakai token owner, serta tidak menawarkan tool link OAuth kepada pelanggan.

Probe langsung terhadap LapiBot dijalankan dengan konteks sesi non-owner dan koneksi Google sebenarnya, sebelum dan sesudah deploy:

- Agent tetap berlabel personal; tidak mengubah record izin atau jenis agent.
- Tools read_sheet_values dan modify_sheet_values tersedia.
- Pembacaan terbatas pada header Peserta!A1:C1 berhasil.
- Tidak menjalankan tool write, mengirim pesan, atau memulai percakapan agent.
- Script: scripts/probe_customer_google_tools_readonly.py; output tidak memuat isi spreadsheet, token, atau identitas pengguna.

Tes membuktikan akses baca aktual dan ketersediaan tool write. Write tidak diuji terhadap data produksi. Alur chat lengkap berikutnya tetap mengikuti instruksi dan izin workflow agent.

## Deploy

Image aktif: managed-agents-app:customer-workflow-tools-20261014, juga ditandai latest.

Dockerfile: deploy/Dockerfile.customer-workflow-tools-hotfix. Hanya empat file runtime terkait dilapisi di atas image produksi sebelumnya. Tidak ada migrasi, seed ulang, reset memory, atau perubahan massal konfigurasi agent.

Canary mengembalikan health HTTP 200. Database menunjukkan nol run aktif sebelum API diganti dengan graceful shutdown. Scheduler, WhatsApp utama/demo, Redis, dan PgBouncer tidak direstart. Pengecekan idle sebelum shutdown tidak menjamin tidak ada request baru pada interval pergantian.

Setelah deploy: API healthy; internal/public health HTTP 200; hash keempat file cocok dengan source yang diuji; log container baru tidak mencatat error/failed/abandoned, penolakan non-operator, atau HTTP 5xx pada periode pemeriksaan. Container pengujian telah dibersihkan.

## Rollback

Image sebelumnya: managed-agents-app:rollback-customer-workflow-tools-20261014.

Setelah run aktif selesai:

```sh
IMAGE_TAG=rollback-customer-workflow-tools-20261014 docker compose -f deploy/docker-compose.prod.yml up -d --no-deps --no-build api
docker tag managed-agents-app:rollback-customer-workflow-tools-20261014 managed-agents-app:latest
```

Rollback mengembalikan perilaku gate lama. Tidak membutuhkan perubahan database. Jangan menurunkan seluruh stack atau menghapus volume.
