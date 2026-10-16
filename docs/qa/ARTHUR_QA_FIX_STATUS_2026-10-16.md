# Status perbaikan laporan QA Arthur — 16 Oktober 2026

Laporan tambahan CV/reset memori Tokyo8 sudah ditangani pada [rilis `4cf0202`, aktif 14.46 WIB](ARTHUR_TOKYO8_CV_MEMORY_FIX_2026-10-16.md). Tabel berikut khusus memetakan item pada lampiran kronologi QA sebelumnya.

**Dari daftar bug pada lampiran ini, yang sudah diperbaiki dan aktif di produksi adalah Bug 1e: link koneksi WhatsApp Meta ke owner terblokir.** Perbaikan ini tidak menyelesaikan loop konfirmasi edit/impor/pause atau balasan quote eskalasi.

Lampiran terbaru identik dengan laporan yang dianalisis sebelumnya. HTTPS produksi `/health` terakhir mengembalikan `ok`, commit aplikasi `4cf0202`. Perbaikan link owner aktif sejak **13.37 WIB, 16 Oktober 2026** dan dipertahankan pada rilis berikutnya.

| Bagian laporan | Status saat ini | Penjelasan |
| --- | --- | --- |
| **1a — Edit instruksi terus meminta konfirmasi** | **Belum diperbaiki** | Frasa persetujuan dan payload draft yang berubah masih dapat membuat gate menolak eksekusi. Instruksi harga/sapaan belum bisa dianggap tersimpan. |
| **1b — Impor FAQ DOCX** | **Belum diperbaiki** | Terblokir mekanisme konfirmasi yang sama. |
| **1c — Impor TXT / knowledge tetap kosong** | **Belum diperbaiki** | Terblokir mekanisme konfirmasi yang sama; hotfix link tidak mengimpor ulang dokumen. |
| **1d — Pause berulang tanpa perubahan status** | **Belum diperbaiki** | Kontrak frasa persetujuan pause belum diselaraskan dengan gate. |
| **1e — Link Meta tidak sampai ke owner** | **Sudah diperbaiki dan dideploy** | Guard kini membedakan agent builder/pengelolaan Arthur dari agent operasional. Owner assistant dapat menerima link penyiapan; tool tetap memeriksa kepemilikan. Link akun owner tetap diblokir pada balasan customer agent operasional. |
| **1 — Perbedaan konfirmasi kode demo / heartbeat / memori** | **Belum diselaraskan** | Cakupan gate belum diubah. Memori Arthur yang tersimpan juga tidak otomatis mengubah instruksi atau knowledge CS. |
| **2 — Quote-reply kartu eskalasi tidak diteruskan** | **Belum diperbaiki** | Penanganan konteks quote pada webhook Meta dan pencarian receipt lintas Arthur–agent customer belum diubah. |
| **3 — Isi `Pesan` kartu ditulis ulang LLM** | **Belum diperbaiki** | Kartu masih memakai ringkasan dari argumen model; belum dijadikan kutipan pesan asli. |
| **3 — Nama `Agent`, nomor sama dengan owner, ID kasus** | **Sebagian merupakan data/ekspektasi QA** | Nama berasal dari profil WhatsApp dan nomor sama karena owner juga menjadi customer penguji. ID kasus pada kartu owner dipakai untuk korelasi. Bukti tersebut tidak cukup untuk menyatakan salah tujuan atau bocor ke customer. Penyajian kartu belum diubah. |
| **4 — Penyangkalan percobaan penyimpanan/eskalasi sebelumnya** | **Belum diperbaiki** | Kontradiksi dengan tool trace memang terverifikasi; bukti hasil tool lintas giliran belum diperbaiki. Kartu eskalasi dari CS merupakan kejadian berbeda dari percobaan handoff Arthur. |
| **4 — Versi 1 / tidak ada revisi pada CS baru** | **Sesuai data yang diperiksa** | Agent baru memang versi 1; mutasi pada retest tertahan. Riwayat agent lama yang dihapus berbeda. Tidak ada bug revisi tersimpan yang terbukti dari gejala ini. |
| **5 — Trial lewat tanggal tetapi masih dapat dipakai** | **Konfigurasi tetap seperti sebelumnya** | Enforcement expiry masih nonaktif. Hotfix tidak mengaktifkannya atau mengubah masa trial. |
| **5 — Nomor WA berada di URL pembayaran** | **Belum diperbaiki** | Pembentukan parameter `wa` pada URL pembayaran belum diubah. |
| **5 — Nama Growth dibanding kode `tier_1`** | **Sesuai katalog kode** | Growth memang memakai identifier `tier_1`; bukan mismatch yang terbukti pada laporan ini. |
| **5 — Kode REFUND berulang** | **Belum diperbaiki** | Repetisi ke owner terverifikasi; kebocoran kode itu ke chat customer belum terbukti pada window yang diperiksa. |
| **5 — Sapaan `Kak` tidak diterapkan** | **Belum selesai** | Penyimpanan instruksi terblokir Bug 1a. Hotfix link tidak mengubah instruksi agent. |

## Bukti dan batas validasi hotfix

- Suite terkait **134 tes lulus**, termasuk **17 kasus khusus link owner**. Kasus akses assistant owner lain, permintaan tanpa konfirmasi, dan kebocoran link ke customer tetap ditolak.
- Probe pada **sesi Lapi Bot aktual di produksi** lulus menggunakan transaksi read-only dan URL contoh: link Meta/Google pada percakapan pengelolaan lolos, sedangkan pada customer agent operasional tetap diblokir.
- Penyelesaian koneksi sampai halaman Meta dan percakapan WhatsApp ulang tetap perlu QA nyata; belum diberi status PASS dari probe tersebut.
- Jalur `confirmation_gate.py`, webhook Meta, lookup quote, kartu eskalasi, dan URL pembayaran tidak berubah setelah image QA `c768a25`.
- Bagian “sudah LOLOS” pada laporan tetap merupakan hasil retest tim QA. Bagian “belum diuji” tetap **belum diuji ulang**, termasuk policy obat keras; tidak otomatis menjadi PASS setelah hotfix ini.

Rincian: [bukti hotfix/deployment dan rollback](ARTHUR_OWNER_LINK_HOTFIX_2026-10-16.md), [analisis laporan beserta validasi log](ARTHUR_RETEST_ROOT_CAUSE_2026-10-16.md).
