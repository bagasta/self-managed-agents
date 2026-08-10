# Cost Estimation

Tanggal snapshot: 2026-07-02

## Infrastructure Cost
Primary cost driver is a VPS or cloud VM running:
- API backend.
- Redis.
- WA services.
- pgbouncer.
- Docker sandbox/deployment workloads.
- PostgreSQL if hosted on same box.

Recommended estimate buckets:
- Small dev VPS: API + Redis + WA + small PostgreSQL.
- Production VPS: CPU/RAM headroom for sandbox and temporary deployments.
- Separate DB: managed PostgreSQL cost if moved off-host.

## AI Model Cost
AI model cost is variable by:
- OpenRouter model pricing.
- Input/output token ratio.
- Subagent count.
- RAG context length.
- Google/MCP recovery retries.
- Memory extraction frequency.

Current subscription design references:
- Trial: about 2,000,000 tokens.
- Tier 1: about 10,000,000 tokens.
- Tier 2: about 20,000,000 shared tokens.
- Tier 3: about 100,000,000 tokens.

## Database Cost
- PostgreSQL storage for messages, runs, tool results, documents, embeddings, and audit data.
- pgvector storage grows with document chunks.
- Run/message retention policy will strongly affect storage cost.

## Storage Cost
- WA store volumes for WhatsApp sessions.
- Sandbox workspaces under `SANDBOX_BASE_DIR`.
- Temporary deployment artifacts and container images.
- Backups for PostgreSQL and WA stores.

## Networking Cost
- API ingress/egress.
- OpenRouter and external API calls.
- WhatsApp service traffic.
- MCP/Google API calls.
- Cloudflare tunnel traffic for temporary deployments.

## Third-Party Services Cost
- OpenRouter LLM usage.
- Tavily usage if browsing enabled.
- Mistral OCR for PDF extraction.
- Sentry if paid tier.
- Google APIs generally quota-limited but may have project-level billing/quotas.

## Monthly Projection
Use this formula per tenant:
```text
monthly_cost =
  infra_share
  + (input_tokens / 1_000_000 * model_input_price)
  + (output_tokens / 1_000_000 * model_output_price)
  + OCR_cost
  + Tavily_cost
  + storage_cost
  + support/incident buffer
```

Baseline example categories:
- Trial user: capped by token quota and one agent.
- Starter user: one WhatsApp agent, moderate messages, no heavy sandbox.
- Pro user: two agents, possible subagents, more Google/RAG usage.
- Ops/coding user: high sandbox/deployment cost and should be priced separately.

## Scaling Projection
- LLM cost scales linearly with tokens.
- Sandbox/deployment cost scales with concurrent CPU/RAM.
- DB cost scales with retained messages/runs/documents.
- WA service state scales with connected device count.
- Redis cost scales with event/rate-limit/session streaming usage.

## Cost Optimization Opportunities
- Enforce token quota pre-run.
- Lower `max_tokens` for WhatsApp customer agents.
- Summarize history aggressively after threshold.
- Use cheaper model for low-risk/simple workflows.
- Cache RAG embeddings and avoid re-embedding duplicate chunks.
- Disable expensive tools for trial plan.
- Shorten deployment TTL and stop idle deployments.
- Archive or delete old run/message/tool logs.
- Alert on cost anomalies by owner and model.

