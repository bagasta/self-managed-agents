import uuid
from unittest.mock import patch

import pytest

from app.core.domain import agent_sop_service
from app.core.domain.agent_sop_service import (
    normalize_agent_operating_manual,
    operating_manual_row_to_artifact,
)
from app.models.agent_operating_manual import AgentOperatingManual


def test_artifact_roundtrip_preserves_full_fields():
    manual = normalize_agent_operating_manual(
        {
            "maturity": "usable",
            "workflows": [{"workflow_id": "wf1", "name": "Order"}],
            "validation_checklist": ["cek pembayaran"],
            "human_approval_points": [{"step": 3, "who": "operator"}],
            "state_plan": {"keys": ["order_status"]},
        }
    )
    row = AgentOperatingManual(agent_id=uuid.uuid4())
    row.artifact = manual
    out = operating_manual_row_to_artifact(row)
    assert out["validation_checklist"] == ["cek pembayaran"]
    assert out["human_approval_points"][0]["who"] == "operator"
    assert out["state_plan"]["keys"] == ["order_status"]


def test_row_to_artifact_falls_back_to_narrow_when_artifact_empty():
    row = AgentOperatingManual(agent_id=uuid.uuid4())
    row.artifact = {}
    row.maturity = "usable"
    row.workflows = [{"workflow_id": "wf1"}]
    out = operating_manual_row_to_artifact(row)
    assert out["maturity"] == "usable"
    assert out["workflows"] == [{"workflow_id": "wf1"}]


def test_normalize_artifact_contains_required_fields():
    """Backfill guard: normalized manual must carry maturity/version/owner_review_required."""
    manual = normalize_agent_operating_manual(
        {
            "maturity": "usable",
            "owner_review_required": False,
            "workflows": [{"workflow_id": "wf1", "name": "Order"}],
        }
    )
    for key in ("maturity", "version", "owner_review_required", "source", "domain", "domain_confidence"):
        assert key in manual, f"normalized manual missing key: {key}"
    assert manual["maturity"] == "usable"
    assert manual["version"] == 1
    assert manual["owner_review_required"] is False


@pytest.mark.asyncio
async def test_sop_read_failure_is_logged():
    class _DB:
        async def execute(self, *a, **k):
            raise RuntimeError("db boom")

    agent_id = uuid.uuid4()
    with patch.object(agent_sop_service.logger, "error") as mock_error:
        out = await agent_sop_service.get_latest_agent_operating_manual(
            agent_id,
            _DB(),
            fallback_tools_config={"operating_manual": {"maturity": "usable"}},
        )

    # Exception must be surfaced via logger.error
    mock_error.assert_called_once()
    call_kwargs = mock_error.call_args
    assert str(agent_id) in str(call_kwargs)
    assert "db boom" in str(call_kwargs)

    # Fallback still works — runtime keeps running
    assert out is not None
