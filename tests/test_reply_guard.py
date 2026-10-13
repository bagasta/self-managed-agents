from app.core.engine.reply_guard import ensure_non_empty_reply


def test_non_empty_model_reply_is_never_rewritten_for_arthur_v2():
    reply = "Rangkuman kebutuhan Minsel sudah siap. Konfirmasi jika ingin saya buat."
    trace: dict[str, str] = {}

    output = ensure_non_empty_reply(
        reply,
        [{"tool": "get_current_plan", "result": '{"ok": true}'}],
        active_groups=["builder"],
        system_plugin="arthur_v2",
        decision_trace=trace,
    )

    assert output == reply
    assert trace["reason"] == "pass_through"


def test_non_empty_model_reply_is_never_rewritten_for_any_agent():
    reply = "Saya sudah mencatat kebutuhanmu dan menunggu konfirmasi."
    assert ensure_non_empty_reply(reply, []) == reply


def test_empty_reply_uses_verified_google_artifact_from_matching_tool():
    trace: dict[str, str] = {}
    output = ensure_non_empty_reply(
        "",
        [{"tool": "create_document", "result": "URL: https://docs.google.com/document/d/file-1/edit"}],
        decision_trace=trace,
    )
    assert "https://docs.google.com/document/d/file-1/edit" in output
    assert trace["reason"] == "fallback_verified_artifact"


def test_empty_reply_never_promotes_search_result_url_to_deliverable():
    output = ensure_non_empty_reply(
        "",
        [{"tool": "tavily_search", "result": "URL: https://www.youtube.com/watch?v=random"}],
    )

    assert "youtube.com" not in output
    assert "belum menghasilkan jawaban akhir yang terverifikasi" in output


def test_google_docs_success_claim_requires_verified_document_tool_artifact():
    trace: dict[str, str] = {}
    output = ensure_non_empty_reply(
        "Proses selesai. Cek hasilnya di sini: https://www.youtube.com/watch?v=random",
        [{"tool": "tavily_search", "result": "https://www.youtube.com/watch?v=random"}],
        user_message="Riset berita AI dan buat laporannya di Google Docs",
        decision_trace=trace,
    )

    assert "youtube.com" not in output
    assert "Google Docs belum berhasil dibuat dan diverifikasi" in output
    assert trace["reason"] == "unverified_google_deliverable"


def test_google_docs_success_claim_passes_with_verified_document_artifact():
    reply = "Laporan sudah dibuat: https://docs.google.com/document/d/report-1/edit"
    output = ensure_non_empty_reply(
        reply,
        [
            {
                "tool": "create_document",
                "result": "Successfully created and populated Google Doc. URL: https://docs.google.com/document/d/report-1/edit",
            }
        ],
        user_message="Buat laporan di Google Docs",
    )

    assert output == reply


def test_google_sheets_success_claim_rejects_unrelated_research_url():
    output = ensure_non_empty_reply(
        "Spreadsheet sudah dibuat. Cek hasilnya: https://example.com/research",
        [{"tool": "tavily_search", "result": "https://example.com/research"}],
        user_message="Buat laporan di Google Sheets",
    )

    assert "example.com" not in output
    assert "Google Sheets belum berhasil dibuat dan diverifikasi" in output


def test_empty_reply_has_a_user_facing_fallback():
    trace: dict[str, str] = {}
    output = ensure_non_empty_reply("", [], decision_trace=trace)
    assert output
    assert trace["reason"] == "fallback_empty_reply"
