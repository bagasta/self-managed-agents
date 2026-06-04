"""
Tests untuk Phase 2 Agent Builder — Platform Rulebook (system-message-builder.md).

Verifikasi:
- File system-message-builder.md ada dan tidak kosong
- Semua seksi wajib ada (identity, platform rulebook, tools, best practices, limits, workflow)
- Best practices WhatsApp ada (no markdown, panjang pesan, bahasa, eskalasi, few-shot)
- Batasan platform terdokumentasi
- tools_config keys sesuai dengan yang ada di platform
- Template system prompt tersedia
- Endpoint referensi ada
"""
from __future__ import annotations

import re
from pathlib import Path

RULEBOOK_PATH = Path(__file__).parent.parent / "system-message-builder.md"


class TestRulebookFileExists:
    def test_file_exists(self):
        assert RULEBOOK_PATH.exists(), "system-message-builder.md harus ada di root project"

    def test_file_not_empty(self):
        content = RULEBOOK_PATH.read_text(encoding="utf-8")
        assert len(content) > 1000, "system-message-builder.md harus substansial (>1000 chars)"


class TestRulebookSections:
    """Semua seksi wajib harus ada."""

    def setup_method(self):
        self.content = RULEBOOK_PATH.read_text(encoding="utf-8")

    def test_has_identity_section(self):
        assert "Arthur" in self.content, "Harus ada identitas agent (Arthur)"

    def test_has_platform_config_section(self):
        assert "Base URL API" in self.content, "Harus ada konfigurasi platform (Base URL)"
        assert "API Key" in self.content, "Harus ada konfigurasi API Key"
        assert "Model default" in self.content, "Harus ada model default"

    def test_has_tools_section(self):
        assert "http_get" in self.content, "Harus ada daftar tools yang tersedia"
        assert "http_post" in self.content
        assert "http_patch" in self.content
        assert "send_agent_wa_qr" in self.content

    def test_has_platform_rulebook_section(self):
        assert "Platform Rulebook" in self.content or "Kapabilitas" in self.content, \
            "Harus ada seksi Platform Rulebook / kapabilitas teknis"

    def test_has_channel_section(self):
        assert "WhatsApp" in self.content, "Harus ada info channel WhatsApp"
        assert "wa-service" in self.content or "channel_type" in self.content

    def test_has_tools_config_reference(self):
        assert "tools_config" in self.content, "Harus ada referensi tools_config"

    def test_has_best_practices_section(self):
        assert "Best Practice" in self.content or "best practice" in self.content.lower(), \
            "Harus ada seksi best practices prompting"

    def test_has_platform_limitations_section(self):
        assert "Belum bisa" in self.content or "tidak bisa" in self.content.lower() or "Batasan" in self.content, \
            "Harus ada daftar batasan platform"

    def test_has_workflow_phases(self):
        assert "Fase" in self.content, "Harus ada alur kerja (fase-fase)"
        assert "Discovery" in self.content or "Menggali" in self.content, "Harus ada fase discovery"

    def test_has_endpoint_reference(self):
        assert "/v1/agents" in self.content, "Harus ada referensi endpoint API"
        assert "POST" in self.content and "GET" in self.content and "PATCH" in self.content

    def test_has_guardrails_section(self):
        assert "Guardrail" in self.content or "guardrail" in self.content.lower(), \
            "Harus ada seksi guardrails"

    def test_has_behavior_rules(self):
        assert "Aturan Perilaku" in self.content or "Wajib Diikuti" in self.content, \
            "Harus ada aturan perilaku yang wajib diikuti"


class TestToolCategories:
    """Kategori tool Arthur harus terdokumentasi sebagai routing policy."""

    REQUIRED_CATEGORIES = [
        "User Management",
        "Plan & Billing",
        "Agent Builder",
        "Agent Management",
        "Channel Management",
        "Workspace / App Connectors",
        "Runtime Support",
    ]

    def setup_method(self):
        self.content = RULEBOOK_PATH.read_text(encoding="utf-8")

    def test_all_tool_categories_documented(self):
        for category in self.REQUIRED_CATEGORIES:
            assert category in self.content, f"Kategori tool '{category}' harus ada di rulebook"

    def test_existing_agent_requests_route_to_agent_management(self):
        assert "Jangan create_agent" in self.content
        assert "list_my_agents/get_agent_detail" in self.content
        assert "update_agent/delete_agent" in self.content

    def test_whatsapp_and_google_are_separate_categories(self):
        assert "Channel Management" in self.content
        assert "create_wa_dev_trial_link" in self.content
        assert "Workspace / App Connectors" in self.content
        assert "generate_google_auth_link" in self.content


