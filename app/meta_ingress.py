"""Standalone Meta webhook ingress, isolated from the AI Staff runtime."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy import select, text

from app.api.meta_webhooks import _account_update_summary, _process_n8n
from app.config import get_settings
from app.core.infra.meta_embedded_signup import verify_webhook_signature
from app.database import AsyncSessionLocal, engine
from app.models.agent import Agent

logger = structlog.get_logger(__name__)
settings = get_settings()
AI_STAFF_WEBHOOK_URL = "http://api:8000/v1/webhooks/meta"
INTERNAL_ROUTE_HEADER = "X-Managed-Meta-Route"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(
    title="Managed Agent Meta Ingress",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


async def _forward_ai_staff(body: bytes, signature: str) -> None:
    """Forward only the AI Staff branch to the internal application API."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=3)) as client:
            response = await client.post(
                AI_STAFF_WEBHOOK_URL,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": signature,
                    INTERNAL_ROUTE_HEADER: "ai_staff",
                },
            )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - isolation boundary must contain AI runtime failure
        # AI Staff failure is intentionally contained here. It must never stop
        # n8n delivery handled by this standalone process.
        logger.error("meta_ingress.ai_staff_forward_failed", error_type=type(exc).__name__)


@app.get("/health")
async def health() -> dict:
    async with AsyncSessionLocal() as db:
        await db.execute(text("SELECT 1"))
    return {"status": "ok", "service": "meta-ingress"}


@app.get("/v1/webhooks/meta")
async def verify_meta_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
) -> Response:
    if (
        hub_mode == "subscribe"
        and hub_challenge
        and hub_verify_token
        and hub_verify_token == settings.meta_webhook_verify_token
    ):
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Webhook verification failed")


@app.post("/v1/webhooks/meta")
async def receive_meta_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256") or ""
    if not verify_webhook_signature(body, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON") from exc
    if payload.get("object") != "whatsapp_business_account":
        return {"ok": True, "ignored": True, "service": "meta-ingress"}

    forward_ai_staff = False
    async with AsyncSessionLocal() as db:
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                if _account_update_summary(change) is not None:
                    # Signup handoff remains owned by the main control plane.
                    forward_ai_staff = True
                    continue
                value = change.get("value") or {}
                metadata = value.get("metadata") or {}
                phone_number_id = str(metadata.get("phone_number_id") or "")
                if change.get("field") != "messages" or not phone_number_id:
                    continue
                agent = (
                    await db.execute(
                        select(Agent).where(
                            Agent.wa_phone_number_id == phone_number_id,
                            Agent.is_deleted.is_(False),
                        )
                    )
                ).scalar_one_or_none()
                if agent is None:
                    continue
                if str(agent.wa_inbound_route or "ai_staff") != "n8n":
                    forward_ai_staff = True
                    continue
                contact_names = {
                    str(item.get("wa_id") or ""): str(item.get("profile", {}).get("name") or "")
                    for item in value.get("contacts") or []
                }
                for message in value.get("messages") or []:
                    if message.get("type") in {"text", "image", "document"}:
                        background_tasks.add_task(
                            _process_n8n,
                            str(agent.id),
                            phone_number_id,
                            message,
                            contact_names.get(str(message.get("from") or ""), ""),
                        )

    if forward_ai_staff:
        background_tasks.add_task(_forward_ai_staff, body, signature)
    return {"ok": True, "service": "meta-ingress"}
