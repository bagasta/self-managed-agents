# Managed Agents API Test Matrix

Collection: `managed-agents.postman_collection.json`  
Environment template: `test-env.json`

## Verified locally

| Area | Verification | Result |
| --- | --- | --- |
| CRUD, RAG/documents, memory, sessions/runs | API and service contract tests | 143 passed |
| MCP configuration, routing, fallback, priority | Capability tests without live OAuth/server | 78 passed |
| Sandbox and subagent paths | Runtime/configuration tests | 30 passed |

One unrelated expectation remains in `tests/test_deploy_path.py`: the Arthur coding preset does not contain the exact legacy phrase `vanilla html/css/javascript`. It does not exercise a public endpoint or Docker execution.

## Safe execution order

1. Import both JSON files into Postman and set `api_key` locally.
2. Run Health, then create the smoke agent in Agent CRUD. The test script saves `agent_id` and `agent_key`.
3. Create a session; its test script saves `session_id`.
4. Run Memory and Documents/RAG flows. Delete their smoke data afterward.
5. Run MCP only with an intentionally reachable `mcp_url` and valid OAuth/server configuration.
6. Run Sandbox only where Docker socket, sandbox image, and runtime enablement are available.
7. Run Agent DELETE last to remove the smoke agent.

MCP, subagent, and sandbox do not have standalone REST endpoints. Their supported API contract is `PATCH /v1/agents/{agent_id}` with `tools_config`, followed by `POST /v1/agents/{agent_id}/sessions/{session_id}/messages`.
