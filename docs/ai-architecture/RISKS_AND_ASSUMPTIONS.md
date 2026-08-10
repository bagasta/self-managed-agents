# Risks and Assumptions

Tanggal snapshot: 2026-07-02

## Technical Risks
| Risk | Impact | Probability | Mitigation |
|---|---:|---:|---|
| Docker socket compromise | Critical | Medium | rootless/gVisor/socket proxy, non-root containers, egress limits |
| Webhook spoofing | High | Medium | HMAC/shared secret, IP allowlist, internal network only |
| Cross-tenant IDOR | High | Medium | owner-based auth on every resource endpoint |
| Full test suite hangs | Medium | Medium | isolate flaky tests, add timeout plugin, release gate |
| MCP auth drift | Medium | High | re-auth runbook, auth status endpoint, clear blocker replies |
| WA session breakage | High | Medium | health checks, QR reconnect flow, store backups |
| RAG/memory prompt injection | High | Medium | trust boundaries, classifiers, action guards |
| Sandbox host OOM | High | Medium | resource budgets, queues, deploy TTL/idle stop |

## Product Risks
| Risk | Impact | Probability | Mitigation |
|---|---:|---:|---|
| Arthur creates weak/generic SOP | High | Medium | deterministic SOP validation and owner review |
| User expects payment automation before ready | Medium | Medium | clear out-of-scope and escalation-based payment flow |
| Agent makes business promise incorrectly | High | Medium | SOP, escalation, reply guards, QA smoke tests |
| Trial number identity confusion | Medium | Medium | strict Arthur dedicated WA vs shared trial number rules |

## Business Risks
| Risk | Impact | Probability | Mitigation |
|---|---:|---:|---|
| LLM cost exceeds plan price | High | Medium | quota enforcement, model routing, alerts |
| WhatsApp account/session operational dependency | High | Medium | dedicated device policy, reconnect workflow |
| Support burden from generated agents | Medium | High | templates, verification, operator runbooks |
| Security incident damages trust | Critical | Medium | hardening, audits, incident response |

## Infrastructure Risks
| Risk | Impact | Probability | Mitigation |
|---|---:|---:|---|
| Single VPS saturation | High | Medium | separate DB/worker, resource caps |
| Redis unavailable | Medium | Medium | health checks, in-memory fallback only for dev |
| DB migration failure | High | Medium | staging dry-run, backups, backward-compatible migrations |
| Disk exhaustion from logs/sandbox/WA store | High | Medium | retention and disk alerts |

## Assumptions
- User identity can be represented by phone/external ID.
- WhatsApp is the primary customer-facing channel.
- Trial shared number is acceptable for demo only.
- Owner/operator users can be identified reliably enough for runtime authorization.
- LLM calls are acceptable latency for customer support use cases.
- External integrations may be unavailable and must fail gracefully.

## Impact Assessment
- Critical: host compromise, secret leak, cross-tenant data leak, uncontrolled WA sends.
- High: agent mis-execution, quota bypass, service outage.
- Medium: UX degradation, auth rework, delayed delivery.
- Low: documentation drift, non-critical dashboard bugs.

## Probability Assessment
- High probability: MCP auth expiry, WA reconnect, prompt/agent config drift.
- Medium probability: sandbox resource pressure, test hang, tenant access mistake.
- Low probability but severe: host compromise through Docker/sandbox.

## Mitigation Plan
1. Harden auth, webhook, sandbox, and SSRF.
2. Add owner authorization and quota enforcement.
3. Make SOP and tool gating deterministic.
4. Build observability dashboards and alerts.
5. Run staging migration and smoke tests before production deploy.
6. Keep this documentation updated with major architecture changes.

