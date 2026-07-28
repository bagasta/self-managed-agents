"""Deterministic discovery gate for Arthur's agent-creation flow."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from sqlalchemy import select

from app.models.message import Message


_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "id": "context_goal",
        "label": "Grup 1: Konteks & Tujuan",
        "fields": ("problem", "usage_context", "agent_name", "audience"),
    },
    {
        "id": "agent_behavior",
        "label": "Grup 2: Perilaku Agent",
        "fields": (
            "main_tasks",
            "capabilities",
            "prohibited_actions",
            "allowed_actions",
            "tone_style",
            "ideal_conversations",
            "avoided_conversations",
        ),
    },
    {
        "id": "escalation_knowledge_boundary",
        "label": "Grup 3: Eskalasi & Batasan Pengetahuan",
        "fields": ("unknown_handling", "escalation_target"),
    },
    {
        "id": "data_knowledge",
        "label": "Grup 4: Data & Pengetahuan",
        "fields": ("knowledge_sources", "sensitive_data_policy"),
    },
    {
        "id": "scale_integration",
        "label": "Grup 5: Skala & Integrasi",
        "fields": (
            "whatsapp_scale",
            "daily_chat_volume",
            "integrations",
            "expected_outputs",
            "vision_requirement",
        ),
    },
    {
        "id": "go_live",
        "label": "Grup 6: Sebelum Go-Live",
        "fields": ("go_live_approver",),
    },
)

_QUESTIONS: dict[str, str] = {
    "problem": "Bagian mana yang paling bikin kamu kewalahan dan ingin dibantu agent ini?",
    "usage_context": "Agent ini akan dipakai untuk kebutuhan pribadi atau bisnis?",
    "agent_name": "Mau kasih nama apa untuk agent-nya?",
    "audience": "Nanti yang chat dengan agent ini siapa: kamu, tim internal, atau customer?",
    "main_tasks": "Kalau ada chat masuk, apa saja yang perlu agent kerjakan sampai urusannya selesai?",
    "capabilities": (
        "Agent ini hanya chat teks, perlu menerima file/gambar, perlu membuat file/laporan, "
        "atau perlu menerima sekaligus membuat file?"
    ),
    "prohibited_actions": "Keputusan atau tindakan apa yang harus selalu ditangani manusia, bukan agent?",
    "allowed_actions": "Apa yang BOLEH agent lakukan dan sampai batas wewenang mana? Contoh: mencatat pesanan boleh, mengonfirmasi pembayaran tidak boleh.",
    "tone_style": "Tone dan gaya bahasanya bagaimana? Contoh: santai, bahasa Indonesia, boleh emoji secukupnya, tetapi tetap sopan.",
    "ideal_conversations": (
        "Berikan 2-3 contoh percakapan ideal. Contoh format: `Customer: Apakah stok masih ada?` lalu "
        "`Agent: Saya cek dari sumber yang tersedia; kalau belum pasti saya eskalasikan ke admin.`"
    ),
    "avoided_conversations": (
        "Berikan contoh percakapan yang harus dihindari/red line. Contoh: customer meminta diskon, lalu agent "
        "langsung menjanjikan diskon tanpa izin Owner."
    ),
    "unknown_handling": "Kalau jawabannya tidak tersedia atau tidak pasti, agent harus melakukan apa?",
    "escalation_target": (
        "Kalau perlu bantuan manusia, siapa yang harus menerima eskalasinya di WhatsApp?"
    ),
    "knowledge_sources": "Perlu pengetahuan/RAG tambahan? Jawab ya atau tidak; jika ya, sebutkan sumbernya seperti file, link, Google Sheet, atau database.",
    "sensitive_data_policy": "Apakah ada data sensitif seperti nama, kontak, atau transaksi? Jelaskan aturan kerahasiaan dan retensinya, atau nyatakan tidak ada.",
    "whatsapp_scale": (
        "Agent dipakai pada satu atau banyak nomor WhatsApp? Jelaskan juga polanya: satu nomor melayani banyak user "
        "sekaligus seperti CS, atau setiap user memiliki nomor sendiri."
    ),
    "daily_chat_volume": (
        "Berapa estimasi volume chat per hari? Boleh berupa kategori seperti puluhan/ratusan "
        "atau rentang, misalnya 50-100 percakapan/hari."
    ),
    "integrations": "Perlu integrasi ke sistem lain seperti Google Workspace, CRM, payment gateway, atau database? Nyatakan juga jika tidak perlu.",
    "expected_outputs": "Output konkretnya apa? Contoh: tambah satu baris spreadsheet, buat PDF, atau kirim notifikasi ke admin. Berikan minimal satu contoh.",
    "vision_requirement": "Apakah agent perlu menerima atau memahami gambar/foto/vision? Jawab ya atau tidak dan beri contoh jika ya.",
    "go_live_approver": "Siapa nama atau role yang akan review dan approve agent sebelum dipakai sungguhan?",
    "user_confirmed": "Saya akan merangkum seluruh jawaban discovery. Setelah rangkumannya benar, minta user menyatakan setuju sebelum agent dibuat.",
}

# This priority is deliberately independent from the legacy six-group display
# order. It is emitted in shadow mode first so production conversations can be
# evaluated before it becomes the authoritative question selector.
_MATERIAL_LEARNING_PRIORITY: tuple[str, ...] = (
    "problem",
    "usage_context",
    "main_tasks",
    "audience",
    "prohibited_actions",
    "unknown_handling",
    "escalation_target",
    "integrations",
    "capabilities",
    "agent_name",
)

_LEARNING_GOALS: dict[str, str] = {
    "problem": "understand_user_pain_point",
    "usage_context": "understand_personal_or_work_context",
    "agent_name": "identify_agent_name",
    "audience": "identify_conversation_audience",
    "main_tasks": "understand_end_to_end_workflow",
    "capabilities": "decide_text_and_file_capabilities",
    "prohibited_actions": "define_human_only_decisions",
    "unknown_handling": "define_unknown_answer_behavior",
    "escalation_target": "define_escalation_conditions_and_recipient",
    "integrations": "identify_required_external_integrations",
    "user_confirmed": "confirm_final_requirements_summary",
}

_UNRESOLVED_RISKS: dict[str, str] = {
    "problem": "the agent may optimize the wrong problem",
    "usage_context": "personal and business safety requirements may be applied incorrectly",
    "agent_name": "the created agent cannot be identified clearly",
    "audience": "tone, access, and workflow may target the wrong users",
    "main_tasks": "the agent may stop before completing the user's real workflow",
    "capabilities": "the runtime may lack required text or file behavior",
    "prohibited_actions": "the agent may take a decision that must remain human-controlled",
    "unknown_handling": "the agent may invent an answer when evidence is unavailable",
    "escalation_target": "a blocked business conversation may not reach the right human",
    "integrations": "the agent may promise or miss an external system action",
    "user_confirmed": "the agent could be created from an unapproved interpretation",
}

_USAGE_CONTEXT_ALIASES = {
    "personal": "personal",
    "pribadi": "personal",
    "sendiri": "personal",
    "pekerjaan": "work",
    "kerja": "work",
    "bisnis": "work",
    "business": "work",
    "work": "work",
    "professional": "work",
}

_IGNORED_OPERATIONAL_HOUR_FIELDS = {
    "active_hours",
    "agent_active_hours",
    "business_hours",
    "jam_aktif",
    "jam_operasional",
    "operational_hours",
    "operating_hours",
}

_EVIDENCE_KEY = "_evidence"
_CONFIRMED_SUMMARY_PREFIX = "Rangkuman Arthur yang dikonfirmasi user: "
_EXPLICIT_CONFIRMATION_MARKERS = (
    "sudah sesuai",
    "sudah benar",
    "semuanya sesuai",
    "saya setuju",
    "setuju dibuat",
    "setuju, buat",
    "lanjut buat",
    "lanjutkan buat",
    "langsung buat",
    "langsung saja buat",
    "oke buat",
    "buat sekarang",
)
_SHORT_EXPLICIT_CONFIRMATIONS = {
    "ok",
    "oke",
    "sudah",
    "sesuai",
    "setuju",
    "buat",
    "buatkan",
    "buat agentnya",
}
_DELEGATION_MARKERS = (
    "atur aja",
    "sesuaikan aja",
    "kamu atur",
    "kamu sesuaikan",
    "lu atur",
    "terserah kamu",
    "buat aja",
    "buatkan aja",
    "saya percayain",
    "cocok kaya gitu",
    "cocok kayak gitu",
    "cocok seperti itu",
)
_DELEGATABLE_FIELDS = {
    "ideal_conversations",
    "avoided_conversations",
    "tone_style",
    "expected_outputs",
}
_OPTIONAL_DISCOVERY_FIELDS = {
    # Data minimization and secret-handling are enforced by the platform safety
    # baseline. Ask for a business-specific retention policy when relevant, but
    # do not restart an otherwise confirmed agent build when the owner did not
    # define one.
    "sensitive_data_policy",
    # One agent is attached to one WhatsApp number by platform contract.
    # Daily volume and audience are useful discovery facts; forcing owners to
    # restate the one-number/many-customers topology adds no build permission
    # and caused otherwise complete CS builds to loop.
    "whatsapp_scale",
    # These details improve an agent, but none grants an integration,
    # external action, or permission. Requiring every owner to design sample
    # dialogues, quantify volume, and nominate a reviewer turned a short
    # WhatsApp intake into a questionnaire and repeatedly stalled creation.
    "allowed_actions",
    "tone_style",
    "ideal_conversations",
    "avoided_conversations",
    "knowledge_sources",
    "daily_chat_volume",
    "expected_outputs",
    "vision_requirement",
    "go_live_approver",
}
_PERSONAL_OPTIONAL_DISCOVERY_FIELDS = {
    # Volume is useful for capacity planning, but it does not change the
    # permission or minimum configuration of a personal assistant.
    "daily_chat_volume",
}
_FILE_CAPABILITY_VALUES = {"text_only", "receive_only", "generate", "both"}
_EVIDENCE_STOPWORDS = {
    "agent",
    "agen",
    "saya",
    "kami",
    "kamu",
    "user",
    "yang",
    "dan",
    "atau",
    "untuk",
    "dengan",
    "dari",
    "dalam",
    "mau",
    "ingin",
    "buat",
    "bikin",
    "perlu",
    "akan",
    "bisa",
    "jadi",
    "sebagai",
}


class DiscoveryEvidenceUnavailable(RuntimeError):
    """Raised when runtime conversation evidence cannot be loaded safely."""


def owner_escalation_phone_selected(user_messages: list[str] | None) -> bool:
    """Resolve the latest explicit escalation-phone choice from user history."""
    owner_markers = (
        "nomor wa saya sendiri",
        "nomer wa saya sendiri",
        "nomor saya sendiri",
        "nomer saya sendiri",
        "wa saya sendiri",
        "nomor wa gua sendiri",
        "nomer wa gua sendiri",
        "nomor ini",
        "nomer ini",
        "ke nomor ini",
        "ke nomer ini",
        "ke nomor saya",
        "ke nomer saya",
        "eskalasi ke saya",
        "eskalasi ke gua",
        "eskalasi ke aku",
        "teruskan ke saya",
        "teruskan ke gua",
        "teruskan ke aku",
        "info ke saya",
        "info ke gua",
        "info ke aku",
    )
    for message in reversed(user_messages or []):
        text = _normalize_evidence_text(message)
        if re.search(
            r"\b(?:bukan|jangan|tidak|ga|gak|nggak)\b(?:\s+\w+){0,4}\s+"
            r"\b(?:eskalasi|teruskan|info)\b(?:\s+\w+){0,4}\s+"
            r"\b(?:saya|gua|aku)\b",
            text,
        ):
            return False
        if re.search(
            r"\b(?:bukan|jangan|tidak|ga|gak|nggak)\b(?:\s+\w+){0,4}\s+"
            r"\b(?:nomor|nomer|wa)\s+(?:ini|saya)\b",
            text,
        ):
            return False
        if any(marker in text for marker in owner_markers):
            return True
        if re.search(r"(?:\+?62|0)\d[\d\s-]{7,16}", str(message or "")):
            return False
        if re.search(
            r"\b(nomor|nomer|wa)\b(?:\s+\w+){0,5}\s+\b(admin|operator|manager|tim)\b",
            text,
        ):
            return False
    return False


def bind_owner_escalation_phone(
    discovery_answers: Any,
    *,
    user_messages: list[str] | None,
    owner_phone: str,
) -> Any:
    """Bind "nomor saya sendiri" to the authenticated session owner.

    This prevents a model typo from silently changing the escalation recipient.
    Explicitly supplied admin/operator numbers are left unchanged.
    """
    trusted_phone = re.sub(r"\D", "", str(owner_phone or ""))
    if len(trusted_phone) < 8 or not owner_escalation_phone_selected(user_messages):
        return discovery_answers
    answers, parse_error = _parse_answers(discovery_answers)
    if parse_error:
        return discovery_answers
    target = answers.get("escalation_target")
    if isinstance(target, dict):
        target = dict(target)
        phone_key = next(
            (
                key
                for key in ("whatsapp_number", "phone", "number", "operator_phone")
                if key in target
            ),
            "whatsapp_number",
        )
        target[phone_key] = trusted_phone
        answers["escalation_target"] = target
    elif _is_answered(target):
        answers["escalation_target"] = {
            "conditions": str(target),
            "recipient": "Owner",
            "whatsapp_number": trusted_phone,
        }
    elif _is_answered(answers.get("unknown_handling")):
        answers["escalation_target"] = {
            "conditions": str(answers["unknown_handling"]),
            "recipient": "Owner",
            "whatsapp_number": trusted_phone,
        }
        evidence = answers.get(_EVIDENCE_KEY)
        evidence = dict(evidence) if isinstance(evidence, dict) else {}
        if not _is_answered(evidence.get("escalation_target")):
            owner_message = next(
                (
                    str(message).strip()
                    for message in reversed(user_messages or [])
                    if owner_escalation_phone_selected([str(message)])
                ),
                "",
            )
            if owner_message:
                evidence["escalation_target"] = owner_message
        if evidence:
            answers[_EVIDENCE_KEY] = evidence
    return answers


def infer_low_risk_discovery_facts(
    discovery_answers: Any,
    *,
    user_messages: list[str] | None,
) -> Any:
    """Derive only context directly entailed by the user's own workflow words."""
    answers, parse_error = _parse_answers(discovery_answers)
    if parse_error:
        return discovery_answers
    supplied_usage_context = _normalize_usage_context(answers.get("usage_context"))
    if supplied_usage_context == "personal":
        return discovery_answers

    work_pattern = re.compile(
        r"\b(?:bisnis|business|usaha|toko|customer|pelanggan|pembeli|"
        r"customer service|cs|admin|order|pesanan|spreadsheet|google sheets?)\b"
    )
    personal_pattern = re.compile(r"\b(?:personal|pribadi)\b")
    supporting_message = ""
    for message in reversed(user_messages or []):
        normalized = _normalize_evidence_text(message)
        # The latest explicit personal statement wins over an older work-like
        # keyword. Do not repair model evidence across a real user correction.
        if personal_pattern.search(normalized):
            return discovery_answers
        if work_pattern.search(normalized):
            supporting_message = str(message).strip()
            break
    if not supporting_message:
        return discovery_answers
    if supplied_usage_context not in {"", "work"}:
        return discovery_answers

    answers["usage_context"] = "work"
    evidence = answers.get(_EVIDENCE_KEY)
    evidence = dict(evidence) if isinstance(evidence, dict) else {}
    # Replace model-authored or missing evidence with immutable user wording.
    # A small model often infers the correct value but cites its own paraphrase,
    # which made the validator re-ask a fact it had already normalized.
    evidence["usage_context"] = supporting_message
    answers[_EVIDENCE_KEY] = evidence
    return answers


