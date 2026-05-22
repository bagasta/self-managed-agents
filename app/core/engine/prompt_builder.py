"""
prompt_builder.py — Membangun system prompt yang dikirim ke LLM.

Dipecah dari agent_runner.py (item 2.1 production plan).

Fungsi yang diekspor:
  build_agent_context_block(agent_model, session, active_groups, custom_tools_db, subagent_list, sender_name)
  build_rag_context(agent_id, user_message, db, tools_config, log)
  maybe_summarize_context(session, db, llm, log)
  build_system_prompt(...)   ← entry point untuk agent_runner
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.engine.context_service import count_user_messages, load_history
from app.core.utils.phone_utils import normalize_phone


# ---------------------------------------------------------------------------
# Tool priority hints
# ---------------------------------------------------------------------------

def build_mcp_tool_priority_notice(
    *,
    mcp_tool_names: list[str],
    sandbox_active: bool,
) -> str:
    """Build a compact prompt addendum so MCP-backed services win over sandbox."""
    visible_names = [name for name in mcp_tool_names if name][:40]
    tool_list = ", ".join(visible_names) if visible_names else "MCP tools"
    if len(mcp_tool_names) > len(visible_names):
        tool_list += f", ... (+{len(mcp_tool_names) - len(visible_names)} more)"

    sandbox_line = (
        "\n- Sandbox tetap boleh dipakai untuk olah file/kode lokal, tetapi hanya sebagai pendukung setelah data/aksi eksternal dilakukan via MCP."
        if sandbox_active
        else ""
    )

    return (
        "\n\n## MCP Tool Priority\n"
        f"MCP tools aktif: {tool_list}.\n"
        "Aturan wajib saat memilih tool:\n"
        "- Jika request user menyangkut layanan eksternal yang tersedia via MCP (Google Workspace, Gmail, Calendar, Drive, Docs, Sheets, Slides, Forms, atau service MCP lain), panggil tool MCP yang relevan sebagai sumber kebenaran.\n"
        "- Jangan memakai sandbox untuk mensimulasikan, membuat file lokal pengganti, scraping manual, atau menjawab normatif jika MCP tool tersedia untuk aksi tersebut.\n"
        "- Jika MCP membutuhkan auth, scope, atau sedang error, sampaikan blocker/auth flow yang benar; jangan diam-diam fallback ke sandbox seolah task berhasil."
        f"{sandbox_line}"
    )


# ---------------------------------------------------------------------------
# Agent Context Block
# ---------------------------------------------------------------------------

def build_agent_context_block(
    agent_model: Any,
    session: Any,
    active_groups: list[str],
    custom_tools_db: list,
    subagent_list: list | None = None,
    sender_name: str | None = None,
) -> str:
    """Bangun blok '## Platform Context' yang di-inject ke atas system prompt."""
    agent_id = session.agent_id
    _raw_cfg = session.channel_config
    _ch_cfg = _raw_cfg if isinstance(_raw_cfg, dict) else {}
    _raw_user_phone = _ch_cfg.get("user_phone") or getattr(session, "external_user_id", None) or ""
    # Strip @lid / @s.whatsapp.net — always expose clean phone number to LLM
    user_phone = normalize_phone(_raw_user_phone) if _raw_user_phone else ""
    channel_type = getattr(session, "channel_type", None) or "api"

    operator_ids: list = getattr(agent_model, "operator_ids", None) or []
    if isinstance(operator_ids, list) and user_phone:
        norm_user = normalize_phone(user_phone)
        is_operator = any(normalize_phone(oid) == norm_user for oid in operator_ids)
    else:
        is_operator = False
    user_role = "OPERATOR" if is_operator else "user"

    lines = [
        "## Platform Context",
        f"- Agent ID: {agent_id}",
        f"- Agent Name: {agent_model.name}",
        f"- Model: {agent_model.model}",
        f"- Active Tools: {', '.join(active_groups) if active_groups else 'none'}",
    ]

    if custom_tools_db:
        ct_lines = [f"  - {ct.name}: {ct.description}" for ct in custom_tools_db]
        lines.append("- Custom Tools:\n" + "\n".join(ct_lines))

    lines.append(f"- Channel: {channel_type}")

    escalation_cfg: dict = getattr(agent_model, "escalation_config", None) or {}
    operator_name: str = escalation_cfg.get("operator_name", "")
    operator_phone_cfg: str = escalation_cfg.get("operator_phone", "")

    if user_phone:
        lines.append(f"- Current User Phone: {user_phone}")
    if is_operator:
        # Operator session — show operator identity, NOT customer sender_name
        _op_label = operator_name or "Operator/Admin"
        lines.append(f"- Current User Name: {_op_label} (ini adalah OPERATOR, bukan customer)")
        lines.append(f"- Current User Role: OPERATOR")
        if operator_phone_cfg:
            lines.append(f"- Operator Phone: {operator_phone_cfg}")
        lines.append("- PENTING: Kamu sedang di-chat oleh OPERATOR. Jangan gunakan nama atau sapaan yang ditujukan ke customer.")
    else:
        if sender_name:
            lines.append(f"- Current User Name: {sender_name}")
        lines.append(f"- Current User Role: {user_role}")
        if operator_phone_cfg:
            lines.append(f"- Operator Phone (pemilik agent): {operator_phone_cfg}")
        if operator_name:
            lines.append(f"- Operator Name: {operator_name}")
    lines.append(f"- Session ID: {session.id}")


    if subagent_list:
        lines.append("\n## Available Subagents")
        lines.append(
            "Delegate specific tasks using `task(name=..., task=...)`. "
            "Always use write_todos to plan before delegating.\n"
            "DELEGATION RULES:\n"
            "- Web/coding/deploy tasks → delegate to sys_coder immediately, do NOT attempt yourself\n"
            "- Your final reply to user: relay the result from subagent, max 3 lines\n"
            "- Do NOT re-explain or expand what the subagent produced\n\n"
            "HASIL SUBAGENT ADALAH SUMBER KEBENARAN:\n"
            "- Jika task() return URL/deploy/file terkirim → sampaikan hasil itu.\n"
            "- Jika task() return blocker seperti butuh CV, file tidak ditemukan, atau minta info tambahan → sampaikan blocker itu apa adanya.\n"
            "- DILARANG bilang 'saya sudah dapat CV', 'nanti saya kirim', 'sedang saya buat', atau klaim sukses jika task() tidak mengembalikan URL/file terkirim.\n"
            "- Untuk pertanyaan status ('mana?', 'belum jadi?', 'udah jadi?'), jawab dari hasil task terakhir; jangan delegasikan ulang kecuali user eksplisit minta coba ulang.\n\n"
            "TASK CONTEXT — WAJIB disertakan di setiap `task=` string:\n"
            f"- Bahasa user: {'Bahasa Indonesia' if sender_name or user_phone else 'ikuti bahasa user'} — subagent HARUS balas dalam bahasa yang sama\n"
            + (f"- Nama user: {sender_name}\n" if sender_name else "")
            + (f"- User phone: {user_phone}\n" if user_phone else "")
            + "- Sertakan konteks singkat dari request user agar subagent tidak buta\n"
            "- Contoh BENAR: task('sys_coder', task='Buatkan landing page untuk user bernama Bagas (bahasa Indonesia). Request: buat portfolio sederhana dengan section About dan Projects.')\n"
            "- Contoh SALAH: task('sys_coder', task='buat portfolio')\n\n"
            "🚨 ATURAN PALING KRITIS — BACA BAIK-BAIK:\n"
            "Sistem ini bekerja seperti ini: OUTPUT TEKS PERTAMA = REPLY FINAL = TASK SELESAI.\n"
            "Tidak ada 'nanti lanjut'. Tidak ada 'sebentar lagi'. Sekali kamu tulis teks → task MATI di situ.\n\n"
            "CONTOH FATAL (JANGAN LAKUKAN):\n"
            "  ❌ Kamu tulis: 'Saya delegasikan ke sys_coder sekarang!' → task SELESAI, sys_coder TIDAK dipanggil\n"
            "  ❌ Kamu tulis: 'Tunggu bentar ya!' → task SELESAI, tidak ada yang dikerjakan\n"
            "  ❌ Kamu tulis: 'Oke lagi diproses...' → task SELESAI, user menunggu selamanya\n\n"
            "CARA BENAR — SATU-SATUNYA CARA:\n"
            "  ✅ LANGSUNG panggil task() atau tool lain TANPA menulis teks apapun dulu\n"
            "  ✅ Setelah SEMUA tool selesai → BARU tulis satu pesan final dengan hasilnya\n"
            "  ✅ Kalau mau update user → pakai notify_user() BUKAN teks biasa\n\n"
            "URUTAN WAJIB:\n"
            "  1. [tidak ada teks] → langsung panggil tool/task\n"
            "  2. [tool berjalan]\n"
            "  3. [setelah semua selesai] → tulis reply final\n\n"
            "🔒 LARANGAN VERIFIKASI HASIL SUB-AGENT (HARD RULE):\n"
            "Workspace dan deployment sub-agent TERPISAH dari workspace-mu. Tool ls/glob/read kamu\n"
            "HANYA melihat workspace-mu sendiri — folder src/, output/, dst di workspace-mu PASTI KOSONG\n"
            "walaupun sub-agent sukses bikin file. Ini bukan bug, ini isolasi by-design.\n\n"
            "DILARANG KERAS:\n"
            "  ❌ Panggil ls/glob/read setelah task() return untuk 'mengecek' apakah file sudah ada\n"
            "  ❌ Bilang 'subagent belum nulis filenya' karena ls() mu kosong\n"
            "  ❌ Re-delegate task() karena 'ngerasa' subagent gagal padahal task() sukses return\n\n"
            "GROUND TRUTH: string yang di-return tool task() = hasil definitif. Kalau ada URL di string\n"
            "itu, URL itu valid, langsung relay ke user. Kalau task() error/exception, baru bilang gagal.\n\n"
            "💾 INGAT URL HASIL DEPLOY (HARD RULE):\n"
            "Setiap kali sub-agent return URL deployment, LANGSUNG simpan ke memory:\n"
            "  remember(key='last_deploy_url', value='<url>')\n"
            "  remember(key='last_deploy_summary', value='<deskripsi singkat>')\n\n"
            "Sebelum delegate task() coding/web baru, WAJIB cek dulu:\n"
            "  recall('last_deploy_url') — kalau ada dan user cuma nanya status ('udah jadi?',\n"
            "  'mana webnya?', 'URL?'), JANGAN re-delegate. Langsung jawab pakai URL yang tersimpan.\n\n"
            "Re-delegate HANYA jika user EKSPLISIT minta:\n"
            "  - Edit/perubahan konten ('ganti warna', 'tambahin section X')\n"
            "  - Web/app baru yang beda total ('buatin landing page lain')\n"
            "  - User bilang URL lama mati / gak bisa diakses\n"
            "Untuk edit, instruksikan sub-agent MODIFY file yang ada, bukan rebuild from scratch.\n\n"
            "📦 PERCAYA LAPORAN PENGIRIMAN FILE DARI SUB-AGENT (HARD RULE):\n"
            "Sub-agent bisa kirim file (PDF, gambar, Excel, dll) LANGSUNG ke user tanpa routing ke kamu.\n"
            "Kalau output task() menyebut file sudah dikirim (misal: '✅ TERKIRIM', 'send_whatsapp_document berhasil',\n"
            "'[DOCUMENT_SENT]', '[IMAGE_SENT]') → FILE SUDAH SAMPAI KE USER. Jangan ragukan ini.\n\n"
            "DILARANG KERAS setelah sub-agent lapor file terkirim:\n"
            "  ❌ Tanya user 'udah nyampe?', 'bisa dibuka?', 'file-nya udah ada?'\n"
            "  ❌ Coba kirim ulang file yang sama\n"
            "  ❌ Bilang 'mungkin belum terkirim' atau 'sepertinya ada masalah pengiriman'\n\n"
            "YANG HARUS DILAKUKAN:\n"
            "  ✅ Langsung recap konten file ke user (apa isinya, apa yang bisa dilakukan selanjutnya)\n"
            "  ✅ Simpan ke memory: remember(key='last_file_sent', value='<nama_file> TERKIRIM')\n"
            "  ✅ Kalau output task() TIDAK menyebut pengiriman → BARU tanya atau kirim sendiri"
        )
        for sa in subagent_list:
            lines.append(f"- **{sa.get('name', '?')}**: {sa.get('description', '')}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# RAG context
# ---------------------------------------------------------------------------

async def build_rag_context(
    agent_id: uuid.UUID,
    user_message: str,
    db: AsyncSession,
    tools_config: dict[str, Any],
    log: Any,
) -> str:
    raw = tools_config.get("rag", {})
    cfg: dict[str, Any] = raw if isinstance(raw, dict) else {}
    max_results: int = int(cfg.get("max_results", 3))

    try:
        from app.core.domain.document_service import (
            search_documents_keyword,
            search_documents_vector,
        )
        from app.core.domain.embedding_service import embed_text

        query_embedding = await embed_text(user_message)
        docs = await search_documents_vector(agent_id, query_embedding, db, max_results)

        if not docs:
            docs = await search_documents_keyword(agent_id, user_message, db, max_results)

        if not docs:
            return ""

        parts: list[str] = []
        for i, doc in enumerate(docs, 1):
            src = f" — *{doc.source}*" if doc.source else ""
            excerpt = doc.content[:1200]
            if len(doc.content) > 1200:
                excerpt += "\n…"
            parts.append(f"**[{i}] {doc.title}**{src}\n{excerpt}")

        context_block = (
            "## Relevant Knowledge Base Context\n"
            "*The following documents were retrieved based on your query. "
            "Use them to inform your answer.*\n\n"
            + "\n\n---\n\n".join(parts)
        )
        log.debug("agent_run.rag_context", docs_found=len(docs))
        return context_block

    except Exception as exc:
        log.warning("agent_run.rag_context_failed", error=str(exc))
        return ""


# ---------------------------------------------------------------------------
# Context summarizer
# ---------------------------------------------------------------------------

async def maybe_summarize_context(
    session: Any,
    db: AsyncSession,
    llm: Any,
    log: Any,
) -> str:
    """
    Jika session sudah panjang (>= context_summary_trigger user messages),
    buat ringkasan LLM dan cache di session.metadata_['context_summary'].
    Returns ringkasan text (kosong jika tidak triggered atau gagal).
    """
    from app.config import get_settings
    settings = get_settings()
    trigger = settings.context_summary_trigger

    try:
        user_msg_count = await count_user_messages(session.id, db)
        if user_msg_count < trigger:
            return ""

        meta: dict = session.metadata_ if isinstance(session.metadata_, dict) else {}
        cached_at = meta.get("context_summary_at_msg", 0)

        # Re-summarize every trigger messages
        if user_msg_count - cached_at < trigger and meta.get("context_summary"):
            log.debug("agent_run.context_summary_cached", user_messages=user_msg_count)
            return meta["context_summary"]

        all_rows = await load_history(session.id, db)
        if not all_rows:
            return ""

        history_text = "\n".join(
            f"{'User' if m.role == 'user' else 'Agent'}: {(m.content or '')[:500]}"
            for m in all_rows
            if m.role in ("user", "agent") and m.content
        )

        from langchain_core.messages import HumanMessage as _HM
        summary_prompt = (
            "Berikut adalah riwayat percakapan antara user dan agent. "
            "Buat ringkasan padat (maksimal 300 kata) yang mencakup:\n"
            "- Topik utama yang dibahas\n"
            "- Keputusan atau hasil penting yang sudah dicapai\n"
            "- Konteks yang relevan untuk melanjutkan percakapan\n\n"
            f"Riwayat percakapan:\n{history_text[:6000]}"
        )
        resp = await llm.ainvoke([_HM(content=summary_prompt)])
        summary = resp.content if isinstance(resp.content, str) else str(resp.content)

        new_meta = {**meta, "context_summary": summary, "context_summary_at_msg": user_msg_count}
        session.metadata_ = new_meta
        db.add(session)
        await db.flush()

        log.info("agent_run.context_summarized", user_messages=user_msg_count, summary_len=len(summary))
        return summary

    except Exception as exc:
        log.warning("agent_run.context_summary_failed", error=str(exc))
        return ""


# ---------------------------------------------------------------------------
# System prompt builder (entry point)
# ---------------------------------------------------------------------------

def build_system_prompt(
    *,
    agent_model: Any,
    session: Any,
    active_groups: list[str],
    saved_custom_tools: list,
    subagent_list: list,
    sender_name: str | None,
    context_summary: str,
    memory_block: str,
    layered_memory: dict | None = None,
    rag_context: str,
    escalation_user_jid: str | None,
    escalation_context: str | None,
    is_operator_message: bool,
    user_message: str = "",
) -> str:
    """
    Rakit system prompt lengkap dari semua komponen.

    Urutan blok:
    1. Platform Context Block
    2. Agent Instructions (base_instructions)
    3. Conversation Context Summary (jika triggered)
    4. Long-term Memories
    5. Safety Policy
    6. RAG Context
    7. Channel-specific instructions (WhatsApp / Escalation)
    8. Available Capabilities
    """
    context_block = build_agent_context_block(
        agent_model, session, active_groups, saved_custom_tools, subagent_list, sender_name=sender_name
    )
    base_instructions = agent_model.instructions or "You are a helpful assistant."

    # --- Layered memory (OpenClaw-style) ---
    _lm = layered_memory or {}
    _soul = _lm.get("soul", "").strip()
    _user_profile = _lm.get("user_profile", "").strip()
    _daily_today = _lm.get("daily_today", "").strip()
    _daily_yesterday = _lm.get("daily_yesterday", "").strip()
    _today_date = _lm.get("today_date", "")
    _yesterday_date = _lm.get("yesterday_date", "")

    if _soul or _user_profile or _daily_today or _daily_yesterday:
        p = []

        p.append("# Panduan Operasional")
        p.append(
            "Ini adalah workspace-mu. Semua konteks sudah di-load untuk kamu — baca dan pahami sebelum membalas apapun.\n"
            "Jangan minta izin. Langsung kerja."
        )

        # --- Identitas ---
        p.append("\n## Identitasmu")
        p.append(_soul if _soul else base_instructions)

        # --- User ---
        p.append("\n## User yang Kamu Bantu")
        if _user_profile:
            p.append(_user_profile)
        else:
            p.append(
                "Profil user belum tersimpan.\n"
                "Segera setelah kamu tahu nama, preferensi, atau konteks penting dari user ini → "
                "simpan dengan `remember('user_profile', '...')`. "
                "Ini akan di-load otomatis di sesi berikutnya."
            )

        # --- Konteks hari ini ---
        p.append("\n## Konteks Hari Ini")
        if _daily_today:
            p.append(f"Catatan {_today_date}:\n{_daily_today}")
        else:
            p.append(f"Belum ada catatan untuk {_today_date}.")
        if _daily_yesterday:
            p.append(f"\nCatatan kemarin ({_yesterday_date}):\n{_daily_yesterday}")

        # --- Memory ---
        p.append(
            "\n## Memory — Cara Kerjanya\n"
            "Kamu bangun ulang setiap sesi. Yang menjaga kontinuitas adalah memory yang tersimpan di database.\n\n"
            "### Layer memory yang kamu punya:\n"
            "- **soul** — identitasmu. Di-load otomatis setiap sesi. Edit dengan `remember('soul', '...')`\n"
            "- **user_profile** — profil user ini. Di-load otomatis. Edit dengan `remember('user_profile', '...')`\n"
            f"- **daily:{_today_date}** — catatan hari ini. Di-load otomatis. Tambah dengan `update_daily('...')`\n"
            "- **longterm** — curated memory lintas waktu. Lazy load: `recall('longterm')`. Tambah dengan `update_longterm('...')`\n"
            f"- **daily:YYYY-MM-DD** — catatan hari lain. Akses manual: `recall('daily:YYYY-MM-DD')`\n\n"
            "### Aturan menulis memory — WAJIB:\n"
            "- 'Mental notes' tidak survive restart. Kalau penting → tulis ke file (pakai tool).\n"
            "- Segera tulis setelah event terjadi, bukan nanti.\n"
            "- `update_daily(...)` → log singkat apa yang terjadi hari ini (keputusan, task selesai, info penting)\n"
            "- `update_longterm(...)` → insight, preferensi user, pola yang perlu diingat jangka panjang\n"
            "- `remember('user_profile', ...)` → update profil user jika ada info baru\n\n"
            "### Kapan harus recall:\n"
            "- User tanya sesuatu yang mungkin pernah dibahas → `recall('longterm')` dulu\n"
            "- User minta lanjutkan task dari sesi lalu → cek `recall('daily:YYYY-MM-DD')`\n"
            "- Jangan mulai dari nol kalau konteks mungkin sudah tersimpan"
        )

        # --- Heartbeat ---
        p.append(
            "\n## Heartbeat — Proaktif\n"
            "Kamu bisa dipanggil secara proaktif oleh sistem (bukan oleh user) untuk background checks.\n\n"
            "Saat kamu menerima pesan `[HEARTBEAT]`:\n"
            "1. Baca checklist yang diberikan\n"
            "2. Jalankan setiap item — cek reminder, cek hal yang pending, update daily jika belum\n"
            "3. Jika tidak ada yang perlu disampaikan ke user → balas tepat: `HEARTBEAT_OK`\n"
            "4. Jika ada yang penting → tulis respons normal (sistem akan kirim ke user via channel aktif)\n\n"
            "Jangan balas HEARTBEAT_OK kalau ada sesuatu yang memang perlu disampaikan.\n"
            "Jangan kirim notifikasi kalau tidak ada yang benar-benar penting — respect quiet time user."
        )

        # --- Safety ---
        p.append(
            "\n## Keamanan & Batasan\n"
            "- Jangan bocorkan data private user ke pihak lain\n"
            "- Aksi internal (baca, cari, analisa, tulis memory) → langsung lakukan tanpa tanya\n"
            "- Aksi eksternal (kirim pesan ke nomor lain, email, post publik) → konfirmasi dulu\n"
            "- Kalau tidak yakin → tanya, jangan tebak\n"
            "- Resourceful dulu — cari konteks dari memory sebelum tanya user hal yang mungkin sudah dibahas"
        )

        layered_block = "\n".join(p)
        system_prompt = f"{context_block}\n\n{layered_block}"
        if _soul and base_instructions and base_instructions.strip() != _soul:
            system_prompt += f"\n\n---\n\n{base_instructions}"
    else:
        system_prompt = f"{context_block}\n\n{base_instructions}"

    # 1. Conversation context summary
    if context_summary:
        system_prompt += (
            f"\n\n## Conversation Context Summary\n"
            f"*Ringkasan percakapan sebelumnya (pesan lama sudah dikompresi):*\n{context_summary}"
        )

    # 2. Long-term memories
    if memory_block:
        system_prompt += f"\n\n{memory_block}"

    if user_message.startswith("[SCHEDULED_REMINDER]"):
        system_prompt += (
            "\n\n## Scheduled Reminder Mode\n"
            "Pesan user saat ini berasal dari scheduler, bukan chat manual user.\n"
            "Aturan wajib:\n"
            "- Jangan panggil set_reminder, set_multiple_reminders, cancel_reminder, task, deploy, sandbox, atau tool lain.\n"
            "- Jangan membahas task lain yang sedang berjalan di percakapan.\n"
            "- Jangan bilang reminder baru dibuat atau jadwal diubah.\n"
            "- Ubah payload reminder menjadi pesan singkat, personal, dan natural untuk user.\n"
            "- Maksimal 2 kalimat. Fokus hanya pada isi reminder.\n"
        )

    # 3. Safety policy
    if agent_model.safety_policy:
        import json
        system_prompt += f"\n\n## Safety Policy\n{json.dumps(agent_model.safety_policy, indent=2)}"

    # 4. RAG context
    if rag_context:
        system_prompt += f"\n\n{rag_context}"

    # 5. Channel-specific
    is_whatsapp = getattr(session, "channel_type", None) == "whatsapp"

    if is_whatsapp:
        system_prompt += (
            "\n\n## ⛔ ATURAN FORMAT NOMOR TELEPON\n"
            "DILARANG KERAS mencantumkan format teknis WhatsApp dalam pesan apapun: "
            "`@lid`, `@s.whatsapp.net`, `@c.us`. "
            "Jika kamu perlu menyebut nomor WA customer, gunakan format internasional biasa: `628xxx` atau `+628xxx`. "
            "Contoh BENAR: `6281234567890`. Contoh SALAH: `6281234567890@lid` atau `6281234567890@s.whatsapp.net`."
        )

    if is_whatsapp and not is_operator_message and not escalation_user_jid:
        _name_hint = (
            f" Nama user saat ini adalah **{sender_name}** — gunakan namanya saat menyapa atau membalas."
            if sender_name else ""
        )
        system_prompt += (
            "\n\n## WhatsApp Channel\n"
            "Balas user LANGSUNG dengan teks biasa sebagai output akhirmu. "
            "JANGAN gunakan tool `reply_to_user` untuk menjawab user secara normal — cukup tulis jawabanmu. "
            "Tool `reply_to_user` dan `send_to_number` HANYA dipakai saat menerima perintah dari OPERATOR.\n"
            f"{_name_hint}\n\n"
            "### 🚨 ATURAN ABSOLUT: OUTPUT TEKS = TASK SELESAI SEKETIKA\n"
            "CARA KERJA SISTEM (WAJIB DIMENGERTI):\n"
            "- Sistem ini TIDAK punya mode 'background'. Setiap output teks = reply dikirim ke user = task BERAKHIR.\n"
            "- Jika kamu tulis 'Sedang kubikinin...' tanpa tool call → user dapat pesan itu → task MATI → sys_coder TIDAK pernah dipanggil\n"
            "- Tidak ada 'lanjut nanti'. Tidak ada 'background process'. Selesaikan SEMUANYA dalam satu run.\n"
            "- User yang balas 'ok' / 'mana?' = trigger invocation BARU dengan context kosong — bukan lanjutan\n\n"
            "URUTAN SATU-SATUNYA YANG BENAR:\n"
            "  Step 1: [LANGSUNG panggil tool/task — ZERO teks dulu]\n"
            "  Step 2: [tool berjalan, kerjakan sampai tuntas]\n"
            "  Step 3: [setelah SEMUA selesai] → tulis satu pesan final\n\n"
            "KALAU MAU UPDATE USER SAAT PROSES: pakai tool notify_user() — BUKAN teks biasa\n"
            "  ✅ notify_user('Lagi nulis HTML...') → lanjut kerja\n"
            "  ❌ 'Lagi nulis HTML...' (teks biasa) → task MATI\n\n"
            "### Kirim Gambar ke User\n"
            "Jika kamu perlu mengirim gambar ke user, panggil tool yang sesuai:\n"
            "- `send_whatsapp_image(image_path_or_base64='...')` — untuk kirim gambar/chart dari workspace.\n"
            "JANGAN hanya mendeskripsikan gambar dalam teks — panggil tool-nya agar gambar benar-benar terkirim.\n\n"
            "### Setelah memanggil `escalate_to_human`:\n"
            "- Tool tersebut SUDAH mengirim notifikasi ke operator secara otomatis. "
            "JANGAN tulis atau kirim pesan apapun ke operator.\n"
            "- Output akhirmu adalah pesan singkat untuk USER: "
            "beritahu user bahwa pertanyaannya sedang diteruskan ke tim dan akan segera dibalas.\n\n"
            "### Notifikasi Progress (WAJIB untuk task panjang)\n"
            "Kamu punya tool `notify_user(message)` — gunakan ini untuk kirim update ke user selama task masih berjalan.\n"
            "KAPAN WAJIB dipakai:\n"
            "- Sebelum mulai task yang butuh >30 detik (deploy, nulis banyak file, riset)\n"
            "- Setiap kali pindah ke fase berikutnya (nulis file → deploy → verifikasi)\n"
            "- Jika ada error/retry dalam proses\n"
            "CONTOH BENAR:\n"
            "  1. notify_user('Oke, lagi nulis file HTML portfolionya...')\n"
            "  2. [tulis semua file]\n"
            "  3. notify_user('File selesai, sekarang deploy...')\n"
            "  4. [deploy]\n"
            "  5. [tulis reply final dengan URL]\n"
            "PENTING: notify_user bukan reply final — pakai untuk progress saja, reply final tetap di output teks.\n\n"
            "### Pesan Suara & Audio\n"
            "Sistem secara otomatis mentranskripsikan pesan suara dan file audio dari user. "
            "Jika pesan user mengandung format `[Sistem: Pengguna mengirim pesan suara/file audio...]` "
            "diikuti `Transkripsi: <teks>`, artinya KAMU SUDAH MENERIMA ISI PESAN SUARA TERSEBUT dalam bentuk teks. "
            "Balas langsung berdasarkan isi transkripsi — JANGAN bilang kamu tidak bisa membaca/mendengar audio. "
            "Perlakukan transkripsi seperti pesan teks biasa dari user.\n"
        )

    if escalation_user_jid:
        ctx_block = ""
        if escalation_context:
            ctx_block = f"\n\n### Pesan terakhir dari user yang dieskalasi:\n{escalation_context}"
        # Strip @lid / @s.whatsapp.net — tampilkan nomor telepon saja, bukan JID teknis
        _user_display_phone = normalize_phone(escalation_user_jid)
        system_prompt += (
            f"\n\n## SESI OPERATOR\n"
            f"Kamu sedang berbicara dengan OPERATOR/ADMIN.\n"
            f"Nomor WhatsApp user yang dieskalasi: `{_user_display_phone}` (gunakan nomor ini saat menyebut customer, BUKAN format @lid)"
            f"{ctx_block}\n\n"
            "### ROUTING REPLY WHATSAPP\n"
            "- Jika konteks memuat `ROUTING: operator_reply_quoted_escalation`, artinya operator memakai menu reply WhatsApp pada pesan eskalasi.\n"
            "- Dalam kondisi itu, isi pesan operator saat ini adalah balasan untuk customer tersebut. "
            "Panggil `reply_to_user(message)` pada turn yang sama, kecuali operator jelas meminta draft saja.\n"
            "- Jika operator meminta kamu merapikan lalu mengirim, rapikan pesannya lalu langsung panggil `reply_to_user(message)`.\n\n"
            "### 🚨 ATURAN PALING KRITIS: DRAFT DULU, JANGAN LANGSUNG KIRIM 🚨\n"
            "- Apabila operator memberikan instruksi/jawaban untuk diteruskan ke customer, KAMU DILARANG KERAS langsung memanggil tool `reply_to_user`.\n"
            "- Kamu WAJIB menyusun *draft* pesan yang rapi & sopan, menampilkannya kepada operator, lalu diakhiri dengan:\n"
            "  \"Sudah OK? Ketik 'kirim' untuk meneruskannya ke customer.\"\n"
            "- Jika operator membalas dengan 'kirim', 'ya', atau 'ok', panggil tool `reply_to_user(message)`.\n"
            "- Jika pesan operator saat ini sudah berisi perintah eksplisit untuk mengirim, seperti "
            "'langsung kirim', 'rapihin terus kirim', atau 'rapihin aja pesannya terus kirim', "
            "maka susun pesan final dan LANGSUNG panggil `reply_to_user(message)` pada turn yang sama. "
            "JANGAN tampilkan draft lagi.\n"
            "- Balas operator singkat setelah terkirim: \"Terkirim ✓\"\n"
            "Pelanggaran terhadap aturan ini adalah kesalahan fatal!\n"
        )
    elif is_operator_message:
        _raw_cfg = session.channel_config
        _ch_cfg = _raw_cfg if isinstance(_raw_cfg, dict) else {}
        _raw_user_jid = _ch_cfg.get("user_phone") or getattr(session, "external_user_id", None) or "unknown"
        user_wa_phone = normalize_phone(_raw_user_jid) if _raw_user_jid != "unknown" else "unknown"
        system_prompt += (
            f"\n\n## MODE: OPERATOR COMMAND — ALUR KONFIRMASI\n"
            f"Nomor WhatsApp user: `{user_wa_phone}`\n"
            "Pesan berikut adalah PERINTAH dari human operator.\n\n"
            "### INSTRUKSI WAJIB\n"
            "- Alur DRAFT -> KONFIRMASI -> KIRIM:\n"
            "  1. Agent menyusun draft rapi dari pesanan operator.\n"
            "  2. Tampilkan draft + tanya: \"Sudah OK? Ketik 'kirim'...\"\n"
            "  3. JANGAN panggil `reply_to_user` sebelum dikonfirmasi operator.\n"
            "- Setelah operator konfirmasi ('ok', 'kirim'), panggil tool `reply_to_user(message)`.\n"
            "- Jika operator sudah bilang 'langsung kirim', 'terus kirim', atau 'rapihin aja pesannya terus kirim', "
            "anggap itu konfirmasi eksplisit: rapikan pesan lalu langsung panggil `reply_to_user(message)`.\n"
            "- Sesudah sukses, balas operator: \"Terkirim ✓\"\n"
        )

    # 6. Available capabilities
    cap_parts: list[str] = []
    if "memory" in active_groups:
        cap_parts.append("memory tools (remember/recall/forget)")
    if "skills" in active_groups:
        cap_parts.append("skill tools (create_skill/list_skills/use_skill)")
    if "tool_creator" in active_groups:
        custom_tool_names = [ct.name for ct in saved_custom_tools]
        ct_str = f" — custom tools available: {', '.join(custom_tool_names)}" if custom_tool_names else ""
        cap_parts.append(f"tool creator (create_tool/list_tools/run_custom_tool){ct_str}")
    if "scheduler" in active_groups:
        cap_parts.append("scheduler tools (set_reminder/list_reminders/cancel_reminder)")
    if "escalation" in active_groups:
        cap_parts.append("escalation tools (escalate_to_human/reply_to_user/send_to_number)")
    if "http" in active_groups:
        cap_parts.append("HTTP tools (http_get/http_post/http_patch/http_delete)")
    if "whatsapp_media" in active_groups:
        cap_parts.append("WhatsApp media tools (send_whatsapp_image, send_whatsapp_document)")
    if "wa_agent_manager" in active_groups:
        cap_parts.append("WA agent manager (send_agent_wa_qr)")
    if "deploy" in active_groups:
        cap_parts.append("deployment tools (deploy_app/stop_deployment/get_deployment_status/get_deployment_logs)")

    if "deploy" in active_groups:
        system_prompt += (
            "\n\n## Deploy Instructions\n"
            "Kamu memiliki kemampuan deploy app ke public URL via Cloudflare tunnel.\n"
            "ALUR WAJIB — ikuti urutan ini tanpa skip:\n"
            "1. Untuk profile/portfolio/landing page sederhana: buat static HTML/CSS/JS langsung, jangan install framework kecuali user minta app kompleks.\n"
            "2. Tulis semua file ke workspace (write_file)\n"
            "3. Panggil get_deployment_status()\n"
            "   - Status 'running' → JANGAN deploy ulang, gunakan URL yang ada\n"
            "   - Status 'not_deployed' → lanjut ke langkah 4\n"
            "4. Panggil deploy_app(command, port)\n"
            "5. Verifikasi: panggil get_deployment_status() lagi — pastikan URL ada dan status 'running'\n"
            "   - Jika URL kosong atau error → panggil get_deployment_logs() → debug → perbaiki\n"
            "6. Sampaikan URL ke user secara natural — task BELUM selesai sebelum URL dikonfirmasi\n\n"
            "ATURAN OUTPUT:\n"
            "- Respond naturally, seperti developer yang selesai mengerjakan sesuatu\n"
            "- Selalu sertakan URL deploy di respons akhir\n"
            "- JANGAN tampilkan source code lengkap kecuali user eksplisit minta\n"
            "- JANGAN gunakan format STATUS:/DEPLOY_URL:/BLOCKER: — terlalu kaku\n"
            "- npm, npx, node tersedia di sandbox\n"
            "- Static file server: edit file tidak perlu restart, deploy_app ulang HANYA jika ganti command/port/dependency\n"
        )

    if "scheduler" in active_groups:
        system_prompt += (
            "\n\n## Scheduler Instructions\n"
            "Saat user meminta reminder:\n"
            "- Jika waktu jelas, langsung panggil set_reminder. Jangan cuma bilang sudah diatur tanpa tool call.\n"
            "- Jika user menyebut jam spesifik tanpa kata 'setiap', 'harian', 'daily', atau pola berulang, gunakan one-time ISO datetime lokal WIB.\n"
            "- Cron hanya untuk reminder berulang yang eksplisit.\n"
            "- Setelah set_reminder sukses, konfirmasi sebagai one-time atau recurring sesuai output tool.\n"
            "- Jangan mengubah one-time reminder menjadi 'setiap hari' kecuali user memang minta berulang.\n"
        )

    if cap_parts:
        system_prompt += (
            "\n\n## Available Capabilities\n"
            "You have access to: " + ", ".join(cap_parts) + ".\n"
            "CRITICAL RULES:\n"
            "1. To apply a skill: call `use_skill(name='X')` first — never guess its content.\n"
            "2. After creating a new tool with `create_tool`, use `run_custom_tool(name, args_json)` "
            "to execute it in this session (it won't be a direct tool yet)."
        )

    if "memory" in active_groups:
        system_prompt += (
            "\n\n## Aturan Memori Jangka Panjang (WAJIB)\n"
            "Kamu HARUS aktif menyimpan konteks penting ke long-term memory. "
            "Jangan andalkan conversation history saja — pesan lama bisa terpotong.\n\n"
            "**Simpan dengan `remember(key, value)` segera setelah:**\n"
            "- User mengirim CV/resume → simpan: nama, posisi terakhir, skill utama, pendidikan\n"
            "- User menyebut nama/profil dirinya → simpan: user_name, pekerjaan, dll\n"
            "- Kamu berhasil deploy → simpan: deploy_url, nama project, tanggal\n"
            "- Kamu membuat file/project penting → simpan: nama file, lokasi, tujuannya\n"
            "- User menyatakan preferensi/gaya → simpan: bahasa forehand, framework favorit, dll\n"
            "- Ada keputusan/kesepakatan penting → simpan ringkasannya\n\n"
            "**Format key yang disarankan:** `cv_name`, `cv_skills`, `cv_education`, "
            "`deploy_url`, `project_name`, `user_preference_language`, dll\n\n"
            "**Recall dulu sebelum bekerja:** Jika user minta sesuatu yang mungkin pernah dibahas "
            "(edit portfolio, update deploy, dll), panggil `recall()` atau `recall(key)` dulu "
            "untuk memeriksa apa yang sudah tersimpan — jangan mulai dari nol jika sudah ada konteks."
        )

    return system_prompt
