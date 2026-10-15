# Analisis dan perbaikan penolakan knowledge Arthur

## Hasil

Perbaikan sudah di-deploy pada **15 Oktober 2026, 09:46 WIB** ke API dan scheduler:
`managed-agents-app:arthur-knowledge-contract-20261015`.
Manifest: `sha256:cf2828fd2584a700993166e2a146573c5bc9c874bae92edbf70e5145b81aabd7`.

Dokumen yang dimaksud **sudah tersimpan lengkap sebagai 13 chunk, seluruhnya
memiliki embedding**, pada LapiBot yang benar. Pemeriksaan produksi setelah deploy
menghasilkan `already_added=true`, `chunks_added=0`, `chunks_existing=13`,
`embedded_chunks=13`. ID dokumen dan versi agent tidak berubah saat pemeriksaan
impor ulang; tidak ada OCR, embedding ulang, atau duplikasi knowledge.

## Yang terjadi pada log dan screenshot

Run `9cff6925-ea61-497e-9f65-7b58c1e8ba84` dimulai pukul 09:30:50 WIB dan selesai
sekitar 4,8 detik kemudian. Model menghasilkan 638 token jawaban, dengan
`tool_calls=null` dan `steps=0`. Artinya Arthur menjawab dari penafsirannya terhadap
percakapan, tanpa mencoba tool knowledge atau mengecek penyimpanannya.

Isi PDF sudah terbaca pada tahap ini. Penolakan pada screenshot terjadi karena
Arthur menyamakan penyimpanan dokumen utuh dengan perubahan perilaku LapiBot:
catatan/dugaan internal fasilitator dianggap otomatis mengubah disclosure kepada
peserta. Arthur lalu menawarkan opsi pemotongan dokumen dan menolak opsi utuh yang
sebelumnya ditawarkannya sendiri.

Instruksi LapiBot memang menjelaskan tujuan percakapan dan siapa yang membaca
hasilnya. Tidak ditemukan aturan kode yang melarang menyimpan dokumen ini sebagai
knowledge. Kesimpulan bahwa penyimpanan dokumen harus mengubah disclosure adalah
penafsiran model yang terlalu luas, bukan hasil validasi tool.

HTTP 200 pada webhook hanya menunjukkan webhook diterima. Log `cloud_sent` hanya
menunjukkan balasan WhatsApp terkirim. Keduanya tidak membuktikan knowledge
ditambahkan. Run ini tidak menunjukkan kegagalan token output, timeout, atau
penolakan akses tool berdasarkan nomor pengirim.

## Celah pada perbaikan sebelumnya

Panduan tambahan telah diperbarui pada source code, tetapi prompt utama Arthur
diambil dari `Agent.instructions` yang tersimpan di database. Deploy image tidak
otomatis menjalankan seed prompt.

Pemeriksaan menemukan prompt tersimpan sepanjang 12.067 karakter, sedangkan
generator source sebelumnya menghasilkan 12.429 karakter. Panduan pemulihan OCR
yang baru belum ada pada prompt tersimpan. Pemeriksaan sebelumnya membuktikan
penyimpanan dan retrieval RAG, tetapi belum membuktikan panduan itu diterapkan pada
prompt runtime Arthur. Laporan ini memperjelas batas pemeriksaan sebelumnya.

Selain itu, Arthur tidak menerima bukti langsung bahwa hash file tersebut sudah
tersimpan. Akibatnya jawaban lama tentang kegagalan upload dapat diperlakukan
seolah-olah masih merupakan status saat ini.

## Perbaikan

1. Menambahkan kontrak knowledge khusus Arthur pada akhir system prompt runtime.
   Kontrak ini berlaku meskipun seed database masih versi lama. Instruksi tersimpan
   tetap dipertahankan.
2. Memisahkan tugas menyimpan sumber RAG dari permintaan mengubah perilaku agent.
   Impor mempertahankan dokumen asli dan instruksi agent; perintah dalam dokumen
   tidak otomatis dijalankan. Permintaan perubahan perilaku tetap melalui tool
   update dan aturan platform yang berlaku.