def _is_explicit_confirmation_message(value: Any) -> bool:
    normalized = _normalize_evidence_text(value)
    if re.search(
        r"\b(?:belum|tidak|nggak|gak|jangan|bukan)\b(?:\s+\w+){0,5}\s+"
        r"\b(?:sesuai|setuju|benar|buat|buatkan)\b",
        normalized,
    ):
        return False
    conversational = bool(
        re.fullmatch(
            r"(?:(?:ya+|iya|sip|siap|ok|oke|mantap|gas)\s+)*"
            r"(?:sudah\s+)?(?:sesuai|setuju|benar)"
            r"(?:\s+(?:ya+|nih|dong))?",
            normalized,
        )
    )
    return (
        normalized in _SHORT_EXPLICIT_CONFIRMATIONS
        or conversational
        or any(
            _normalize_evidence_text(marker) in normalized
            for marker in _EXPLICIT_CONFIRMATION_MARKERS
        )
    )


async def load_discovery_user_messages(
    db_factory: Any,
    session_id: str | None,
    *,
    current_user_message: str = "",
) -> list[str]:
    """Load persisted evidence used to prove confirmed discovery answers.

    Arthur's model supplies ``discovery_answers``. Without checking those values
    against persisted user messages, the model can fill unanswered fields with
    plausible assumptions and still satisfy the structural validator. The one
    allowed non-user source is Arthur's immediately preceding summary when the
    latest user message explicitly confirms that summary.
    """
    if db_factory is None or not session_id:
        return []
    try:
        parsed_session_id = uuid.UUID(str(session_id))
        async with db_factory() as db:
            rows = (
                await db.execute(
                    select(Message.role, Message.content)
                    .where(
                        Message.session_id == parsed_session_id,
                        Message.role.in_(("user", "agent")),
                        Message.content.is_not(None),
                    )
                    .order_by(Message.timestamp.asc(), Message.step_index.asc())
                )
            ).all()
    except Exception as exc:  # Fail closed: never create from unverified assumptions.
        raise DiscoveryEvidenceUnavailable(
            "Riwayat jawaban user belum dapat diverifikasi."
        ) from exc
    conversation = [
        (str(row[0]), str(row[1]).strip())
        for row in rows
        if len(row) >= 2 and str(row[1] or "").strip()
    ]
    current_message = str(current_user_message or "").strip()
    if current_message and (
        not conversation
        or conversation[-1] != ("user", current_message)
    ):
        # Builder tools use a separate DB session. The active inbound message is
        # still uncommitted there, so explicitly attach the trusted runtime input
        # to the evidence stream. Treating it as the next conversation row also
        # binds confirmations such as "setuju" to Arthur's immediately preceding
        # summary instead of asking for the same confirmation forever.
        conversation.append(("user", current_message))
    evidence_messages: list[str] = []
    for index, (role, content) in enumerate(conversation):
        if role != "user":
            continue
        previous = conversation[index - 1] if index > 0 else None
        if previous and previous[0] == "agent":
            agent_text = previous[1]
            if _is_explicit_confirmation_message(content):
                # A summary becomes evidence only when the immediately
                # following user turn explicitly confirms it.
                evidence_messages.append(
                    _CONFIRMED_SUMMARY_PREFIX + agent_text
                )
            elif (
                "?" in agent_text
                and len(_normalize_evidence_text(content).split()) <= 12
            ):
                # Preserve the question context for terse replies such as
                # "Perlu" or "puluhan". Keep the raw user reply as a separate
                # final item so confirmation still must be the latest message.
                evidence_messages.append(
                    f"Pertanyaan Arthur: {agent_text}\nJawaban user: {content}"
                )
        evidence_messages.append(content)
    return evidence_messages


