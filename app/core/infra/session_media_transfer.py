"""Short-lived, content-bound transfer of one registered session attachment.

The Google MCP server cannot read an agent sandbox path. Its upload tool can
fetch this signed URL instead; no public directory or shared tenant mount exists.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import time
from urllib.parse import unquote, urlsplit
import uuid

import jwt

from app.config import get_settings

AUDIENCE = "session-media-transfer-v1"
TTL_SECONDS = 120
MAX_BYTES = 20 * 1024 * 1024


def _key():
    settings = get_settings()
    secret = settings.channel_secret_key or settings.api_key
    if not secret:
        raise ValueError("Attachment transfer signing is not configured")
    return hashlib.sha256((AUDIENCE + ":" + secret).encode()).digest()


def _read_registered(session, entry):
    root = (Path(get_settings().sandbox_base_dir) / str(session.id)).resolve()
    path = Path(entry["host_path"]).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("Attachment source is unavailable")
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Attachment is too large")
    return data


def build_session_media_url(session, requested_path):
    from app.core.engine.tool_builder import _resolve_session_media_file

    parsed = urlsplit(requested_path)
    if parsed.scheme == "file":
        if parsed.netloc:
            raise ValueError("Attachment source is unavailable")
        requested_path = unquote(parsed.path)
    if not requested_path.startswith("/workspace/"):
        raise ValueError("Only registered session attachment paths can be uploaded")
    entry = _resolve_session_media_file(session, requested_path, allowed_media_types={"image", "document", "sticker"})
    if entry is None:
        raise ValueError("Attachment is not registered in this conversation")
    data = _read_registered(session, entry)
    now = int(time.time())
    token = jwt.encode({"aud": AUDIENCE, "iat": now, "exp": now + TTL_SECONDS,
                        "sid": str(session.id), "aid": str(session.agent_id),
                        "sha": hashlib.sha256(data).hexdigest(),
                        "reset": (getattr(session, "metadata_", None) or {}).get("context_reset_at")},
                       _key(), algorithm="HS256")
    base = get_settings().app_public_url.rstrip("/")
    if urlsplit(base).scheme not in {"http", "https"}:
        raise ValueError("Public attachment transfer URL is not configured")
    return f"{base}/v1/session-media/{token}"


def decode_session_media_token(token):
    try:
        claims = jwt.decode(token, _key(), algorithms=["HS256"], audience=AUDIENCE,
                            options={"require": ["exp", "iat", "sid", "aid", "sha"]})
        uuid.UUID(claims["sid"])
        uuid.UUID(claims["aid"])
        if claims["exp"] - claims["iat"] > TTL_SECONDS:
            raise ValueError("Invalid transfer lifetime")
        return claims
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid or expired attachment transfer") from exc


def read_session_media_transfer(session, claims):
    from app.core.engine.tool_builder import _session_media_file_candidates

    if str(session.id) != claims["sid"] or str(session.agent_id) != claims["aid"]:
        raise ValueError("Attachment context changed")
    if (getattr(session, "metadata_", None) or {}).get("context_reset_at") != claims.get("reset"):
        raise ValueError("Attachment context was reset")
    for entry in _session_media_file_candidates(session):
        if entry.get("media_type") not in {"image", "document", "sticker"}:
            continue
        try:
            data = _read_registered(session, entry)
        except (OSError, ValueError):
            continue
        if hashlib.sha256(data).hexdigest() == claims["sha"]:
            return data, entry
    raise ValueError("Attachment is no longer available")
