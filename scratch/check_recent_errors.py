import asyncio
import asyncpg
import sys
sys.stdout.reconfigure(encoding='utf-8')

async def main():
    conn = await asyncpg.connect("postgresql://postgres:Binatanglaut123.@localhost:5432/managed_agents")
    msgs = await conn.fetch("SELECT run_id, role, tool_name, content FROM messages ORDER BY created_at DESC LIMIT 15")
    print("=== RECENT MESSAGES ===")
    for m in msgs:
        content_preview = m['content'][:200] if m['content'] else '(empty)'
        print(f"Run: {m['run_id']} | Role: {m['role']} | Tool: {m['tool_name']}")
        print(f"Content: {content_preview}")
        print("-" * 50)
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
