"""Serialize local VNC access and keep short human-input leases across workers."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class ComputerInUse(Exception):
    pass


@contextmanager
def computer_access(host: str, port: int):
    identity = hashlib.sha256(f"{host}:{port}".encode()).hexdigest()[:24]
    path = Path(tempfile.gettempdir()) / f"managed-agents-computer-{identity}.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ComputerInUse from exc
        try:
            raw = handle.read()
            try:
                lease = json.loads(raw) if raw else {}
                if not isinstance(lease, dict):
                    raise ValueError
            except ValueError as exc:
                raise ComputerInUse from exc
            if lease.get("expires_at", 0) <= time.time():
                lease = {}
            yield lease
            handle.seek(0)
            handle.truncate()
            json.dump(lease, handle)
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def issue_lease(lease: dict, principal: str) -> dict:
    if lease:
        return {"ok": False, "code": "human_input_active", "message": "Sesi input pribadi lain masih terbuka. Tutup sesi itu atau tunggu lima menit."}
    token = secrets.token_urlsafe(32)
    lease.update(token_hash=hashlib.sha256(token.encode()).hexdigest(), principal=principal, expires_at=time.time() + 300)
    return {"ok": True, "token": token, "expires_at": lease["expires_at"]}


def owns_lease(lease: dict, principal: str, token: str) -> bool:
    return bool(lease and lease.get("principal") == principal and token and secrets.compare_digest(
        str(lease.get("token_hash", "")), hashlib.sha256(token.encode()).hexdigest(),
    ))


def paused_result() -> dict:
    return {"ok": False, "code": "human_input_active", "message": "Owner sedang mengisi data pribadi. Jangan mengoperasikan atau mengambil screenshot komputer sampai sesi input ditutup. Lanjutkan pekerjaan lain yang independen.", "requires_human_takeover": True, "continue_independent_tasks": True}
