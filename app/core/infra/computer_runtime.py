"""Bounded VNC adapter for an owner-assigned interactive computer.

This adapter intentionally does not reuse DockerSandbox. Docker continues to
own code, files, and deployment. The computer adapter controls only the VNC
desktop explicitly assigned by runtime configuration.
"""
from __future__ import annotations

import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


_KEY_ALIASES = {
    "ALT": "alt", "BACKSPACE": "bsp", "CTRL": "ctrl", "DELETE": "delete",
    "DOWN": "down", "END": "end", "ENTER": "enter", "ESC": "esc",
    "HOME": "home", "LEFT": "left", "PAGEDOWN": "pgdn", "PAGEUP": "pgup",
    "RIGHT": "right", "SHIFT": "shift", "SPACE": "space", "TAB": "tab", "UP": "up",
}
_SENSITIVE_MARKERS = ("password", "passcode", "otp", "one-time code", "verification code", "api key", "access token")


@dataclass
class ComputerRuntime:
    enabled: bool
    owner_id: str
    vnc_host: str
    vnc_port: int
    viewer_url: str
    timeout_seconds: int = 12
    max_actions: int = 20
    _actions: int = field(default=0, init=False)

    @classmethod
    def from_settings(cls, settings: Any) -> "ComputerRuntime":
        return cls(
            enabled=bool(getattr(settings, "computer_runtime_enabled", False)),
            owner_id=str(getattr(settings, "computer_runtime_owner_id", "") or "").strip(),
            vnc_host=str(getattr(settings, "computer_runtime_vnc_host", "127.0.0.1") or "127.0.0.1"),
            vnc_port=int(getattr(settings, "computer_runtime_vnc_port", 5902) or 5902),
            viewer_url=str(getattr(settings, "computer_runtime_viewer_url", "") or "").strip(),
            timeout_seconds=max(1, int(getattr(settings, "computer_runtime_timeout_seconds", 12) or 12)),
            max_actions=max(1, int(getattr(settings, "computer_runtime_max_actions_per_run", 20) or 20)),
        )

    def status(self, *, owner_id: str | None, is_platform_admin: bool = False) -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "code": "computer_runtime_disabled", "message": "Computer runtime belum diaktifkan oleh platform."}
        if not self.owner_id:
            return {"ok": False, "code": "computer_unassigned", "message": "Computer belum ditetapkan ke owner mana pun."}
        if not is_platform_admin and str(owner_id or "") != self.owner_id:
            return {"ok": False, "code": "computer_not_assigned", "message": "Computer ini tidak ditetapkan ke owner assistant ini."}
        try:
            with socket.create_connection((self.vnc_host, self.vnc_port), timeout=self.timeout_seconds):
                pass
        except OSError:
            return {"ok": False, "code": "computer_unavailable", "message": "Computer yang ditetapkan sedang offline atau tidak dapat dijangkau."}
        return {
            "ok": True,
            "status": "ready",
            "viewer_url": self.viewer_url or None,
            "takeover_supported": bool(self.viewer_url),
            "remaining_actions": self.max_actions - self._actions,
        }

    def open_url(self, *, owner_id: str | None, url: str) -> dict[str, Any]:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {"ok": False, "code": "invalid_url", "message": "URL harus memakai http:// atau https://."}
        return self._run(owner_id=owner_id, commands=["key", "ctrl-l"], text=url.strip(), trailing_key="enter")

    def click(self, *, owner_id: str | None, x: int, y: int) -> dict[str, Any]:
        if not (0 <= x <= 3840 and 0 <= y <= 2160):
            return {"ok": False, "code": "invalid_coordinates", "message": "Koordinat klik berada di luar batas layar yang diizinkan."}
        return self._run(owner_id=owner_id, commands=["move", str(x), str(y), "click", "1"])

    def type_text(self, *, owner_id: str | None, text: str) -> dict[str, Any]:
        clean = text.strip()
        if not clean:
            return {"ok": False, "code": "empty_text", "message": "Teks yang diketik tidak boleh kosong."}
        if len(clean) > 2_000:
            return {"ok": False, "code": "text_too_long", "message": "Teks dibatasi 2.000 karakter per aksi."}
        if any(marker in clean.lower() for marker in _SENSITIVE_MARKERS):
            return {"ok": False, "code": "sensitive_input_blocked", "message": "Jangan mengetik kredensial, OTP, atau token. Minta owner mengambil alih komputer melalui viewer."}
        return self._run(owner_id=owner_id, text=clean)

    def press_key(self, *, owner_id: str | None, key: str) -> dict[str, Any]:
        raw_parts = key.strip().upper().replace("+", "-").split("-")
        if not raw_parts or any(part not in _KEY_ALIASES and not (len(part) == 1 and part.isalnum()) for part in raw_parts):
            return {"ok": False, "code": "unsupported_key", "message": "Key tidak didukung. Gunakan tombol navigasi atau kombinasi CTRL/ALT/SHIFT dengan huruf/angka."}
        normalized = "-".join(_KEY_ALIASES.get(part, part.lower()) for part in raw_parts)
        return self._run(owner_id=owner_id, commands=["key", normalized])

    def _run(
        self,
        *,
        owner_id: str | None,
        commands: list[str] | None = None,
        text: str | None = None,
        trailing_key: str | None = None,
    ) -> dict[str, Any]:
        status = self.status(owner_id=owner_id)
        if not status.get("ok"):
            return status
        if self._actions >= self.max_actions:
            return {"ok": False, "code": "computer_action_limit", "message": "Batas aksi komputer untuk satu run tercapai. Laporkan progres atau minta instruksi lanjutan."}
        # The API process may be launched through a virtualenv while its PATH
        # remains the host PATH. Resolve the companion console script from the
        # active interpreter first so a correctly installed driver is usable.
        # Keep the virtualenv launcher path intact. ``resolve()`` follows the
        # common ``.venv/bin/python -> /usr/bin/python`` symlink and would
        # incorrectly look for ``/usr/bin/vncdotool`` instead of the driver
        # installed beside the active virtualenv interpreter.
        driver = Path(sys.executable).parent / "vncdotool"
        command = [str(driver) if driver.is_file() else "vncdotool", "-s", f"{self.vnc_host}::{self.vnc_port}"]
        command.extend(commands or [])
        if text is not None:
            command.extend(["type", text])
        if trailing_key:
            command.extend(["key", trailing_key])
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=self.timeout_seconds)
        except FileNotFoundError:
            return {"ok": False, "code": "computer_driver_missing", "message": "Driver VNC belum terpasang pada runtime aplikasi."}
        except subprocess.TimeoutExpired:
            return {"ok": False, "code": "computer_action_timeout", "message": "Aksi komputer melebihi batas waktu."}
        if result.returncode != 0:
            return {"ok": False, "code": "computer_action_failed", "message": "Aksi komputer gagal dijalankan.", "detail": (result.stderr or result.stdout).strip()[:300]}
        self._actions += 1
        return {"ok": True, "status": "action_sent", "remaining_actions": self.max_actions - self._actions}
