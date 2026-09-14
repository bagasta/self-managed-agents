"""Least-privilege Google OAuth scope policy for managed agents.

The integration service receives concrete OAuth scopes, never a product name
whose meaning it has to expand to a broad default grant.
"""
from __future__ import annotations

from collections.abc import Iterable


_READ_SCOPES = {
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
    "calendar": "https://www.googleapis.com/auth/calendar.readonly",
    "drive": "https://www.googleapis.com/auth/drive.readonly",
    "docs": "https://www.googleapis.com/auth/documents.readonly",
    "sheets": "https://www.googleapis.com/auth/spreadsheets.readonly",
    "forms": "https://www.googleapis.com/auth/forms.body.readonly",
    "slides": "https://www.googleapis.com/auth/presentations.readonly",
    "tasks": "https://www.googleapis.com/auth/tasks.readonly",
    "contacts": "https://www.googleapis.com/auth/contacts.readonly",
    "chat": "https://www.googleapis.com/auth/chat.spaces.readonly",
}

_WRITE_SCOPES = {
    "calendar": "https://www.googleapis.com/auth/calendar",
    "docs": "https://www.googleapis.com/auth/documents",
    "sheets": "https://www.googleapis.com/auth/spreadsheets",
    "forms": "https://www.googleapis.com/auth/forms.body",
    "slides": "https://www.googleapis.com/auth/presentations",
    "tasks": "https://www.googleapis.com/auth/tasks",
    "contacts": "https://www.googleapis.com/auth/contacts",
    "chat": "https://www.googleapis.com/auth/chat.messages",
}


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
        if service not in _READ_SCOPES:
            continue
        operations = ["read"]
        if wants_write:
            if service == "gmail":
                if any(term in text for term in ("kirim", "balas", "send", "reply")):
                    operations.append("send")
                if any(term in text for term in ("hapus", "archive", "label", "modify", "delete")):
                    operations.append("modify")
            elif service == "drive":
                # drive.file grants access only to files created/opened by this app.
                operations.append("write")
            elif service in _WRITE_SCOPES:
                operations.append("write")
        policy[service] = operations
    return policy


def oauth_scopes_for_google_permissions(permissions: dict[str, Iterable[str]]) -> list[str]:
    """Return the smallest concrete OAuth scope set for a validated policy."""
    scopes: list[str] = []
    for raw_service, raw_operations in permissions.items():
        service = str(raw_service).strip().casefold()
        operations = {str(value).strip().casefold() for value in raw_operations}
        if "read" in operations and service in _READ_SCOPES:
            scopes.append(_READ_SCOPES[service])
        if service == "gmail":
            if "send" in operations:
                scopes.append("https://www.googleapis.com/auth/gmail.send")
            if "modify" in operations:
                scopes.append("https://www.googleapis.com/auth/gmail.modify")
        elif service == "drive" and "write" in operations:
            scopes.append("https://www.googleapis.com/auth/drive.file")
        elif "write" in operations and service in _WRITE_SCOPES:
            scopes.append(_WRITE_SCOPES[service])
    return list(dict.fromkeys(scopes))
