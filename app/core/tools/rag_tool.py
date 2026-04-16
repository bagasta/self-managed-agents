"""
RAG retrieval tool — searches the agent's document knowledge base.

Uses PostgreSQL ILIKE keyword search (no embedding required).
Upgrade path: swap search_documents() for pgvector similarity search.

Configured via tools_config["rag"]:
  enabled      bool — must be true for this tool to be registered
  max_results  int  — number of chunks to return (default 5)

Example tools_config entry:
{
  "rag": {
    "enabled": true,
    "max_results": 5
  }
}
"""
from __future__ import annotations

import uuid
from typing import Any

from langchain_core.tools import tool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.document_service import search_documents


def build_rag_tools(
    agent_id: uuid.UUID,
    db: AsyncSession,
    tools_config: dict[str, Any],
) -> list:
    """Return LangChain RAG tools bound to this agent's document store."""
    cfg: dict[str, Any] = tools_config.get("rag", {})
    max_results: int = int(cfg.get("max_results", 5))

    @tool
    async def search_knowledge_base(query: str) -> str:
        """Search the agent's knowledge base for documents relevant to the query.
        Args:
          query — keywords or a natural-language question to search for
        Returns a formatted list of matching document excerpts."""
        docs = await search_documents(agent_id, query, db, max_results=max_results)
        if not docs:
            return f"No documents found matching: '{query}'"

        parts: list[str] = []
        for i, doc in enumerate(docs, 1):
            source = f" (source: {doc.source})" if doc.source else ""
            excerpt = doc.content[:800]
            if len(doc.content) > 800:
                excerpt += "…"
            parts.append(f"[{i}] **{doc.title}**{source}\n{excerpt}")

        return f"Found {len(docs)} document(s) for '{query}':\n\n" + "\n\n---\n\n".join(parts)

    return [search_knowledge_base]