def _parse_answers(value: Any) -> tuple[dict[str, Any], str | None]:
    if isinstance(value, dict):
        return dict(value), None
    if value in (None, ""):
        return {}, None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            return {}, f"discovery_answers bukan JSON valid: {exc.msg}"
        if isinstance(parsed, dict):
            return parsed, None
    return {}, "discovery_answers harus berupa JSON object."


def _is_answered(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _normalize_evidence_text(value: Any) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[^\w+]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _evidence_quotes(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if isinstance(value, dict):
        collected: list[str] = []
        for key in ("quote", "quotes", "user_quote", "user_quotes"):
            collected.extend(_evidence_quotes(value.get(key)))
        return collected
    return []


def _evidence_quote_candidates(value: Any) -> list[str]:
    """Accept natural evidence wrappers while still matching persisted text.

    Small models commonly send values such as ``Pesan user: 'Professional'``.
    The immutable source remains the database message; this helper only extracts
    the quoted span that should be looked up in that source.
    """
    candidates: list[str] = []
    for raw in _evidence_quotes(value):
        variants = [raw]
        variants.extend(
            match.strip()
            for match in re.findall(r"""['"`]([^'"`]{2,1000})['"`]""", raw)
            if match.strip()
        )
        without_prefix = re.sub(
            r"^\s*(?:pesan|jawaban|kutipan)\s+user\s*:\s*",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip().strip("'\"`")
        if without_prefix:
            variants.append(without_prefix)
        for candidate in variants:
            candidate = candidate.strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _verified_evidence_quotes(
    field: str,
    evidence: dict[str, Any],
    user_messages: list[str],
) -> list[str]:
    verified: list[str] = []
    for quote in _evidence_quote_candidates(evidence.get(field)):
        normalized_quote = _normalize_evidence_text(quote)
        if len(normalized_quote) < 3:
            continue
        for message in user_messages:
            normalized_message = _normalize_evidence_text(message)
            exact_match = normalized_quote in normalized_message
            if field == "user_confirmed":
                matched = exact_match
            else:
                matched = exact_match or _evidence_quote_matches_message(quote, message)
            if matched and message not in verified:
                # Always return the persisted source text, never the model's
                # paraphrase. Downstream validation still checks that this
                # source actually supports the normalized answer.
                verified.append(message)
    return verified


def _answer_evidence_tokens(value: Any) -> set[str]:
    if isinstance(value, dict):
        text = " ".join(str(item) for item in value.values())
    elif isinstance(value, (list, tuple, set)):
        text = " ".join(
            " ".join(str(item) for item in entry.values())
            if isinstance(entry, dict)
            else str(entry)
            for entry in value
        )
    else:
        text = str(value or "")
    return {
        token
        for token in _normalize_evidence_text(text).split()
        if len(token) >= 3 and token not in _EVIDENCE_STOPWORDS
    }


def _evidence_quote_matches_message(quote: str, message: str) -> bool:
    """Resolve a close paraphrase back to an immutable persisted user message."""
    quote_tokens = _answer_evidence_tokens(quote)
    message_tokens = _answer_evidence_tokens(message)
    if not quote_tokens or not message_tokens:
        return False

    # Never fuzzy-match a changed phone, amount, count, or other numeric fact.
    quote_numbers = {token for token in quote_tokens if any(char.isdigit() for char in token)}
    if quote_numbers and not quote_numbers.issubset(message_tokens):
        return False

    shared = quote_tokens & message_tokens
    if len(shared) >= 2:
        quote_coverage = len(shared) / len(quote_tokens)
        message_coverage = len(shared) / len(message_tokens)
        return quote_coverage >= 0.5 or message_coverage >= 0.5

    # Short replies such as "saya sendiri" or "mungkin ratusan" often carry
    # one distinctive fact. Accept only when that fact covers the whole short
    # persisted reply; the answer-support check below still has to pass.
    return (
        len(shared) == 1
        and len(message_tokens) == 1
        and len(next(iter(shared))) >= 5
        and len(_normalize_evidence_text(message).split()) <= 4
    )


def _evidence_supports_answer(field: str, answer: Any, quotes: list[str]) -> bool:
    """Reject a real but unrelated quote attached to an invented answer."""
    quote_text = _normalize_evidence_text(" ".join(quotes))
    if not quote_text:
        return False
    if field == "agent_name":
        return _normalize_evidence_text(answer) in quote_text
    if field == "usage_context":
        if str(answer or "") == "personal":
            return bool(re.search(r"\b(personal|pribadi|sendiri)\b", quote_text))
        return bool(
            re.search(
                r"\b(pekerjaan|kerja|bisnis|business|work|profesional|usaha|toko|"
                r"customer|pelanggan|pembeli|customer service|cs|admin|order|pesanan|"
                r"spreadsheet|google sheets?)\b",
                quote_text,
            )
        )
    if field == "daily_chat_volume":
        answer_numbers = re.findall(r"\d+", _normalize_evidence_text(answer))
        quote_numbers = re.findall(r"\d+", quote_text)
        if answer_numbers and quote_numbers:
            return any(
                answer_number == quote_number
                or quote_number.startswith(answer_number)
                or answer_number.startswith(quote_number)
                for answer_number in answer_numbers
                for quote_number in quote_numbers
            )
    if field == "whatsapp_scale":
        answer_text = _normalize_evidence_text(answer)
        if "inbound" in answer_text and re.search(
            r"\b(nunggu|menunggu|chat duluan|menghubungi duluan|inbound)\b",
            quote_text,
        ):
            return True
    if field == "escalation_target" and (
        re.search(r"\b(nomor|nomer|wa)\b.*\b(saya|sendiri)\b", quote_text)
        or re.search(
            r"\b(?:eskalasi|teruskan|info)\b(?:\s+\w+){0,4}\s+\b(?:saya|gua|aku)\b",
            quote_text,
        )
    ):
        return True
    if field in _DELEGATABLE_FIELDS and any(
        marker in quote_text for marker in _DELEGATION_MARKERS
    ):
        # Arthur may draft safe presentation details when the user delegates
        # them, but the full resulting summary still needs a fresh explicit
        # confirmation before create_agent can pass.
        return True
    answer_tokens = _answer_evidence_tokens(answer)
    quote_tokens = _answer_evidence_tokens(quote_text)
    return bool(answer_tokens & quote_tokens)


def _confirmation_evidence_is_valid(
    evidence: dict[str, Any],
    user_messages: list[str],
    *,
    require_confirmed_summary: bool = False,
) -> bool:
    if not user_messages:
        return False
    latest_user_message = _normalize_evidence_text(user_messages[-1])
    if not _is_explicit_confirmation_message(latest_user_message):
        return False
    if require_confirmed_summary and not any(
        str(message).startswith(_CONFIRMED_SUMMARY_PREFIX)
        for message in user_messages[:-1]
    ):
        return False
    return any(
        _normalize_evidence_text(quote) in latest_user_message
        for quote in _verified_evidence_quotes("user_confirmed", evidence, user_messages)
    )


def _is_safe_confirmation_continuation(value: Any) -> bool:
    normalized = _normalize_evidence_text(value)
    return normalized in {
        "ok",
        "oke",
        "setuju",
        "lanjut",
        "lanjutkan",
        "buat",
        "buatkan",
        "langsung buat",
        "buat agentnya",
        "langsung buat agentnya",
        "ok langsung buat agentnya",
        "oke langsung buat agentnya",
    }


def _normalize_usage_context(value: Any) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    if text in _USAGE_CONTEXT_ALIASES:
        return _USAGE_CONTEXT_ALIASES[text]
    for alias, normalized in _USAGE_CONTEXT_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", text):
            return normalized
    return ""


def _confirmed(value: Any) -> bool:
    if value is True:
        return True
    return str(value or "").strip().lower() in {
        "true",
        "yes",
        "ya",
        "iya",
        "sudah",
        "sesuai",
        "sudah sesuai",
        "sudah benar",
        "setuju",
        "confirmed",
        "dikonfirmasi",
    }


def _field_is_optional(field: str, usage_context: str) -> bool:
    return field in _OPTIONAL_DISCOVERY_FIELDS or (
        usage_context == "personal"
        and field in _PERSONAL_OPTIONAL_DISCOVERY_FIELDS
    )


def discovery_file_capability(answers: dict[str, Any] | None) -> str:
    """Derive only an explicit, user-confirmed file decision from discovery.

    Product details or a generic "menjawab pertanyaan" capability are not enough
    to decide this. The answer must either contain a concrete file workflow or a
    global text-only/no-file statement.
    """
    data = answers if isinstance(answers, dict) else {}
    explicit = str(data.get("file_capability") or "").strip().lower()
    if explicit == "enabled":
        explicit = "both"
    if explicit == "not_needed":
        explicit = "text_only"
    if explicit not in _FILE_CAPABILITY_VALUES:
        explicit = ""

    receive_text = _normalize_evidence_text(
        " ".join(
            str(data.get(field) or "")
            for field in ("main_tasks", "capabilities", "knowledge_sources", "vision_requirement")
        )
    )
    generate_text = _normalize_evidence_text(
        " ".join(
            str(data.get(field) or "")
            for field in ("capabilities", "expected_outputs")
        )
    )
    # A negated generation phrase is not positive capability evidence. The old
    # substring check read "tidak perlu membuat file" as "membuat file".
    positive_generate_text = re.sub(
        r"\b(?:tidak|tak|ga|gak|nggak|enggak|tanpa)\b"
        r"(?:\s+\w+){0,4}\s+"
        r"\b(?:buat|bikin|membuat|menghasilkan|generate|susun|render|"
        r"export|ekspor|mengirim)\b"
        r"(?:\s+\w+){0,4}\s+"
        r"\b(?:file|dokumen|laporan|pdf|excel|xlsx|csv)\b",
        " ",
        generate_text,
    )
    all_text = f"{receive_text} {generate_text}".strip()
    text_only_markers = (
        "hanya chat teks",
        "hanya teks",
        "cuma chat teks",
        "cuma teks",
        "teks saja",
        "text only",
        "tidak perlu file",
        "tidak butuh file",
        "tanpa file",
        "tidak perlu dokumen atau gambar",
        "tanpa dokumen atau gambar",
    )
    receive_markers = (
        "menerima file",
        "menerima dokumen",
        "menerima gambar",
        "menerima foto",
        "membaca file",
        "membaca dokumen",
        "membaca gambar",
        "membaca foto",
        "mengolah file",
        "mengolah dokumen",
        "mengolah data excel",
        "katalog pdf",
        "bukti transfer",
        "foto produk",
    )
    generate_markers = (
        "buat file",
        "bikin file",
        "buat laporan",
        "bikin laporan",
        "membuat file",
        "membuat dokumen",
        "membuat laporan",
        "membuat pdf",
        "generate file",
        "generate dokumen",
        "generate laporan",
        "generate pdf",
        "generate excel",
        "export file",
        "ekspor file",
        "mengirim file",
        "mengirim dokumen",
        "visualisasi data",
    )
    # Concrete positive workflows win over a local negative such as "tidak
    # perlu gambar" when another answer explicitly requires a PDF/Excel file.
    receives_files = any(marker in receive_text for marker in receive_markers) or bool(
        re.search(
            r"\b(?:terima|menerima|baca|membaca|lihat|melihat|analisis|menganalisis|"
            r"olah|mengolah)\b(?:\s+\w+){0,3}\s+\b(?:file|dokumen|gambar|foto)\b",
            receive_text,
        )
    )
    generates_files = any(
        marker in positive_generate_text for marker in generate_markers
    ) or bool(
        re.search(
            r"\b(?:buat|bikin|membuat|hasilkan|menghasilkan|generate|susun|render|"
            r"export|ekspor)\b(?:\s+\w+){0,4}\s+\b(?:file|dokumen|laporan|pdf|excel|csv)\b",
            positive_generate_text,
        )
        or re.search(
            r"\b(?:laporan|report)\b(?:\s+\w+){0,5}\s+"
            r"\b(?:file|format|pdf|excel|xlsx|csv)\b",
            positive_generate_text,
        )
    )
    if receives_files and generates_files:
        derived = "both"
    elif generates_files:
        derived = "generate"
    elif receives_files:
        derived = "receive_only"
    elif any(marker in all_text for marker in text_only_markers):
        derived = "text_only"
    else:
        derived = ""

    # The typed value is canonical only when it agrees with the evidence-backed
    # capability prose. This lets subsequent tool calls reuse the normalized
    # enum without allowing a model to override contradictory user evidence.
    if explicit and (not derived or explicit == derived):
        return explicit
    return derived


def _has_two_to_three_conversation_examples(value: Any) -> bool:
    if isinstance(value, (list, tuple)):
        count = len([item for item in value if _is_answered(item)])
        return 2 <= count <= 3
    text = str(value or "").strip()
    if not text:
        return False
    numbered = re.findall(r"(?:^|\n)\s*(?:\d+[.)]|[-*])\s+", text)
    customer_turns = re.findall(r"\b(?:customer|user|pelanggan)\s*:", text, flags=re.IGNORECASE)
    count = max(len(numbered), len(customer_turns), 2 if "||" in text else 0)
    return 2 <= count <= 3


def _problem_is_pain_point(value: Any) -> bool:
    text = " ".join(str(value or "").strip().lower().split())
    if not text:
        return False
    feature_only = re.match(
        r"^(?:saya\s+)?(?:mau|ingin|butuh|perlu|buat|bikin|buatkan|bikinkan)?\s*"
        r"(?:agent|agen|bot)\b",
        text,
    )
    pain_markers = (
        "lambat",
        "lama",
        "sulit",
        "kesulitan",
        "sering",
        "lupa",
        "manual",
        "tidak",
        "belum",
        "gagal",
        "masalah",
        "kewalahan",
        "terlambat",
        "menunggu",
        "kehilangan",
        "bingung",
    )
    return not feature_only or any(marker in text for marker in pain_markers)


def _whatsapp_scale_is_detailed(value: Any) -> bool:
    text = " ".join(str(value or "").strip().lower().split())
    if re.search(r"\b(inbound|menunggu pelanggan|nunggu pelanggan|pelanggan chat duluan)\b", text):
        return True
    number_scope = bool(re.search(r"\b(1|satu|banyak|beberapa|multi)\b", text))
    user_pattern = bool(
        re.search(
            r"\b(banyak user|banyak pengguna|banyak customer|customer sekaligus|pelanggan sekaligus|"
            r"tiap user|setiap user|per user|tiap pengguna|setiap pengguna|sendiri|satu user|satu pengguna)\b",
            text,
        )
    )
    return number_scope and user_pattern


def _daily_chat_volume_is_estimated(value: Any) -> bool:
    text = _normalize_evidence_text(value)
    return bool(
        re.search(r"\d", text)
        or re.search(
            r"\b(?:sedikit|belasan|puluhan|ratusan|ribuan)\b",
            text,
        )
    )


def _phone_from_escalation(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("whatsapp_number", "phone", "number", "operator_phone"):
            phone = str(value.get(key) or "").strip()
            if phone:
                return phone
    text = str(value or "")
    match = re.search(r"(?:\+?62|0)[\d\s-]{7,16}", text)
    return match.group(0).strip() if match else ""


def _work_escalation_is_detailed(value: Any, operator_phone: str = "") -> bool:
    if isinstance(value, dict):
        recipient = next(
            (
                str(value.get(key) or "").strip()
                for key in ("recipient", "recipient_name_or_role", "name", "role")
                if str(value.get(key) or "").strip()
            ),
            "",
        )
        conditions = next(
            (
                str(value.get(key) or "").strip()
                for key in ("conditions", "trigger_conditions", "triggers", "when")
                if str(value.get(key) or "").strip()
            ),
            "",
        )
        phone = _phone_from_escalation(value) or str(operator_phone or "").strip()
        return bool(recipient and conditions and len(re.sub(r"\D", "", phone)) >= 8)
    text = str(value or "").strip()
    phone = _phone_from_escalation(text) or str(operator_phone or "").strip()
    has_recipient = bool(re.search(r"\b(owner|admin|operator|manager|atasan|tim)\b", text, re.IGNORECASE))
    return bool(has_recipient and len(re.sub(r"\D", "", phone)) >= 8)


def _group_for_field(field: str) -> dict[str, Any]:
    for group in _GROUPS:
        if field in group["fields"]:
            return group
    return {"id": "confirmation", "label": "Konfirmasi", "fields": ("user_confirmed",)}


def _semantic_discovery_shadow(
    *,
    answers: dict[str, Any],
    unresolved_fields: list[str],
    require_confirmation: bool,
    confirmation_evidence_valid: bool,
) -> dict[str, Any]:
    """Return the next semantic learning goal without changing the legacy gate."""
    unresolved = set(unresolved_fields)
    selected_field = next(
        (field for field in _MATERIAL_LEARNING_PRIORITY if field in unresolved),
        next(iter(unresolved_fields), None),
    )
    confirmation_pending = bool(
        require_confirmation
        and (
            not _confirmed(answers.get("user_confirmed"))
            or not confirmation_evidence_valid
        )
    )
    if selected_field is None and confirmation_pending:
        selected_field = "user_confirmed"

    if unresolved_fields:
        state = "needs_clarification"
    elif confirmation_pending:
        state = "awaiting_confirmation"
    else:
        state = "complete"

    known_facts = {
        key: value
        for key, value in answers.items()
        if key != "user_confirmed"
        and key not in unresolved
        and _is_answered(value)
    }
    return {
        "state": state,
        "known_facts": known_facts,
        "unresolved_material_fields": list(unresolved_fields),
        "learning_field": selected_field,
        "learning_goal": _LEARNING_GOALS.get(
            str(selected_field or ""),
            f"clarify_{selected_field}" if selected_field else None,
        ),
        "risk_if_unresolved": _UNRESOLVED_RISKS.get(
            str(selected_field or ""),
            "the resulting agent configuration may not match the user's intent"
            if selected_field
            else None,
        ),
        "must_confirm_before_create": confirmation_pending,
        "selection_mode": "shadow_risk_priority_v1",
    }


def validate_agent_discovery(
    discovery_answers: Any,
    *,
    agent_name: str = "",
    operator_phone: str = "",
    require_confirmation: bool = True,
    user_messages: list[str] | None = None,
    require_evidence: bool = False,
    require_confirmed_summary: bool = False,
    persisted_confirmation_verified: bool = False,
) -> dict[str, Any]:
    """Validate Arthur's six discovery groups without asking for active hours."""
    answers, parse_error = _parse_answers(discovery_answers)
    raw_evidence = answers.pop(_EVIDENCE_KEY, {})
    evidence = raw_evidence if isinstance(raw_evidence, dict) else {}
    ignored_fields = sorted(key for key in answers if key in _IGNORED_OPERATIONAL_HOUR_FIELDS)
    for key in ignored_fields:
        answers.pop(key, None)

    supplied_name = str(answers.get("agent_name") or "").strip()
    external_name = str(agent_name or "").strip()
    if not supplied_name and external_name:
        answers["agent_name"] = external_name

    usage_context = _normalize_usage_context(answers.get("usage_context"))
    if usage_context:
        answers["usage_context"] = usage_context

    # A concrete receipt/photo/document workflow is an explicit capability
    # decision in the owner's own words. Preserve it as the capability answer
    # so the validator does not make Arthur ask the same fact again using the
    # artificial label "vision" or "file capability".
    if not _is_answered(answers.get("capabilities")) and _is_answered(
        answers.get("main_tasks")
    ):
        inferred_file_capability = discovery_file_capability(
            {"main_tasks": answers.get("main_tasks")}
        )
        if inferred_file_capability:
            answers["capabilities"] = str(answers["main_tasks"])

    missing_fields: list[str] = []
    invalid_fields: list[str] = []
    validation_errors: list[str] = [parse_error] if parse_error else []
    missing_evidence_fields: list[str] = []
    unsupported_evidence_fields: list[str] = []
    verified_evidence_fields: list[str] = []

    # Optional business policy must never be invented. If the model supplies an
    # optional value without matching user evidence, drop it and keep the
    # platform safety baseline instead of blocking or silently trusting it.
    if require_evidence:
        persisted_user_messages = list(user_messages or [])
        for field in {
            *_OPTIONAL_DISCOVERY_FIELDS,
            *(
                _PERSONAL_OPTIONAL_DISCOVERY_FIELDS
                if usage_context == "personal"
                else set()
            ),
        }:
            if not _is_answered(answers.get(field)):
                continue
            verified_quotes = _verified_evidence_quotes(
                field,
                evidence,
                persisted_user_messages,
            )
            if not (
                verified_quotes
                and _evidence_supports_answer(
                    field,
                    answers.get(field),
                    verified_quotes,
                )
            ):
                answers.pop(field, None)

    required_fields: list[str] = []
    for group in _GROUPS:
        for field in group["fields"]:
            if usage_context == "personal" and field in {"escalation_target", "go_live_approver"}:
                continue
            if _field_is_optional(field, usage_context):
                continue
            required_fields.append(field)

    delegated_fields: set[str] = set()
    for field in required_fields:
        if not _is_answered(answers.get(field)):
            missing_fields.append(field)

    if require_evidence:
        persisted_user_messages = list(user_messages or [])
        for field in required_fields:
            if not _is_answered(answers.get(field)):
                continue
            verified_quotes = _verified_evidence_quotes(field, evidence, persisted_user_messages)
            if not verified_quotes:
                # Recover evidence deterministically from immutable conversation
                # history when a small model omitted the _evidence entry but its
                # normalized answer itself closely matches a real user message.
                # Numeric facts remain protected by
                # _evidence_quote_matches_message.
                verified_quotes = _verified_evidence_quotes(
                    field,
                    {field: answers.get(field)},
                    persisted_user_messages,
                )
            if not verified_quotes and field in _DELEGATABLE_FIELDS:
                # "Sesuaikan saja" is an explicit delegation for safe copy
                # details.  It is not permission for external actions; the
                # drafted value still has to appear in the final confirmed
                # summary.
                verified_quotes = [
                    message
                    for message in persisted_user_messages
                    if any(
                        marker in _normalize_evidence_text(message)
                        for marker in _DELEGATION_MARKERS
                    )
                ][-1:]
            if verified_quotes and _evidence_supports_answer(
                field,
                answers.get(field),
                verified_quotes,
            ):
                verified_evidence_fields.append(field)
                if field in _DELEGATABLE_FIELDS:
                    quote_text = _normalize_evidence_text(" ".join(verified_quotes))
                    if any(marker in quote_text for marker in _DELEGATION_MARKERS):
                        delegated_fields.add(field)
            elif verified_quotes:
                unsupported_evidence_fields.append(field)
                invalid_fields.append(field)
            else:
                missing_evidence_fields.append(field)
                invalid_fields.append(field)
        if missing_evidence_fields:
            validation_errors.append(
                "Jawaban discovery berikut tidak memiliki kutipan yang cocok dengan pesan user: "
                + ", ".join(missing_evidence_fields)
                + ". Jangan mengarang; tanyakan ke user dan isi _evidence dengan kutipan persis jawabannya."
            )
        if unsupported_evidence_fields:
            validation_errors.append(
                "Kutipan user tidak mendukung isi jawaban pada field: "
                + ", ".join(unsupported_evidence_fields)
                + ". Jangan menempelkan kutipan yang tidak relevan atau menambahkan detail baru."
            )

    if _is_answered(answers.get("usage_context")) and not usage_context:
        invalid_fields.append("usage_context")
        validation_errors.append("usage_context harus personal atau pekerjaan/bisnis.")
    if _is_answered(answers.get("problem")) and not _problem_is_pain_point(answers.get("problem")):
        invalid_fields.append("problem")
        validation_errors.append("problem harus menjelaskan pain point/masalah, bukan hanya fitur atau jenis agent.")
    file_capability = discovery_file_capability(answers)
    if file_capability:
        answers["file_capability"] = file_capability
    if _is_answered(answers.get("capabilities")) and not file_capability:
        invalid_fields.append("capabilities")
        validation_errors.append(
            "capabilities harus menjelaskan tugas konkret dan memilih kemampuan file secara eksplisit: "
            "hanya chat teks, menerima file/gambar, membuat file/laporan, atau keduanya."
        )
    if (
        not _field_is_optional("ideal_conversations", usage_context)
        and _is_answered(answers.get("ideal_conversations"))
        and "ideal_conversations" not in delegated_fields
        and not _has_two_to_three_conversation_examples(answers.get("ideal_conversations"))
    ):
        invalid_fields.append("ideal_conversations")
        validation_errors.append("ideal_conversations harus berisi 2-3 contoh percakapan.")
    if (
        "whatsapp_scale" not in _OPTIONAL_DISCOVERY_FIELDS
        and _is_answered(answers.get("whatsapp_scale"))
        and not _whatsapp_scale_is_detailed(answers.get("whatsapp_scale"))
    ):
        invalid_fields.append("whatsapp_scale")
        validation_errors.append(
            "whatsapp_scale harus menjelaskan jumlah nomor dan pola satu nomor-banyak user atau tiap user-punya nomor."
        )
    if (
        not _field_is_optional("daily_chat_volume", usage_context)
        and _is_answered(answers.get("daily_chat_volume"))
        and not _daily_chat_volume_is_estimated(answers.get("daily_chat_volume"))
    ):
        invalid_fields.append("daily_chat_volume")
        validation_errors.append(
            "daily_chat_volume harus memuat estimasi angka, rentang, atau kategori seperti puluhan/ratusan chat per hari."
        )
    if usage_context == "work" and _is_answered(answers.get("escalation_target")):
        if not _work_escalation_is_detailed(answers.get("escalation_target"), operator_phone):
            invalid_fields.append("escalation_target")
            validation_errors.append(
                "Untuk pekerjaan/bisnis, escalation_target harus memuat kondisi pemicu, nama/role penerima, dan nomor WhatsApp."
            )
        discovery_phone = _phone_from_escalation(answers.get("escalation_target"))
        if discovery_phone and operator_phone:
            if re.sub(r"\D", "", discovery_phone) != re.sub(r"\D", "", str(operator_phone)):
                invalid_fields.append("escalation_target")
                validation_errors.append(
                    "Nomor eskalasi di discovery berbeda dari operator_phone yang akan dipakai. Minta user memilih nomor yang benar."
                )
    if supplied_name and external_name and supplied_name.casefold() != external_name.casefold():
        invalid_fields.append("agent_name")
        validation_errors.append(
            f"Nama di discovery ('{supplied_name}') berbeda dari nama yang akan dibuat ('{external_name}')."
        )

    confirmation_evidence_valid = True
    if require_evidence and require_confirmation and _confirmed(answers.get("user_confirmed")):
        persisted_continuation = bool(
            persisted_confirmation_verified
            and user_messages
            and _is_safe_confirmation_continuation(user_messages[-1])
        )
        confirmation_evidence_valid = persisted_continuation or _confirmation_evidence_is_valid(
            evidence,
            list(user_messages or []),
            require_confirmed_summary=require_confirmed_summary,
        )
        if not confirmation_evidence_valid:
            invalid_fields.append("user_confirmed")
            missing_evidence_fields.append("user_confirmed")
            answers.pop("user_confirmed", None)
            validation_errors.append(
                "Konfirmasi akhir belum terbukti dari pesan user terakhir. Tampilkan rangkuman lengkap dan "
                "minta user membalas eksplisit seperti 'sudah', 'sesuai', atau 'sudah sesuai'."
            )

    unresolved_set = {*missing_fields, *invalid_fields}
    unresolved_fields = [
        field
        for group in _GROUPS
        for field in group["fields"]
        if field in unresolved_set
    ]
    completed_fields = [
        field for field in required_fields if field not in unresolved_fields and _is_answered(answers.get(field))
    ]

    if unresolved_fields:
        next_group = _group_for_field(unresolved_fields[0])
        group_missing = [field for field in next_group["fields"] if field in unresolved_fields]
    elif require_confirmation and (
        not _confirmed(answers.get("user_confirmed")) or not confirmation_evidence_valid
    ):
        next_group = {"id": "confirmation", "label": "Konfirmasi Akhir", "fields": ("user_confirmed",)}
        group_missing = ["user_confirmed"]
    else:
        next_group = None
        group_missing = []

    progress = []
    for group in _GROUPS:
        applicable = [
            field
            for field in group["fields"]
            if not (usage_context == "personal" and field in {"escalation_target", "go_live_approver"})
            and not (
                _field_is_optional(field, usage_context)
                and not _is_answered(answers.get(field))
            )
        ]
        pending = [field for field in applicable if field in unresolved_fields]
        progress.append(
            {
                "group_id": group["id"],
                "label": group["label"],
                "status": "skipped" if not applicable else "complete" if not pending else "pending",
                "completed": len(applicable) - len(pending),
                "required": len(applicable),
            }
        )

    complete = not unresolved_fields and (
        not require_confirmation
        or (_confirmed(answers.get("user_confirmed")) and confirmation_evidence_valid)
    )
    # WhatsApp discovery is progressive: expose exactly one unresolved field.
    # The next plan call advances from the persisted answers.
    next_questions = [
        {"topic": field, "question": _QUESTIONS[field]}
        for field in group_missing[:1]
    ]
    semantic_discovery = _semantic_discovery_shadow(
        answers=answers,
        unresolved_fields=unresolved_fields,
        require_confirmation=require_confirmation,
        confirmation_evidence_valid=confirmation_evidence_valid,
    )
    return {
        "complete": complete,
        "usage_context": usage_context,
        "normalized_answers": answers,
        "required_fields": required_fields,
        "completed_fields": completed_fields,
        "missing_fields": missing_fields,
        "invalid_fields": invalid_fields,
        "missing_evidence_fields": missing_evidence_fields,
        "unsupported_evidence_fields": unsupported_evidence_fields,
        "verified_evidence_fields": verified_evidence_fields,
        "evidence_required": require_evidence,
        "validation_errors": validation_errors,
        "group_progress": progress,
        "next_group": (
            {"id": next_group["id"], "label": next_group["label"]}
            if next_group else None
        ),
        "next_questions": next_questions,
        "skipped_for_personal": (
            ["escalation_target", "go_live_approver"] if usage_context == "personal" else []
        ),
        "ignored_fields": ignored_fields,
        "operational_hours_requested": False,
        "file_capability": file_capability,
        "confirmation_evidence_valid": confirmation_evidence_valid,
        "semantic_discovery": semantic_discovery,
    }


def discovery_operator_phone(discovery: dict[str, Any]) -> str:
    """Return the confirmed work escalation phone, when present."""
    answers = discovery.get("normalized_answers") or {}
    return _phone_from_escalation(answers.get("escalation_target"))


def discovery_escalation_policy(discovery: dict[str, Any]) -> str:
    """Map confirmed discovery to the existing owner/operator/none setting."""
    target = (discovery.get("normalized_answers") or {}).get("escalation_target")
    if discovery.get("usage_context") == "personal" and not _is_answered(target):
        return "none"
    if isinstance(target, dict):
        recipient = " ".join(
            str(target.get(key) or "")
            for key in ("recipient", "recipient_name_or_role", "name", "role")
        ).lower()
    else:
        recipient = str(target or "").lower()
    return "owner" if "owner" in recipient or "pemilik" in recipient else "operator"
