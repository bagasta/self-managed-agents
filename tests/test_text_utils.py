from app.core.utils.text_utils import markdown_to_wa


def test_markdown_table_becomes_whatsapp_bullets():
    source = """Status akun

| Item | Detail |
|------|--------|
| Plan | Trial |
| Agent | 0 dari 1 |
"""

    rendered = markdown_to_wa(source)

    assert "|" not in rendered
    assert "• Plan: Trial" in rendered
    assert "• Agent: 0 dari 1" in rendered
