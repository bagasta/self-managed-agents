"""Daily, deduplicated Meta credential health checks for connected agents."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import structlog
from sqlalchemy import select

from app.config import get_settings
from app.core.infra.channel_service import decrypt_value
from app.core.infra.redis_client import get_redis
from app.database import AsyncSessionLocal
from app.models.agent import Agent

logger = structlog.get_logger(__name__)


def _masked_phone_id(value: str | None) -> str:
    text = str(value or "")
    return f"***{text[-4:]}" if text else "(missing)"


async def _notify(payload: dict) -> None:
    target = get_settings().meta_token_health_alert_webhook_url.strip()
    if not target:
        logger.warning("meta_token_health.alert_target_missing", **payload)
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(target, json=payload)
            response.raise_for_status()
    except Exception as exc:
        logger.warning("meta_token_health.alert_delivery_failed", error_type=type(exc).__name__)


async def _previous_state(agent_id: str) -> str | None:
    redis = await get_redis()
    if redis is None:
        return None
    try:
        value = await redis.get(f"meta-token-health:{agent_id}")
        return str(value) if value else None
    except Exception:
        return None


async def _remember_state(agent_id: str, value: str) -> None:
    redis = await get_redis()
    if redis is not None:
        try:
            await redis.set(f"meta-token-health:{agent_id}", value, ex=172800)
        except Exception:
            pass


async def run_meta_token_health_check() -> dict[str, int]:
    """Validate token and phone access; alert only on failure/recovery transitions."""
    settings = get_settings()
    async with AsyncSessionLocal() as db:
        agents = list((await db.execute(select(Agent).where(
            Agent.wa_connection_type == "cloud_api",
            Agent.wa_phone_number_id.is_not(None),
            Agent.wa_access_token_encrypted.is_not(None),
        ))).scalars().all())

    healthy = failed = 0
    app_token = f"{settings.meta_app_id}|{settings.meta_app_secret}"
    async with httpx.AsyncClient(timeout=20) as client:
        for agent in agents:
            event = {
                "event": "meta.token_health",
                "source": "managed-agent",
                "agent_id": str(agent.id),
                "agent_name": agent.name,
                "phone_number_id_masked": _masked_phone_id(agent.wa_phone_number_id),
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            status_value = "ok"
            error_type = None
            try:
                token = decrypt_value(str(agent.wa_access_token_encrypted))
                debug = await client.get(
                    f"https://graph.facebook.com/{settings.meta_graph_api_version}/debug_token",
                    params={"input_token": token, "access_token": app_token},
                )
                debug.raise_for_status()
                if not bool((debug.json().get("data") or {}).get("is_valid")):
                    raise RuntimeError("meta_token_invalid")
                phone = await client.get(
                    f"https://graph.facebook.com/{settings.meta_graph_api_version}/{agent.wa_phone_number_id}",
                    params={"fields": "id,is_on_biz_app,platform_type"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                phone.raise_for_status()
            except Exception as exc:
                status_value = "failed"
                error_type = type(exc).__name__

            previous = await _previous_state(str(agent.id))
            await _remember_state(str(agent.id), status_value)
            if status_value == "ok":
                healthy += 1
                if previous == "failed":
                    await _notify({**event, "status": "recovered"})
            else:
                failed += 1
                logger.warning("meta_token_health.failed", error_type=error_type, **event)
                if previous != "failed":
                    await _notify({**event, "status": "failed", "error_type": error_type})

    logger.info("meta_token_health.completed", checked=len(agents), healthy=healthy, failed=failed)
    return {"checked": len(agents), "healthy": healthy, "failed": failed}
