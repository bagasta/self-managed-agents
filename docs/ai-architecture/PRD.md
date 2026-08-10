# PRD - Managed Agent Platform

Tanggal snapshot: 2026-07-02

## Project Overview
Managed Agent Platform adalah backend self-hosted untuk membuat, mengonfigurasi, menjalankan, dan mengelola AI agent multi-channel. Fokus produk saat ini adalah agent WhatsApp yang dapat dibuat secara config-driven oleh Arthur/builder agent, dilengkapi memory, RAG, sub-agent, sandbox Docker, scheduler, human escalation, dan integrasi Google Workspace lewat MCP.

## Problem Statement
Pemilik bisnis kecil dan tim operasional butuh AI agent yang bisa langsung bekerja di WhatsApp tanpa harus menulis kode. Tantangan utamanya adalah membuat agent yang sesuai SOP bisnis, aman saat berinteraksi dengan customer, bisa eskalasi ke manusia, dan bisa diperbaiki atau diperluas tanpa deploy kode baru.

## Business Goals
- Memungkinkan user membuat agent bisnis lewat percakapan dengan Arthur.
- Menyediakan trial number WhatsApp untuk validasi cepat sebelum user menghubungkan nomor sendiri.
- Menjual akses berbasis subscription, token quota, dan fitur premium seperti sub-agent, file generation, Google Workspace, dan deployment.
- Menurunkan beban manual customer service, admin order, reminder, dan pekerjaan operasional berulang.
- Menjaga biaya LLM dan sandbox tetap terkendali lewat quota, rate limit, dan resource cap.

## User Personas
- Owner bisnis: ingin agent WhatsApp yang bisa menjawab customer, intake order, follow-up, dan eskalasi.
- Operator/admin: menerima eskalasi, approval, atau instruksi kirim pesan langsung ke customer.
- Developer/internal operator: menjaga deployment, database, WA service, MCP, dan observability.
- AI coding agent: membaca codebase dan dokumen ini untuk implementasi fitur atau perbaikan.

## User Journey
1. User menghubungi Arthur lewat WhatsApp atau API.
2. Arthur mengidentifikasi kebutuhan, paket, owner identity, channel, dan SOP dasar.
3. Arthur membuat blueprint, operating manual, instructions, dan agent record.
4. User mencoba agent lewat shared `wa-dev-service` trial number atau menghubungkan WhatsApp dedicated via QR.
5. Customer mengirim pesan ke agent.
6. Agent menjalankan runtime: prompt context, memory, tools, MCP, RAG, scheduler, escalation.
7. Jawaban dikirim balik lewat channel atau API response.
8. Owner memperbaiki instruksi, SOP, memory, tools, atau channel berdasarkan hasil test.

## Functional Requirements
- CRUD agent dengan model, instructions, tools_config, safety_policy, quota, owner, channel, dan WhatsApp device.
- Session dan message execution per agent menggunakan `X-Agent-Key`.
- Memory jangka panjang per agent dan scope user.
- Skill library per agent.
- Custom tool creation dan execution di sandbox.
- RAG upload/search untuk TXT, MD, PDF, DOCX, PPTX.
- WhatsApp production service satu device per agent.
- WA dev service satu shared trial number untuk multi-agent demo.
- Scheduler/reminder dan heartbeat proactive.
- Human escalation dengan operator flow.
- Sub-agent delegation untuk researcher, coder, writer, analyst, atau agent custom.
- Google Workspace MCP integration dengan OAuth/re-auth flow.
- Subscription, user, plan, token quota, top-up, dan renewal.
- Observability dasar: health, detailed health, metrics, structured logs, Sentry optional.

## Non-Functional Requirements
- Runtime async berbasis FastAPI dan SQLAlchemy async.
- Sandbox harus punya resource cap dan cleanup orphan.
- Semua operasi customer-visible harus punya guard terhadap false success claim.
- Semua external integration failure harus disampaikan sebagai blocker, bukan disamarkan sebagai sukses.
- Data multi-tenant harus diisolasi lewat owner, session scope, API key, dan agent key.
- Deployment production berjalan di Docker Compose dengan Redis, WA services, pgbouncer, dan Traefik.

## Success Metrics
- Time-to-first-agent dari chat Arthur ke agent siap diuji.
- Agent activation rate dari trial ke nomor dedicated.
- Escalation completion rate dan operator response time.
- Token cost per active customer.
- Persentase run sukses tanpa recovery/fallback.
- WA inbound-to-reply latency.
- Jumlah incident security/runtime per bulan.
- Test suite release gate hijau untuk path kritis.

## Out of Scope
- Frontend production penuh selain UI dev/dashboard eksperimen.
- Payment gateway otomatis end-to-end.
- Multi-cloud orchestration.
- Fully isolated enterprise tenant deployment.
- Support channel non-WhatsApp sebagai channel customer utama.

## Assumptions
- OpenRouter menjadi gateway utama LLM.
- PostgreSQL menjadi source of truth.
- WhatsApp Web lewat whatsmeow diterima untuk tahap saat ini.
- Google Workspace MCP dijalankan sebagai service terpisah.
- Arthur punya WhatsApp dedicated identity sendiri; `wa-dev-service` hanya shared trial number untuk agent user baru.

