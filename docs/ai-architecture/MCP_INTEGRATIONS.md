# MCP Integrations

Tanggal snapshot: 2026-07-02

## MCP Name: Google Workspace
- Description: External connector for Gmail, Calendar, Drive, Docs, Sheets, Slides, Forms, Tasks, Contacts, and related Google Workspace actions.
- Authentication Method: OAuth via Google integration service; runtime injects bearer token into MCP server config when connected.
- Permissions: determined by OAuth scopes; missing scopes produce auth/scope failure.
- Available Actions: create/edit/read Google artifacts, draft Gmail, calendar manage event, Drive file operations, Forms creation/update, Contacts/Tasks actions.
- Limitations: auth token can expire; Forms/People APIs may be disabled; tool args require exact schemas.
- Error Handling: `google_mcp_support.py` detects 401/scope errors, fetches auth links, blocks false success, and can retry with MCP-only path.
- Security Considerations: owner/operator authorization required; customer sessions should not mutate owner's Google unless policy allows it.

## MCP Name: Generic MCP Servers
- Description: Arbitrary MCP server entries can be configured in `tools_config.mcp.servers`.
- Authentication Method: server-specific headers under config; streamable HTTP is expected.
- Permissions: whatever server exposes; platform currently trusts tool descriptions/actions.
- Available Actions: loaded dynamically via `MultiServerMCPClient`.
- Limitations: connection failure removes tools for the run; import dependency required.
- Error Handling: errors returned as `{server_name: error_message}` and prompt notice can be added.
- Security Considerations: only configure trusted MCP servers; do not expose destructive tools to untrusted agents without policy gates.

## Configuration Shapes
Current wrapper form:
```json
{
  "mcp": {
    "enabled": true,
    "servers": {
      "google_workspace": {
        "url": "http://localhost:8002/mcp",
        "headers": {}
      }
    }
  }
}
```

Legacy form:
```json
{
  "mcp": {
    "google_workspace": {
      "url": "http://localhost:8002/mcp"
    }
  }
}
```

## Runtime Rules
- Google Workspace requests should run in parent-only mode when needed to avoid subagent/sandbox fallback.
- If MCP auth is missing, runtime removes the Google MCP server until auth exists and returns an auth link/blocker.
- MCP tools are prepended to tool list when loaded, so service tools win over local simulation.
- WhatsApp-unsafe MCP tool collisions are filtered for WA contexts.

## Environment Variables
- `WORKSPACE_MCP_URL`
- `WORKSPACE_MCP_RUNTIME_URL`
- `WORKSPACE_MCP_URL_LOCAL`
- `WORKSPACE_MCP_PREFER_LOCAL`
- `WORKSPACE_MCP_TOKEN`
- `GOOGLE_INTEGRATION_SERVICE_URL`

## Operational Runbook
- See `docs/google-mcp-runbook.md`.
- Main commands: `make mcp-smoke-live-onboard`, `make mcp-smoke-live-reauth`, `make mcp-smoke-live`, `make mcp-smoke-live-strict`.

