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
    ("daily_chat_volume", ("volume harian", "chat per hari", "berapa banyak chat", "puluhan atau ratusan", "20 50", "50 90")),
    ("vision_requirement", ("perlu bisa lihat", "baca gambar", "analisis gambar", "memahami gambar", "vision")),
    ("usage_context", ("untuk bisnis", "untuk pekerjaan", "keperluan bisnis", "personal atau pekerjaan")),
    ("go_live_approver", ("siapa yang approve", "siapa yang menyetujui", "approver", "review sebelum")),
    ("ideal_conversations", ("contoh percakapan ideal", "contoh pas", "alur percakapan")),
    ("expected_outputs", ("output survey", "hasil survey", "disimpan di mana", "dicatat di mana")),
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
        ("daily_chat_volume", ("chat per hari", "orang per hari", "puluhan", "ratusan", "50an")),
        ("vision_requirement", ("baca gambar", "lihat gambar", "analisis gambar", "menerima gambar", "terima gambar")),
        ("usage_context", ("untuk bisnis", "untuk pekerjaan", "keperluan bisnis", "keperluan pribadi")),
        ("go_live_approver", ("saya sendiri yang approve", "gua sendiri yang approve", "approver", "yang approve")),
        ("ideal_conversations", ("contoh percakapan ideal", "contohnya kamu atur", "atur aja", "sesuaikan aja")),
        ("expected_outputs", ("hasil survey", "output survey", "dicatat di google sheets", "di google sheets")),
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
    facts: dict[str, Any] | None = None,
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
    persisted = facts if isinstance(facts, dict) else {}
    persisted_answers = persisted.get("discovery_answers")
    if isinstance(persisted_answers, dict):
        unresolved = {
            str(field)
            for field in persisted.get("unresolved_fields") or []
        }
        answered_topics.update(
            str(field)
            for field, value in persisted_answers.items()
            if field not in unresolved and value not in (None, "", [], {})
        )
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


def merge_discovery_answers(
    discovery_answers: Any,
    facts: dict[str, Any] | None,
) -> Any:
    """Merge a partial tool payload with the last verified discovery snapshot.

    Models may omit old fields once chat history is summarized. Persisted facts
    are authoritative, while values and evidence explicitly supplied on the
    current call replace their prior versions. A verified confirmation is
    inherited only when the incoming payload does not change a confirmed fact.
    """
    if isinstance(discovery_answers, str):
        try:
            incoming = json.loads(discovery_answers)
        except (TypeError, ValueError):
            return discovery_answers
    elif isinstance(discovery_answers, dict):
        incoming = dict(discovery_answers)
    elif discovery_answers in (None, ""):
        incoming = {}
    else:
        return discovery_answers

    snapshot = facts if isinstance(facts, dict) else {}
    prior_answers = snapshot.get("discovery_answers")
    prior_evidence = snapshot.get("discovery_evidence")
    merged = dict(prior_answers) if isinstance(prior_answers, dict) else {}
    confirmation_applies = persisted_confirmation_applies(incoming, snapshot)
    if not confirmation_applies:
        merged.pop("user_confirmed", None)

    incoming_evidence = incoming.pop("_evidence", {})
    merged.update(incoming)

    evidence = dict(prior_evidence) if isinstance(prior_evidence, dict) else {}
    if isinstance(incoming_evidence, dict):
        evidence.update(incoming_evidence)
    if evidence:
        merged["_evidence"] = evidence
    return merged


def persisted_confirmation_applies(
    discovery_answers: Any,
    facts: dict[str, Any] | None,
) -> bool:
    """Return whether a DB-verified confirmation still covers this payload."""
    snapshot = facts if isinstance(facts, dict) else {}
    if snapshot.get("confirmation_verified") is not True:
        return False
    prior = snapshot.get("discovery_answers")
    if not isinstance(prior, dict) or prior.get("user_confirmed") is not True:
        return False
    if isinstance(discovery_answers, str):
        try:
            incoming = json.loads(discovery_answers)
        except (TypeError, ValueError):
            return False
    elif isinstance(discovery_answers, dict):
        incoming = discovery_answers
    elif discovery_answers in (None, ""):
        incoming = {}
    else:
        return False
    for field, value in incoming.items():
        if field in {"_evidence", "user_confirmed"}:
            continue
        if field not in prior or prior[field] != value:
            return False
    return True


