import asyncio
import asyncpg
import json

async def main():
    conn = await asyncpg.connect("postgresql://postgres:Binatanglaut123.@localhost:5432/managed_agents")
    
    # Update Lagrange agent to enable sandbox and deploy tools
    new_tools = {"memory": True, "skills": True, "sandbox": True, "deploy": True, "whatsapp_media": True}
    await conn.execute("UPDATE agents SET tools_config = $1 WHERE id = 'fede08d8-945f-4ea8-b76b-998179ba00bf'", json.dumps(new_tools))
    print("Lagrange tools_config updated to:", new_tools)
    
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
