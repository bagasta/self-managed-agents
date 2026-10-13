"""FIFO turns for the one shared desktop, held across a bot's tool calls."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path


_run_turns: ContextVar[list | None] = ContextVar("computer_run_turns", default=None)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@contextmanager
def _queue(host: str, port: int):
    key = hashlib.sha256(f"{host}:{port}".encode()).hexdigest()[:24]
    path = Path(tempfile.gettempdir()) / f"managed-agents-turns-{key}.json"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            raw = handle.read()
            state = json.loads(raw) if raw else {"current": None, "waiting": []}
            now = time.time()
            current = state.get("current")
            if current and (current["expires_at"] <= now or not _alive(current["pid"])):
                state["current"] = None
            state["waiting"] = [item for item in state.get("waiting", []) if item["expires_at"] > now and _alive(item["pid"])]
            yield state
            handle.seek(0)
            handle.truncate()
            json.dump(state, handle)
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def turn_status(host: str, port: int) -> dict:
    with _queue(host, port) as state:
        current = state.get("current") or {}
        return {"agent_id": current.get("agent_id"), "agent_name": current.get("agent_name"), "waiting_count": len(state["waiting"])}


class ComputerTurn:
    def __init__(self, host: str, port: int, agent_id: str, agent_name: str, wait_seconds: int):
        self.host, self.port = host, port
        self.agent_id, self.agent_name = agent_id, agent_name
        self.wait_seconds = wait_seconds
        self.token = uuid.uuid4().hex
        self.closed = threading.Event()
        turns = _run_turns.get()
        if turns is not None:
            turns.append(self)

    def acquire(self) -> dict:
        deadline = time.monotonic() + self.wait_seconds
        while not self.closed.is_set():
            with _queue(self.host, self.port) as state:
                if self.closed.is_set():
                    break
                current = state.get("current")
                if current and current["token"] == self.token:
                    current["expires_at"] = time.time() + 1800
                    return {"ok": True}
                if not any(item["token"] == self.token for item in state["waiting"]):
                    state["waiting"].append({"token": self.token, "pid": os.getpid(), "agent_id": self.agent_id, "agent_name": self.agent_name, "expires_at": time.time() + self.wait_seconds + 5})
                if not current and state["waiting"][0]["token"] == self.token:
                    state["current"] = state["waiting"].pop(0)
                    state["current"]["expires_at"] = time.time() + 1800
                    return {"ok": True}
            if time.monotonic() >= deadline:
                self.release()
                return {"ok": False, "code": "computer_turn_timeout", "message": "Komputer masih dipakai bot lain. Giliran belum diperoleh dalam batas tunggu; jangan mengklaim komputer tidak tersedia atau tugas telah selesai."}
            self.closed.wait(0.25)
        return {"ok": False, "code": "computer_run_finished", "message": "Run sudah berakhir; jangan mengirim aksi komputer lanjutan."}

    def release(self, *, close: bool = False) -> None:
        if close:
            self.closed.set()
        with _queue(self.host, self.port) as state:
            if (state.get("current") or {}).get("token") == self.token:
                state["current"] = None
            state["waiting"] = [item for item in state["waiting"] if item["token"] != self.token]


def computer_run_scope(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        turns: list[ComputerTurn] = []
        token = _run_turns.set(turns)
        try:
            return await function(*args, **kwargs)
        finally:
            for turn in turns:
                turn.closed.set()
            try:
                # Locks cover tiny metadata updates; no desktop I/O occurs here.
                for turn in turns:
                    await asyncio.to_thread(turn.release, close=True)
            finally:
                _run_turns.reset(token)
    return wrapped
