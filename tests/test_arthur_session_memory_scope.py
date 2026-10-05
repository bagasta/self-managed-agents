from pathlib import Path
from types import SimpleNamespace
import uuid

from app.core.domain.tenant_identity import (
    arthur_memory_scope,
    arthur_ui_session_isolated_memory,
)


def test_arthur_ui_test_sessions_use_session_scope_without_changing_owner_memory():
    owner_id = uuid.uuid4()
    session_a = SimpleNamespace(
        id=uuid.uuid4(),
        channel_type="api",
        metadata_={"source": "arthur-ui", "memory_mode": "isolated"},
    )
    session_b = SimpleNamespace(
        id=uuid.uuid4(),
        channel_type="api",
        metadata_={"source": "arthur-ui", "memory_mode": "isolated"},
    )

    assert arthur_ui_session_isolated_memory(session_a)
    assert arthur_memory_scope(
        owner_id, session_a.id,
        isolate_session=arthur_ui_session_isolated_memory(session_a),
    ) == f"session:{session_a.id}"
    assert arthur_memory_scope(
        owner_id, session_b.id,
        isolate_session=arthur_ui_session_isolated_memory(session_b),
    ) == f"session:{session_b.id}"
    owner_session_a = arthur_memory_scope(owner_id, uuid.uuid4())
    owner_session_b = arthur_memory_scope(owner_id, uuid.uuid4())
    assert owner_session_a == owner_session_b == f"user:{owner_id}"


def test_non_ui_sessions_keep_verified_owner_memory_scope():
    owner_id = uuid.uuid4()
    whatsapp_session = SimpleNamespace(
        id=uuid.uuid4(),
        channel_type="whatsapp",
        metadata_={"source": "arthur-ui", "memory_mode": "isolated"},
    )

    assert not arthur_ui_session_isolated_memory(whatsapp_session)
    assert arthur_memory_scope(
        owner_id, whatsapp_session.id,
        isolate_session=arthur_ui_session_isolated_memory(whatsapp_session),
    ) == f"user:{owner_id}"

    ordinary_api_session = SimpleNamespace(
        id=uuid.uuid4(),
        channel_type="api",
        metadata_={"source": "other-api-client"},
    )
    assert not arthur_ui_session_isolated_memory(ordinary_api_session)
    assert arthur_memory_scope(
        owner_id, ordinary_api_session.id,
        isolate_session=arthur_ui_session_isolated_memory(ordinary_api_session),
    ) == f"user:{owner_id}"


def test_arthur_ui_new_session_failure_cannot_reuse_previous_session_id():
    source = (Path(__file__).resolve().parents[1] / "UI-DEV" / "app.js").read_text()
    new_session = source.split("async function arthurNewSession() {", 1)[1].split(
        "\nasync function arthurSendMessage()", 1
    )[0]
    send_message = source.split("async function arthurSendMessage() {", 1)[1].split(
        "\nfunction arthurChatKeydown", 1
    )[0]

    assert new_session.index("Arthur.sessionId = null;") < new_session.index("await api(")
    assert "channel_type: 'api'" in new_session
    assert "metadata: { source: 'arthur-ui', memory_mode: 'isolated', test_plan: 'enterprise' }" in new_session
    assert "Arthur.sessionCreatePromise" in new_session
    assert "if (Arthur.sessionCreatePromise || !Arthur.sessionId || Arthur.sendInProgress) return;" in send_message
    assert "generation === Arthur.sessionGeneration && sessionId === Arthur.sessionId" in send_message


def test_arthur_ui_isolation_marker_is_scoped_and_non_ui_channel_stays_owner_scoped():
    owner_id = uuid.uuid4()
    legacy_ui_session = SimpleNamespace(
        id=uuid.uuid4(),
        channel_type=None,
        metadata_={"source": "arthur-ui", "memory_mode": "isolated"},
    )
    assert arthur_ui_session_isolated_memory(legacy_ui_session)
    assert arthur_memory_scope(
        owner_id,
        legacy_ui_session.id,
        isolate_session=arthur_ui_session_isolated_memory(legacy_ui_session),
    ) == f"session:{legacy_ui_session.id}"

    whatsapp = SimpleNamespace(
        id=uuid.uuid4(),
        channel_type="whatsapp",
        metadata_={"source": "arthur-ui", "memory_mode": "isolated"},
    )
    assert not arthur_ui_session_isolated_memory(whatsapp)
    assert arthur_memory_scope(
        owner_id, whatsapp.id,
        isolate_session=arthur_ui_session_isolated_memory(whatsapp),
    ) == f"user:{owner_id}"
