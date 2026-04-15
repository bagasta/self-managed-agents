"""
Agent runner: wires OpenRouter LLM + sandbox tools + LangGraph ReAct agent,
loads session history, runs the agent, persists all steps to DB.

DeepAgents swap-in note
-----------------------
This uses LangGraph's `create_react_agent` for Milestone 1.
When LangChain DeepAgents API is confirmed, replace the agent build block with:

    from deepagents import create_deep_agent
    from deepagents.middleware import TodoListMiddleware, FilesystemMiddleware

    agent = create_deep_agent(
        llm=llm,
        tools=extra_tools,           # non-sandbox tools (HTTP, RAG, etc.)
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
from app.core.sandbox import DockerSandbox
from app.models.message import Message
from app.models.session import Session

logger = structlog.get_logger(__name__)
settings = get_settings()


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
    log = logger.bind(
        run_id=str(run_id),
        session_id=str(session.id),
        agent_id=str(session.agent_id),
        model=agent_model.model,
    )
    log.info("agent_run.start")

    # --- LLM via OpenRouter (OpenAI-compatible API) ---
    llm = ChatOpenAI(
        model=agent_model.model,
        api_key=settings.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        max_tokens=4096,
    )

    # --- Sandbox + tools ---
    sandbox = DockerSandbox(session.id)
    tools = build_sandbox_tools(sandbox)

    # --- System prompt ---
    system_prompt = agent_model.instructions or "You are a helpful assistant."
    if agent_model.safety_policy:
        policy_text = json.dumps(agent_model.safety_policy, indent=2)
        system_prompt += f"\n\n## Safety Policy\n{policy_text}"

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
    # swap create_react_agent for DeepAgents when ready (see module docstring)
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
    pending_tool_records: list[Message] = []  # DB records waiting for their result

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
            # Back-fill result into the matching steps entry and DB record
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
