"""Transactional revision archive in a namespace never loaded as agent recall."""
from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import event, inspect, select

from app.core.infra.channel_service import encrypt_value, decrypt_value

ADMIN_SCOPE = "__arthur_control_v2__"
REVISION_FIELDS = ("name", "description", "instructions", "model", "temperature", "max_tokens", "tools_config", "operator_ids", "allowed_senders", "escalation_config")
_installed = False


def encode_archive(value):
    return encrypt_value(json.dumps(value, ensure_ascii=False, default=str))


def decode_archive(value):
    return json.loads(decrypt_value(value))


def snapshot(agent):
    return {field: copy.deepcopy(getattr(agent, field)) for field in REVISION_FIELDS}


def _revision_row(agent_id, version, state, stage):
    rid = uuid.uuid4()
    value = {"id": str(rid), "version": version, "stage": stage,
             "recorded_at": datetime.now(timezone.utc).isoformat(), "snapshot": state}
    return {"id": rid, "agent_id": agent_id, "scope": ADMIN_SCOPE,
            "key": "revision:" + str(rid), "value_data": encode_archive(value)}


def register_revision_tracking():
    global _installed
    if _installed:
        return
    _installed = True
    from app.models.agent import Agent
    from app.models.memory import Memory

    @event.listens_for(Agent, "before_update")
    def archive_change(mapper, connection, target):
        state = inspect(target)
        if not any(state.attrs[field].history.has_changes() for field in REVISION_FIELDS):
            return
        old = connection.execute(select(Agent.__table__).where(Agent.id == target.id).with_for_update()).mappings().first()
        if old is None:
            return
        before = {field: copy.deepcopy(old[field]) for field in REVISION_FIELDS}
        after = copy.deepcopy(before)
        for field in REVISION_FIELDS:
            if state.attrs[field].history.has_changes():
                after[field] = copy.deepcopy(getattr(target, field))
        if before == after:
            return
        # Some runtime resource updates do not explicitly bump the version.
        # Keep optimistic guards meaningful without double-incrementing callers
        # which already advance it. Quota-only updates never reach this branch.
        target.version = max(int(target.version or 0), int(old["version"]) + 1)
        # Same transaction as the configuration update. No quota-only noise,
        # and no schema change. Full snapshots are encrypted, never injected
        # into global/user memory or returned by ordinary memory tools.
        connection.execute(Memory.__table__.insert(), [
            _revision_row(target.id, old["version"], before, "before"),
            _revision_row(target.id, target.version, after, "after"),
        ])
