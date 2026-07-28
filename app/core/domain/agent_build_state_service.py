"""Restart-safe shadow/runtime state for Arthur agent-building workflows."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_build_draft import AgentBuildDraft

_QUESTION_RE = re.compile(r"(?:^|\n|(?<=[.!]))\s*([^\n?]{4,300}\?)", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"https?://[^\s<>'\"]+")
_SPACE_RE = re.compile(r"\s+")
_MANIFEST_WRAPPER_FIELDS = {"_evidence", "user_confirmed"}

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


def _canonical_manifest_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonical_manifest_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _MANIFEST_WRAPPER_FIELDS
            and item not in (None, "", [], {})
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_manifest_value(item) for item in value]
    if isinstance(value, str):
        return _SPACE_RE.sub(" ", value).strip()
    return value


def canonical_discovery_manifest(discovery_answers: Any) -> dict[str, Any]:
    """Return the stable, confirmation-independent agent requirement manifest."""
    if not isinstance(discovery_answers, dict):
        return {}
    canonical = _canonical_manifest_value(discovery_answers)
    return canonical if isinstance(canonical, dict) else {}


def _manifest_hash(manifest: dict[str, Any]) -> str:
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def question_topic(text: str) -> str | None:
    canonical = canonical_question(text)
    for topic, terms in _QUESTION_TOPIC_TERMS:
        if any(term in canonical for term in terms):
            return topic
    return None


def extract_questions(reply: str, *, max_questions: int = 3) -> list[str]:
    source = str(reply or "")
    # A discovery question may contain a sample dialogue such as
    # ``Customer: Apakah stok ada?``. The question mark inside inline code is
    # example content, not the end of the surrounding user-facing question.
    # Mask only those question marks while preserving string offsets so guards
    # never delete half a sentence and leak a suffix such as "` lalu Agent: ...".
    masked_chars = list(source)
    for code_match in _INLINE_CODE_RE.finditer(source):
        for index in range(code_match.start(), code_match.end()):
            if masked_chars[index] == "?":
                masked_chars[index] = " "
    # Query strings are data, not user-facing questions. Without masking this
    # separator, the repeated-question guard deletes the URL prefix through
    # ``?`` and leaks only ``t=<oauth-token>`` to WhatsApp.
    for url_match in _URL_RE.finditer(source):
        for index in range(url_match.start(), url_match.end()):
            if masked_chars[index] == "?":
                masked_chars[index] = " "
    masked = "".join(masked_chars)

    found: list[str] = []
    seen: set[str] = set()
    for match in _QUESTION_RE.finditer(masked):
        start, end = match.span(1)
        question = _SPACE_RE.sub(" ", source[start:end]).strip()
        canonical = canonical_question(question)
        if canonical and canonical not in seen:
            seen.add(canonical)
            found.append(question)
    return found[:max_questions]


def _remove_question_with_wrappers(reply: str, question: str) -> str:
    """Remove one question and its matching Markdown wrapper atomically."""
    start = reply.find(question)
    if start < 0:
        return reply
    end = start + len(question)
    wrapped_prefix = next(
        (marker for marker in ("**", "__") if question.startswith(marker)),
        "",
    )
    if wrapped_prefix and reply[end : end + len(wrapped_prefix)] == wrapped_prefix:
        end += len(wrapped_prefix)
    elif start >= 2 and reply[start - 2 : start] in {"**", "__"}:
        marker = reply[start - 2 : start]
        if reply[end : end + 2] == marker:
            start -= 2
            end += 2
    cleaned = reply[:start] + reply[end:]
    return re.sub(r"(?m)^[ \t]+(?=\S)", "", cleaned)


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
        cleaned = _remove_question_with_wrappers(cleaned, question)
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


def guard_single_discovery_question(reply: str) -> tuple[str, list[str]]:
    """Keep at most one user-facing question in a discovery WhatsApp reply."""
    questions = extract_questions(reply, max_questions=12)
    if len(questions) <= 1:
        return reply, []

    cleaned = str(reply or "")
    removed: list[str] = []
    for question in questions[1:]:
        cleaned = _remove_question_with_wrappers(cleaned, question)
        removed.append(question)
    cleaned = re.sub(r"(?m)^\s*[-*\d.)]*\s*$", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
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
    candidate = dict(prior)
    for field, value in incoming.items():
        if field in {"_evidence", "user_confirmed"}:
            continue
        candidate[field] = value
    confirmed_hash = str(snapshot.get("confirmed_manifest_hash") or "")
    if confirmed_hash:
        return _manifest_hash(canonical_discovery_manifest(candidate)) == confirmed_hash
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
    *,
    confirmation_message_id: str | None = None,
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
        canonical_file_capability = str(
            discovery.get("file_capability")
            or normalized.get("file_capability")
            or ""
        ).strip().lower()
        if canonical_file_capability:
            canonical_answers["file_capability"] = canonical_file_capability

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

        agent_manifest = canonical_discovery_manifest(canonical_answers)
        manifest_hash = _manifest_hash(agent_manifest)
        prior_manifest_hash = str(facts.get("manifest_hash") or "")
        prior_manifest_version = int(facts.get("manifest_version") or 0)
        manifest_version = (
            prior_manifest_version
            if prior_manifest_hash == manifest_hash and prior_manifest_version > 0
            else prior_manifest_version + 1
        )

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
            "agent_manifest": agent_manifest,
            "manifest_hash": manifest_hash,
            "manifest_version": manifest_version,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if complete:
            facts["confirmed_manifest_hash"] = manifest_hash
            facts["confirmed_manifest_version"] = manifest_version
            facts["confirmation_message_id"] = confirmation_message_id
        else:
            facts.pop("confirmed_manifest_hash", None)
            facts.pop("confirmed_manifest_version", None)
            facts.pop("confirmation_message_id", None)
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
    if "create_wa_dev_trial_link" in tool_names and succeeded("create_wa_dev_trial_link"):
        return "demo_ready"
    if "send_agent_wa_qr" in tool_names and succeeded("send_agent_wa_qr"):
        return "demo_ready"
    if any("auth" in name or "oauth" in name for name in tool_names):
        return "integration_auth_pending"
    if "create_spreadsheet" in tool_names:
        sheet_write_tools = {
            "modify_sheet_values",
            "append_table_rows",
            "create_sheet",
        }
        if sheet_write_tools.intersection(tool_names) and "update_agent" in tool_names:
            return "verifying"
        return "integration_setup"
    if any(
        name in {"update_agent", "set_agent_memory"} and succeeded(name)
        for name in tool_names
    ):
        return "verifying"
    # The latest planning result is the corrected source of truth when a model
    # calls plan_agent more than once in the same run.
    for step in reversed(steps or []):
        if str(step.get("tool") or "") != "plan_agent":
            continue
        result = _step_result(step)
        status = str(result.get("plan_status") or result.get("status") or "").lower()
        if status == "ready":
            return "ready_to_create"
        if status in {"needs_clarification", "clarification"}:
            return "discovery"
        break
    if extract_questions(final_reply):
        return "discovery"
    return current_state


def _first_result_value(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in keys and item not in (None, "", [], {}):
                return str(item)
        for item in value.values():
            found = _first_result_value(item, keys)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _first_result_value(item, keys)
            if found:
                return found
    return ""


def _integration_artifact_status_from_steps(
    draft: AgentBuildDraft,
    steps: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    integrations = dict(draft.integration_status_json or {})
    artifacts = dict(draft.artifact_status_json or {})
    google = dict(integrations.get("google_workspace") or {})
    sheet = dict(artifacts.get("google_sheet") or {})

    for step in steps or []:
        tool_name = str(step.get("tool") or "")
        result = _step_result(step)
        result_text = str(step.get("result") or "").casefold()
        failed = (
            result.get("success") is False
            or bool(result.get("error"))
            or "[error]" in result_text
        )
        if tool_name == "generate_google_auth_link" and not failed:
            google.update(
                {
                    "status": "auth_pending",
                    "auth_link_issued": True,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        elif tool_name == "create_spreadsheet" and not failed:
            spreadsheet_id = _first_result_value(
                result,
                {"spreadsheet_id", "spreadsheetId", "file_id", "id"},
            )
            spreadsheet_url = _first_result_value(
                result,
                {"spreadsheet_url", "web_view_link", "url"},
            )
            if not spreadsheet_id:
                id_match = re.search(
                    r"(?:/spreadsheets/d/|\bID:\s*)([A-Za-z0-9_-]{10,})",
                    str(step.get("result") or ""),
                    flags=re.IGNORECASE,
                )
                spreadsheet_id = id_match.group(1) if id_match else ""
            if not spreadsheet_url:
                url_match = re.search(
                    r"https://docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]+"
                    r"(?:/[^\s\"']*)?",
                    str(step.get("result") or ""),
                    flags=re.IGNORECASE,
                )
                spreadsheet_url = url_match.group(0) if url_match else ""
            sheet.update(
                {
                    "status": "resource_created",
                    "spreadsheet_id": spreadsheet_id,
                    "spreadsheet_url": spreadsheet_url,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            google["status"] = "resource_ready"
        elif tool_name in {
            "modify_sheet_values",
            "append_table_rows",
            "create_sheet",
        } and not failed:
            sheet.update(
                {
                    "status": "write_verified",
                    "write_verified": True,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        elif tool_name == "update_agent" and not failed and sheet.get("spreadsheet_id"):
            sheet["bound_to_agent"] = True
            google["status"] = "configured"

    if google:
        integrations["google_workspace"] = google
    if sheet:
        artifacts["google_sheet"] = sheet
    return integrations, artifacts


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
        confirmation_message_id=draft.last_inbound_message_id,
    )
    integration_status, artifact_status = _integration_artifact_status_from_steps(
        draft,
        steps,
    )

    expected_version = int(draft.state_version or 1)
    values: dict[str, Any] = {
        "workflow_state": new_state,
        "facts_json": facts,
        "confirmation_status": confirmation_status,
        "question_history_json": history,
        "skill_versions_json": dict(skill_versions),
        "integration_status_json": integration_status,
        "artifact_status_json": artifact_status,
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
    integration_text = json.dumps(
        draft.integration_status_json or {},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    artifact_text = json.dumps(
        draft.artifact_status_json or {},
        ensure_ascii=False,
        separators=(",", ":"),
    )
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
        f"### Status integrasi terverifikasi\n{integration_text}\n"
        f"### Status resource/artifact terverifikasi\n{artifact_text}\n"
        "### Evidence user terbaru\n"
        f"{evidence_lines}\n"
        "### Pertanyaan canonical yang sudah pernah diajukan\n"
        f"{question_lines}\n"
        "Jangan meminta user mengulang evidence di atas. Jika sebuah jawaban mengubah fakta induk, "
        "jelaskan invalidation sebelum menanyakan turunannya kembali."
    )
