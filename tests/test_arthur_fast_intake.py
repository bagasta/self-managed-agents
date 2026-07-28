"""Regression coverage for Arthur's concise WhatsApp intake."""

import json

from app.core.engine.agent_followups import _needs_builder_plan_completion
from app.core.engine.arthur_skill_runtime import classify_builder_whatsapp_action
from app.core.engine.reply_guard import ensure_non_empty_reply
from app.core.tools.builder_discovery import validate_agent_discovery


def test_business_cs_brief_does_not_require_optional_questionnaire_fields() -> None:
    """A complete CS workflow can be confirmed without sample-dialogue theatre."""
    discovery = validate_agent_discovery(
        {
            "problem": "Admin kursus coding kewalahan menangani banyak pendaftar WhatsApp.",
            "usage_context": "bisnis",
            "agent_name": "KodingJoy",
            "audience": "customer eksternal dan admin",
            "main_tasks": (
                "Melayani calon siswa sampai mendaftar, mencatat pendaftaran ke Google Sheet, "
                "menerima foto bukti pembayaran, lalu melaporkannya ke admin."
            ),
            "prohibited_actions": "Tidak boleh memberi diskon, refund, atau mengonfirmasi pembayaran.",
            "unknown_handling": "Tanyakan ke admin bila informasi belum ada atau di luar wewenang.",
            "escalation_target": {
                "conditions": "pertanyaan di luar informasi, permintaan diskon, atau bukti pembayaran masuk",
                "recipient": "Admin",
                "whatsapp_number": "62895626765423",
            },
            "integrations": "Google Sheets untuk pencatatan dan WhatsApp untuk laporan ke admin.",
            "user_confirmed": True,
        },
        agent_name="KodingJoy",
        operator_phone="62895626765423",
        require_confirmation=True,
        require_evidence=False,
    )

    assert discovery["complete"] is True
    assert discovery["file_capability"] == "receive_only"
    assert not set(discovery["missing_fields"]) & {
        "ideal_conversations",
        "daily_chat_volume",
        "expected_outputs",
        "vision_requirement",
        "go_live_approver",
    }


def test_numbered_demo_choice_generates_trial_action() -> None:
    offer = (
        "1. Nomor demo Arthur — saya kirim link wa.me dan kode.\n"
        "2. Nomor khusus milikmu — saya kirim scan sekali dari WhatsApp."
    )

    assert classify_builder_whatsapp_action("1", offer) == "trial_link"
    assert classify_builder_whatsapp_action("2", offer) == "dedicated_qr"


def test_greeting_never_runs_builder_planning_recovery() -> None:
    assert not _needs_builder_plan_completion(
        [],
        is_builder=True,
        primary_skill="arthur-discovery",
        workflow_state="discovery",
        user_message="Halo",
    )


def test_builder_reasoning_text_is_never_sent_for_a_greeting() -> None:
    reply = ensure_non_empty_reply(
        "Baik, saya panggil perencanaan dengan semua data yang sudah terkumpul",
        [],
        active_groups=["builder"],
        user_message="Halo",
    )

    assert "panggil perencanaan" not in reply.casefold()
    assert reply.startswith("Halo! Aku Arthur")


def test_required_planner_call_and_natural_whatsapp_reply_work_together() -> None:
    steps = [
        {
            "tool": "plan_agent",
            "result": json.dumps(
                {
                    "plan_status": "needs_clarification",
                    "capability_clarifications": [
                        {
                            "topic": "agent_name",
                            "question": "Mau kasih nama apa untuk agent-nya?",
                        }
                    ],
                }
            ),
        }
    ]

    assert not _needs_builder_plan_completion(
        steps,
        is_builder=True,
        primary_skill="arthur-discovery",
        workflow_state="discovery",
        user_message="Saya kewalahan balas customer dan mencatat order.",
    )

    reply = (
        "Sip, berarti agent ini akan membantu CS sekaligus pencatatan order. "
        "Kamu mau kasih nama apa untuk agent-nya?"
    )
    assert ensure_non_empty_reply(reply, steps) == reply
