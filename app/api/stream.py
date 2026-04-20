"""
SSE (Server-Sent Events) endpoint — real-time push ke UI.

Endpoint:
  GET /v1/sessions/{session_id}/stream

Client (UI) connect ke endpoint ini dan terima event saat:
  - Scheduler mengirim reminder/proactive message
  - (future) agent sedang berpikir / tool call progress

Format event SSE:
  event: message
  data: {"type": "scheduled_message", "reply": "...", "label": "...", "run_id": "..."}

  event: ping
  data: {}

Koneksi otomatis ditutup setelah `timeout` detik (default 5 menit) jika tidak ada event.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import AsyncIterator

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import event_bus
from app.database import get_db
from app.models.session import Session

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/sessions", tags=["stream"])

_PING_INTERVAL = 20  # detik — keep-alive agar koneksi tidak di-drop proxy


async def _verify_stream_key(
    api_key: str | None = Query(default=None, alias="api_key"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Auth untuk SSE: terima key via header ATAU query param (EventSource tidak support header)."""
    from app.config import get_settings
    key = x_api_key or api_key
    if not key or key != get_settings().api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@router.get("/{session_id}/stream")
async def session_stream(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_verify_stream_key),
    timeout: int = Query(default=300, ge=30, le=3600, description="Max koneksi dalam detik"),
):
    """
    SSE stream — terima event real-time dari session ini.

    Gunakan ini untuk tampilkan scheduled reminder / proactive message di UI
    tanpa perlu polling.

    **Event types:**
    - `message` — agent reply baru (dari scheduler atau event lain)
    - `ping` — keep-alive setiap 20 detik
    """
    session = (
        await db.execute(select(Session).where(Session.id == session_id))
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

    sid = str(session_id)
    log = logger.bind(session_id=sid)
    log.info("stream.connected")

    async def _event_generator() -> AsyncIterator[str]:
        q = event_bus.subscribe(sid)
        try:
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    log.info("stream.timeout")
                    break

                wait = min(_PING_INTERVAL, remaining)
                try:
                    event = await asyncio.wait_for(q.get(), timeout=wait)
                    event_type = event.pop("_event_type", "message")
                    yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Keep-alive ping
                    yield "event: ping\ndata: {}\n\n"
        finally:
            event_bus.unsubscribe(sid, q)
            log.info("stream.disconnected")

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Connection": "keep-alive",
        },
    )
