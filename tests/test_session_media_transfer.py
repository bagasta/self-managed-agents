"""The Drive server fetches the original bytes of one authorized attachment."""
import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock
import uuid

from fastapi import FastAPI
import httpx
import jwt
import pytest

from app.api.session_media import router
from app.database import get_db
from app.config import Settings
from app.core.infra import session_media_transfer as transfer
from app.core.engine.google_mcp_support import sanitize_google_forms_tools
from app.models.session import Session
from test_arthur_p1_operations import database, agent_row, OWNER


@pytest.fixture
def attachment(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, sandbox_base_dir=str(tmp_path),
                        api_key="isolated-transfer-key", app_public_url="https://platform.example")
    monkeypatch.setattr(transfer, "get_settings", lambda: settings)
    session = Session(id=uuid.uuid4(), agent_id=uuid.uuid4(), channel_type="whatsapp", external_user_id=OWNER)
    folder = tmp_path / str(session.id) / "shared" / "current_input"
    folder.mkdir(parents=True)
    path = folder / "cv.jpg"
    original = b"original CV image bytes, not extracted text"
    path.write_bytes(original)
    session.metadata_ = {"last_incoming_media": {"filename": "cv.jpg", "media_type": "image",
                         "mimetype": "image/jpeg", "current_shared_workspace_path": str(path)}}
    return session, path, original


@pytest.mark.asyncio
async def test_drive_upload_fetches_exact_registered_binary_in_same_session(database, attachment):
    session, path, original = attachment
    target = await agent_row(database)
    session.agent_id = target.id
    async with database() as db:
        db.add(session)
        await db.commit()
    app = FastAPI()
    app.include_router(router)
    async def db_override():
        async with database() as db:
            yield db
    app.dependency_overrides[get_db] = db_override
    calls = []

    class DriveTool:
        name = "create_drive_file"
        description = "Upload a file"
        async def ainvoke(self, args):
            calls.append(args)
            assert args["folder_id"] == "reviewed-folder"
            assert args["content"] is None
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                response = await client.get(args["fileUrl"])
            assert response.status_code == 200
            assert response.content == original
            assert response.headers["content-type"] == "image/jpeg"
            assert response.headers["cache-control"] == "private, no-store"
            return {"id": "verified-drive-file"}

    tool = sanitize_google_forms_tools([DriveTool()], MagicMock(), session=session)[0]
    result = await tool.ainvoke({"file_name": "CV.jpg", "fileUrl": "file:///workspace/shared/current_input/cv.jpg",
                               "mime_type": "image/jpeg", "folder_id": "reviewed-folder"})
    assert result == {"id": "verified-drive-file"} and len(calls) == 1
    claims = transfer.decode_session_media_token(calls[0]["fileUrl"].rsplit("/", 1)[-1])
    assert claims["sha"] == hashlib.sha256(original).hexdigest()
    assert transfer.read_session_media_transfer(session, claims)[0] == original


@pytest.mark.parametrize("change", ["tamper", "expired", "replaced", "reset", "other_session", "foreign_path", "symlink"])
def test_transfer_rejects_stale_or_cross_session_sources(attachment, change):
    session, path, original = attachment
    url = transfer.build_session_media_url(session, "/workspace/shared/current_input/cv.jpg")
    token = url.rsplit("/", 1)[-1]
    claims = transfer.decode_session_media_token(token)
    with pytest.raises(ValueError):
        if change == "tamper":
            transfer.decode_session_media_token(token[:-8] + "changed!")
        elif change == "expired":
            claims.update(iat=1, exp=2)
            transfer.decode_session_media_token(jwt.encode(claims, transfer._key(), algorithm="HS256"))
        elif change == "replaced":
            path.write_bytes(b"different CV")
            transfer.read_session_media_transfer(session, claims)
        elif change == "reset":
            session.metadata_ = {**session.metadata_, "context_reset_at": "new-boundary"}
            transfer.read_session_media_transfer(session, claims)
        elif change == "other_session":
            session.id = uuid.uuid4()
            transfer.read_session_media_transfer(session, claims)
        elif change == "foreign_path":
            session.id = uuid.uuid4()
            transfer.build_session_media_url(session, "/workspace/shared/current_input/cv.jpg")
        else:
            outside = path.parents[3] / "private.jpg"
            outside.write_bytes(original)
            path.unlink()
            path.symlink_to(outside)
            transfer.build_session_media_url(session, "/workspace/shared/current_input/cv.jpg")


@pytest.mark.asyncio
async def test_local_path_is_not_forwarded_to_remote_drive_without_registered_session():
    called = []
    class DriveTool:
        name = "create_drive_file"
        description = "Upload a file"
        async def ainvoke(self, args):
            called.append(args)
    tool = sanitize_google_forms_tools([DriveTool()], MagicMock())[0]
    result = await tool.ainvoke({"file_name": "CV.jpg", "fileUrl": "file:///app/private.json"})
    assert result.startswith("[error] DRIVE_ATTACHMENT_UNAVAILABLE") and not called


@pytest.mark.asyncio
async def test_invalid_transfer_token_returns_404_with_real_settings(attachment):
    app = FastAPI()
    app.include_router(router)
    async def unused_db():
        yield None
    app.dependency_overrides[get_db] = unused_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        response = await client.get("https://platform.example/v1/session-media/not-a-valid-token")
    assert response.status_code == 404