async def load_build_discovery_facts(
    db_factory: Any,
    session_id: str | None,
) -> dict[str, Any]:
    """Load the committed canonical discovery snapshot for a planning tool."""
    if db_factory is None or not session_id:
        return {}
    parsed_session_id = uuid.UUID(str(session_id))
    async with db_factory() as db:
        stmt = (
            select(AgentBuildDraft.facts_json)
            .where(
                AgentBuildDraft.session_id == parsed_session_id,
                AgentBuildDraft.completed_at.is_(None),
            )
            .order_by(AgentBuildDraft.updated_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        facts = result.scalar_one_or_none()
    return dict(facts) if isinstance(facts, dict) else {}


def discovery_snapshot_from_steps(
    existing_facts: dict[str, Any] | None,
    steps: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Build the next canonical fact snapshot from the latest plan result."""
    facts = dict(existing_facts or {})
    confirmation_status = "pending"
    if bool((facts.get("discovery_answers") or {}).get("user_confirmed")):
        confirmation_status = "confirmed"

    for step in reversed(steps or []):
        if str(step.get("tool") or "") != "plan_agent":
            continue
        result_data = _step_result(step)
        discovery = result_data.get("discovery")
        if not isinstance(discovery, dict):
            discovery = result_data.get("discovery_progress")
        if not isinstance(discovery, dict):
            continue

        step_args = step.get("args") if isinstance(step.get("args"), dict) else {}
        raw_answers = step_args.get("discovery_answers")
        if isinstance(raw_answers, str):
            try:
                raw_answers = json.loads(raw_answers)
            except (TypeError, ValueError):
                raw_answers = {}
        raw_answers = raw_answers if isinstance(raw_answers, dict) else {}
        raw_evidence = raw_answers.get("_evidence")
        raw_evidence = raw_evidence if isinstance(raw_evidence, dict) else {}

        normalized = discovery.get("normalized_answers")
        normalized = normalized if isinstance(normalized, dict) else {}
        completed = {
            str(field)
            for field in discovery.get("completed_fields") or []
        }
        prior_answers = facts.get("discovery_answers")
        canonical_answers = dict(prior_answers) if isinstance(prior_answers, dict) else {}
        for field in completed:
            if field in normalized:
                canonical_answers[field] = normalized[field]

        complete = bool(discovery.get("complete"))
        if complete:
            canonical_answers["user_confirmed"] = True
            confirmation_status = "confirmed"
        else:
            canonical_answers.pop("user_confirmed", None)
            confirmation_status = "pending"

        prior_evidence = facts.get("discovery_evidence")
        canonical_evidence = dict(prior_evidence) if isinstance(prior_evidence, dict) else {}
        for field in completed:
            if field in raw_evidence:
                canonical_evidence[field] = raw_evidence[field]
        if complete and "user_confirmed" in raw_evidence:
            canonical_evidence["user_confirmed"] = raw_evidence["user_confirmed"]
        else:
            canonical_evidence.pop("user_confirmed", None)

        facts = {
            **facts,
            "discovery_answers": canonical_answers,
            "discovery_evidence": canonical_evidence,
            "required_fields": list(discovery.get("required_fields") or []),
            "unresolved_fields": list(
                dict.fromkeys(
                    [
                        *(discovery.get("missing_fields") or []),
                        *(discovery.get("invalid_fields") or []),
                    ]
                )
            ),
            "verified_evidence_fields": list(
                discovery.get("verified_evidence_fields") or []
            ),
            "file_capability": str(discovery.get("file_capability") or ""),
            "confirmation_verified": complete,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        break
    return facts, confirmation_status


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
    def succeeded(tool_name: str) -> bool:
        for step in reversed(steps or []):
            if str(step.get("tool") or "") != tool_name:
                continue
            result = _step_result(step)
            if result.get("success") is True:
                return True
            if tool_name == "create_agent" and result.get("agent_id") and not result.get("error"):
                return True
        return False

    tool_names = [str(step.get("tool") or "") for step in steps]
    if "delete_agent" in tool_names and succeeded("delete_agent"):
        return "complete"
    if "create_agent" in tool_names and succeeded("create_agent"):
        if any("auth" in name or "oauth" in name for name in tool_names):
            return "integration_auth_pending"
        if "create_wa_dev_trial_link" in tool_names and succeeded("create_wa_dev_trial_link"):
            return "demo_ready"
        return "agent_created"
    if any(
        name in {"update_agent", "set_agent_memory"} and succeeded(name)
        for name in tool_names
    ):
        return "verifying"
    if any("auth" in name or "oauth" in name for name in tool_names):
        return "integration_auth_pending"
    if "create_wa_dev_trial_link" in tool_names and succeeded("create_wa_dev_trial_link"):
        return "demo_ready"
    for step in steps:
        if str(step.get("tool") or "") != "plan_agent":
            continue
        result = _step_result(step)
        status = str(result.get("plan_status") or result.get("status") or "").lower()
        if status == "ready":
            return "ready_to_create"
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
    facts, confirmation_status = discovery_snapshot_from_steps(
        draft.facts_json,
        steps,
    )

    expected_version = int(draft.state_version or 1)
    values: dict[str, Any] = {
        "workflow_state": new_state,
        "facts_json": facts,
        "confirmation_status": confirmation_status,
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
    facts = dict(draft.facts_json or {})
    discovery_answers = facts.get("discovery_answers")
    discovery_answers = discovery_answers if isinstance(discovery_answers, dict) else {}
    unresolved_fields = [
        str(field) for field in facts.get("unresolved_fields") or []
    ]
    facts_text = json.dumps(discovery_answers, ensure_ascii=False, separators=(",", ":"))
    unresolved_text = ", ".join(unresolved_fields) or "tidak ada"
    return (
        "## Arthur Persistent Build State\n"
        f"- build_id: {draft.id}\n"
        f"- intent: {draft.intent}\n"
        f"- workflow_state: {draft.workflow_state}\n"
        f"- confirmation_status: {draft.confirmation_status}\n"
        f"- state_version: {draft.state_version}\n"
        "### Fakta discovery canonical (gunakan kembali; jangan ditanyakan ulang)\n"
        f"{facts_text or '{}'}\n"
        f"### Field yang benar-benar belum selesai\n- {unresolved_text}\n"
        "### Evidence user terbaru\n"
        f"{evidence_lines}\n"
        "### Pertanyaan canonical yang sudah pernah diajukan\n"
        f"{question_lines}\n"
        "Jangan meminta user mengulang evidence di atas. Jika sebuah jawaban mengubah fakta induk, "
        "jelaskan invalidation sebelum menanyakan turunannya kembali."
    )
