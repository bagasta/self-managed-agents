"""
ToolsConfig — Pydantic schema for agent tools_config JSON field.

Validates tools_config at create/update time so misconfiguration is caught
at the API boundary (422) rather than silently failing minutes into a run.

Extra keys are allowed (extra="allow") for forward-compatibility.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class SubagentsConfig(BaseModel):
    model_config = {"extra": "allow"}

    enabled: bool = False
    agent_ids: list[str] = Field(default_factory=list)


class McpServerConfig(BaseModel):
    model_config = {"extra": "allow"}

    url: str
    headers: dict[str, str] = Field(default_factory=dict)


class ToolsConfig(BaseModel):
    model_config = {"extra": "allow"}

    # Sandbox / execution
    sandbox: bool = False
    tool_creator: bool = False

    # Communication
    scheduler: bool = False
    http: bool = False
    escalation: bool = True

    # Knowledge
    rag: bool = False

    # Memory & skills (always available, but can be disabled)
    memory: bool = True
    skills: bool = True

    # Channel integrations
    whatsapp_media: bool = True
    wa_agent_manager: bool = False

    # Sub-agents
    subagents: SubagentsConfig | bool = Field(default_factory=SubagentsConfig)

    # MCP config (supports both shapes):
    # - Legacy: {"google_workspace": {"url": "..."}}
    # - Current: {"enabled": true, "servers": {"google_workspace": {"url": "..."}}}
    mcp: dict[str, Any] = Field(default_factory=dict)

    # Deployment tooling (system agents only)
    deploy: bool = False

    @model_validator(mode="before")
    @classmethod
    def coerce_mcp_to_dict(cls, values: Any) -> Any:
        """Coerce mcp: false (boolean) → {} so UI doesn't break validation."""
        if isinstance(values, dict):
            mcp_val = values.get("mcp")
            if mcp_val is not None and not isinstance(mcp_val, dict):
                values["mcp"] = {}
        return values

    @model_validator(mode="after")
    def tool_creator_requires_sandbox(self) -> "ToolsConfig":
        if self.tool_creator and not self.sandbox:
            raise ValueError("tool_creator requires sandbox: true")
        return self


from enum import Enum

class AgentProfile(str, Enum):
    ASSISTANT = "assistant"
    SUPPORT = "support"
    RESEARCH = "research"
    KNOWLEDGE = "knowledge"
    OPS = "ops"
    BUILDER = "builder"

class ProfileConfig(BaseModel):
    tools_config: ToolsConfig
    default_model: str
    safety_policy: str
    description: str

PROFILE_PRESETS: dict[AgentProfile, ProfileConfig] = {
    AgentProfile.ASSISTANT: ProfileConfig(
        tools_config=ToolsConfig(memory=True, skills=True, escalation=False, scheduler=True),
        default_model="openai/gpt-4o-mini",
        safety_policy="Standard",
        description="General purpose assistant with memory and scheduling",
    ),
    AgentProfile.SUPPORT: ProfileConfig(
        tools_config=ToolsConfig(memory=True, skills=True, escalation=True, whatsapp_media=True),
        default_model="openai/gpt-4o-mini",
        safety_policy="Strict: must not leak internal docs or break character",
        description="Customer support agent with escalation capabilities",
    ),
    AgentProfile.RESEARCH: ProfileConfig(
        tools_config=ToolsConfig(memory=True, skills=True, http=True, sandbox=False),
        default_model="openai/gpt-4o",
        safety_policy="Standard",
        description="Research agent capable of web browsing via HTTP",
    ),
    AgentProfile.KNOWLEDGE: ProfileConfig(
        tools_config=ToolsConfig(memory=True, skills=True, rag=True),
        default_model="openai/gpt-4o-mini",
        safety_policy="Strict: only answer based on documents",
        description="RAG-enabled agent for answering from knowledge base",
    ),
    AgentProfile.OPS: ProfileConfig(
        tools_config=ToolsConfig(memory=True, skills=True, sandbox=True, tool_creator=True, deploy=True),
        default_model="anthropic/claude-3.5-sonnet",
        safety_policy="High Risk: has sandbox access. Monitor strictly.",
        description="Operations/Coding agent with sandbox and deployment access",
    ),
    AgentProfile.BUILDER: ProfileConfig(
        tools_config=ToolsConfig(memory=True, skills=False), # Builder tools handled via capabilities
        default_model="anthropic/claude-3.5-sonnet",
        safety_policy="Admin only",
        description="Agent Builder system agent",
    ),
}
