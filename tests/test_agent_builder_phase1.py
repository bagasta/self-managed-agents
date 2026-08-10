"""
Tests untuk Phase 1 Agent Builder — capabilities flag (sebelumnya is_system_agent).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

class TestAgentModelCapabilities:
    def test_agent_model_has_capabilities_field(self):
        from app.models.agent import Agent
        assert hasattr(Agent, "capabilities")

    def test_agent_capabilities_default_empty(self):
        from app.models.agent import Agent
        agent = Agent(name="Regular Agent", instructions="You are a helpful agent.")
        assert agent.capabilities == []

    def test_agent_can_be_set_with_capabilities(self):
        from app.models.agent import Agent
        builder = Agent(name="Agent Builder", instructions="You help users create agents.", capabilities=["system"])
        assert "system" in builder.capabilities

class TestAgentCreateSchemaCapabilities:
    def test_agent_create_has_capabilities_field(self):
        from app.schemas.agent import AgentCreate
        fields = AgentCreate.model_fields
        assert "capabilities" in fields

    def test_agent_create_capabilities_default_empty(self):
        from app.schemas.agent import AgentCreate
        agent = AgentCreate(name="Test Agent")
        assert agent.capabilities == []

    def test_agent_create_can_set_capabilities(self):
        from app.schemas.agent import AgentCreate
        builder = AgentCreate(name="Builder", capabilities=["system"])
        assert "system" in builder.capabilities

class TestAgentUpdateSchemaCapabilities:
    def test_agent_update_has_capabilities_field(self):
        from app.schemas.agent import AgentUpdate
        fields = AgentUpdate.model_fields
        assert "capabilities" in fields

    def test_agent_update_capabilities_default_none(self):
        from app.schemas.agent import AgentUpdate
        update = AgentUpdate()
        assert update.capabilities is None

    def test_agent_update_can_set_capabilities(self):
        from app.schemas.agent import AgentUpdate
        update_sys = AgentUpdate(capabilities=["system"])
        assert update_sys.capabilities == ["system"]

class TestAgentResponseSchemaCapabilities:
    def test_agent_response_has_capabilities_field(self):
        from app.schemas.agent import AgentResponse
        fields = AgentResponse.model_fields
        assert "capabilities" in fields

    def test_agent_response_serializes_capabilities(self):
        from app.schemas.agent import AgentResponse
        now = datetime.now(timezone.utc)
        fake_agent_data = {
            "id": uuid.uuid4(),
            "name": "Builder",
            "description": None,
            "instructions": "Build agents.",
            "model": "openai/gpt-5.1",
            "temperature": 0.7,
            "tools_config": {},
            "sandbox_config": {},
            "safety_policy": {},
            "escalation_config": {},
            "operator_ids": [],
            "allowed_senders": None,
            "version": 1,
            "is_deleted": False,
            "capabilities": ["system"],
            "api_key": "test-key",
            "token_quota": 4_000_000,
            "tokens_used": 0,
            "active_until": now,
            "quota_period_days": 30,
            "wa_device_id": None,
            "channel_type": None,
            "created_at": now,
            "updated_at": now,
            "max_tokens": 1024,
        }
        response = AgentResponse.model_validate(fake_agent_data)
        assert response.capabilities == ["system"]
