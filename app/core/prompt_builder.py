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

from app.core.context_service import count_user_messages, load_history
from app.core.phone_utils import normalize_phone


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
    user_phone = _ch_cfg.get("user_phone") or getattr(session, "external_user_id", None) or ""
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
    if user_phone:
        lines.append(f"- Current User Phone: {user_phone}")
    if sender_name:
        lines.append(f"- Current User Name: {sender_name}")
    lines.append(f"- Current User Role: {user_role}")
    lines.append(f"- Session ID: {session.id}")

    if subagent_list:
        lines.append("\n## Available Subagents")
        lines.append(
            "Delegate specific tasks using `task(name=..., task=...)`. "
            "Always use write_todos to plan before delegating."
        )
        for sa in subagent_list:
            lines.append(f"- **{sa['name']}**: {sa['description']}")

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
        from app.core.document_service import (
            search_documents_keyword,
            search_documents_vector,
        )
        from app.core.embedding_service import embed_text

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
    rag_context: str,
    escalation_user_jid: str | None,
    escalation_context: str | None,
    is_operator_message: bool,
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

    # 3. Safety policy
    if agent_model.safety_policy:
        import json
        system_prompt += f"\n\n## Safety Policy\n{json.dumps(agent_model.safety_policy, indent=2)}"

    # 4. RAG context
    if rag_context:
        system_prompt += f"\n\n{rag_context}"

    # 5. Channel-specific
    is_whatsapp = getattr(session, "channel_type", None) == "whatsapp"

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
            "### Kirim Gambar ke User\n"
            "Jika kamu perlu mengirim gambar ke user, panggil tool yang sesuai:\n"
            "- `send_whatsapp_image(image_path_or_base64='...')` — untuk kirim gambar/chart dari workspace.\n"
            "JANGAN hanya mendeskripsikan gambar dalam teks — panggil tool-nya agar gambar benar-benar terkirim.\n\n"
            "### Setelah memanggil `escalate_to_human`:\n"
            "- Tool tersebut SUDAH mengirim notifikasi ke operator secara otomatis. "
            "JANGAN tulis atau kirim pesan apapun ke operator.\n"
            "- Output akhirmu adalah pesan singkat untuk USER: "
            "beritahu user bahwa pertanyaannya sedang diteruskan ke tim dan akan segera dibalas.\n"
        )

    if escalation_user_jid:
        ctx_block = ""
        if escalation_context:
            ctx_block = f"\n\n### Pesan terakhir dari user yang dieskalasi:\n{escalation_context}"
        system_prompt += (
            f"\n\n## SESI OPERATOR\n"
            f"Kamu sedang berbicara dengan OPERATOR/ADMIN.\n"
            f"Target user WhatsApp (Chat ID): `{escalation_user_jid}`"
            f"{ctx_block}\n\n"
            "### 🚨 ATURAN PALING KRITIS: DRAFT DULU, JANGAN LANGSUNG KIRIM 🚨\n"
            "- Apabila operator memberikan instruksi/jawaban untuk diteruskan ke customer, KAMU DILARANG KERAS langsung memanggil tool `reply_to_user`.\n"
            "- Kamu WAJIB menyusun *draft* pesan yang rapi & sopan, menampilkannya kepada operator, lalu diakhiri dengan:\n"
            "  \"Sudah OK? Ketik 'kirim' untuk meneruskannya ke customer.\"\n"
            "- SETELAH operator membalas dengan 'kirim', 'ya', atau 'ok', BARULAH kamu diizinkan memanggil tool `reply_to_user(message)`.\n"
            "- Balas operator singkat setelah terkirim: \"Terkirim ✓\"\n"
            "Pelanggaran terhadap aturan ini adalah kesalahan fatal!\n"
        )
    elif is_operator_message:
        _raw_cfg = session.channel_config
        _ch_cfg = _raw_cfg if isinstance(_raw_cfg, dict) else {}
        user_wa_jid = _ch_cfg.get("user_phone") or getattr(session, "external_user_id", None) or "unknown"
        system_prompt += (
            f"\n\n## MODE: OPERATOR COMMAND — ALUR KONFIRMASI\n"
            f"WhatsApp JID user dalam eskalasi: `{user_wa_jid}`\n"
            "Pesan berikut adalah PERINTAH dari human operator.\n\n"
            "### INSTRUKSI WAJIB\n"
            "- Alur DRAFT -> KONFIRMASI -> KIRIM:\n"
            "  1. Agent menyusun draft rapi dari pesanan operator.\n"
            "  2. Tampilkan draft + tanya: \"Sudah OK? Ketik 'kirim'...\"\n"
            "  3. JANGAN panggil `reply_to_user` sebelum dikonfirmasi operator.\n"
            "- Setelah operator konfirmasi ('ok', 'kirim'), panggil tool `reply_to_user(message)`.\n"
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

    if cap_parts:
        system_prompt += (
            "\n\n## Available Capabilities\n"
            "You have access to: " + ", ".join(cap_parts) + ".\n"
            "CRITICAL RULES:\n"
            "1. To apply a skill: call `use_skill(name='X')` first — never guess its content.\n"
            "2. After creating a new tool with `create_tool`, use `run_custom_tool(name, args_json)` "
            "to execute it in this session (it won't be a direct tool yet)."
        )

    return system_prompt
