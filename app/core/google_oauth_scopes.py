"""Google Workspace OAuth scopes approved for the managed-agent dashboard.

This module is the server-side counterpart of the dashboard MCP tool picker.
Arthur must only request scopes exposed there, so a WhatsApp-created agent has
the same verified OAuth grant as an agent configured in the dashboard.
"""
from __future__ import annotations

from collections.abc import Iterable


GOOGLE_WORKSPACE_BASELINE_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)

# Keep this catalog aligned with managed-agent-dashboard/src/types.ts.
# Gmail and Drive are dashboard tools with a single approved permission; the
# remaining products have a readonly/manage pair.
_READ_SCOPES = {
    "calendar": "https://www.googleapis.com/auth/calendar.events.readonly",
    "docs": "https://www.googleapis.com/auth/documents.readonly",
    "sheets": "https://www.googleapis.com/auth/spreadsheets.readonly",
    "forms": "https://www.googleapis.com/auth/forms.body.readonly",
}

_WRITE_SCOPES = {
    "calendar": "https://www.googleapis.com/auth/calendar.events",
    "docs": "https://www.googleapis.com/auth/documents",
    "sheets": "https://www.googleapis.com/auth/spreadsheets",
    "forms": "https://www.googleapis.com/auth/forms.body",
}

GOOGLE_WORKSPACE_DASHBOARD_SERVICES = frozenset({
    "gmail", "calendar", "drive", *_READ_SCOPES, *_WRITE_SCOPES,
})


def infer_google_service_operations(requirement_text: str, services: Iterable[str]) -> dict[str, list[str]]:
    """Derive a conservative operation policy from the confirmed workflow text.

    Read access is always the default. Write access is added only when the
    confirmed requirement contains an explicit action verb.
    """
    text = " ".join(str(requirement_text or "").casefold().split())
    write_terms = (
        "kirim", "balas", "buat", "tambah", "ubah", "update", "hapus",
        "simpan", "upload", "jadwalkan", "create", "send", "reply", "write",
        "delete", "modify", "edit", "append",
    )
    wants_write = any(term in text for term in write_terms)
    policy: dict[str, list[str]] = {}
    for raw_service in services:
        service = str(raw_service).strip().casefold()
        if service not in GOOGLE_WORKSPACE_DASHBOARD_SERVICES:
            continue
        if service == "gmail":
            # The verified dashboard catalog exposes Gmail send, not inbox or
            # mailbox-modification access.
            if any(term in text for term in ("kirim", "balas", "send", "reply")):
                policy[service] = ["send"]
            continue
        if service == "drive":
            # drive.file is the single verified Drive scope and is limited to
            # files created or opened by this app.
            policy[service] = ["write"]
            continue

        operations = ["read"]
        if wants_write and service in _WRITE_SCOPES:
            operations.append("write")
        policy[service] = operations
    return policy


def reconcile_google_service_operations(
    requirement_text: str,
    services: Iterable[str],
    configured_operations: dict[str, Iterable[str]] | None = None,
) -> dict[str, list[str]]:
    """Return a canonical policy for old and new managed-agent configs.

    Older dashboard records used ``manage`` while current runtime records use
    ``write``. Missing operations are inferred only for the already-allowed
    services, so reconciliation cannot silently enable a new Google product.
    """
    normalized_services = [
        str(service).strip().casefold()
        for service in services
        if str(service).strip().casefold() in GOOGLE_WORKSPACE_DASHBOARD_SERVICES
    ]
    inferred = infer_google_service_operations(requirement_text, normalized_services)
    configured = configured_operations if isinstance(configured_operations, dict) else {}
    policy: dict[str, list[str]] = {}
    for service in normalized_services:
        raw_operations = {
            str(operation).strip().casefold()
            for operation in configured.get(service, [])
            if str(operation).strip()
        }
        if "manage" in raw_operations:
            raw_operations.add("write")
        raw_operations.discard("manage")
        raw_operations.update(inferred.get(service, []))
        if service == "gmail":
            if "send" in raw_operations:
                policy[service] = ["send"]
            continue
        if service == "drive":
            policy[service] = ["write"]
            continue
        ordered = [operation for operation in ("read", "write") if operation in raw_operations]
        if ordered:
            policy[service] = ordered
    return policy


def oauth_scopes_for_google_permissions(permissions: dict[str, Iterable[str]]) -> list[str]:
    """Return dashboard-approved OAuth scopes for a validated policy.

    A manage/write scope implies its readonly counterpart, matching the
    dashboard's ``getMcpToolScopes`` behavior.
    """
    scopes = list(GOOGLE_WORKSPACE_BASELINE_SCOPES)
    for raw_service, raw_operations in permissions.items():
        service = str(raw_service).strip().casefold()
        operations = {str(value).strip().casefold() for value in raw_operations}
        if "manage" in operations:
            operations.add("write")
        if service == "gmail" and "send" in operations:
            scopes.append("https://www.googleapis.com/auth/gmail.send")
        elif service == "drive" and operations.intersection({"read", "write"}):
            scopes.append("https://www.googleapis.com/auth/drive.file")
        elif "write" in operations and service in _WRITE_SCOPES:
            scopes.append(_WRITE_SCOPES[service])
        elif "read" in operations and service in _READ_SCOPES:
            scopes.append(_READ_SCOPES[service])
    return list(dict.fromkeys(scopes))
