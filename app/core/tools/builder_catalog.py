"""Static catalog data for Arthur builder tools.

Kept separate from builder_tools.py so the runtime tool logic is easier to audit.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Structured preset definitions — source of truth for agent types
# ---------------------------------------------------------------------------

AGENT_PRESETS: dict[str, dict] = {
    "coding_deploy_agent": {
        "label": "Coding & Deploy Agent",
        "description": "Agent yang bisa menulis kode, menjalankannya di sandbox Docker, dan men-deploy ke public URL via Cloudflare tunnel.",
        "default_model": "openai/gpt-4.1-mini",
        "default_temperature": 0.5,
        "default_max_tokens": 2048,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": False,
            "sandbox": True,
            "deploy": True,
            "tool_creator": False,
            "scheduler": False,
            "rag": False,
            "http": False,
            "whatsapp_media": False,
            "subagents": {"enabled": True},
        },
        "required_tools": ["sandbox", "deploy", "subagents"],
        "forbidden_tools": [],
        "channel_requirements": [],
        "runtime_limitations": [
            "deploy_requires_docker_socket",
            "cloudflare_tunnel_url_changes_on_redeploy",
            "deploy_ttl_24h_max",
            "no_persistent_storage_across_sessions",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, asisten coding & deploy yang mengorkestrasi subagent sys_coder.\n\n"
            "CARA KERJA — WAJIB BEDAKAN DUA JENIS OUTPUT:\n\n"
            "A. WEBSITE / WEB APP / LANDING PAGE / PORTFOLIO / TOOLS WEB:\n"
            "   1. Terima request dari user\n"
            "   2. Delegasikan ke sys_coder via task(): 'Buat [website/tools], vanilla HTML/CSS/JS terpisah, deploy ke port 8080'\n"
            "   3. sys_coder akan deploy dan return URL https://*.trycloudflare.com\n"
            "   4. Relay URL ke user: 'Sudah live di: [URL] (aktif 24 jam)'\n"
            "   JANGAN kirim file HTML via WhatsApp untuk website — user butuh URL, bukan file\n\n"
            "B. FILE DELIVERABLE (PDF, laporan, data, chart, script untuk didownload):\n"
            "   1. Delegasikan ke sys_coder/sys_analyst via task()\n"
            "   2. Subagent simpan ke /workspace/shared/<filename> dan return SIAP_DIKIRIM_PARENT\n"
            "   3. Kirim file ke user via send_whatsapp_document() atau send_whatsapp_image()\n\n"
            "ATURAN KERAS:\n"
            "- JANGAN instruksikan agent menulis kode sendiri — semua kode via task() ke subagent\n"
            "- JANGAN panggil deploy_app() sendiri — biarkan sys_coder yang deploy\n"
            "- JANGAN campur deploy (→ URL) dengan file delivery (→ WhatsApp)\n"
            "- Bersikap seperti AI Assistant yang ramah — relay hasil, bukan jelaskan kode\n"
            "- JANGAN tampilkan source code di jawaban kecuali user eksplisit minta\n"
            "- Jawaban BELUM selesai sampai URL atau file sudah dikonfirmasi ke user"
        ),
        "smoke_test": {
            "strategy": "manual",
            "steps": [
                "Kirim pesan: 'buat halaman HTML hello world dan deploy'",
                "Cek agent merespons dengan ramah dan memberikan DEPLOY_URL yang valid",
                "Buka URL di browser — pastikan halaman tampil",
                "Kirim pesan edit: 'ganti tulisannya jadi Selamat Datang'",
                "Pastikan agent tidak deploy ulang, hanya update file",
            ],
            "expected_status": "Pesan ramah menyertakan URL trycloudflare.com",
            "known_failure_modes": [
                "Docker socket tidak tersedia → deploy_app() akan error",
                "Cloudflare rate limit → URL tidak muncul dalam 40s",
            ],
        },
    },
    "cs_whatsapp_basic": {
        "label": "CS WhatsApp Basic",
        "description": "Agent customer service untuk WhatsApp — jawab pertanyaan pelanggan, eskalasi ke operator jika perlu.",
        "default_model": "openai/gpt-4.1-mini",
        "default_temperature": 0.7,
        "default_max_tokens": 800,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": True,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": False,
            "rag": False,
            "http": False,
            "whatsapp_media": True,
            "subagents": {"enabled": False},
        },
        "required_tools": ["escalation"],
        "forbidden_tools": ["deploy"],
        "channel_requirements": ["whatsapp_device_required"],
        "runtime_limitations": [
            "markdown_not_rendered_on_whatsapp",
            "no_broadcast_capability",
            "one_wa_number_per_agent",
            "wa_device_scan_required_before_use",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, {role} dari {business}.\n\n"
            "TUGASMU:\n"
            "- Jawab pertanyaan pelanggan dengan ramah dan singkat\n"
            "- Catat nama dan kebutuhan user ke memory saat pertama kali ngobrol\n"
            "- Eskalasikan ke operator jika tidak bisa bantu atau ada komplain serius\n\n"
            "CARA BICARA:\n"
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya santai tapi sopan.\n"
            "Panjang pesan: singkat, 1-3 kalimat\n"
            "JANGAN pakai simbol *, #, atau format markdown apapun\n\n"
            "ESKALASI KE OPERATOR:\n"
            "Eskalasikan jika: komplain serius, pertanyaan tidak bisa dijawab, minta refund\n"
            "Cara WAJIB: panggil tool escalate_to_human(reason, summary) terlebih dahulu — baru balas user\n"
            "JANGAN bilang 'diteruskan ke tim' tanpa memanggil tool escalate_to_human\n\n"
            "INFORMASI BISNIS:\n"
            "{business_info}\n\n"
            "CONTOH PERCAKAPAN:\n"
            "User: Halo, saya mau tanya soal produk\n"
            "{name}: Halo! Tentu, saya siap bantu. Produk apa yang ingin ditanyakan?"
        ),
        "smoke_test": {
            "strategy": "manual_wa",
            "steps": [
                "Scan QR di wa-dev-service / wa-service untuk hubungkan nomor WA",
                "Kirim pesan: 'Halo, apakah toko buka hari ini?'",
                "Pastikan agent merespons mengikuti bahasa user tanpa markdown",
                "Kirim: 'Saya mau komplain pesanan saya rusak'",
                "Pastikan agent memanggil escalate_to_human sebelum merespons",
            ],
            "expected_status": "Respons singkat tanpa markdown, eskalasi berjalan saat diminta",
            "known_failure_modes": [
                "WA device belum di-connect → pesan tidak terkirim",
                "Escalation_config kosong → operator tidak dapat notifikasi",
            ],
        },
    },
    "approval_gated_service_agent": {
        "label": "Approval-Gated Service Agent",
        "description": (
            "Agent WhatsApp untuk layanan/order berbayar yang mengumpulkan kebutuhan, meminta pembayaran, "
            "meneruskan bukti ke admin/operator, menunggu approval, lalu mengirim hasil layanan."
        ),
        "default_model": "openai/gpt-4.1-mini",
        "default_temperature": 0.4,
        "default_max_tokens": 2048,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": True,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": False,
            "rag": False,
            "http": True,
            "whatsapp_media": True,
            "subagents": {"enabled": False},
        },
        "required_tools": ["escalation"],
        "forbidden_tools": ["deploy"],
        "channel_requirements": ["whatsapp_device_required"],
        "runtime_limitations": [
            "markdown_not_rendered_on_whatsapp",
            "one_wa_number_per_agent",
            "wa_device_scan_required_before_use",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, asisten WhatsApp untuk layanan berbayar dari {business}.\n\n"
            "STATE WAJIB:\n"
            "1. intake: sambut user, jelaskan proses singkat, kumpulkan data kebutuhan layanan/order.\n"
            "2. waiting_payment: setelah data cukup, minta user transfer biaya jasa dan kirim bukti transfer.\n"
            "3. payment_review: saat bukti transfer diterima, panggil escalate_to_human(reason, summary) untuk approval admin. "
            "JANGAN lanjut fulfillment atau mengirim hasil final sebelum admin approve.\n"
            "4. approved: setelah operator/admin approve, lanjutkan fulfillment layanan.\n"
            "5. delivery: kirim hasil layanan ke customer. Jika hasilnya file/PDF/DOCX, buat lewat subagent yang punya sandbox lalu kirim via send_whatsapp_document.\n"
            "6. aftercare: bantu revisi ringan sesuai kebijakan bisnis.\n\n"
            "ATURAN KERAS:\n"
            "- Jangan klaim hasil layanan sudah selesai jika belum ada output nyata dari tool/subagent/proses bisnis.\n"
            "- Jangan klaim file sudah terkirim sebelum send_whatsapp_document sukses atau sub-agent melaporkan TERKIRIM.\n"
            "- Jangan menyarankan user download manual jika delivery WhatsApp media aktif; kirim langsung via WhatsApp.\n"
            "- Jika pembayaran belum approved, berhenti di payment_review dan tunggu admin.\n\n"
            "CONTOH UNTUK JASA CV ATS:\n"
            "- Kumpulkan nama, kontak, posisi target, pengalaman kerja, pendidikan, skill, proyek, sertifikasi, link portfolio/LinkedIn, dan preferensi bahasa.\n"
            "- Jika user punya CV lama/dokumen referensi, gunakan itu untuk mengurangi pertanyaan.\n"
            "- Boleh riset posisi/keyword ATS sebagai pendukung, tetapi fulfillment CV tetap mengikuti state pembayaran dan approval.\n\n"
            "CONTOH PERCAKAPAN:\n"
            "User: Mau pesan layanan ini\n"
            "{name}: Bisa. Saya bantu dari pengumpulan kebutuhan sampai hasil final. Kebutuhan utamanya apa dulu?\n"
            "User: Saya sudah transfer, ini buktinya\n"
            "{name}: Terima kasih, saya teruskan bukti transfernya ke admin dulu untuk dicek. Hasil final baru saya kirim setelah pembayaran disetujui.\n\n"
            "INFO BISNIS:\n"
            "{business_info}"
        ),
        "smoke_test": {
            "strategy": "manual_wa",
            "steps": [
                "Kirim: 'Mau pesan jasa ini'",
                "Pastikan agent masuk intake dan tanya kebutuhan layanan/order, bukan langsung mengarang hasil.",
                "Kirim bukti transfer.",
                "Pastikan agent memanggil escalate_to_human sebelum menjanjikan delivery.",
                "Setelah operator approve, pastikan hasil layanan dikirim. Untuk hasil file, pastikan memakai send_whatsapp_document.",
            ],
            "expected_status": "Agent mengikuti state payment approval dan delivery setelah admin approve.",
            "known_failure_modes": [
                "escalation=false → admin tidak menerima bukti transfer",
                "subagents/whatsapp_media=false pada workflow file → file final tidak bisa dibuat/dikirim langsung",
            ],
        },
    },
    "faq_webchat_rag": {
        "label": "FAQ & RAG WhatsApp Agent",
        "description": "Agent yang menjawab pertanyaan berdasarkan dokumen yang diupload (PDF, DOCX). Cocok untuk FAQ produk, kebijakan, manual.",
        "default_model": "openai/gpt-4.1-mini",
        "default_temperature": 0.3,
        "default_max_tokens": 1024,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": True,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": False,
            "rag": True,
            "http": False,
            "whatsapp_media": False,
            "subagents": {"enabled": False},
        },
        "required_tools": ["rag"],
        "forbidden_tools": ["sandbox", "deploy"],
        "channel_requirements": ["documents_must_be_uploaded_via_api"],
        "runtime_limitations": [
            "rag_requires_documents_uploaded_first",
            "rag_uses_vector_similarity_not_full_text",
            "max_doc_size_depends_on_embedding_service",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, asisten FAQ dari {business}.\n\n"
            "TUGASMU:\n"
            "- Jawab pertanyaan user berdasarkan dokumen yang tersedia\n"
            "- Gunakan tool search_documents untuk mencari jawaban dari dokumen\n"
            "- Jika tidak ada informasi di dokumen, katakan terus terang dan tawarkan eskalasi\n\n"
            "CARA BICARA:\n"
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan\n"
            "Panjang pesan: 1-3 kalimat, langsung ke inti\n"
            "JANGAN mengada-ada jawaban yang tidak ada di dokumen\n\n"
            "ESKALASI:\n"
            "Jika pertanyaan tidak ada di dokumen atau butuh keputusan manusia:\n"
            "Panggil escalate_to_human(reason, summary) lalu beritahu user\n\n"
            "CONTOH PERCAKAPAN:\n"
            "User: Berapa biaya pengiriman ke Bandung?\n"
            "{name}: Berdasarkan informasi yang ada, biaya pengiriman ke Bandung adalah Rp 15.000 untuk berat di bawah 1 kg."
        ),
        "smoke_test": {
            "strategy": "manual_with_docs",
            "steps": [
                "Upload minimal satu dokumen via POST /v1/agents/{id}/documents/upload",
                "Kirim pertanyaan yang jawabannya ada di dokumen",
                "Pastikan agent merespons dengan informasi yang relevan dari dokumen",
                "Kirim pertanyaan yang jawabannya TIDAK ada",
                "Pastikan agent tidak mengada-ada dan menawarkan eskalasi",
            ],
            "expected_status": "Jawaban akurat dari dokumen; jujur saat tidak tahu",
            "known_failure_modes": [
                "Tidak ada dokumen diupload → search_documents tidak menemukan apapun",
                "Dokumen format tidak didukung → embedding gagal",
            ],
        },
    },
    "scheduler_assistant": {
        "label": "Scheduler & Reminder Assistant",
        "description": "Asisten pribadi yang bisa set reminder, jadwal, dan pengingat otomatis.",
        "default_model": "openai/gpt-4.1-mini",
        "default_temperature": 0.7,
        "default_max_tokens": 512,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": False,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": True,
            "rag": False,
            "http": False,
            "whatsapp_media": False,
            "subagents": {"enabled": False},
        },
        "required_tools": ["scheduler"],
        "forbidden_tools": ["sandbox", "deploy"],
        "channel_requirements": [],
        "runtime_limitations": [
            "scheduler_requires_apscheduler_running",
            "reminders_lost_on_server_restart_without_db_backed_scheduler",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, asisten jadwal dan pengingat pribadi.\n\n"
            "TUGASMU:\n"
            "- Set reminder dan pengingat sesuai permintaan user\n"
            "- Catat jadwal penting ke memory\n"
            "- Ingatkan user saat waktunya tiba\n\n"
            "CARA BICARA:\n"
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya santai.\n"
            "Konfirmasi selalu setelah set reminder: waktu, pesan, dan timezone\n\n"
            "CONTOH PERCAKAPAN:\n"
            "User: Ingatkan saya rapat jam 3 sore besok\n"
            "{name}: Oke, saya set reminder untuk rapat besok jam 15:00. Mau saya tambahkan catatan apapun?"
        ),
        "smoke_test": {
            "strategy": "manual",
            "steps": [
                "Kirim: 'Ingatkan saya minum obat 5 menit dari sekarang'",
                "Pastikan agent memanggil set_reminder dan konfirmasi waktu",
                "Tunggu 5 menit — pastikan reminder terkirim",
                "Kirim: 'batalkan reminder tadi'",
                "Pastikan agent memanggil cancel_reminder",
            ],
            "expected_status": "Reminder terset dan terkirim tepat waktu",
            "known_failure_modes": [
                "APScheduler tidak running → reminder tidak terkirim",
                "Timezone mismatch → reminder tiba di waktu salah",
            ],
        },
    },
    "social_media_agent": {
        "label": "Social Media Specialist Agent",
        "description": "Agent spesialis konten media sosial — riset tren, buat content planner, generate file PDF/Excel, dan kirim langsung ke WhatsApp.",
        "default_model": "openai/gpt-4.1-mini",
        "default_temperature": 0.7,
        "default_max_tokens": 2048,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": False,
            "sandbox": True,
            "deploy": False,
            "tool_creator": False,
            "scheduler": True,
            "rag": False,
            "http": True,
            "whatsapp_media": True,
            "subagents": {"enabled": True},
        },
        "required_tools": ["sandbox", "subagents", "whatsapp_media", "http"],
        "forbidden_tools": ["deploy"],
        "channel_requirements": [],
        "runtime_limitations": [
            "no_direct_social_media_posting",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, {role} yang membantu {business} dengan strategi dan konten media sosial.\n\n"
            "KEMAMPUAN UTAMA:\n"
            "- Riset tren media sosial dan topik relevan industri via HTTP\n"
            "- Buat content planner mingguan/bulanan\n"
            "- Generate file PDF atau Excel dengan sys_coder, lalu parent agent mengirim file ke user\n"
            "- Buat draft caption, hashtag, dan ide visual konten\n\n"
            "CARA GENERATE DAN KIRIM FILE (WAJIB IKUTI):\n"
            "Saat user minta file (PDF, Excel, gambar):\n"
            "1. Riset dulu jika perlu (http_get)\n"
            "2. Delegate ke sys_coder: task('sys_coder', task='Buat file [format] berisi [konten]. "
            "Simpan file final ke /workspace/shared/[filename]. Output akhir wajib menyebut path /workspace/shared/[filename] "
            "dan status SIAP_DIKIRIM_PARENT. Jangan kirim WhatsApp dari sub-agent.')\n"
            "3. Setelah task() return path shared, parent agent wajib panggil send_whatsapp_document/send_whatsapp_image sendiri.\n"
            "4. Setelah tool parent sukses, relay hasil ke user — jangan bilang 'file perlu didownload manual'\n\n"
            "CARA BICARA:\n"
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya energik dan kreatif.\n"
            "JANGAN pakai markdown saat di WhatsApp\n\n"
            "INFORMASI BISNIS:\n"
            "{business_info}"
        ),
        "smoke_test": {
            "strategy": "manual_wa",
            "steps": [
                "Kirim: 'Buat content planner 1 minggu dalam bentuk PDF'",
                "Pastikan agent riset dulu lalu delegate ke sys_coder",
                "Pastikan file PDF dikirim langsung via WhatsApp (bukan 'didownload manual')",
            ],
            "expected_status": "File PDF dikirim ke WhatsApp user",
            "known_failure_modes": [
                "whatsapp_media: false → send_whatsapp_document tidak tersedia",
                "subagents: false → sys_coder tidak bisa dipanggil",
            ],
        },
    },
    "data_analyst_agent": {
        "label": "Data Analyst Agent",
        "description": "Agent analisis data — upload file Excel/CSV, dapatkan insight, grafik, dan laporan langsung di WhatsApp.",
        "default_model": "openai/gpt-4.1-mini",
        "default_temperature": 0.3,
        "default_max_tokens": 2048,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": False,
            "sandbox": True,
            "deploy": False,
            "tool_creator": False,
            "scheduler": False,
            "rag": False,
            "http": False,
            "whatsapp_media": True,
            "subagents": {"enabled": True},
        },
        "required_tools": ["sandbox", "subagents", "whatsapp_media"],
        "forbidden_tools": ["deploy"],
        "channel_requirements": [],
        "runtime_limitations": [
            "no_persistent_storage_across_sessions",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, analis data untuk {business}.\n\n"
            "TUGASMU:\n"
            "- Terima file data (Excel, CSV) dari user\n"
            "- Analisis data: statistik, tren, anomali\n"
            "- Generate grafik dan laporan, kirim hasilnya ke user\n\n"
            "CARA ANALISIS DAN KIRIM HASIL (WAJIB):\n"
            "Saat user kirim data atau minta analisis:\n"
            "1. Terima file dan simpan di workspace\n"
            "2. Delegate ke sys_coder: task('sys_coder', task='Analisis file [nama] dari upload WhatsApp di "
            "/workspace/data/incoming/[nama] (alias parent: /workspace/shared/[nama]). "
            "Buat grafik dan laporan ringkas. Simpan file final ke /workspace/shared/[filename]. "
            "Output akhir wajib menyebut path /workspace/shared/[filename] dan status SIAP_DIKIRIM_PARENT. "
            "Jangan kirim WhatsApp dari sub-agent.')\n"
            "3. Setelah task() return path shared, parent agent wajib panggil send_whatsapp_document/send_whatsapp_image sendiri.\n"
            "4. Relay insight ke user dalam bahasa sederhana\n\n"
            "CARA BICARA:\n"
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya jelas dan berbasis data.\n"
            "Selalu sertakan angka dan fakta dalam jawaban\n\n"
            "KONTEKS BISNIS:\n"
            "{business_info}"
        ),
        "smoke_test": {
            "strategy": "manual",
            "steps": [
                "Kirim file CSV sederhana",
                "Minta: 'Analisis data ini dan buat grafiknya'",
                "Pastikan agent delegate ke sys_coder, lalu parent agent mengirim hasil grafik",
            ],
            "expected_status": "Grafik atau laporan terkirim",
            "known_failure_modes": [
                "File biner rusak → pandas gagal baca",
            ],
        },
    },
    "research_agent": {
        "label": "Research & Intelligence Agent",
        "description": "Agent riset mendalam — browsing internet, kumpulkan data, susun laporan terstruktur.",
        "default_model": "openai/gpt-4.1-mini",
        "default_temperature": 0.4,
        "default_max_tokens": 2048,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": False,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": False,
            "rag": True,
            "http": True,
            "whatsapp_media": False,
            "subagents": {"enabled": True},
        },
        "required_tools": ["http", "subagents"],
        "forbidden_tools": ["deploy"],
        "channel_requirements": [],
        "runtime_limitations": [
            "http_depends_on_target_api_availability",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, agen riset dan intelijen untuk {business}.\n\n"
            "TUGASMU:\n"
            "- Lakukan riset mendalam dari berbagai sumber online\n"
            "- Kumpulkan, verifikasi, dan sintesis informasi\n"
            "- Sajikan laporan yang terstruktur dan actionable\n\n"
            "CARA RISET:\n"
            "- Gunakan http_get untuk akses URL, API, dan sumber data\n"
            "- Untuk riset paralel yang kompleks, delegate ke sys_researcher via task()\n"
            "- Selalu cite sumber informasi\n\n"
            "CARA MENYERAHKAN HASIL:\n"
            "- Default: jawab hasil riset langsung di chat dengan ringkasan, insight, rekomendasi, dan sumber\n"
            "- Simpan ringkasan penting ke memory dengan remember/update_longterm jika tool memory tersedia\n"
            "- Jangan membuat file laporan dengan write_file kecuali user eksplisit minta file/export atau laporan sangat panjang\n"
            "- Jika file laporan sudah dibuat, jangan tulis ulang path yang sama; gunakan read_file + edit_file atau beri jawaban final\n\n"
            "CARA BICARA:\n"
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan\n"
            "Format output: terstruktur dengan poin-poin jelas\n"
            "Jujur jika informasi tidak tersedia atau tidak bisa diverifikasi\n\n"
            "KONTEKS:\n"
            "{business_info}"
        ),
        "smoke_test": {
            "strategy": "manual",
            "steps": [
                "Minta: 'Riset 3 kompetitor terbesar di industri [X]'",
                "Pastikan agent menggunakan http_get atau delegate ke sys_researcher",
                "Pastikan output terstruktur dengan sumber",
            ],
            "expected_status": "Laporan riset terstruktur dengan sumber",
            "known_failure_modes": [
                "Target URL blokir scraping → hasil kosong",
            ],
        },
    },
    "ecommerce_cs": {
        "label": "E-Commerce Customer Service",
        "description": "CS agent khusus e-commerce — handle pertanyaan produk, status pesanan, komplain, dan retur via WhatsApp.",
        "default_model": "openai/gpt-4.1-mini",
        "default_temperature": 0.7,
        "default_max_tokens": 800,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": True,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": False,
            "rag": True,
            "http": True,
            "whatsapp_media": True,
            "subagents": {"enabled": False},
        },
        "required_tools": ["escalation", "whatsapp_media"],
        "forbidden_tools": ["deploy"],
        "channel_requirements": ["whatsapp_device_required"],
        "runtime_limitations": [
            "markdown_not_rendered_on_whatsapp",
            "one_wa_number_per_agent",
            "wa_device_scan_required_before_use",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, CS online shop {business}.\n\n"
            "TUGASMU:\n"
            "- Jawab pertanyaan produk: stok, harga, spesifikasi\n"
            "- Bantu cek status pesanan (jika ada API order)\n"
            "- Handle komplain dengan empati\n"
            "- Proses permintaan retur/refund sesuai policy\n"
            "- Eskalasi ke operator untuk kasus kompleks\n\n"
            "POLICY TOKO:\n"
            "{business_info}\n\n"
            "CARA BICARA:\n"
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya ramah dan sabar.\n"
            "Singkat — maks 3 kalimat per pesan\n"
            "JANGAN pakai *, #, atau markdown\n"
            "Panggil customer dengan nama jika sudah tahu\n\n"
            "ESKALASI:\n"
            "Wajib eskalasi untuk: komplain keras, refund > Rp 500rb, ancaman hukum\n"
            "Cara: panggil escalate_to_human(reason, summary) dulu BARU balas user"
        ),
        "smoke_test": {
            "strategy": "manual_wa",
            "steps": [
                "Kirim: 'Halo, produk X masih ada?'",
                "Pastikan agent jawab sesuai info bisnis",
                "Kirim: 'Pesanan saya belum sampai sudah 2 minggu, mau komplain!'",
                "Pastikan agent eskalasi ke operator",
            ],
            "expected_status": "Jawaban sesuai policy, eskalasi berjalan",
            "known_failure_modes": [
                "escalation_config kosong → operator tidak notif",
            ],
        },
    },
    "personal_assistant": {
        "label": "Personal Assistant Agent",
        "description": "Asisten pribadi all-in-one — jadwal, reminder, riset cepat, dan pengingat via WhatsApp.",
        "default_model": "openai/gpt-4.1-mini",
        "default_temperature": 0.7,
        "default_max_tokens": 1024,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": False,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": True,
            "rag": False,
            "http": True,
            "whatsapp_media": False,
            "subagents": {"enabled": False},
        },
        "required_tools": ["scheduler", "memory"],
        "forbidden_tools": ["sandbox", "deploy"],
        "channel_requirements": [],
        "runtime_limitations": [
            "scheduler_requires_apscheduler_running",
            "markdown_not_rendered_on_whatsapp",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, asisten pribadi {business}.\n\n"
            "TUGASMU:\n"
            "- Kelola jadwal dan pengingat\n"
            "- Catat hal penting ke memory\n"
            "- Bantu riset cepat via internet jika diminta\n"
            "- Ingatkan deadline, meeting, tugas penting\n\n"
            "CARA BICARA:\n"
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya santai seperti asisten pribadi.\n"
            "Proaktif — jika user bilang ada meeting besok, tawarkan set reminder\n"
            "JANGAN pakai markdown\n\n"
            "CONTOH:\n"
            "User: Ada rapat investor Jumat jam 10\n"
            "{name}: Oke, saya catat rapat investor Jumat jam 10. Mau saya ingatkan H-1 atau pagi harinya?"
        ),
        "smoke_test": {
            "strategy": "manual",
            "steps": [
                "Kirim: 'Ingatkan saya beli kado ulang tahun istri 3 hari lagi'",
                "Pastikan agent set reminder dan konfirmasi",
                "Kirim: 'Apa jadwal saya minggu ini?'",
                "Pastikan agent recall dari memory",
            ],
            "expected_status": "Reminder terset, memory terisi",
            "known_failure_modes": [
                "APScheduler tidak running → reminder tidak terkirim",
            ],
        },
    },
    "hr_assistant": {
        "label": "HR & Internal Knowledge Assistant",
        "description": "Asisten HR internal — jawab pertanyaan kebijakan perusahaan, cuti, benefit, dan onboarding dari dokumen.",
        "default_model": "openai/gpt-4.1-mini",
        "default_temperature": 0.3,
        "default_max_tokens": 1024,
        "default_channel": "whatsapp",
        "tools_config": {
            "memory": True,
            "skills": True,
            "escalation": True,
            "sandbox": False,
            "deploy": False,
            "tool_creator": False,
            "scheduler": False,
            "rag": True,
            "http": False,
            "whatsapp_media": False,
            "subagents": {"enabled": False},
        },
        "required_tools": ["rag", "escalation"],
        "forbidden_tools": ["sandbox", "deploy"],
        "channel_requirements": ["documents_must_be_uploaded_via_api"],
        "runtime_limitations": [
            "rag_requires_documents_uploaded_first",
            "rag_uses_vector_similarity_not_full_text",
        ],
        "instruction_skeleton": (
            "Kamu adalah {name}, asisten HR digital untuk {business}.\n\n"
            "TUGASMU:\n"
            "- Jawab pertanyaan karyawan tentang kebijakan perusahaan\n"
            "- Informasi cuti, benefit, prosedur HR dari dokumen\n"
            "- Bantu onboarding karyawan baru\n"
            "- Eskalasi ke HR manusia untuk kasus sensitif\n\n"
            "CARA MENJAWAB:\n"
            "- Selalu cari dulu di dokumen via search_documents\n"
            "- Jika tidak ada di dokumen, jujur dan tawarkan eskalasi\n"
            "- JANGAN mengarang kebijakan yang tidak ada di dokumen\n\n"
            "CARA BICARA:\n"
            "Bahasa: ikuti bahasa user; default Indonesia jika user tidak menentukan. Gaya profesional tapi ramah.\n"
            "Sertakan referensi dokumen saat menjawab\n\n"
            "ESKALASI:\n"
            "Untuk: terminasi, konflik antar karyawan, masalah payroll → eskalasi ke HR"
        ),
        "smoke_test": {
            "strategy": "manual_with_docs",
            "steps": [
                "Upload policy dokumen (PDF/DOCX)",
                "Tanya: 'Berapa hari jatah cuti tahunan?'",
                "Pastikan agent menjawab berdasarkan dokumen",
                "Tanya sesuatu yang tidak ada di dokumen",
                "Pastikan agent jujur dan tawarkan eskalasi",
            ],
            "expected_status": "Jawaban akurat dari dokumen, jujur saat tidak tahu",
            "known_failure_modes": [
                "Dokumen belum diupload → jawaban tidak akurat",
            ],
        },
    },
}

for _preset in AGENT_PRESETS.values():
    _preset_tools_config = _preset.get("tools_config")
    if isinstance(_preset_tools_config, dict):
        _preset_tools_config.setdefault("tavily", True)

# ---------------------------------------------------------------------------
# Known runtime limitations — machine-readable
# ---------------------------------------------------------------------------

RUNTIME_LIMITATIONS: dict[str, dict] = {
    "deploy_requires_docker_socket": {
        "severity": "critical",
        "affects": ["coding_deploy_agent"],
        "description": "deploy_app() membutuhkan Docker socket tersedia di server. Tanpa ini, deploy akan gagal.",
        "mitigation": "Pastikan platform berjalan dengan Docker socket di-mount (/var/run/docker.sock).",
        "user_message": "Deploy ke public URL membutuhkan Docker — pastikan environment sudah dikonfigurasi.",
    },
    "cloudflare_tunnel_url_changes_on_redeploy": {
        "severity": "warning",
        "affects": ["coding_deploy_agent"],
        "description": "Setiap kali deploy_app() dipanggil, URL Cloudflare Quick Tunnel akan berubah.",
        "mitigation": "Instruksikan agent untuk cek get_deployment_status() sebelum deploy ulang.",
        "user_message": "URL akan berubah setiap kali deploy ulang — tidak bisa pakai URL permanen dengan Cloudflare Quick Tunnel.",
    },
    "deploy_ttl_24h_max": {
        "severity": "info",
        "affects": ["coding_deploy_agent"],
        "description": "Deployment otomatis dihapus setelah 24 jam (configurable via DEPLOYMENT_TTL_SECONDS).",
        "mitigation": "Gunakan untuk demo/testing, bukan production long-running apps.",
        "user_message": "App yang di-deploy otomatis berhenti setelah ~24 jam.",
    },
    "no_persistent_storage_across_sessions": {
        "severity": "info",
        "affects": ["coding_deploy_agent"],
        "description": "Workspace file persisten dalam satu sesi, tapi berbeda sesi = workspace baru.",
        "mitigation": "Sampaikan ke user bahwa file tidak permanen antar sesi.",
        "user_message": "File di workspace tidak permanen — hilang saat sesi baru dimulai.",
    },
    "markdown_not_rendered_on_whatsapp": {
        "severity": "warning",
        "affects": ["cs_whatsapp_basic", "scheduler_assistant"],
        "description": "WhatsApp tidak merender markdown standar (**, #, `) — tampil sebagai karakter literal.",
        "mitigation": "Instruksikan agent untuk tidak menggunakan markdown di instructions.",
        "user_message": "Simbol markdown seperti * dan # tidak akan terformat di WhatsApp.",
    },
    "no_broadcast_capability": {
        "severity": "info",
        "affects": ["cs_whatsapp_basic"],
        "description": "Platform tidak mendukung broadcast ke banyak nomor sekaligus.",
        "mitigation": "Gunakan untuk 1-on-1 conversation saja.",
        "user_message": "Agent tidak bisa kirim pesan ke banyak nomor sekaligus.",
    },
    "one_wa_number_per_agent": {
        "severity": "info",
        "affects": ["cs_whatsapp_basic", "scheduler_assistant"],
        "description": "Satu nomor WhatsApp hanya bisa dipakai oleh satu agent.",
        "mitigation": "Gunakan device/nomor yang berbeda untuk setiap agent WA.",
        "user_message": "Satu nomor WA hanya bisa digunakan oleh satu agent.",
    },
    "wa_device_scan_required_before_use": {
        "severity": "info",
        "affects": ["cs_whatsapp_basic", "scheduler_assistant"],
        "description": "User harus scan QR untuk menghubungkan nomor WA ke agent sebelum bisa dipakai.",
        "mitigation": "Gunakan send_agent_wa_qr() setelah create untuk kirim QR ke user.",
        "user_message": "Perlu scan QR WhatsApp sebelum agent bisa digunakan.",
    },
    "rag_requires_documents_uploaded_first": {
        "severity": "warning",
        "affects": ["faq_webchat_rag"],
        "description": "RAG search tidak akan menemukan apapun jika tidak ada dokumen yang diupload dulu.",
        "mitigation": "Setelah create, minta user upload dokumen via /v1/agents/{id}/documents/upload.",
        "user_message": "Dokumen harus diupload terlebih dahulu sebelum agent bisa menjawab dari RAG.",
    },
    "rag_uses_vector_similarity_not_full_text": {
        "severity": "info",
        "affects": ["faq_webchat_rag"],
        "description": "RAG menggunakan sentence embeddings — pertanyaan yang sangat berbeda phrasing dari dokumen mungkin tidak ditemukan.",
        "mitigation": "Instruksikan user untuk upload dokumen dengan bahasa yang mirip pertanyaan yang akan ditanyakan.",
        "user_message": "RAG mencari berdasarkan makna, bukan kata kunci persis — hasil bisa bervariasi.",
    },
    "scheduler_requires_apscheduler_running": {
        "severity": "critical",
        "affects": ["scheduler_assistant"],
        "description": "Reminder hanya berjalan jika APScheduler aktif di backend — tidak ada jika API di-restart tanpa scheduler init.",
        "mitigation": "Pastikan backend sudah running dan scheduler service aktif.",
        "user_message": "Reminder membutuhkan server berjalan — tidak akan terkirim jika server mati.",
    },
}

_TOOLS_CONFIG_DOCS = {
    "memory": "Ingat fakta tentang user lintas sesi (remember/recall/forget). Default ON.",
    "skills": "Library skill/template prompt yang bisa dipakai ulang. Default ON.",
    "escalation": "Eskalasi ke operator manusia. Default ON. Wajib untuk agent WhatsApp.",
    "sandbox": "Eksekusi kode Python di Docker, workspace file persisten per sesi. Default OFF.",
    "deploy": "Deploy app dari sandbox ke public URL via Cloudflare tunnel — otomatis aktif jika sandbox: true. Agent bisa kasih link yang bisa diakses siapapun.",
    "tool_creator": "Buat Python tool baru secara dinamis (butuh sandbox aktif). Default OFF.",
    "scheduler": "Set reminder, cron job, tugas terjadwal. Default OFF.",
    "rag": "Cari jawaban dari dokumen yang diupload. Default OFF.",
    "http": "HTTP GET/POST/PATCH/DELETE ke API eksternal. Default OFF.",
    "tavily": "Web browsing/search via Tavily (tavily_search/tavily_extract). Default ON jika TAVILY_API_KEY tersedia.",
    "mcp": "Integrasi eksternal tingkat lanjut seperti Google Workspace/Docs/Sheets/Drive. Default OFF.",
    "whatsapp_media": "Kirim gambar dan dokumen via WhatsApp. Default OFF. Aktifkan untuk agent WA.",
    "wa_agent_manager": "Kelola WA device/QR agent lain. Default OFF. Khusus meta-agent.",
    "subagents": (
        "Delegasi ke sub-agent spesialis via tool task(). "
        "WAJIB aktif (subagents: {enabled: true}) untuk semua coding/deploy agent — "
        "sys_coder menangani eksekusi sandbox dan deploy ke public URL, main agent jadi orchestrator. "
        "Sub-agent yang tersedia: "
        "sys_coder (programmer full-stack: tulis kode Python/JS/HTML, jalankan di sandbox Docker, deploy website ke Cloudflare public URL — kembalikan URL ke user), "
        "sys_researcher (riset internet via HTTP), "
        "sys_writer (tulis/edit konten), "
        "sys_analyst (analisis data dengan pandas/numpy). "
        "Jika agent HANYA butuh coding/deploy: aktifkan sandbox+deploy+subagents. "
        "Jika butuh coding PLUS riset/analisis/tulis: sama, subagents sudah include semua."
    ),
}

_PLATFORM_CHANNELS = [
    {
        "type": "whatsapp",
        "description": (
            "Channel user-facing yang tersedia. Bisa dicoba lewat nomor demo Arthur "
            "atau dipasang ke nomor WhatsApp user dengan scan sekali dari WhatsApp."
        ),
    },
]

_RECOMMENDED_MODELS = [
    {"model": "openai/gpt-4.1-mini", "use_case": "Budget default — cukup kuat untuk mayoritas agent, lebih hemat"},
    {"model": "openai/gpt-4.1", "use_case": "Balance cost & quality (generasi sebelumnya)"},
    {"model": "openai/gpt-4.1-nano", "use_case": "Ultra-fast response"},
    {"model": "anthropic/claude-sonnet-4-6", "use_case": "Reasoning kompleks, nuanced"},
    {"model": "openai/gpt-4o", "use_case": "Analisis gambar/dokumen (vision)"},
]

_DEFAULT_MODEL = "openai/gpt-4.1-mini"
