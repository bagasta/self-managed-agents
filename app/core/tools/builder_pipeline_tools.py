"""Optimized, quality-gated Arthur agent creation pipeline."""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Callable

import structlog
from langchain_core.tools import tool

from app.core.tools.builder_google import has_google_workspace_tools

logger = structlog.get_logger(__name__)

SettingsProvider = Callable[[], Any]
LoggerProvider = Callable[[], Any]


def _json_object(value: Any, *, label: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{label} tidak mengembalikan JSON object")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} tidak mengembalikan JSON object")
    return parsed


def _pre_create_failure(
    *,
    run_id: str,
    reason: str,
    detail: Any,
    fallback_to_legacy: bool,
) -> str:
    return json.dumps(
        {
            "success": False,
            "pipeline": "optimized",
            "pipeline_run_id": run_id,
            "creation_attempted": False,
            "fallback_to_legacy": fallback_to_legacy,
            "error": reason,
            "detail": detail,
        },
        ensure_ascii=False,
    )


def build_builder_pipeline_tools(
    *,
    plan_tool: Any,
    blueprint_tool: Any,
    manual_tool: Any,
    instruction_tool: Any,
    soul_tool: Any,
    validation_tool: Any,
    create_tool: Any,
    verify_tool: Any,
    connector_tool: Any,
    get_settings: SettingsProvider,
    get_logger: LoggerProvider | None = None,
) -> dict[str, Any]:
    """Build a composite tool while retaining every specialist quality gate."""
    _get_logger = get_logger or (lambda: logger)

    @tool
    async def create_agent_from_brief(
        user_goal: str,
        agent_name: str = "",
        requested_features: str = "",
        persona: str = "ramah dan profesional",
        business_context: str = "",
        target_users: str = "pengguna WhatsApp yang membutuhkan layanan ini",
        operator_phone: str = "",
        operator_name: str = "Owner",
        known_constraints: str = "",
        extra_rules: str = "",
        domain: str = "",
    ) -> str:
        """Create one agent from a complete brief using the optimized pipeline.

        Use this for a NEW agent after the purpose, target users, workflow, data
        to collect, escalation, integrations, and expected output are clear.
        It preserves the specialist blueprint/manual/instruction/soul writers,
        validates before create, reads the agent back, and returns one result.

        If the result has fallback_to_legacy=true and creation_attempted=false,
        continue with the legacy plan/compose/validate/create sequence in the
        same turn. Never retry creation when creation_attempted=true.
        """
        run_id = str(uuid.uuid4())
        log = _get_logger().bind(pipeline_run_id=run_id, pipeline="optimized")
        log.info("builder_pipeline.started", agent_name=agent_name or None)

        try:
            plan = _json_object(
                await plan_tool.ainvoke(
                    {
                        "user_goal": user_goal,
                        "agent_name": agent_name,
                        "channel": "whatsapp",
                        "requested_features": requested_features,
                        "persona": persona,
                        "business_context": business_context,
                        "operator_phone": operator_phone,
                    }
                ),
                label="plan_agent",
            )
        except Exception as exc:
            log.warning("builder_pipeline.plan_failed", error=str(exc)[:300])
            return _pre_create_failure(
                run_id=run_id,
                reason="optimized_plan_failed",
                detail=str(exc),
                fallback_to_legacy=True,
            )

        plan_status = str(plan.get("plan_status") or "")
        if plan_status != "ready":
            return json.dumps(
                {
                    "success": False,
                    "pipeline": "optimized",
                    "pipeline_run_id": run_id,
                    "creation_attempted": False,
                    "fallback_to_legacy": False,
                    "status": plan_status or "not_ready",
                    "validation_errors": plan.get("validation_errors") or [],
                    "capability_clarifications": plan.get("capability_clarifications") or [],
                    "next_action": plan.get("next_action") or "Lengkapi brief agent terlebih dahulu.",
                },
                ensure_ascii=False,
            )

        preset_id = str(plan.get("detected_preset") or "cs_whatsapp_basic")
        effective_name = str(plan.get("agent_name") or agent_name or "Agent Baru").strip()
        recommended = plan.get("recommended_config") if isinstance(plan.get("recommended_config"), dict) else {}
        tools_config = recommended.get("tools_config") if isinstance(recommended.get("tools_config"), dict) else {}
        model = str(recommended.get("model") or "deepseek/deepseek-v4-flash")
        channel_type = str(recommended.get("channel_type") or "whatsapp")
        max_tokens = int(recommended.get("max_tokens") or 0)
        temperature = float(recommended.get("temperature") or 0.7)

        blueprint_args = {
            "preset_id": preset_id,
            "user_goal": user_goal,
            "agent_name": effective_name,
            "business_context": business_context,
            "target_users": target_users,
            "channel": channel_type,
            "requested_features": requested_features,
            "known_constraints": known_constraints,
        }
        try:
            blueprint_payload = _json_object(
                await blueprint_tool.ainvoke(blueprint_args),
                label="compose_agent_blueprint",
            )
            blueprint = blueprint_payload.get("blueprint")
            if not isinstance(blueprint, dict) or not blueprint.get("workflow_steps"):
                raise ValueError("blueprint tidak memiliki workflow_steps")
            blueprint_json = json.dumps(blueprint, ensure_ascii=False)
        except Exception as exc:
            log.warning("builder_pipeline.blueprint_failed", error=str(exc)[:300])
            return _pre_create_failure(
                run_id=run_id,
                reason="optimized_blueprint_failed",
                detail=str(exc),
                fallback_to_legacy=True,
            )

        writer_context = "\n".join(
            part
            for part in (
                f"Tujuan agent: {user_goal}",
                business_context.strip(),
                f"Target pengguna: {target_users}" if target_users else "",
                f"Batasan: {known_constraints}" if known_constraints else "",
            )
            if part
        )
        escalation_info = ""
        if operator_phone or operator_name:
            escalation_info = (
                f"Eskalasi keputusan manusia kepada {operator_name or 'Owner'}"
                + (f" di {operator_phone}" if operator_phone else "")
            )

        manual_call = manual_tool.ainvoke(
            {
                **blueprint_args,
                "agent_blueprint": blueprint_json,
                "domain": domain,
            }
        )
        instruction_call = instruction_tool.ainvoke(
            {
                "preset_id": preset_id,
                "agent_name": effective_name,
                "business_context": writer_context,
                "persona": persona,
                "channel": channel_type,
                "escalation_info": escalation_info,
                "extra_rules": extra_rules,
                "agent_blueprint": blueprint_json,
            }
        )
        soul_call = soul_tool.ainvoke(
            {
                "preset_id": preset_id,
                "agent_name": effective_name,
                "role": user_goal,
                "business": business_context,
                "persona": persona,
                "tasks": user_goal,
                "business_info": writer_context,
                "escalation": escalation_info,
                "extra_rules": extra_rules,
            }
        )
        writer_results = await asyncio.gather(
            manual_call,
            instruction_call,
            soul_call,
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in writer_results):
            errors = [str(result) for result in writer_results if isinstance(result, BaseException)]
            log.warning("builder_pipeline.parallel_writers_failed", errors=errors)
            return _pre_create_failure(
                run_id=run_id,
                reason="optimized_writer_failed",
                detail=errors,
                fallback_to_legacy=True,
            )

        try:
            manual_payload = _json_object(writer_results[0], label="compose_agent_operating_manual")
            instruction_payload = _json_object(writer_results[1], label="compose_agent_instructions")
            soul_payload = _json_object(writer_results[2], label="compose_agent_soul")
            operating_manual = manual_payload.get("operating_manual")
            instructions = str(instruction_payload.get("instructions") or "").strip()
            soul = str(soul_payload.get("soul") or "").strip()
            if not isinstance(operating_manual, dict) or not operating_manual.get("workflows"):
                raise ValueError("operating manual tidak memiliki workflows")
            if instruction_payload.get("remaining_placeholders"):
                raise ValueError("instructions masih memiliki placeholder")
            if soul_payload.get("remaining_placeholders"):
                raise ValueError("soul masih memiliki placeholder")
            if len(instructions) < 100 or len(soul) < 80:
                raise ValueError("hasil writer terlalu pendek")
        except Exception as exc:
            log.warning("builder_pipeline.writer_quality_failed", error=str(exc)[:300])
            return _pre_create_failure(
                run_id=run_id,
                reason="optimized_writer_quality_failed",
                detail=str(exc),
                fallback_to_legacy=True,
            )

        async def _validate(candidate_instructions: str) -> dict[str, Any]:
            return _json_object(
                await validation_tool.ainvoke(
                    {
                        "name": effective_name,
                        "instructions": candidate_instructions,
                        "tools_config": json.dumps(tools_config, ensure_ascii=False),
                        "model": model,
                        "channel_type": channel_type,
                        "preset_id": preset_id,
                    }
                ),
                label="validate_agent_config",
            )

        try:
            validation = await _validate(instructions)
        except Exception as exc:
            return _pre_create_failure(
                run_id=run_id,
                reason="optimized_validation_failed",
                detail=str(exc),
                fallback_to_legacy=True,
            )

        min_quality = max(0, min(100, int(get_settings().arthur_builder_min_quality_score)))
        if not validation.get("valid") or int(validation.get("quality_score") or 0) < min_quality:
            repair_rules = (
                f"{extra_rules}\nPerbaiki seluruh quality gate berikut tanpa mengurangi detail workflow: "
                + json.dumps(
                    {
                        "errors": validation.get("errors") or [],
                        "warnings": validation.get("warnings") or [],
                        "suggestions": validation.get("suggestions") or [],
                    },
                    ensure_ascii=False,
                )
            ).strip()
            try:
                repaired_payload = _json_object(
                    await instruction_tool.ainvoke(
                        {
                            "preset_id": preset_id,
                            "agent_name": effective_name,
                            "business_context": writer_context,
                            "persona": persona,
                            "channel": channel_type,
                            "escalation_info": escalation_info,
                            "extra_rules": repair_rules,
                            "agent_blueprint": blueprint_json,
                        }
                    ),
                    label="compose_agent_instructions_repair",
                )
                repaired = str(repaired_payload.get("instructions") or "").strip()
                if repaired_payload.get("remaining_placeholders") or len(repaired) < 100:
                    raise ValueError("hasil repair instructions tidak valid")
                repaired_validation = await _validate(repaired)
                if (
                    repaired_validation.get("valid")
                    and int(repaired_validation.get("quality_score") or 0) >= min_quality
                ):
                    instructions = repaired
                    validation = repaired_validation
                else:
                    raise ValueError(json.dumps(repaired_validation, ensure_ascii=False))
            except Exception as exc:
                log.warning("builder_pipeline.quality_gate_failed", error=str(exc)[:500])
                return _pre_create_failure(
                    run_id=run_id,
                    reason="optimized_quality_gate_failed",
                    detail=validation,
                    fallback_to_legacy=True,
                )

        escalation_config = recommended.get("escalation_config")
        if not isinstance(escalation_config, dict):
            escalation_config = {}
        if operator_phone:
            escalation_config["operator_phone"] = operator_phone
            escalation_config.setdefault("channel_type", "whatsapp")
        if operator_name:
            escalation_config["operator_name"] = operator_name

        create_args = {
            "name": effective_name,
            "instructions": instructions,
            "description": user_goal,
            "model": model,
            "temperature": temperature,
            "tools_config": tools_config,
            "channel_type": channel_type,
            "escalation_config": escalation_config,
            "operator_phone": operator_phone,
            "operator_name": operator_name,
            "max_tokens": max_tokens,
            "soul": soul,
            "blueprint": blueprint_json,
            "business_context": business_context or user_goal,
            "domain": domain,
            "operating_manual": operating_manual,
            "file_capability": (
                "enabled"
                if tools_config.get("sandbox") and tools_config.get("whatsapp_media")
                else "text_only"
            ),
        }
        try:
            creation = _json_object(
                await create_tool.ainvoke(create_args),
                label="create_agent",
            )
        except Exception as exc:
            log.error("builder_pipeline.create_failed", error=str(exc)[:500])
            return json.dumps(
                {
                    "success": False,
                    "pipeline": "optimized",
                    "pipeline_run_id": run_id,
                    "creation_attempted": True,
                    "fallback_to_legacy": False,
                    "error": "optimized_create_failed",
                    "detail": str(exc),
                },
                ensure_ascii=False,
            )
        if creation.get("success") is not True:
            creation.update(
                {
                    "pipeline": "optimized",
                    "pipeline_run_id": run_id,
                    "creation_attempted": True,
                    "fallback_to_legacy": False,
                }
            )
            return json.dumps(creation, ensure_ascii=False)

        agent_id = str(creation.get("agent_id") or "")
        verification: dict[str, Any] = {}
        try:
            verification = _json_object(
                await verify_tool.ainvoke({"agent_id": agent_id}),
                label="verify_agent",
            )
        except Exception as exc:
            log.warning("builder_pipeline.verify_failed", error=str(exc)[:300])
            verification = {"status": "verification_error", "error": str(exc)}

        google_auth: dict[str, Any] | None = None
        if has_google_workspace_tools(tools_config):
            try:
                google_auth = _json_object(
                    await connector_tool.ainvoke({"agent_id": agent_id}),
                    label="generate_google_auth_link",
                )
            except Exception as exc:
                log.warning("builder_pipeline.google_auth_prepare_failed", error=str(exc)[:300])
                google_auth = {"connected": False, "error": str(exc)}

        google_connected = bool(google_auth and google_auth.get("connected"))
        if google_auth is not None:
            creation["google_workspace_connected"] = google_connected
            creation["needs_google_auth"] = not google_connected

        creation.update(
            {
                "pipeline": "optimized",
                "pipeline_run_id": run_id,
                "creation_attempted": True,
                "fallback_to_legacy": False,
                "quality_gate": validation,
                "verification": verification,
                "google_auth": google_auth,
            }
        )
        log.info(
            "builder_pipeline.completed",
            agent_id=agent_id,
            quality_score=validation.get("quality_score"),
            google_connected=google_connected,
        )
        return json.dumps(creation, ensure_ascii=False)

    return {"create_agent_from_brief": create_agent_from_brief}
