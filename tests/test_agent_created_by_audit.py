from types import SimpleNamespace

from scripts.audit_agent_created_by_metadata import (
    CreatedByInference,
    _classify_readiness,
    infer_created_by_metadata,
)


def _agent(**overrides):
    values = {
        "created_by_type": None,
        "created_by_agent_id": None,
        "created_by_agent_name": None,
        "capabilities": [],
        "instructions": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_infer_keeps_existing_created_by_metadata():
    inference = infer_created_by_metadata(
        _agent(
            created_by_type="api",
            created_by_agent_id="api-client",
            created_by_agent_name="API",
        )
    )

    assert inference.created_by_type == "api"
    assert inference.created_by_agent_id == "api-client"
    assert inference.created_by_agent_name == "API"
    assert inference.confidence == "existing"


def test_infer_system_agent_from_capability():
    inference = infer_created_by_metadata(_agent(capabilities=["system", "builder"]))

    assert inference.created_by_type == "system"
    assert inference.created_by_agent_name == "System"
    assert inference.confidence == "high"


def test_infer_arthur_builder_from_platform_identity_memory():
    inference = infer_created_by_metadata(
        _agent(),
        global_memory_keys={"platform_identity"},
        arthur_agent_id="arthur-id",
    )

    assert inference.created_by_type == "arthur_builder"
    assert inference.created_by_agent_id == "arthur-id"
    assert inference.created_by_agent_name == "Arthur"
    assert inference.confidence == "high"


def test_infer_arthur_builder_from_instructions_marker():
    inference = infer_created_by_metadata(
        _agent(instructions="IDENTITAS PLATFORM DAN OWNER\nKamu dibuat dan dikonfigurasi oleh Arthur."),
        arthur_agent_id="arthur-id",
    )

    assert inference.created_by_type == "arthur_builder"
    assert inference.created_by_agent_name == "Arthur"
    assert inference.confidence == "high"


def test_infer_unknown_when_no_reliable_evidence():
    inference = infer_created_by_metadata(_agent(instructions="Kamu adalah agent CS."))

    assert inference.created_by_type is None
    assert inference.confidence == "unknown"


def test_audit_readiness_flags_rag_without_documents_as_needs_fix():
    inference = CreatedByInference(
        created_by_type="arthur_builder",
        created_by_agent_id="arthur-id",
        created_by_agent_name="Arthur",
        confidence="existing",
        reason="metadata already present",
    )
    status, category, blockers, warnings = _classify_readiness(
        _agent(
            owner_external_id="62811xxx",
            operator_ids=["62811xxx"],
            tools_config={"rag": True},
        ),
        inference=inference,
        action="already_ok",
        document_count=0,
    )

    assert status == "launch_blocked"
    assert category == "needs_fix"
    assert "rag_documents_required" in blockers
    assert warnings == []


def test_audit_readiness_groups_complete_agent_as_ready():
    inference = CreatedByInference(
        created_by_type="arthur_builder",
        created_by_agent_id="arthur-id",
        created_by_agent_name="Arthur",
        confidence="existing",
        reason="metadata already present",
    )
    status, category, blockers, warnings = _classify_readiness(
        _agent(
            owner_external_id="62811xxx",
            operator_ids=["62811xxx"],
            tools_config={"rag": True, "escalation": True},
            channel_type="whatsapp",
            wa_device_id="wa-device-1",
        ),
        inference=inference,
        action="already_ok",
        document_count=3,
    )

    assert status == "launch_ready"
    assert category == "ready"
    assert blockers == []
    assert warnings == []


def test_audit_readiness_does_not_require_owner_for_system_agent():
    inference = CreatedByInference(
        created_by_type="system",
        created_by_agent_id=None,
        created_by_agent_name="System",
        confidence="existing",
        reason="metadata already present",
    )
    status, category, blockers, warnings = _classify_readiness(
        _agent(capabilities=["system"], tools_config={"memory": True}),
        inference=inference,
        action="already_ok",
        document_count=0,
    )

    assert status == "launch_ready"
    assert category == "ready"
    assert "owner_missing" not in blockers
    assert warnings == []


def test_audit_readiness_groups_unknown_created_by_as_manual_review():
    inference = CreatedByInference(
        created_by_type=None,
        created_by_agent_id=None,
        created_by_agent_name=None,
        confidence="unknown",
        reason="no reliable source metadata found",
    )
    status, category, blockers, warnings = _classify_readiness(
        _agent(owner_external_id="62811xxx", operator_ids=["62811xxx"], tools_config={"memory": True}),
        inference=inference,
        action="needs_manual_review",
        document_count=None,
    )

    assert status == "launch_ready_with_warnings"
    assert category == "needs_manual_review"
    assert blockers == []
    assert "created_by_metadata_needs_manual_review" in warnings
