from app.core.tools.builder_google import (
    configure_google_workspace_services,
    infer_google_workspace_services,
)
from app.core.google_oauth_scopes import infer_google_service_operations, oauth_scopes_for_google_permissions
from app.core.engine.google_mcp_support import filter_google_mcp_tools_by_services


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


def test_email_read_requirement_uses_only_gmail_readonly_scope() -> None:
    permissions = infer_google_service_operations("Baca dan rangkum email masuk saja.", ["gmail"])

    assert permissions == {"gmail": ["read"]}
    assert oauth_scopes_for_google_permissions(permissions) == [
        "https://www.googleapis.com/auth/gmail.readonly"
    ]


def test_email_send_requirement_does_not_request_modify_scope() -> None:
    permissions = infer_google_service_operations("Balas email pelanggan yang masuk.", ["gmail"])

    assert permissions == {"gmail": ["read", "send"]}
    assert oauth_scopes_for_google_permissions(permissions) == [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
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
