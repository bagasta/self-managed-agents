import asyncio
import asyncpg

OLD_DOMAIN = "accessibility-netscape-suffered-origins.trycloudflare.com"
NEW_DOMAIN = "rebates-clinical-reduces-employees.trycloudflare.com"

async def main():
    conn = await asyncpg.connect("postgresql://postgres:Binatanglaut123.@localhost:5432/managed_agents")
    
    # 1. Search agent_memories
    rows = await conn.fetch("SELECT id, agent_id, key, value_data FROM agent_memories WHERE value_data LIKE $1", f"%{OLD_DOMAIN}%")
    print(f"Found {len(rows)} memories containing old domain.")
    for r in rows:
        print(f"Updating memory key: {r['key']}")
        new_val = r['value_data'].replace(OLD_DOMAIN, NEW_DOMAIN)
        await conn.execute("UPDATE agent_memories SET value_data = $1 WHERE id = $2", new_val, r['id'])

    # 2. Search conversation_summaries
    rows_summary = await conn.fetch("SELECT id, summary_text FROM conversation_summaries WHERE summary_text LIKE $1", f"%{OLD_DOMAIN}%")
    print(f"Found {len(rows_summary)} summaries containing old domain.")
    for r in rows_summary:
        print(f"Updating summary id: {r['id']}")
        new_summary = r['summary_text'].replace(OLD_DOMAIN, NEW_DOMAIN)
        await conn.execute("UPDATE conversation_summaries SET summary_text = $1 WHERE id = $2", new_summary, r['id'])

    # 3. Search messages
    rows_msg = await conn.fetch("SELECT id, content FROM messages WHERE content LIKE $1", f"%{OLD_DOMAIN}%")
    print(f"Found {len(rows_msg)} messages containing old domain.")
    for r in rows_msg:
        new_content = r['content'].replace(OLD_DOMAIN, NEW_DOMAIN)
        await conn.execute("UPDATE messages SET content = $1 WHERE id = $2", new_content, r['id'])

    await conn.close()
    print("Cleanup completed successfully.")

if __name__ == "__main__":
    asyncio.run(main())
