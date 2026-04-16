"""
Agent runner: wires OpenRouter LLM + sandbox tools + memory/skill/custom-tool
tools + LangGraph ReAct agent, loads session history, runs the agent,
persists all steps to DB.

Tool selection is driven by tools_config (see _is_enabled below).
Default behaviour if a key is absent:
  sandbox / memory / skills / tool_creator  → enabled
  http / rag                                → disabled (opt-in)

DeepAgents swap-in note
-----------------------
This uses LangGraph's `create_react_agent` for Milestone 1/2.
When LangChain DeepAgents API is confirmed, replace the agent build block with:

    from deepagents import create_deep_agent
    from deepagents.middleware import TodoListMiddleware, FilesystemMiddleware

    agent = create_deep_agent(
        llm=llm,
        tools=extra_tools,
        system_prompt=system_prompt,
        middlewares=[
            TodoListMiddleware(),
            FilesystemMiddleware(sandbox=sandbox),
        ],
    )

The rest of run_agent() stays the same — same ainvoke call, same message parsing.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.custom_tool_service import list_custom_tools
from app.core.memory_service import build_memory_context, upsert_memory, get_memory, list_memories, delete_memory
from app.core.sandbox import DockerSandbox
from app.core.skill_service import create_or_update_skill, get_skill, list_skills as _list_skills
from app.core.custom_tool_service import create_or_update_custom_tool, list_custom_tools
from app.core.tools.http_tool import build_http_tools
from app.core.tools.rag_tool import build_rag_tools
from app.models.message import Message
from app.models.session import Session

logger = structlog.get_logger(__name__)
settings = get_settings()


# ---------------------------------------------------------------------------
# tools_config helpers
# ---------------------------------------------------------------------------

def _is_enabled(tools_config: dict[str, Any], key: str, default: bool = True) -> bool:
    """Check whether a tool group is enabled.
    For 'sandbox', 'memory', 'skills', 'tool_creator': default True (backward compat).
    For 'http', 'rag': default False (opt-in).
    """
    cfg = tools_config.get(key)
    if cfg is None:
        return default
    if isinstance(cfg, bool):
        return cfg
    if isinstance(cfg, dict):
        return bool(cfg.get("enabled", default))
    return default


# ---------------------------------------------------------------------------
# Sandbox tools
# ---------------------------------------------------------------------------

def build_sandbox_tools(sandbox: DockerSandbox) -> list:
    """Return LangChain tools bound to this sandbox instance."""

    @tool
    def bash(cmd: str) -> str:
        """Execute a bash command in the isolated sandbox workspace. Returns stdout+stderr."""
        return sandbox.bash(cmd)

    @tool
    def write_file(path: str, content: str) -> str:
        """Write text content to a file at the given path inside the sandbox workspace."""
        return sandbox.write_file(path, content)

    @tool
    def read_file(path: str) -> str:
        """Read and return the full text content of a file in the sandbox workspace."""
        return sandbox.read_file(path)

    @tool
    def list_files(directory: str = ".") -> str:
        """List all files under a directory in the sandbox workspace."""
        return sandbox.list_files(directory)

    return [bash, write_file, read_file, list_files]


# ---------------------------------------------------------------------------
# Memory tools (async closures bound to agent_id + db session)
# ---------------------------------------------------------------------------

def build_memory_tools(agent_id: uuid.UUID, db: AsyncSession) -> list:
    """Return LangChain tools for long-term memory, bound to this agent."""

    @tool
    async def remember(key: str, value: str) -> str:
        """Store or update a fact in long-term memory. Args: key (short label), value (text to remember)."""
        await upsert_memory(agent_id, key, value, db)
        return f"Remembered: {key} = {value}"

    @tool
    async def recall(query: str) -> str:
        """Retrieve a memory entry by its key. Args: query (the key to look up)."""
        mem = await get_memory(agent_id, query, db)
        if mem:
            return f"{mem.key}: {mem.value_data}"
        # fallback: list all keys so agent knows what's available
        all_mems = await list_memories(agent_id, db)
        if not all_mems:
            return "No memories stored yet."
        keys = ", ".join(m.key for m in all_mems)
        return f"No memory found for '{query}'. Available keys: {keys}"

    @tool
    async def forget(key: str) -> str:
        """Delete a memory entry by key. Args: key (the key to remove)."""
        deleted = await delete_memory(agent_id, key, db)
        return f"Forgotten: {key}" if deleted else f"No memory found for key '{key}'"

    return [remember, recall, forget]


# ---------------------------------------------------------------------------
# Skill tools
# ---------------------------------------------------------------------------

def build_skill_tools(agent_id: uuid.UUID, db: AsyncSession) -> list:
    """Return LangChain tools for skill management."""

    @tool
    async def create_skill(name: str, description: str, content_md: str) -> str:
        """Save a reusable skill (instruction/prompt block) to the skill library.
        Args: name (unique short identifier), description (what it does), content_md (full instructions in markdown)."""
        skill = await create_or_update_skill(agent_id, name, description, content_md, db)
        return f"Skill '{skill.name}' saved successfully."

    @tool
    async def list_skills() -> str:
        """List all available skills for this agent."""
        skills = await _list_skills(agent_id, db)
        if not skills:
            return "No skills saved yet."
        lines = [f"- **{s.name}**: {s.description}" for s in skills]
        return "Available skills:\n" + "\n".join(lines)

    @tool
    async def use_skill(name: str) -> str:
        """Load and return the full content of a skill by name to use in current context.
        Args: name (the skill identifier)."""
        skill = await get_skill(agent_id, name, db)
        if not skill:
            return f"No skill found with name '{name}'"
        return f"# Skill: {skill.name}\n\n{skill.content_md}"

    return [create_skill, list_skills, use_skill]


# ---------------------------------------------------------------------------
# Custom Tool Creator tools
# ---------------------------------------------------------------------------

def build_tool_creator_tools(agent_id: uuid.UUID, db: AsyncSession, sandbox: DockerSandbox) -> list:
    """Return LangChain tools that let the agent create and run its own Python tools."""

    @tool
    async def create_tool(name: str, description: str, python_code: str) -> str:
        """Save a new Python tool for this agent. The code must define a function with the same name as `name`.
        Args: name (function name, snake_case), description (what it does), python_code (valid Python code)."""
        ct, err = await create_or_update_custom_tool(agent_id, name, description, python_code, db)
        if err:
            return f"[error] Could not save tool: {err}"
        return f"Tool '{name}' saved successfully. It will be available in future sessions."

    @tool
    async def list_tools() -> str:
        """List all custom tools created by this agent."""
        tools = await list_custom_tools(agent_id, db)
        if not tools:
            return "No custom tools created yet."
        lines = [f"- **{t.name}**: {t.description}" for t in tools]
        return "Custom tools:\n" + "\n".join(lines)

    @tool
    async def run_custom_tool(name: str, args_json: str = "{}") -> str:
        """Execute a saved custom tool by running its Python code in the sandbox.
        IMPORTANT: If you just created a new tool using create_tool, it won't be available as a direct LangChain tool until the next session.
        In the current session, you MUST use this run_custom_tool to execute your newly created tool.
        Args: name (tool name), args_json (JSON string of keyword arguments for the function)."""
        tools = await list_custom_tools(agent_id, db)
        tool_map = {t.name: t for t in tools}
        if name not in tool_map:
            return f"[error] No custom tool named '{name}'. Use list_tools() to see available tools."

        ct = tool_map[name]
        try:
            args = json.loads(args_json)
        except json.JSONDecodeError as e:
            return f"[error] Invalid args_json: {e}"

        args_repr = ", ".join(f"{k}={repr(v)}" for k, v in args.items())
        runner_code = f"""{ct.code}

