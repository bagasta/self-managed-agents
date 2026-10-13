"""HTTP response hardening for the browser-facing API and UI."""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import get_settings


def _viewer_origin() -> str | None:
    from urllib.parse import urlsplit

    try:
        viewer = urlsplit(get_settings().computer_runtime_viewer_url)
        if viewer.scheme not in {"http", "https"} or not viewer.hostname:
            return None
        # The viewer URL is configured by the operator.  Keep the exception
        # exact to this origin rather than permitting arbitrary frames.
        return f"{viewer.scheme}://{viewer.netloc}"
    except ValueError:
        return None


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add baseline browser protections without exposing any secret material."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        if request.url.path.startswith(("/v1/computer/input-session", "/v1/computer/private-input")):
            response.headers["Cache-Control"] = "no-store"
        viewer_origin = _viewer_origin()
        frame_sources = "'self'" + (f" {viewer_origin}" if viewer_origin else "")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
            "form-action 'self'; object-src 'none'; img-src 'self' data: blob:; "
            f"frame-src {frame_sources}; "
            "script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response
