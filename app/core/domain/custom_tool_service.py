"""
Custom Tool service: CRUD + dynamic loading of agent-written Python tools.

Security model:
- Code is validated for syntax before saving.
- Execution happens inside the Docker sandbox (not in the API process).
- The sandbox tool `bash` is used to run custom tool code safely.
"""
from __future__ import annotations

import ast
import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.custom_tool import CustomTool


def validate_python_syntax(code: str) -> str | None:
    """Returns None if valid, or error message string."""
    try:
        ast.parse(code)
        return None
    except SyntaxError as e:
        return f"SyntaxError at line {e.lineno}: {e.msg}"


async def create_or_update_custom_tool(
    agent_id: uuid.UUID,
    name: str,
    description: str,
    code: str,
    db: AsyncSession,
) -> tuple[CustomTool | None, str | None]:
    """Validate syntax then upsert. Returns (tool, None) or (None, error_msg)."""
    err = validate_python_syntax(code)
    if err:
        return None, err

    stmt = (
        pg_insert(CustomTool)
        .values(
            id=uuid.uuid4(),
            agent_id=agent_id,
            name=name,
            description=description,
            code=code,
        )
        .on_conflict_do_update(
            constraint="uq_agent_tool_name",
            set_={"description": description, "code": code},
        )
        .returning(CustomTool)
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.scalar_one(), None


async def get_custom_tool(
    agent_id: uuid.UUID, name: str, db: AsyncSession
) -> CustomTool | None:
    stmt = select(CustomTool).where(
        CustomTool.agent_id == agent_id, CustomTool.name == name
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_custom_tools(
    agent_id: uuid.UUID, db: AsyncSession
) -> list[CustomTool]:
    stmt = (
        select(CustomTool)
        .where(CustomTool.agent_id == agent_id)
        .order_by(CustomTool.name)
    )
    return list((await db.execute(stmt)).scalars().all())


async def delete_custom_tool(
    agent_id: uuid.UUID, name: str, db: AsyncSession
) -> bool:
    stmt = delete(CustomTool).where(
        CustomTool.agent_id == agent_id, CustomTool.name == name
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount > 0