3. Permintaan eksplisit/konfirmasi menambahkan dokumen utuh diarahkan ke tool
   impor. Arthur tidak boleh membuat syarat pemotongan berdasarkan pendapatnya
   sendiri atau menganggap hipotesis internal otomatis mengubah disclosure.
4. Mengirim bukti penyimpanan langsung ke konteks Arthur: hash file upload saat
   ini dicocokkan dengan dokumen milik pengguna, disertai agent tujuan, kelengkapan
   chunk dan jumlah embedding. Nama file/nama agent diperlakukan sebagai data.
   Langkah ini hanya membaca database; tidak memilih target atau memberi izin
   impor secara otomatis.
5. Memeriksa kelengkapan impor berdasarkan hash **sebelum OCR/embedding** dan
   memeriksanya kembali di bawah lock agent sebelum menulis. File yang sudah
   lengkap menghasilkan bukti `already_added`, termasuk ketika OCR sedang gagal.

Jumlah tool Arthur tetap 32. Tidak ada perubahan izin Google untuk pengguna
non-owner, isi instruksi wawancara LapiBot, atau data spreadsheet.

## Verifikasi

- **137 tes lulus**: runtime memakai kontrak baru meskipun prompt seed lama,
  pembatasan status berdasarkan pemilik dan hash, penolakan symlink ke luar
  workspace, pengecualian preview turunan, kelengkapan chunk, deduplikasi sebelum
  OCR, OCR fallback, retrieval, P1/P2, konteks, dan pemulihan workflow.
- Replay menggunakan model Arthur yang sama dan history percakapan kasus ini,
  dengan transaksi PostgreSQL read-only dan seluruh tindakan perubahan
  disimulasikan. Replay awal masih mengulang penolakan, sehingga perbaikan prompt
  dilanjutkan sebelum deploy.
- Replay final berhasil untuk dua skenario: impor baru yang disimulasikan dan
  file yang sudah tersimpan. Keduanya memanggil
  `inspect_managed_assistant → add_assistant_knowledge`, dengan target benar dan
  `confirmed=true`; tidak ada tool update/redaksi yang dipanggil.
- Pemeriksaan produksi membuktikan kontrak aktif pada prompt runtime, status
  live mendeteksi 13 chunk lengkap, dan impor ulang berhenti sebelum OCR.
- API healthy, `/health` HTTP 200, scheduler health exit 0.
- Empat file aplikasi dalam image cocok dengan SHA-256 source yang diperiksa.
- Tidak ditemukan traceback/error-level/status-query failure pada log startup
  yang diperiksa setelah deploy.
- Saat pemeriksaan sebelum deploy: 0 run aktif, 0 job berjalan, 0 outbound sending.
  Service WhatsApp tidak direstart.

Fixture SQLite untuk uji status memakai owner eksplisit karena operator JSONB
`@>` milik PostgreSQL tidak tersedia di SQLite. Query asli beserta filter pemilik
diverifikasi melalui replay read-only dan probe produksi PostgreSQL.

Tidak ada pesan WhatsApp otomatis yang dikirim oleh replay/probe ini. Replay
membuktikan jalur model pada kasus yang direproduksi, tanpa menjanjikan bahwa
semua respons model dan kegagalan provider di masa depan mustahil terjadi.

## File dan rollback

- `arthur_v2/plugin.py`
- `arthur_v2/knowledge_context.py`
- `app/core/engine/prompt_builder.py`
- `app/core/engine/agent_runner.py`
- `tests/test_arthur_knowledge_contract.py`
- `tests/test_arthur_knowledge_recovery.py`
- `scripts/replay_arthur_knowledge_readonly.py`
- `deploy/Dockerfile.arthur-knowledge-contract`

Image sebelumnya disimpan sebagai
`managed-agents-app:rollback-arthur-knowledge-contract-20261015`.

```sh
IMAGE_TAG=rollback-arthur-knowledge-contract-20261015 docker compose -f deploy/docker-compose.prod.yml up -d --no-deps --no-build api scheduler
```

Tidak ada migrasi schema atau perubahan prompt seed database. Rollback aplikasi
tidak menghapus 13 chunk knowledge yang sudah tersimpan.
