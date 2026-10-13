from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from app.api import team_chat
from app.models.team_chat import TeamChatAttachment, TeamChatMessage, TeamChatRoom


def test_computer_file_token_is_owner_scoped_and_short_lived(monkeypatch):
    monkeypatch.setattr(team_chat, "get_settings", lambda: SimpleNamespace(
        api_key="bridge-test-key", computer_runtime_file_base_url="http://computer-api:8000", app_public_url="",
    ))
    room = TeamChatRoom(id=uuid.uuid4(), owner_user_id=uuid.uuid4(), workspace_id="test", kind="direct", title="Chat", manager_agent_id=uuid.uuid4())
    attachment = TeamChatAttachment(id=uuid.uuid4(), room_id=room.id, filename="brand.png", content_type="image/png", size_bytes=4, storage_key="brand.png")
    principal = SimpleNamespace(owner_user_id=room.owner_user_id)

    url = team_chat._computer_attachment_url(attachment, room, principal)
    token = url.rsplit("token=", 1)[1]

    assert url.startswith("http://computer-api:8000/v1/team-chat/computer-files/")
    assert team_chat._verified_computer_file_claims(token, attachment.id)["room_id"] == str(room.id)
    assert team_chat._verified_computer_file_claims(token, uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_collector_moves_new_browser_download_into_bot_attachment(monkeypatch, tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    source = downloads / "hasil.pdf"
    source.write_bytes(b"pdf")
    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(team_chat, "TEAM_CHAT_UPLOAD_ROOT", upload_root)
    monkeypatch.setattr(team_chat, "get_settings", lambda: SimpleNamespace(computer_runtime_download_dir=str(downloads)))
    added = []
    db = SimpleNamespace(add=added.append)
    room = TeamChatRoom(id=uuid.uuid4(), owner_user_id=uuid.uuid4(), workspace_id="test", kind="direct", title="Chat", manager_agent_id=uuid.uuid4())
    pending = TeamChatMessage(id=uuid.uuid4(), room_id=room.id, sender_type="agent", sender_agent_id=uuid.uuid4(), content="", status="working")

    collected = await team_chat._collect_computer_downloads(
        db, pending=pending, room=room, since=datetime.now(timezone.utc),
    )

    assert len(collected) == 1
    assert collected[0].message_id == pending.id
    assert not source.exists()
    assert (upload_root / collected[0].storage_key).read_bytes() == b"pdf"
    assert added == collected
