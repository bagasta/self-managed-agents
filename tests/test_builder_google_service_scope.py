from app.core.tools.builder_google import (
    configure_google_workspace_services,
    infer_google_workspace_services,
)
from app.core.google_oauth_scopes import (
    infer_google_service_operations,
    oauth_scopes_for_google_permissions,
    reconcile_google_service_operations,
)
from app.core.engine.google_mcp_support import (
    filter_google_mcp_tools_by_services,
    reconcile_google_mcp_tools_config,
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


def test_service_selection_and_write_intent_can_come_from_separate_confirmed_text() -> None:
    config = configure_google_workspace_services(
        {},
        "Google Docs dan Google Drive",
        operation_requirement_text="Riset berita lalu buat dan isi laporan di Google Docs.",
    )

    google = config["mcp"]["servers"]["google_workspace"]
    assert google["allowed_services"] == ["drive", "docs"]
    assert google["allowed_operations"] == {
        "drive": ["write"],
        "docs": ["read", "write"],
    }
    assert "https://www.googleapis.com/auth/documents" in google["oauth_scopes"]


def test_reconcile_normalizes_legacy_manage_and_fills_missing_operations() -> None:
    permissions = reconcile_google_service_operations(
        "Buat laporan di Google Docs dan catat jadwal di Google Calendar.",
        ["docs", "calendar", "drive"],
        {"docs": ["read", "manage"]},
    )

    assert permissions == {
        "docs": ["read", "write"],
        "calendar": ["read", "write"],
        "drive": ["write"],
    }


def test_runtime_reconcile_never_enables_an_unselected_google_service() -> None:
    config = reconcile_google_mcp_tools_config(
        {
            "mcp": {
                "enabled": True,
                "servers": {
                    "google_workspace": {
                        "allowed_services": ["docs"],
                        "allowed_operations": {"docs": ["read"]},
                    }
                },
            }
        },
        requirement_text="Buat laporan di Google Docs dan kirim Gmail.",
    )

    google = config["mcp"]["servers"]["google_workspace"]
    assert google["allowed_services"] == ["docs"]
    assert google["allowed_operations"] == {"docs": ["read", "write"]}
    assert "gmail" not in google["allowed_operations"]
    assert "https://www.googleapis.com/auth/documents" in google["oauth_scopes"]


def test_email_read_requirement_uses_only_gmail_readonly_scope() -> None:
    permissions = infer_google_service_operations("Baca dan rangkum email masuk saja.", ["gmail"])

    assert permissions == {}
    assert oauth_scopes_for_google_permissions(permissions) == [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ]


def test_email_send_requirement_does_not_request_modify_scope() -> None:
    permissions = infer_google_service_operations("Balas email pelanggan yang masuk.", ["gmail"])

    assert permissions == {"gmail": ["send"]}
    assert oauth_scopes_for_google_permissions(permissions) == [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/gmail.send",
    ]


def test_sheets_manage_scope_matches_dashboard_without_readonly_duplicate() -> None:
    permissions = infer_google_service_operations(
        "Buat dan update spreadsheet transaksi.", ["sheets"]
    )

    assert permissions == {"sheets": ["read", "write"]}
    assert oauth_scopes_for_google_permissions(permissions) == [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/spreadsheets",
    ]


def test_calendar_and_drive_use_dashboard_verified_scopes() -> None:
    permissions = infer_google_service_operations(
        "Baca kalender lalu buat jadwal dan upload file ke Google Drive.",
        ["calendar", "drive"],
    )

    assert oauth_scopes_for_google_permissions(permissions) == [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/drive.file",
    ]


def test_gmail_read_only_hides_write_tools() -> None:
    class Tool:
        def __init__(self, name: str) -> None:
            self.name = name

    class Log:
        def info(self, *args, **kwargs) -> None:
            return None

    tools = [Tool("get_gmail_message"), Tool("send_email"), Tool("delete_gmail_message")]
    filtered = filter_google_mcp_tools_by_services(
        tools,
        tools_config={
            "mcp": {"servers": {"google_workspace": {
                "allowed_services": ["gmail"],
                "allowed_operations": {"gmail": ["read"]},
            }}},
        },
        log=Log(),
    )

    assert [tool.name for tool in filtered] == ["get_gmail_message"]
