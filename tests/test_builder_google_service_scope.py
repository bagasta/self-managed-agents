from app.core.tools.builder_google import (
    configure_google_workspace_services,
    infer_google_workspace_services,
)


def test_spreadsheet_requirement_does_not_enable_google_tasks() -> None:
    services = infer_google_workspace_services(
        "Pendaftaran disimpan ke Google Spreadsheet dan status pembayaran dicatat."
    )

    assert services == ["sheets", "drive"]
    assert "tasks" not in services
    assert "calendar" not in services


def test_configure_google_services_persists_least_privilege_allowlist() -> None:
    config = configure_google_workspace_services(
        {"memory": True},
        "Gunakan Google Sheets untuk mencatat transaksi.",
    )

    google = config["mcp"]["servers"]["google_workspace"]
    assert google["allowed_services"] == ["sheets", "drive"]
