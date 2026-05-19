from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    base = os.getenv("GOOGLE_MCP_INTEGRATION_URL", "http://localhost:8003").rstrip("/")
    external_user_id = os.getenv("GOOGLE_MCP_EXTERNAL_USER_ID", "62895619356936").strip()
    agent_id = os.getenv("GOOGLE_MCP_AGENT_ID", "46ed1c39-c343-4d42-a5ff-2559f43efa0e").strip()

    payload = {
        "external_user_id": external_user_id,
        "agent_id": agent_id,
    }

    req = urllib.request.Request(
        url=f"{base}/v1/integrations/google/connect",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = resp.getcode()
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        print(f"ERROR HTTP {exc.code}: {text}")
        return 1
    except Exception as exc:
        print(f"ERROR request failed: {exc}")
        return 1

    if status != 200:
        print(f"ERROR unexpected status {status}: {body}")
        return 1

    try:
        data = json.loads(body)
    except Exception:
        print(f"ERROR invalid JSON response: {body}")
        return 1

    auth_url = data.get("auth_url")
    raw_auth_url = data.get("raw_auth_url")

    if not auth_url:
        print(f"ERROR auth_url missing: {body}")
        return 1

    print("=== Google MCP Re-auth Link ===")
    print(auth_url)
    if raw_auth_url:
        print("\n=== Raw Auth URL (debug) ===")
        print(raw_auth_url)

    print("\nNext:")
    print("1) Open auth_url and complete Google consent")
    print("2) Run: make mcp-smoke-live")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