class TestWhatsAppBestPractices:
    """Best practices spesifik untuk WhatsApp harus terdokumentasi."""

    def setup_method(self):
        self.content = RULEBOOK_PATH.read_text(encoding="utf-8")

    def test_no_markdown_rule(self):
        has_rule = (
            "markdown" in self.content.lower()
            or "bold" in self.content.lower()
            or "heading" in self.content.lower()
        )
        assert has_rule, "Harus ada aturan tentang tidak menggunakan markdown di WA"

    def test_message_length_rule(self):
        has_length_rule = (
            "paragraf" in self.content.lower()
            or "panjang pesan" in self.content.lower()
            or "wall of text" in self.content.lower()
        )
        assert has_length_rule, "Harus ada aturan panjang pesan"

    def test_language_rule(self):
        has_lang = "Bahasa" in self.content or "bahasa" in self.content
        assert has_lang, "Harus ada aturan bahasa respons"

    def test_escalation_rule(self):
        assert "eskalasi" in self.content.lower(), "Harus ada aturan/instruksi eskalasi"

    def test_few_shot_rule(self):
        has_fewshot = (
            "contoh percakapan" in self.content.lower()
            or "few-shot" in self.content.lower()
            or "Contoh" in self.content
        )
        assert has_fewshot, "Harus ada anjuran few-shot examples di system prompt"


class TestPlatformLimitations:
    """Batasan platform harus terdokumentasi dengan jelas."""

    def setup_method(self):
        self.content = RULEBOOK_PATH.read_text(encoding="utf-8")

    def test_broadcast_limitation(self):
        assert "broadcast" in self.content.lower(), \
            "Harus ada info bahwa broadcast tidak didukung"

    def test_one_device_per_agent(self):
        has_device_limit = (
            "satu nomor WA per agent" in self.content.lower()
            or "satu device" in self.content.lower()
            or "one number" in self.content.lower()
        )
        assert has_device_limit, "Harus ada info bahwa satu nomor WA per agent"


class TestToolsConfigKeys:
    """Semua tools_config keys yang ada di platform harus terdokumentasi."""

    REQUIRED_KEYS = [
        "memory",
        "skills",
        "escalation",
        "sandbox",
        "tool_creator",
        "scheduler",
        "rag",
        "http",
        "mcp",
        "whatsapp_media",
        "wa_agent_manager",
        "subagents",
    ]

    def setup_method(self):
        self.content = RULEBOOK_PATH.read_text(encoding="utf-8")

    def test_all_tools_config_keys_documented(self):
        for key in self.REQUIRED_KEYS:
            assert key in self.content, f"tools_config key '{key}' harus ada di rulebook"


class TestSystemPromptTemplate:
    """Template system prompt harus tersedia."""

    def setup_method(self):
        self.content = RULEBOOK_PATH.read_text(encoding="utf-8")

    def test_has_prompt_template(self):
        has_template = (
            "Template" in self.content
            or "template" in self.content
            or "Struktur System Prompt" in self.content
        )
        assert has_template, "Harus ada template struktur system prompt"

    def test_template_has_identity_placeholder(self):
        has_identity = (
            "[Nama]" in self.content
            or "Kamu adalah" in self.content
            or "[peran]" in self.content
        )
        assert has_identity, "Template harus punya placeholder identitas agent"

    def test_template_has_escalation_section(self):
        has_esc = (
            "ESKALASI" in self.content
            or "Eskalasikan" in self.content
        )
        assert has_esc, "Template harus punya seksi eskalasi"


class TestInputSupportDocumented:
    """Input yang didukung platform harus terdokumentasi."""

    def setup_method(self):
        self.content = RULEBOOK_PATH.read_text(encoding="utf-8")

    def test_text_input(self):
        assert "Teks" in self.content or "teks" in self.content

    def test_voice_note_input(self):
        has_vn = (
            "Voice Note" in self.content
            or "voice note" in self.content.lower()
            or "audio" in self.content.lower()
            or "PTT" in self.content
        )
        assert has_vn, "Harus ada info tentang voice note/audio input"

    def test_document_input(self):
        has_doc = "PDF" in self.content or "DOCX" in self.content or "dokumen" in self.content.lower()
        assert has_doc, "Harus ada info tentang input dokumen"
