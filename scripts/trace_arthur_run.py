"""One-off trace puller: find the Beyond Beauty / klinik Arthur conversation and
dump tool-call steps for the two suspected misses (subscription check + QR send).

Prints a compact summary; full dump goes to /tmp/arthur_trace.txt.
"""
import asyncio
import json
import os
import re
import sys

import asyncpg


def db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        with open(".env") as f:
            for line in f:
                if line.startswith("DATABASE_URL="):
                    url = line.split("=", 1)[1].strip()
                    break
    # asyncpg wants plain postgres:// , strip SQLAlchemy driver suffix
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    return url


SEARCH = "%6289477477238%"
SEARCH2 = "%Beyond Beauty%"
SEARCH3 = "%klinik%"


async def main() -> None:
    conn = await asyncpg.connect(db_url())
    out = open("/tmp/arthur_trace.txt", "w")

    def w(s: str = "") -> None:
        out.write(s + "\n")

    # 1. find candidate sessions
    rows = await conn.fetch(
        """
        SELECT s.id, s.agent_id, s.external_user_id, s.channel_config,
               max(m."timestamp") AS last_ts
        FROM sessions s
        JOIN messages m ON m.session_id = s.id
        WHERE EXISTS (
            SELECT 1 FROM messages mm
            WHERE mm.session_id = s.id
              AND (mm.content ILIKE $1 OR mm.content ILIKE $2 OR mm.content ILIKE $3)
        )
        GROUP BY s.id, s.agent_id, s.external_user_id, s.channel_config
        ORDER BY last_ts DESC
        LIMIT 10
        """,
        SEARCH, SEARCH2, SEARCH3,
    )
    print(f"candidate_sessions={len(rows)}")
    w(f"# candidate sessions: {len(rows)}")
    for r in rows:
        cc = r["channel_config"]
        w(f"session={r['id']} agent={r['agent_id']} ext_user={r['external_user_id']} "
          f"last_ts={r['last_ts']} channel_config={cc}")
    w()

    if not rows:
        print("NO MATCHING SESSION in local DB")
        w("NO MATCHING SESSION")
        out.close()
        await conn.close()
        return

    # 2. for the most recent matching session, dump all messages + steps
    sess = rows[0]["id"]
    print(f"tracing_session={sess}")
    msgs = await conn.fetch(
        """
        SELECT role, content, tool_name, tool_args, tool_result,
               step_index, run_id, "timestamp" AS ts
        FROM messages
        WHERE session_id = $1
        ORDER BY "timestamp" ASC, step_index ASC
        """,
        sess,
    )
    w(f"# session {sess} — {len(msgs)} messages")
    sub_calls = 0
    qr_calls = 0
    for m in msgs:
        role = m["role"]
        content = (m["content"] or "")
        tool_name = m["tool_name"] or ""
        targs = m["tool_args"]
        targs_str = ""
        if targs is not None:
            try:
                targs_str = json.dumps(targs if isinstance(targs, (list, dict)) else json.loads(targs))[:300]
            except Exception:
                targs_str = str(targs)[:300]
        tresult = (m["tool_result"] or "")[:700].replace("\n", " ")
        snippet = content[:600].replace("\n", " ")
        w(f"--- [{m['ts']}] role={role} step={m['step_index']} run={m['run_id']} tool={tool_name}")
        if targs_str:
            w(f"    tool_args={targs_str}")
        if tresult:
            w(f"    tool_result={tresult}")
        if snippet:
            w(f"    content={snippet}")
        blob = (content + " " + tool_name + " " + targs_str + " " + tresult).lower()
        if "subscription" in blob or "plan_code" in blob or "trial" in blob or "enterprise" in blob:
            sub_calls += 1
        if "wa_qr" in blob or "qr_sent" in blob or "qr_image" in blob or "qr" in tool_name.lower():
            qr_calls += 1
    print(f"subscription_related_msgs={sub_calls} qr_related_msgs={qr_calls}")
    w()
    w(f"# subscription_related_msgs={sub_calls} qr_related_msgs={qr_calls}")
    out.close()
    await conn.close()
    print("full dump -> /tmp/arthur_trace.txt")


if __name__ == "__main__":
    asyncio.run(main())
