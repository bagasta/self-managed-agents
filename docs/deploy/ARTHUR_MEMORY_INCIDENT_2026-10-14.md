# Analisis dan perbaikan Arthur: pembuatan berulang dan bias memori

Tanggal: 14 Oktober 2026, WIB.

## Hasil

Perbaikan sudah dipasang pada API produksi dengan image `managed-agents-app:arthur-memory-20261014-r2`. Health check internal dan HTTPS publik mengembalikan HTTP 200. Scheduler, WhatsApp utama, WhatsApp demo, Redis, dan PgBouncer tidak direstart. Riwayat dan memori pengguna tidak dihapus.

Pemeriksaan akhir sekitar 15:42 WIB: container API healthy; hash sembilan file runtime cocok dengan source yang diuji; log container baru tidak mencatat event error/failed/abandoned atau respons HTTP 5xx. Image final juga ditandai latest dan container pengujian sementara sudah dibersihkan.

Validasi akhir: **72 tes lulus**. Replay menggunakan model asli dan konteks akun terdampak berhasil untuk pembuatan ulang LapiBot serta pemisahan proyek baru. Semua tindakan perubahan pada replay disimulasikan: tidak membuat agent pengguna, tidak membuat OAuth sungguhan, dan tidak mengirim WhatsApp.

## Yang terjadi pada screenshot

Tiga percobaan cocok dengan log server:

| Waktu WIB | Run | Temuan |
| --- | --- | --- |
| 14:50 | `0af5c086…` | Generasi pertama menghabiskan 4096 completion token tanpa jawaban/tool yang bisa dieksekusi. Retry hanya menyelesaikan cek paket dan daftar agent; jawaban akhir tetap kosong. |
| 15:12 | `f64968f7…` | Generasi dan retry sama-sama tidak menghasilkan jawaban atau aksi pembuatan yang selesai. Total completion token tercatat 8192. |
| 15:15 | `3876733f…` | Generasi pertama terpotong. Retry menyelesaikan cek paket, tetapi tidak memanggil create_assistant dan tidak menghasilkan jawaban akhir. |

Runtime kemudian mengirim salah satu fallback yang tampak pada screenshot. Log retry sebelumnya menyebut “complete” walaupun yang selesai hanya beberapa tool baca dan `reply_len=0`. Perbaikan deploy sebelumnya untuk edit/model belum menyelesaikan kegagalan ini.

## Akar masalah

1. **Memori lintas proyek menumpuk dalam satu konteks owner.** Ada 198 catatan scoped, total 47.839 karakter. Isinya mencampur properti, klinik, toko, personal assistant, LapiBot lama, serta status setup yang sudah berubah. Ini bukan kebocoran antar-owner, melainkan pencampuran proyek milik owner yang sama.
2. **Status historis bersaing dengan fakta terkini.** Misalnya catatan lama masih menyatakan Trial atau tidak ada agent aktif, sedangkan tool paket terkini menunjukkan Business dan beberapa agent aktif. Status koneksi, kode trial, dan link lama juga tersimpan.
3. **Konteks yang sama diduplikasi.** Generic memory, catatan harian, longterm, last_turn, active_context, ringkasan sesi, dan history membawa kembali potongan percakapan yang saling tumpang tindih. Fallback kosong juga disimpan sebagai “jawaban final” dalam memori runtime.
4. **Instruksi Arthur lama masih disisipkan ke V2.** Label grup `builder` menyebabkan prompt builder menambahkan alur V1 seperti compose_agent_blueprint/create_agent, padahal V2 menyediakan create_assistant/update_assistant. Model mendapat kontrak yang bertentangan.
5. **Anggaran output tidak cukup untuk serialisasi workflow lengkap.** Batas 4096 terlalu kecil pada kasus ini. Retry juga memulai ulang dari input awal. Override token melalui RunnableBinding berisiko hilang ketika graph melakukan bind_tools lagi.
6. **Belum ada batas proyek yang bertahan antar-pesan.** Dalam uji negatif awal, model tetap membuat LapiBot simulasi ketika permintaan terbaru meminta proyek klinik baru. Ini membuktikan bahwa instruksi “jangan bias” saja belum memadai.

## Perbaikan

