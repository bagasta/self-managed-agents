"""Safe Clevio Marketplace discovery surface.

ClawHub is a catalog and verified source for instruction-only skills. Clevio
never executes an OpenClaw package directly; external plugins need an adapter.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import re
import stat
import time
from typing import Any
from zipfile import BadZipFile, ZipFile

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.workforce import WorkforcePrincipal, get_workforce_principal
from app.config import get_settings
from app.core.domain.skill_service import create_or_update_skill
from app.database import get_db
from app.models.agent import Agent

router = APIRouter(prefix="/v1/marketplace", tags=["marketplace"])

_CLAW_HUB = "https://clawhub.ai/api/v1"
_CACHE_SECONDS = 300
_catalog_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


class SkillInstall(BaseModel):
    agent_id: str
    owner_handle: str = ""
    version: str | None = None
    review_sha256: str | None = None


def _native_items() -> list[dict[str, Any]]:
    configured = bool(get_settings().workspace_mcp_url and get_settings().workspace_mcp_token)
    return [{
        "id": "clevio:google-workspace", "kind": "plugin", "name": "Google Workspace",
        "description": "Drive, Docs, Sheets, dan layanan Google melalui konektor Clevio.",
        "publisher": "Clevio", "source": "native", "category": "Productivity",
        "status": "configured" if configured else "setup_required", "install_mode": "managed",
        "capabilities": ["Google Drive", "Docs", "Sheets"], "external_url": None,
    }]


def _matches_native_query(item: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    searchable = " ".join(str(item.get(field) or "") for field in ("name", "description", "publisher", "category"))
    return query.casefold() in searchable.casefold()


def _validate_skill_reference(slug: str, owner_handle: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", slug, re.I):
        raise HTTPException(422, "Slug skill tidak valid")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", owner_handle, re.I):
        raise HTTPException(422, "Publisher skill tidak valid")


def _normalize(item: dict[str, Any], kind: str) -> dict[str, Any] | None:
    name = str(item.get("displayName") or item.get("name") or item.get("slug") or "").strip()
    slug = str(item.get("slug") or item.get("id") or item.get("name") or "").strip()
    if not name or not slug:
        return None
    owner = item.get("owner") or item.get("publisher") or {}
    publisher = item.get("ownerHandle") or (owner.get("handle") if isinstance(owner, dict) else str(owner or "ClawHub"))
    version = item.get("version") or item.get("latestVersion") or (item.get("tags") or {}).get("latest")
    if isinstance(version, dict):
        version = version.get("version")
    canonical_url = item.get("canonicalUrl")
    if not isinstance(canonical_url, str) or not canonical_url.startswith("/") or canonical_url.startswith("//"):
        canonical_url = f"/{publisher}/skills/{slug}" if kind == "skill" else None
    return {
        "id": f"clawhub:{kind}:{publisher}:{slug}", "kind": kind, "name": name,
        "description": str(item.get("description") or item.get("summary") or "Tidak ada deskripsi dari registry."),
        "publisher": publisher or "ClawHub", "source": "clawhub", "category": str(item.get("category") or "Community"),
        "status": "compatibility_check" if kind == "skill" else "adapter_required",
        "install_mode": "managed_skill" if kind == "skill" else "adapter_required", "capabilities": [],
        "external_url": f"https://clawhub.ai{canonical_url}" if canonical_url else None,
        "version": version, "owner_handle": publisher,
    }


async def _clawhub_items(kind: str, query: str) -> list[dict[str, Any]]:
    cache_key = f"{kind}:{query.casefold()}"
    cached = _catalog_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    endpoint = "/search" if kind == "skill" and query else "/skills" if kind == "skill" else "/plugins/search" if query else "/plugins"
    params = {"q": query, "limit": 18} if query else {"limit": 18, "sort": "recommended"}
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            response = await client.get(f"{_CLAW_HUB}{endpoint}", params=params, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
        raw = payload.get("results") if query else payload.get("items")
        if not isinstance(raw, list):
            raise ValueError("ClawHub returned an unexpected catalog shape")
        normalized_items = [
            normalized for entry in raw if isinstance(entry, dict)
            if isinstance(item := (entry.get("package") if kind == "plugin" and query else entry), dict)
            if (normalized := _normalize(item, kind))
        ]
        items = list({item["id"].casefold(): item for item in normalized_items}.values())
    except (httpx.HTTPError, ValueError, TypeError) as error:
        raise HTTPException(502, "Katalog ClawHub sedang tidak tersedia. Coba lagi sebentar.") from error
    _catalog_cache[cache_key] = (time.monotonic(), items)
    return items


@router.get("/catalog")
async def catalog(
    kind: str = Query("all", pattern="^(all|skill|plugin)$"),
    q: str = Query("", max_length=100),
    _: WorkforcePrincipal = Depends(get_workforce_principal),
):
    requested = [kind] if kind != "all" else ["skill", "plugin"]
    external = await asyncio.gather(*(_clawhub_items(item_kind, q.strip()) for item_kind in requested))
    native = [item for item in _native_items() if _matches_native_query(item, q.strip())]
    items = native + [item for group in external for item in group]
    if kind != "all":
        items = [item for item in items if item["kind"] == kind]
    return {"items": items, "source": "clawhub-verified", "review_required": False,
            "community_install_enabled": get_settings().marketplace_community_skill_install_enabled}


def _skill_description(markdown: str, fallback: str) -> str:
    match = re.search(r"(?ms)^---\s*$.*?^description:\s*[\"']?(.+?)[\"']?\s*$.*?^---\s*$", markdown)
    return (match.group(1).strip() if match else fallback)[:20_000]


async def _clawhub_get(
    client: httpx.AsyncClient, path: str, *, params: dict[str, str] | None = None,
    retry_conflict: bool = False,
) -> httpx.Response:
    """Retry one transient upstream failure without bypassing verification."""
    for attempt in range(2):
        response = await client.get(f"{_CLAW_HUB}{path}", params=params)
        retryable = {502, 503, 504}
        if retry_conflict:
            retryable.add(409)
        if response.status_code not in retryable or attempt:
            return response
        await asyncio.sleep(0.35)
    raise AssertionError("unreachable")


async def _skill_file_bytes(client: httpx.AsyncClient, slug: str, version: str) -> bytes:
    """Read the whole archive so instruction-only imports cannot silently drop hooks."""
    archive_response = await _clawhub_get(
        client, "/download", params={"slug": slug, "version": version},
    )
    archive_response.raise_for_status()
    if len(archive_response.content) > 2_000_000:
        raise HTTPException(413, "Arsip skill terlalu besar untuk ditinjau")
    if "application/json" in archive_response.headers.get("content-type", ""):
        raise HTTPException(
            422,
            "Paket tidak kompatibel: ClawHub hanya menyediakan rujukan sumber GitHub, bukan arsip yang dapat diverifikasi Clevio.",
            headers={"X-Marketplace-Error-Code": "incompatible"},
        )
    try:
        with ZipFile(io.BytesIO(archive_response.content)) as archive:
            files = [entry for entry in archive.infolist() if not entry.is_dir()]
            if len(files) > 32:
                raise HTTPException(413, "Paket skill memiliki terlalu banyak berkas untuk diimpor")
            candidates = [entry for entry in files
                          if entry.filename.rsplit("/", 1)[-1].casefold() == "skill.md"]
            if len(candidates) != 1:
                raise HTTPException(422, "Arsip skill tidak memiliki tepat satu SKILL.md")
            for entry in files:
                parts = entry.filename.split("/")
                if (any(part in {"", ".", ".."} for part in parts)
                        or "\\" in entry.filename
                        or any(ord(char) < 32 for char in entry.filename)):
                    raise HTTPException(422, "Arsip skill memiliki nama berkas yang tidak valid")
                if stat.S_IFMT(entry.external_attr >> 16) == stat.S_IFLNK:
                    raise HTTPException(422, "Arsip skill tidak boleh berisi tautan simbolis")
            references = [entry for entry in files if entry is not candidates[0]]
            unsupported = [
                entry for entry in files
                if any(part.casefold() in {"hooks", "scripts", "bin", "src"}
                       for part in entry.filename.split("/")[:-1])
                or (entry is not candidates[0]
                    and not entry.filename.casefold().endswith((".md", ".markdown")))
            ]
            if unsupported:
                raise HTTPException(
                    422,
                    "Paket tidak kompatibel: skill ini menyertakan berkas kode atau aset yang belum didukung runtime Clevio.",
                    headers={"X-Marketplace-Error-Code": "incompatible"},
                )
            content = bytearray()
            for entry in [candidates[0], *sorted(references, key=lambda item: item.filename)]:
                if entry is not candidates[0]:
                    content.extend(f"\n\n---\n\n# Referensi paket: {entry.filename}\n\n".encode("utf-8"))
                if len(content) > 200_000:
                    raise HTTPException(413, "Instruksi skill terlalu besar untuk diimpor")
                remaining = 200_000 - len(content)
                if entry.file_size > remaining:
                    raise HTTPException(413, "Instruksi skill terlalu besar untuk diimpor")
                with archive.open(entry) as source:
                    data = source.read(remaining + 1)
                if len(data) > remaining:
                    raise HTTPException(413, "Instruksi skill terlalu besar untuk diimpor")
                content.extend(data)
            return bytes(content)
    except BadZipFile as error:
        raise HTTPException(502, "ClawHub mengirim arsip skill yang tidak valid") from error


async def _fetch_verified_skill(slug: str, owner_handle: str, version: str | None) -> tuple[str, str, str]:
    """Resolve a clean exact version and return its supported Markdown bytes."""
    params = {"ownerHandle": owner_handle}
    if version:
        params["version"] = version
    phase = "verifikasi"
    try:
        timeout = httpx.Timeout(connect=5.0, read=45.0, write=5.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            verification = await _clawhub_get(
                client, f"/skills/{slug}/verify", params=params, retry_conflict=True,
            )
            verification.raise_for_status()
            verdict = verification.json()
            if not isinstance(verdict, dict):
                raise HTTPException(502, "Respons verifikasi ClawHub tidak valid")
            security = verdict.get("security")
            if (verdict.get("ok") is not True or verdict.get("decision") != "pass"
                    or not isinstance(security, dict) or security.get("status") != "clean"):
                reasons = verdict.get("reasons") or []
                reason = ", ".join(str(item)[:80] for item in reasons[:3]) if isinstance(reasons, list) else ""
                detail = "Skill belum memenuhi verifikasi keamanan ClawHub"
                raise HTTPException(422, f"{detail} ({reason})" if reason else detail)
            resolved_version = verdict.get("version")
            if (not isinstance(resolved_version, str) or not resolved_version
                    or str(verdict.get("slug") or "").casefold() != slug.casefold()
                    or str(verdict.get("publisherHandle") or "").casefold() != owner_handle.casefold()
                    or (version and resolved_version != version)):
                raise HTTPException(409, "Identitas atau versi skill berubah selama verifikasi")
            # The public download route has no owner-qualified selector.
            # Reject ambiguous slugs before fetching package bytes.
            phase = "identitas publisher"
            detail = await _clawhub_get(client, f"/skills/{slug}")
            detail.raise_for_status()
            detail_data = detail.json()
            detail_owner = detail_data.get("owner") if isinstance(detail_data, dict) else None
            unqualified_owner = detail_owner.get("handle") if isinstance(detail_owner, dict) else None
            if str(unqualified_owner or "").casefold() != owner_handle.casefold():
                raise HTTPException(409, "Skill dengan slug ini ambigu di ClawHub; pilih sumber lain")
            phase = "berkas skill"
            content = await _skill_file_bytes(client, slug, resolved_version)
    except HTTPException:
        raise
    except httpx.TimeoutException as error:
        raise HTTPException(503, f"ClawHub terlalu lama merespons pada tahap {phase}; coba lagi nanti") from error
    except httpx.HTTPStatusError as error:
        upstream_status = error.response.status_code
        if upstream_status == 409 and phase == "verifikasi":
            raise HTTPException(503, "Versi skill sedang berbenturan di verifikasi ClawHub. Coba lagi nanti; instalasi tidak dapat dilanjutkan tanpa hasil verifikasi yang sah") from error
        if upstream_status == 409 and phase == "berkas skill":
            raise HTTPException(503, "Versi skill lolos verifikasi, tetapi berkasnya belum tersedia dari ClawHub. Coba lagi nanti") from error
        raise HTTPException(502, f"ClawHub gagal pada tahap {phase} (HTTP {upstream_status})") from error
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(502, "Koneksi ke ClawHub gagal saat mengambil skill") from error
    if len(content) > 200_000:
        raise HTTPException(413, "Instruksi skill terlalu besar untuk diimport")
    try:
        markdown = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise HTTPException(422, "SKILL.md harus berupa UTF-8 yang valid") from error
    if not markdown.strip():
        raise HTTPException(422, "SKILL.md kosong")
    return markdown, hashlib.sha256(content).hexdigest(), resolved_version


@router.get("/skills/{slug}/review")
async def review_skill(
    slug: str,
    owner_handle: str = Query(..., min_length=1, max_length=128),
    version: str | None = Query(None, max_length=128),
    _: WorkforcePrincipal = Depends(get_workforce_principal),
):
    """Optional inspection of the exact bytes that a bot would receive."""
    _validate_skill_reference(slug, owner_handle)
    markdown, digest, resolved_version = await _fetch_verified_skill(slug, owner_handle, version)
    return {
        "source": f"@{owner_handle}/{slug}", "version": resolved_version,
        "sha256": digest, "content": markdown,
        "warning": "Instruksi pihak ketiga dapat memengaruhi perilaku bot. Membaca isi lengkap bersifat opsional.",
    }


@router.post("/skills/{slug}/install")
async def install_skill(
    slug: str, payload: SkillInstall, db: AsyncSession = Depends(get_db),
    principal: WorkforcePrincipal = Depends(get_workforce_principal),
):
    """Import a verified public ClawHub skill as a managed Clevio agent skill."""
    if not get_settings().marketplace_community_skill_install_enabled:
        raise HTTPException(403, "Install skill komunitas dinonaktifkan sampai disetujui operator")
    _validate_skill_reference(slug, payload.owner_handle)
    if payload.review_sha256 is not None and not re.fullmatch(r"[a-f0-9]{64}", payload.review_sha256, re.I):
        raise HTTPException(422, "Bukti review skill tidak valid")
    try:
        agent_id = __import__("uuid").UUID(payload.agent_id)
    except ValueError as error:
        raise HTTPException(422, "Bot tujuan tidak valid") from error
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.is_deleted.is_(False)))).scalar_one_or_none()
    if agent is None or (principal.owner_user_id and agent.owner_user_id != principal.owner_user_id):
        raise HTTPException(404, "Bot tujuan tidak ditemukan")
    config = dict(agent.tools_config or {})
    if config.get("computer", False):
        raise HTTPException(422, "Skill komunitas tidak dapat dipasang ke bot dengan computer aktif")
    if (config.get("builder") or config.get("wa_agent_manager") or config.get("system_plugin")
            or "builder" in (agent.capabilities or [])):
        raise HTTPException(422, "Bot pengelola tidak menjalankan skill komunitas; pilih bot spesialis biasa")
    markdown, digest, resolved_version = await _fetch_verified_skill(slug, payload.owner_handle, payload.version)
    if payload.review_sha256 is not None and not hmac.compare_digest(digest, payload.review_sha256.casefold()):
        raise HTTPException(409, "Isi skill berubah setelah ditinjau; buka isi terbaru sebelum melanjutkan")
    name = f"clawhub-{payload.owner_handle}-{slug}"[:255]
    source_ref = f"@{payload.owner_handle}/{slug}"
    description = _skill_description(markdown, f"Imported from ClawHub: {source_ref}")
    description = f"{description[:19_800]} [ClawHub {source_ref} v{resolved_version}]"
    skill = await create_or_update_skill(agent.id, name, description[:20_000], markdown, db)
    config["skills"] = True
    agent.tools_config = config
    agent.version += 1
    await db.commit()
    return {"id": str(skill.id), "name": skill.name, "agent_id": str(agent.id), "source": source_ref,
            "version": resolved_version, "status": "installed", "runtime_access": "enabled"}