# Auto-generated runner
if __name__ == "__main__":
    import json
    result = {name}({args_repr})
    print(json.dumps({{"result": result}}) if result is not None else "null")
"""
        sandbox.write_file(f"_custom_tool_{name}.py", runner_code)
        output = sandbox.bash(f"python /workspace/_custom_tool_{name}.py")
        return output

    return [create_tool, list_tools, run_custom_tool]


# ---------------------------------------------------------------------------
# Dynamic custom tool LangChain wrappers (load saved tools as proper LC tools)
# ---------------------------------------------------------------------------

def build_loaded_custom_tools(custom_tools_db: list, sandbox: DockerSandbox) -> list:
    """
    For each saved custom tool, create a LangChain @tool that executes it in the sandbox.
    Called at agent boot so that previously created tools are available immediately.
    """
    lc_tools = []
    for ct in custom_tools_db:
        def _make_runner(ct_name: str, ct_code: str, ct_desc: str):
            @tool(ct_name, description=ct_desc)
            def _runner(args_json: str = "{}") -> str:
                """Execute a saved custom tool in the sandbox."""
                try:
                    args = json.loads(args_json)
                except json.JSONDecodeError as e:
                    return f"[error] Invalid args_json: {e}"
                args_repr = ", ".join(f"{k}={repr(v)}" for k, v in args.items())
                runner_code = f"""{ct_code}

