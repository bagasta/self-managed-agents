"""Google Workspace helper functions for Arthur builder tools."""
from __future__ import annotations

import re
from typing import Any

from app.config import get_settings


def google_workspace_mcp_server_config() -> dict[str, str]:
    settings = get_settings()
    return {
        "url": settings.workspace_mcp_url or "https://msj90wr2-8002.asse.devtunnels.ms/mcp",
        "transport": "streamable_http",
    }


def enable_google_workspace_tools(tools_config: dict[str, Any] | None) -> dict[str, Any]:
    """Enable Google Workspace tooling without clobbering other tool config."""
    merged = dict(tools_config or {})
    raw_mcp = merged.get("mcp")
    mcp_cfg = dict(raw_mcp) if isinstance(raw_mcp, dict) else {}

    if "servers" in mcp_cfg or "enabled" in mcp_cfg:
        servers = dict(mcp_cfg.get("servers") or {})
    else:
        servers = {
            name: dict(cfg)
            for name, cfg in mcp_cfg.items()
            if isinstance(cfg, dict)
        }

    existing_google = dict(servers.get("google_workspace") or {})
    google_cfg = google_workspace_mcp_server_config()
    existing_google.setdefault("url", google_cfg["url"])
    existing_google.setdefault("transport", google_cfg["transport"])
    servers["google_workspace"] = existing_google

    mcp_cfg["enabled"] = True
    mcp_cfg["servers"] = servers
    # Google data belongs to the agent owner by default. Customer-scoped OAuth
    # must be explicitly selected for products where every end user connects
    # their own Google account.
    mcp_cfg.setdefault("auth_mode", "owner")
    merged["mcp"] = mcp_cfg
    merged.setdefault("tavily", True)
    return merged


def has_google_workspace_tools(tools_config: dict[str, Any] | None) -> bool:
    if not isinstance(tools_config, dict):
        return False
    mcp_cfg = tools_config.get("mcp")
    if not isinstance(mcp_cfg, dict):
        return False
    if "servers" in mcp_cfg or "enabled" in mcp_cfg:
        return bool(mcp_cfg.get("enabled")) and "google_workspace" in (mcp_cfg.get("servers") or {})
    return isinstance(mcp_cfg.get("google_workspace"), dict)


def google_workspace_option(feature_text: str, explicit_google: bool) -> dict[str, Any]:
    text = (feature_text or "").lower()
    app_reasons: list[tuple[str, str]] = []

    def add(app: str, reason: str) -> None:
        if not any(existing == app for existing, _ in app_reasons):
            app_reasons.append((app, reason))

    if any(k in text for k in ("gmail", "email", "inbox", "kirim email", "balas email")):
        add("Gmail", "membaca atau mengirim email dari akun user")
    if any(k in text for k in ("calendar", "kalender", "jadwal", "reminder", "pengingat", "meeting", "deadline", "h-7", "h-1")):
        add("Google Calendar", "membuat jadwal dan pengingat langsung di kalender user")
    if any(k in text for k in ("docs", "google docs", "laporan", "notulen", "proposal", "surat", "itinerary", "checklist")):
        add("Google Docs", "membuat atau memperbarui dokumen yang bisa dibuka user")
    if any(k in text for k in ("sheets", "spreadsheet", "excel", "tabel", "budget", "anggaran", "laporan angka")):
        add("Google Sheets", "menyimpan data, budget, atau tabel dalam spreadsheet")
    if any(k in text for k in ("drive", "file", "folder", "upload", "lampiran", "dokumen referensi")):
        add("Google Drive", "menyimpan dan membaca file dari Drive user")

    should_offer = bool(app_reasons)
    apps = [app for app, _ in app_reasons]
    reasons = [reason for _, reason in app_reasons]
    if explicit_google and not apps:
        apps = ["Google Workspace"]
        reasons = ["menghubungkan agent ke akun Google user"]
        should_offer = True

    if not should_offer:
        return {
            "should_offer": False,
            "enabled": False,
            "suggested_apps": [],
            "reasons": [],
            "user_facing_pitch": "",
            "if_user_declines": "Lanjutkan tanpa integrasi Google.",
        }

    app_text = ", ".join(apps)
    pitch = (
        f"Kebutuhan ini bisa lebih praktis kalau agent terhubung ke {app_text}: "
        f"{'; '.join(reasons)}. Mau saya konekkan ke Google, atau dibuat tanpa Google dulu?"
    )
    if explicit_google:
        pitch = (
            f"Karena kamu sudah minta pakai {app_text}, agent akan saya siapkan dengan integrasi Google. "
            "Nanti kamu tinggal buka link login Google supaya agent bisa akses akunmu."
        )

    return {
        "should_offer": should_offer and not explicit_google,
        "enabled": explicit_google,
        "suggested_apps": apps,
        "reasons": reasons,
        "user_facing_pitch": pitch,
        "if_user_accepts": "Panggil plan_agent lagi dengan requested_features memuat google, lalu create/update dengan integrasi Google aktif.",
        "if_user_declines": "Lanjutkan tanpa integrasi Google; agent tetap bisa berjalan dengan memory/reminder internal sesuai tools yang tersedia.",
    }


def negates_google_workspace(text: str) -> bool:
    lowered = (text or "").lower()
    patterns = (
        r"\b(tanpa|jangan|tidak|ga|gak|nggak|enggak|belum|nanti)\b.{0,32}\b(google|workspace|gmail|calendar|drive|docs|sheets)\b",
        r"\b(google|workspace|gmail|calendar|drive|docs|sheets)\b.{0,32}\b(tanpa|jangan|tidak|ga|gak|nggak|enggak|belum|nanti)\b",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)
