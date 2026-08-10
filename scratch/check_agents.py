import asyncio
import asyncpg

async def main():
    conn = await asyncpg.connect("postgresql://postgres:Binatanglaut123.@localhost:5432/managed_agents")
    rows = await conn.fetch("SELECT id, name, description, tools_config, channel_type FROM agents WHERE is_deleted = false")
    print("=== AGENTS IN DATABASE ===")
    for r in rows:
        print(f"ID: {r['id']} | Name: {r['name']} | Tools: {r['tools_config']}")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
