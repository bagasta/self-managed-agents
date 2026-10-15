"""Explicit operation policy for reconfiguring an existing Google connector."""
from __future__ import annotations

from app.core.google_oauth_scopes import infer_google_service_operations


def setup_google_permissions(agent, services, requested=None):
    config = agent.tools_config or {}
    mcp = config.get("mcp") or {}
    saved = (mcp.get("servers", mcp)).get("google_workspace") or {}
    previous = saved.get("allowed_operations") or {}
    requested = requested or {}
    if set(requested) - set(services):
        raise ValueError("Operasi Google hanya boleh diisi untuk layanan yang dipilih.")
    instructions = str(agent.instructions or "").split("# ATURAN PENGGUNAAN TOOLS", 1)[0]
    aliases = {"gmail": ("gmail", "email", "e-mail"), "calendar": ("calendar", "kalender")}
    policy = {}
    for service in services:
        if service in requested:
            allowed = {"send"} if service == "gmail" else {"read", "write"}
            operations = list(dict.fromkeys(requested[service]))
            if set(operations) - allowed:
                raise ValueError("Operasi Google tidak didukung. Gmail hanya send; layanan lain read/write.")
            policy[service] = operations
        elif service in previous:
            policy[service] = list(previous[service])
        elif service in aliases:
            import re
            sentences = re.split(r"[.\n;]+", instructions)
            context = "\n".join(s for s in sentences if any(alias in s.casefold() for alias in aliases[service]))
            inferred = infer_google_service_operations(context, [service])
            if service == "gmail" and not inferred.get("gmail"):
                raise ValueError("Jelaskan dan konfirmasikan operasi kirim email yang diperlukan sebelum mengaktifkan Gmail.")
            policy[service] = inferred.get(service, ["read"])
        else:
            policy.update(infer_google_service_operations(instructions, [service]))
    return policy