- Arthur mendapat recall terbatas, maksimum 6000 karakter, yang memilih catatan proyek bernama pada permintaan aktif serta preferensi personal yang relevan. Status paket, koneksi, kode/link sementara, log harian, dan snapshot task lama tidak otomatis diangkat menjadi fakta aktif.
- Untuk replay “buat lapibot”, recall turun dari kumpulan scoped 47.839 karakter menjadi **1956 karakter**. Detail lama tetap tersimpan dan dapat dicari secara spesifik bila diperlukan.
- Permintaan eksplisit seperti “buat agent baru” memulai batas proyek di metadata sesi. Pesan berikutnya memakai history sejak batas itu dan tidak memakai ringkasan proyek lama. Ini hanya membatasi konteks; tidak mengeksekusi pembuatan atau menghapus data.
- History yang diinjeksi dibatasi 20.000 karakter dan ringkasan lama maksimum 3500 karakter. Memori daily/longterm/active_context yang duplikatif tidak diinjeksi pada Arthur V2.
- Instruksi builder V1 tidak lagi ditambahkan pada V2. Aturan memory V2 meminta penyimpanan requirement dengan nama/ID proyek dan melarang penyimpanan link autentikasi/status sementara sebagai fakta durable.
- Ekstraksi otomatis generik yang menghasilkan key lintas proyek seperti auto_task_status tidak lagi dijalankan untuk Arthur V2. Memory tools eksplisit tetap tersedia.
- Kedua fallback pada screenshot dikeluarkan dari history yang diputar ulang, termasuk data lama yang belum diberi tag. Fallback tidak lagi menimpa memori runtime.
- Anggaran output Arthur menjadi **16.384 token**, terpisah dari anggaran assistant target. Prompt meminta instruksi target ringkas dan hanya memuat workflow yang disetujui.
- Retry melanjutkan dari hasil tool yang sudah selesai, membuang generasi terminal yang tidak lengkap, dan tidak dimulai bila ada tool call menggantung dengan status eksekusi tidak diketahui. Anggaran retry dinaikkan pada field model sebenarnya sehingga bertahan saat bind_tools.
- Log retry membedakan jawaban selesai dan hasil parsial. Jika create_assistant benar-benar berhasil tetapi jawaban akhir kosong, fallback hanya melaporkan keberhasilan yang dibuktikan hasil tool; tidak mengarang link atau status koneksi.

## Bukti pengujian

72 tes mencakup pemisahan proyek, penolakan konteks proyek lama pada permintaan baru, batas ukuran recall/history, instruksi V2 tanpa alur V1, pengecualian fallback dari history/memory, kelanjutan retry tanpa mengulang hasil mutation, penolakan recovery pada tool menggantung, anggaran retry setelah bind_tools, serta regresi edit, model, Google OAuth, knowledge, context binding, dan memory umum.

Replay model asli memakai session owner terdampak dengan tool mutation yang seluruhnya diganti stub:

| Skenario | Hasil |
| --- | --- |
| `buat lapibot`, replay pertama | Satu create_assistant simulasi, Google Sheets sesuai brief, jawaban akhir 595 karakter; 5046 completion token total. |
| `buat lapibot`, verifikasi ulang | Satu create_assistant simulasi bernama LapiBot, Google Sheets, jawaban akhir 855 karakter; 5731 completion token total. |
| Agent reservasi klinik baru, berbeda dari LapiBot | Tidak membuat agent dari brief lama; mengecek paket/inventory dan meminta brief proyek baru. Recall 588 karakter, tanpa history proyek lama. |

Script reproduksi: `scripts/replay_arthur_creation_readonly.py`. Memerlukan session UUID sebagai argumen; tidak mencetak isi percakapan, nomor owner, atau token autentikasi. Script hanya melakukan pembacaan paket/inventory dan inferensi model; semua action tool disimulasikan.

## Deploy dan rollback

- Image rollback sebelum pekerjaan ini: `managed-agents-app:rollback-arthur-memory-20261014`.
- Dockerfile hotfix: `deploy/Dockerfile.arthur-memory-hotfix`; hanya melapisi file runtime terkait di atas image produksi sebelumnya.
- Candidate diboot dalam canary tanpa rute publik dan dengan startup side effects dinonaktifkan; health HTTP 200.
- Pengecekan database menunjukkan nol run aktif sebelum setiap penggantian API. Compose memakai graceful shutdown. Ada interval kecil antara pengecekan dan shutdown, sehingga ini tidak dinyatakan sebagai jaminan downtime nol.
- Seed V2 memperbarui prompt Arthur dan budget output; tidak mengubah assistant milik pengguna.
- Tidak menjalankan migrasi, menghapus sesi, atau mereset seluruh memori. Perubahan lokal yang sudah ada sebelum pekerjaan ini dipertahankan.

Rollback, setelah run aktif selesai:

```sh
IMAGE_TAG=rollback-arthur-memory-20261014 docker compose -f deploy/docker-compose.prod.yml up -d --no-deps --no-build api
docker exec deploy-api-1 python -m arthur_v2.seed
docker tag managed-agents-app:rollback-arthur-memory-20261014 managed-agents-app:latest
```

Metadata batas proyek yang telah tersimpan tidak menghapus riwayat dan diabaikan oleh kode lama. Jangan menurunkan seluruh stack atau menghapus volume.

## Batas kepastian

Kasus yang dilaporkan telah direproduksi dan jalur perbaikannya lulus pengujian serta replay model asli. Replay tidak membuktikan pengiriman WhatsApp, OAuth eksternal, atau pembuatan agent sungguhan dari ujung ke ujung. Keberhasilan semua variasi percakapan di masa depan tidak dapat dijamin; log produksi perlu menjadi bukti ketika request pengguna berikutnya masuk. Tidak ada pesan uji yang dikirim atas nama pengguna.
