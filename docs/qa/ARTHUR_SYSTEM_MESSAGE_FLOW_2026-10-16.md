# Penulisan system message Arthur V2

Perubahan 16 Oktober 2026 memisahkan penulisan instruksi dari percakapan Arthur.

| Tahap | Pelaksana | Hasil |
| --- | --- | --- |
| Wawancara dan draft kebutuhan | Model percakapan Arthur yang tersimpan pada record agent | Brief instruksi, tujuan, workflow, dan kemampuan yang disetujui owner. |
| Validasi | Backend `create_assistant` / `update_assistant` | Konfirmasi, ownership, kebijakan, slot/plan dan kelengkapan workflow sebelum penyimpanan. |
| Menulis system message | `anthropic/claude-sonnet-5.5` melalui OpenRouter | Dokumen Markdown dengan judul `# SYSTEM MESSAGE — <nama>`, identitas, dan bagian bernomor sesuai tugas. Tidak menerima tools eksekusi atau seluruh riwayat/memori Arthur. |
| Menyimpan | Backend | Memeriksa hasil lengkap dan format Markdown, memeriksa kebijakan, menambahkan aturan tools backend tanpa ditulis ulang, menyimpan instruksi beserta receipt model/format/hash brief. Kegagalan penulis membatalkan pembuatan/perubahan. |
| Merakit prompt runtime | `build_system_prompt` | Menggabungkan instruksi tersimpan dengan konteks platform, memori, knowledge/RAG, waktu dan aturan channel. Tanpa memanggil Sonnet lagi untuk setiap chat. |
| Percakapan agent buatan | Model runtime milik agent | Default normal tetap DeepSeek Flash; advance tetap Claude Haiku. Model Arthur dan agent yang sudah ada tidak diganti oleh perubahan penulis. |

Format mengikuti struktur contoh LapiBot yang diberikan owner: heading, pemisah, daftar, penekanan tebal, serta bagian MISI, PERSONA, ALUR KERJA, aturan dan eskalasi sesuai kebutuhan. Data, nomor, spreadsheet atau workflow spesifik pada contoh tidak menjadi template universal.

Penulis digunakan saat membuat agent dan ketika mengganti instruksi penuh. Edit nama/tujuan tanpa instruksi tidak memanggilnya. Agent lama tidak ditulis ulang otomatis. Jika API penulis gagal, sistem tidak diam-diam memakai model percakapan sebagai pengganti. Rollback mengembalikan snapshot instruksi yang dipilih, bukan menulis ulang dengan AI.

Ini penulisan dokumen oleh model khusus, bukan agent otonom baru. Validasi struktur tidak membuktikan setiap aturan bisnis sudah sempurna; owner tetap perlu QA perilaku agent dengan workflow aslinya.

Kode: [writer](../../arthur_v2/instruction_writer.py), [pembuatan/pengubahan](../../arthur_v2/plugin.py), [konfigurasi](../../app/config.py), [prompt runtime](../../app/core/engine/prompt_builder.py).

Riwayat: sebelum perubahan ini, V2 menyimpan instruksi dari model utama Arthur langsung dengan tambahan aturan backend. Composer builder legacy terpisah bukan jalur aktif V2.

Status rilis, bukti pengujian dan rollback: [laporan hotfix](ARTHUR_INSTRUCTION_DOCUMENT_FIX_2026-10-16.md).
