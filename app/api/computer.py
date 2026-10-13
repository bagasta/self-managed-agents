"""Authenticated access to the owner-assigned interactive computer.

Viewer, screenshot, and private-input endpoints are reserved for authenticated
humans. Bot actions remain limited to their opted-in VNC tools. Private input
temporarily excludes bot access to the same local runtime.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.workforce import WorkforcePrincipal, get_workforce_principal
from app.config import get_settings
from app.core.infra.computer_runtime import ComputerRuntime
from app.core.infra.computer_handoff import ComputerInUse, computer_access, issue_lease, owns_lease


router = APIRouter(prefix="/v1/computer", tags=["computer"])


def _runtime_result(principal: WorkforcePrincipal) -> dict:
    runtime = ComputerRuntime.from_settings(get_settings())
    result = runtime.status(
        owner_id=str(principal.owner_user_id) if principal.owner_user_id else None,
        is_platform_admin=principal.is_platform_admin,
    )
    if result.get("ok"):
        return result
    code = str(result.get("code") or "computer_unavailable")
    if code in {"computer_not_assigned", "computer_unassigned"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=result["message"])
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=result["message"])


@router.get("/status")
async def computer_status(
    principal: WorkforcePrincipal = Depends(get_workforce_principal),
) -> dict:
    """Return availability for the platform admin or the assigned owner."""
    return _runtime_result(principal)


@router.post("/takeover")
async def computer_takeover(
    principal: WorkforcePrincipal = Depends(get_workforce_principal),
) -> dict:
    """Return the shared viewer URL for a human takeover session."""
    result = _runtime_result(principal)
    if not result.get("viewer_url"):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Viewer komputer belum dikonfigurasi.")
    return {
        "ok": True,
        "viewer_url": result["viewer_url"],
        "shared": True,
        "note": "Gunakan takeover untuk login, OTP, password, atau keputusan sensitif.",
    }


@router.get("/snapshot")
async def computer_snapshot(
    principal: WorkforcePrincipal = Depends(get_workforce_principal),
) -> dict:
    """Return the actual VNC framebuffer used by computer tools.

    This is intentionally authenticated like takeover.  It lets a UI prove it
    is showing the same desktop the agent acts on, rather than treating an
    independently loaded noVNC iframe as evidence.
    """
    runtime = ComputerRuntime.from_settings(get_settings())
    result = runtime.capture_screen(
        owner_id=str(principal.owner_user_id) if principal.owner_user_id else None,
        is_platform_admin=principal.is_platform_admin,
        human_access=True,
    )
    if result.get("ok"):
        return result
    code = str(result.get("code") or "computer_unavailable")
    if code in {"computer_not_assigned", "computer_unassigned"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=result["message"])
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=result["message"])


async def _private_payload(request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if request.url.scheme != "https" and request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(400, "Input pribadi memerlukan HTTPS atau koneksi localhost.")
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, "Input pribadi harus berasal dari workspace ini.")
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "Gunakan formulir input pribadi.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16384:
            raise HTTPException(413, "Input terlalu panjang.")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Formulir tidak valid.") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "Formulir tidak valid.")
    return data


def _human_action(principal: WorkforcePrincipal, payload: dict, action: str) -> dict:
    runtime = ComputerRuntime.from_settings(get_settings())
    available = runtime.status(owner_id=str(principal.owner_user_id) if principal.owner_user_id else None, is_platform_admin=principal.is_platform_admin)
    if not available.get("ok"):
        raise HTTPException(403 if available.get("code") in {"computer_not_assigned", "computer_unassigned"} else 503, available["message"])
    identity = f"admin:{principal.owner_user_id or 'platform'}" if principal.is_platform_admin else f"owner:{principal.owner_user_id}"
    try:
        with computer_access(runtime.vnc_host, runtime.vnc_port) as lease:
            if action == "begin":
                result = issue_lease(lease, identity)
                if not result.get("ok"):
                    raise HTTPException(409, result["message"])
                return result
            token = payload.get("token")
            if not isinstance(token, str) or not owns_lease(lease, identity, token):
                raise HTTPException(409, "Sesi input pribadi berakhir. Buka kembali formulir.")
            if action == "end":
                lease.clear()
                return {"ok": True, "status": "released"}
            value, x, y = payload.get("value"), payload.get("x"), payload.get("y")
            if not isinstance(value, str) or not 1 <= len(value) <= 1000 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise HTTPException(400, "Isi satu nilai sepanjang 1–1.000 karakter tanpa baris baru.")
            if type(x) is not int or type(y) is not int or not 0 <= x <= 3840 or not 0 <= y <= 2160:
                raise HTTPException(400, "Pilih kolom tujuan pada tampilan komputer.")
            if type(payload.get("replace", True)) is not bool:
                raise HTTPException(400, "Pilihan penggantian kolom tidak valid.")
            worker_input = json.dumps({"server": f"{runtime.vnc_host}::{runtime.vnc_port}", "x": x, "y": y, "value": value, "replace": payload.get("replace", True)})
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "app.core.infra.computer_private_input"],
                    input=worker_input, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=max(runtime.timeout_seconds, len(value) * 0.05 + 10), check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise HTTPException(503, "Pengisian belum dapat dipastikan. Periksa kolom di komputer sebelum mencoba lagi.") from None
            if result.returncode:
                raise HTTPException(503, "Pengisian belum dapat dipastikan. Periksa kolom di komputer sebelum mencoba lagi.")
            return {"ok": True, "status": "input_sent", "message": "Input dikirim ke kolom pilihan. Periksa hasilnya di komputer."}
    except ComputerInUse:
        raise HTTPException(409, "Komputer sedang menjalankan aksi. Coba lagi sebentar.") from None


@router.post("/input-session")
async def begin_input_session(request: Request, response: Response, principal: WorkforcePrincipal = Depends(get_workforce_principal)) -> dict:
    payload = await _private_payload(request, response)
    return await asyncio.to_thread(_human_action, principal, payload, "begin")


@router.post("/input-session/end")
async def end_input_session(request: Request, response: Response, principal: WorkforcePrincipal = Depends(get_workforce_principal)) -> dict:
    payload = await _private_payload(request, response)
    return await asyncio.to_thread(_human_action, principal, payload, "end")


@router.post("/private-input")
async def private_input(request: Request, response: Response, principal: WorkforcePrincipal = Depends(get_workforce_principal)) -> dict:
    # No Pydantic request model: validation errors must never echo a raw secret.
    payload = await _private_payload(request, response)
    return await asyncio.to_thread(_human_action, principal, payload, "type")
