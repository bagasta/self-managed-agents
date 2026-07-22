"""Restart-safe shadow/runtime state for Arthur agent-building workflows."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_build_draft import AgentBuildDraft

_QUESTION_RE = re.compile(r"(?:^|\n|(?<=[.!]))\s*([^\n?]{4,300}\?)", re.MULTILINE)
_SPACE_RE = re.compile(r"\s+")

_QUESTION_TOPIC_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pain_point", ("pain point", "masalah", "kendala", "kewalahan", "sering nanya", "hambatan")),
    ("agent_name", ("nama agent", "nama yang diinginkan", "agent-nya mau", "agentnya mau")),
    ("target_user", ("siapa pengguna", "target pengguna", "siapa yang akan ngobrol", "pelanggan atau", "customer atau")),
    ("business_context", ("bisnis apa", "layanan apa", "jualan apa", "toko online kamu sendiri", "untuk klien")),
    ("task_scope", ("tugas utama", "harus bisa apa", "kemampuan apa", "pertanyaan apa saja", "kebutuhan utama")),
    ("knowledge_source", ("sumber informasi", "ambil jawaban", "website", "knowledge base", "data produk")),
    ("escalation", ("eskalasi", "diteruskan", "hubungi siapa", "nomor operator", "bantuan manusia")),
    ("file_capability", ("menerima file", "membuat file", "dokumen", "pdf", "excel", "csv", "visualisasi data")),
    ("integration", ("integrasi", "google sheets", "spreadsheet", "oauth", "connector")),
    ("trigger_timing", ("kapan agent", "setelah pembelian", "jadwal", "jam operasional", "trigger")),
    ("success_metric", ("indikator berhasil", "ukuran keberhasilan", "target keberhasilan", "kpi")),
    ("tone_language", ("gaya bahasa", "tone", "bahasa apa", "formal atau", "sapaan")),
)


def canonical_question(text: str) -> str:
    clean = _SPACE_RE.sub(" ", str(text or "").strip().casefold())
    return re.sub(r"[^a-z0-9\s]", "", clean)


def question_topic(text: str) -> str | None:
    canonical = canonical_question(text)
    for topic, terms in _QUESTION_TOPIC_TERMS:
        if any(term in canonical for term in terms):
            return topic
    return None


def extract_questions(reply: str, *, max_questions: int = 3) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _QUESTION_RE.finditer(reply or ""):
        question = _SPACE_RE.sub(" ", match.group(1)).strip()
        canonical = canonical_question(question)
        if canonical and canonical not in seen:
            seen.add(canonical)
            found.append(question)
    return found[:max_questions]


def answered_question_topics(evidence: list[dict[str, Any]] | None) -> set[str]:
    """Infer only requirement slots that the user has explicitly addressed."""
    text = "\n".join(
        str(item.get("value") or "").casefold()
        for item in list(evidence or [])
        if isinstance(item, dict) and item.get("status") == "answered"
    )
    topics: set[str] = set()
    patterns: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("pain_point", ("masalahnya", "kendalanya", "kewalahan", "pain point", "sering ditanya")),
        ("agent_name", ("nama agent", "nama agentnya", "namanya ", "belum ditentukan")),
        ("target_user", ("untuk customer", "untuk pelanggan", "yang akan chat", "penggunanya")),
        ("task_scope", ("akan menanyakan", "harus menjawab", "bantu jawab", "tugasnya", "survey ")),
        ("knowledge_source", ("ambil jawaban", "sumbernya", "dari website", "knowledge base")),
        ("escalation", ("eskalasi", "diteruskan ke", "hubungi saya", "nomor saya", "bantuan manusia")),
        ("file_capability", ("menerima file", "membuat file", "tidak perlu file", "hanya cs")),
        ("integration", ("google sheets", "spreadsheet", "oauth", "integrasi ")),
        ("trigger_timing", ("setelah pembelian", "setelah beli", "setiap jam", "setiap hari", "jadwalnya")),
        ("success_metric", ("targetnya", "kpi", "dianggap berhasil", "ukuran keberhasilan")),
        ("tone_language", ("gaya bahasanya", "bahasa indonesia", "formal", "santai")),
    )
    for topic, terms in patterns:
        if any(term in text for term in terms):
            topics.add(topic)
    return topics


def guard_repeated_questions(
    reply: str,
    question_history: list[dict[str, Any]] | None,
    evidence: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str]]:
    """Remove exact canonical questions already shown to the user.

    The prompt tells Arthur not to repeat questions, but this runtime guard is
    the final deterministic boundary in case a provider ignores that rule.
    """
    prior = {
        str(item.get("canonical") or "")
        for item in list(question_history or [])
        if isinstance(item, dict) and item.get("canonical")
    }
    prior_topics = {
        str(item.get("topic") or question_topic(str(item.get("question") or "")) or "")
        for item in list(question_history or [])
        if isinstance(item, dict)
    }
    prior_topics.discard("")
    answered_topics = answered_question_topics(evidence)
    cleaned = str(reply or "")
    removed: list[str] = []
    for question in extract_questions(cleaned, max_questions=12):
        canonical = canonical_question(question)
        topic = question_topic(question)
        if (
            canonical not in prior
            and (topic is None or topic not in prior_topics)
            and (topic is None or topic not in answered_topics)
        ):
            continue
        cleaned = cleaned.replace(question, "", 1)
        removed.append(question)

    if not removed:
        return cleaned, []

    cleaned = re.sub(r"(?m)^\s*[-*\d.)]*\s*$", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if not cleaned:
        cleaned = (
            "Saya sudah mencatat jawaban sebelumnya dan tidak akan menanyakannya lagi. "
            "Saya lanjutkan dari informasi yang sudah tersimpan."
        )
    return cleaned, removed


async def get_active_build_draft(
    session_id: uuid.UUID,
    db: AsyncSession,
) -> AgentBuildDraft | None:
    now = datetime.now(timezone.utc)
    stmt = (
        select(AgentBuildDraft)
        .where(
            AgentBuildDraft.session_id == session_id,
            AgentBuildDraft.completed_at.is_(None),
        )
        .order_by(AgentBuildDraft.updated_at.desc())
        .limit(1)
    )
    draft = (await db.execute(stmt)).scalar_one_or_none()
    if draft is not None and draft.expires_at is not None and draft.expires_at <= now:
        draft.completed_at = now
        draft.workflow_state = "expired"
        await db.flush()
        return None
    return draft


async def ensure_build_draft(
    *,
    session_id: uuid.UUID,
    owner_external_id: str,
    intent: str,
    message_id: str,
    user_message: str,
    prompt_version: str,
    engine_version: str,
    db: AsyncSession,
) -> AgentBuildDraft:
    draft = await get_active_build_draft(session_id, db)
    if draft is None:
        draft = AgentBuildDraft(
            owner_external_id=owner_external_id or f"session:{session_id}",
            session_id=session_id,
            intent=intent,
            workflow_state="discovery" if intent in {"discover", "create"} else "idle",
            prompt_version=prompt_version,
            engine_version=engine_version,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db.add(draft)
        await db.flush()

    if draft.last_inbound_message_id == message_id:
        return draft

    evidence = list(draft.evidence_json or [])
    evidence.append(
        {
            "type": "user_message",
            "source_message_id": message_id,
            "value": (user_message or "")[:4000],
            "status": "answered",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    evidence = evidence[-40:]
    expected_version = int(draft.state_version or 1)
    result = await db.execute(
        update(AgentBuildDraft)
        .where(
            AgentBuildDraft.id == draft.id,
            AgentBuildDraft.state_version == expected_version,
        )
        .values(
            intent=(draft.intent if draft.intent not in {"discover", "idle"} else intent),
            evidence_json=evidence,
            last_inbound_message_id=message_id,
            prompt_version=prompt_version,
            engine_version=engine_version,
            state_version=expected_version + 1,
            updated_at=datetime.now(timezone.utc),
        )
    )
    if result.rowcount != 1:
        raise RuntimeError("Arthur build state changed concurrently; retry from fresh state")
    await db.refresh(draft)
    return draft


def _step_result(step: dict[str, Any]) -> dict[str, Any]:
    raw = step.get("result")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def infer_workflow_state(
    current_state: str,
    steps: list[dict[str, Any]],
    final_reply: str,
) -> str:
    tool_names = [str(step.get("tool") or "") for step in steps]
    if "delete_agent" in tool_names:
        return "complete"
    if "create_agent" in tool_names:
        if any("auth" in name or "oauth" in name for name in tool_names):
            return "integration_auth_pending"
        if "create_wa_dev_trial_link" in tool_names:
            return "demo_ready"
        return "agent_created"
    if any(name in {"update_agent", "set_agent_memory"} for name in tool_names):
        return "verifying"
    if any("auth" in name or "oauth" in name for name in tool_names):
        return "integration_auth_pending"
    if "create_wa_dev_trial_link" in tool_names:
        return "demo_ready"
    for step in steps:
        if str(step.get("tool") or "") != "plan_agent":
            continue
        result = _step_result(step)
        status = str(result.get("plan_status") or result.get("status") or "").lower()
        if status == "ready":
            return "awaiting_confirmation"
        if status in {"needs_clarification", "clarification"}:
            return "discovery"
    if extract_questions(final_reply):
        return "discovery"
    return current_state


async def record_build_outcome(
    *,
    draft: AgentBuildDraft,
    final_reply: str,
    steps: list[dict[str, Any]],
    skill_versions: dict[str, str],
    db: AsyncSession,
) -> AgentBuildDraft:
    history = list(draft.question_history_json or [])
    existing = {str(item.get("canonical") or "") for item in history if isinstance(item, dict)}
    for question in extract_questions(final_reply, max_questions=12):
        canonical = canonical_question(question)
        if canonical and canonical not in existing:
            history.append(
                {
                    "question": question,
                    "canonical": canonical,
                    "topic": question_topic(question),
                    "asked_at": datetime.now(timezone.utc).isoformat(),
                    "state_version": int(draft.state_version or 1),
                }
            )
            existing.add(canonical)
    history = history[-30:]
    new_state = infer_workflow_state(draft.workflow_state, steps, final_reply)
    expected_version = int(draft.state_version or 1)
    values: dict[str, Any] = {
        "workflow_state": new_state,
        "question_history_json": history,
        "skill_versions_json": dict(skill_versions),
        "state_version": expected_version + 1,
        "updated_at": datetime.now(timezone.utc),
    }
    if new_state == "complete":
        values["completed_at"] = datetime.now(timezone.utc)
    result = await db.execute(
        update(AgentBuildDraft)
        .where(
            AgentBuildDraft.id == draft.id,
            AgentBuildDraft.state_version == expected_version,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise RuntimeError("Arthur build outcome lost optimistic-lock race")
    await db.refresh(draft)
    return draft


def build_state_prompt(draft: AgentBuildDraft) -> str:
    evidence = [
        item for item in list(draft.evidence_json or [])[-8:]
        if isinstance(item, dict) and item.get("value")
    ]
    questions = [
        str(item.get("question"))
        for item in list(draft.question_history_json or [])[-8:]
        if isinstance(item, dict) and item.get("question")
    ]
    evidence_lines = "\n".join(
        f"- [{item.get('status', 'answered')}] {str(item.get('value'))[:800]}"
        for item in evidence
    ) or "- Belum ada evidence tersimpan."
    question_lines = "\n".join(f"- {question}" for question in questions) or "- Belum ada."
    return (
        "## Arthur Persistent Build State\n"
        f"- build_id: {draft.id}\n"
        f"- intent: {draft.intent}\n"
        f"- workflow_state: {draft.workflow_state}\n"
        f"- confirmation_status: {draft.confirmation_status}\n"
        f"- state_version: {draft.state_version}\n"
        "### Evidence user terbaru\n"
        f"{evidence_lines}\n"
        "### Pertanyaan canonical yang sudah pernah diajukan\n"
        f"{question_lines}\n"
        "Jangan meminta user mengulang evidence di atas. Jika sebuah jawaban mengubah fakta induk, "
        "jelaskan invalidation sebelum menanyakan turunannya kembali."
    )