if __name__ == "__main__":
    import json
    result = {ct_name}({args_repr})
    print(json.dumps({{"result": result}}) if result is not None else "null")
"""
                sandbox.write_file(f"_custom_tool_{ct_name}.py", runner_code)
                return sandbox.bash(f"python /workspace/_custom_tool_{ct_name}.py")
            return _runner
        lc_tools.append(_make_runner(ct.name, ct.code, ct.description))
    return lc_tools


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

def _db_messages_to_lc(db_messages: list[Message]) -> list[BaseMessage]:
    """
    Convert DB message rows to LangChain message objects for context.

    Only user/agent turns are included. ToolMessage rows are skipped because
    reconstructing them requires the original tool_call_id from the AIMessage —
    without it LangGraph rejects the sequence. The agent gets full conversational
    context from human/AI turns alone.
    """
    result: list[BaseMessage] = []
    for msg in db_messages:
        if msg.role == "user" and msg.content:
            result.append(HumanMessage(content=msg.content))
        elif msg.role == "agent" and msg.content:
            result.append(AIMessage(content=msg.content))
    return result


async def _load_history(session_id: uuid.UUID, db: AsyncSession) -> list[Message]:
    stmt = (
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.step_index, Message.timestamp)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

async def run_agent(
    *,
    agent_model: Any,   # app.models.agent.Agent ORM instance
    session: Session,
    user_message: str,
    db: AsyncSession,
) -> dict[str, Any]:
    run_id = uuid.uuid4()
    agent_id: uuid.UUID = session.agent_id
    tools_config: dict[str, Any] = agent_model.tools_config or {}
    log = logger.bind(
        run_id=str(run_id),
        session_id=str(session.id),
        agent_id=str(agent_id),
        model=agent_model.model,
    )
    log.info("agent_run.start")

    # --- LLM via OpenRouter (OpenAI-compatible API) ---
    temperature: float = getattr(agent_model, "temperature", 0.7)
    llm = ChatOpenAI(
        model=agent_model.model,
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        max_tokens=4096,
        temperature=temperature,
    )

    # --- Sandbox ---
    sandbox = DockerSandbox(session.id)

    # --- Gather tools per tools_config ---
    tools: list = []
    active_tool_groups: list[str] = []

    if _is_enabled(tools_config, "sandbox"):
        tools.extend(build_sandbox_tools(sandbox))
        active_tool_groups.append("sandbox")

    if _is_enabled(tools_config, "memory"):
        tools.extend(build_memory_tools(agent_id, db))
        active_tool_groups.append("memory")

    if _is_enabled(tools_config, "skills"):
        tools.extend(build_skill_tools(agent_id, db))
        active_tool_groups.append("skills")

    if _is_enabled(tools_config, "tool_creator"):
        tools.extend(build_tool_creator_tools(agent_id, db, sandbox))
        active_tool_groups.append("tool_creator")

    if _is_enabled(tools_config, "http", default=False):
        tools.extend(build_http_tools(tools_config))
        active_tool_groups.append("http")

    if _is_enabled(tools_config, "rag", default=False):
        tools.extend(build_rag_tools(agent_id, db, tools_config))
        active_tool_groups.append("rag")

    # Load previously saved custom tools so they're callable directly
    if _is_enabled(tools_config, "tool_creator"):
        saved_custom_tools = await list_custom_tools(agent_id, db)
        tools.extend(build_loaded_custom_tools(saved_custom_tools, sandbox))

    log.debug("agent_run.tools_ready", groups=active_tool_groups, tool_count=len(tools))

    # --- System prompt = instructions + saved memories ---
    base_prompt = agent_model.instructions or "You are a helpful assistant."
    memory_block = await build_memory_context(agent_id, db)
    system_prompt = base_prompt
    if memory_block:
        system_prompt += f"\n\n{memory_block}"
    if agent_model.safety_policy:
        policy_text = json.dumps(agent_model.safety_policy, indent=2)
        system_prompt += f"\n\n## Safety Policy\n{policy_text}"

    # Describe available self-extending capabilities based on enabled tool groups
    capability_parts: list[str] = []
    if "memory" in active_tool_groups:
        capability_parts.append("memory tools (remember/recall/forget)")
    if "skills" in active_tool_groups:
        capability_parts.append("skill tools (create_skill/list_skills/use_skill)")
    if "tool_creator" in active_tool_groups:
        capability_parts.append("tool creator tools (create_tool/list_tools/run_custom_tool)")
    if "http" in active_tool_groups:
        capability_parts.append("HTTP tools (http_get/http_post)")
    if "rag" in active_tool_groups:
        capability_parts.append("knowledge base search (search_knowledge_base)")

    if capability_parts:
        system_prompt += (
            "\n\n## Available Tool Capabilities\n"
            "You have access to: " + ", ".join(capability_parts) + ".\n"
            "CRITICAL RULES:\n"
            "1. If a user asks you to 'use a skill' or apply it (e.g., 'pakai skill X'), you MUST physically call the `use_skill(name='X')` tool FIRST to retrieve its instructions. Do NOT guess the instruction.\n"
            "2. If you create a new custom tool with `create_tool`, you cannot call it directly as a normal tool in this exact session. "
            "Instead, immediately after creation, you must use `run_custom_tool(name, args_json)` to execute it."
        )

    # --- Load conversation history ---
    history_rows = await _load_history(session.id, db)
    prior_messages = _db_messages_to_lc(history_rows)
    log.debug("agent_run.history_loaded", message_count=len(prior_messages))

    # --- Persist user message ---
    step_base = max((m.step_index for m in history_rows), default=-1) + 1
    db.add(Message(
        session_id=session.id,
        role="user",
        content=user_message,
        step_index=step_base,
        run_id=run_id,
    ))
    await db.flush()

    # --- Build agent graph ---
    graph = create_react_agent(
        llm,
        tools=tools,
        prompt=system_prompt,
    )

    input_messages: list[BaseMessage] = prior_messages + [HumanMessage(content=user_message)]
    steps: list[dict[str, Any]] = []
    final_reply = ""
    step_counter = step_base + 1

    # --- Run ---
    try:
        result = await graph.ainvoke(
            {"messages": input_messages},
            config={"recursion_limit": settings.agent_max_steps * 2},
        )
    except Exception as exc:
        log.error("agent_run.error", error=str(exc))
        final_reply = f"Agent error: {exc}"
        db.add(Message(
            session_id=session.id,
            role="agent",
            content=final_reply,
            step_index=step_counter,
            run_id=run_id,
        ))
        await db.flush()
        sandbox.close()
        return {"reply": final_reply, "steps": [], "run_id": run_id}

    # --- Parse result messages (only the new ones after the input) ---
    all_messages: list[BaseMessage] = result.get("messages", [])
    new_messages = all_messages[len(input_messages):]

    tool_step = 0
    pending_tool_records: list[Message] = []

    for msg in new_messages:
        if isinstance(msg, AIMessage):
            if msg.content:
                text = msg.content if isinstance(msg.content, str) else str(msg.content)
                final_reply = text
                db.add(Message(
                    session_id=session.id,
                    role="agent",
                    content=text,
                    step_index=step_counter,
                    run_id=run_id,
                ))
                step_counter += 1

            for tc in (msg.tool_calls or []):
                tool_step += 1
                steps.append({
                    "step": tool_step,
                    "tool": tc["name"],
                    "args": tc.get("args", {}),
                    "result": "",
                })
                record = Message(
                    session_id=session.id,
                    role="tool",
                    tool_name=tc["name"],
                    tool_args=tc.get("args", {}),
                    step_index=step_counter,
                    run_id=run_id,
                )
                db.add(record)
                pending_tool_records.append(record)
                step_counter += 1

        elif isinstance(msg, ToolMessage):
            output = msg.content if isinstance(msg.content, str) else str(msg.content)
            for entry in reversed(steps):
                if entry["result"] == "":
                    entry["result"] = output[:500]
                    break
            if pending_tool_records:
                pending_tool_records.pop(0).tool_result = output[:2000]

    await db.flush()
    sandbox.close()

    log.info("agent_run.complete", steps=len(steps), reply_len=len(final_reply))
    return {"reply": final_reply, "steps": steps, "run_id": run_id}
