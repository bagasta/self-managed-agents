# Database Schema

Tanggal snapshot: 2026-07-02

## Entity List
- `agents`
- `sessions`
- `messages`
- `runs`
- `agent_memories`
- `skills`
- `custom_tools`
- `documents`
- `scheduled_jobs`
- `users`
- `subscription_plans`
- `user_subscriptions`
- `token_topups`
- `user_api_keys`
- `agent_operating_manuals`

## Core Attributes
### agents
- `id` UUID PK.
- `name`, `description`, `instructions`, `model`, `temperature`, `max_tokens`.
- `tools_config`, `sandbox_config`, `safety_policy`, `escalation_config` JSONB.
- `wa_device_id`, `channel_type`.
- `operator_ids`, `capabilities`, `allowed_senders`.
- `owner_external_id`, `created_by_type`, `created_by_agent_id`, `created_by_agent_name`.
- `api_key`, `token_quota`, `tokens_used`, `active_until`, `quota_period_days`.
- `version`, `is_deleted`, timestamps.

### sessions
- `id` UUID PK.
- `agent_id` FK.
- `external_user_id`, `metadata`, `workspace_dir`.
- `channel_type`, `channel_config`.
- `escalation_active`, `ai_disabled`, timestamps.

### messages
- `id` UUID PK.
- `session_id` FK.
- `role`, `content`, `tool_name`, `tool_args`, `tool_result`.
- `step_index`, `run_id`, `timestamp`.

### runs
- `id` UUID PK.
- `session_id` FK.
- `status`, `started_at`, `completed_at`, `error_message`.
- `tokens_used`, `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_tokens`.
- `openrouter_cost_usd`, `usage_details`, `created_at`.

### documents
- `id` UUID PK.
- `agent_id` FK.
- `title`, `content`, `source`, `doc_metadata`.
- `embedding` pgvector with embedding dimension from `embedding_service`.
- timestamps.

### subscriptions
- `users`: identity, email, phone, external ID, trial flags.
- `subscription_plans`: static plan definitions and quotas.
- `user_subscriptions`: one active subscription per user, quota usage, expiry/grace.
- `token_topups`: admin/payment top-up ledger.

### agent_operating_manuals
- `agent_id`, `version`, `source`, `domain`, `domain_confidence`.
- `maturity`, `owner_review_required`.
- `missing_context`, `assumptions`, `workflows`, `artifact`.
- review metadata and timestamps.

## Relationships
- `agents` 1:N `sessions`, `agent_memories`, `skills`, `custom_tools`, `documents`, `scheduled_jobs`, `agent_operating_manuals`.
- `sessions` 1:N `messages`, `runs`.
- `users` 1:1 `user_subscriptions`.
- `subscription_plans` 1:N `user_subscriptions`.
- `user_subscriptions` 1:N `token_topups`.
- `agents.owner_external_id` maps to user external identity by convention, not a strict FK.

## Indexing Strategy
- `agents.api_key` unique index.
- `agents.wa_device_id` unique index.
- FK columns should be indexed for large-scale production if not already covered by migrations.
- `documents.embedding` should use pgvector index once corpus grows.
- `messages.session_id`, `messages.run_id`, `runs.session_id`, and `agent_memories(agent_id, scope, key)` are high-value query indexes.

## Constraints
- UUID primary keys.
- FK cascade exists for several child tables such as documents and subscription relations.
- Soft delete on `agents.is_deleted`.
- `ToolsConfig` validates tool config at API create/update boundary.
- `tool_creator` requires `sandbox=true`.
- `api_key` generated per agent and must be unique.

## Data Retention Policy
- Current code keeps runs, messages, memories, documents, and custom tools indefinitely unless deleted by API or cleanup tooling.
- Sandbox workspace TTL is controlled by `SANDBOX_WORKSPACE_TTL_SECONDS`.
- Deployment TTL is controlled by deployment service.
- Recommended: define retention per tenant for messages/runs, and document deletion/export policy.

## Data Ownership
- Agent owner is represented by `owner_external_id`.
- Session/user memory scope uses `external_user_id`.
- Operator access is represented by `operator_ids`, owner, and escalation config.
- Current management API still needs stronger owner enforcement for multi-tenant production.

## ERD Description
```text
users --1:1-- user_subscriptions --N:1-- subscription_plans
  |             |
  |             +--1:N-- token_topups
  |
  +-- by external_id/owner_external_id convention
       agents --1:N-- sessions --1:N-- messages
          |          +--1:N-- runs
          +--1:N-- documents
          +--1:N-- agent_memories
          +--1:N-- skills
          +--1:N-- custom_tools
          +--1:N-- scheduled_jobs
          +--1:N-- agent_operating_manuals
```

