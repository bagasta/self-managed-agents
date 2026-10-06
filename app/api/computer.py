"""Authenticated access to the owner-assigned interactive computer.

The endpoint exposes only the viewer handoff. Bot actions remain limited to
the VNC tools injected into opted-in agents. Platform admin can take over any
configured computer; an owner-bound key can take over only its own assignment.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.workforce import WorkforcePrincipal, get_workforce_principal
from app.config import get_settings
from app.core.infra.computer_runtime import ComputerRuntime


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
    )
    if result.get("ok"):
        return result
    code = str(result.get("code") or "computer_unavailable")
    if code in {"computer_not_assigned", "computer_unassigned"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=result["message"])
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=result["message"])
