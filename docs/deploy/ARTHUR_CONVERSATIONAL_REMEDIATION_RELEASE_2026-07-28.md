# Arthur Conversational Remediation Release

> Historical Arthur legacy release note. The production builder is now Arthur
> V2; use [ARTHUR_V2_VPS_DEPLOY.md](ARTHUR_V2_VPS_DEPLOY.md) for deployment.

**Date:** 2026-07-28

## Release identity

- Engine: `arthur-progressive-v4`
- Prompt contract: `arthur-kernel-v15`
- Skill bundle: `arthur-skills-2026-07-28-v17`
- Discovery skill: `arthur-discovery@1.3.0`

## Implemented

- Reply guard no longer decides whether a clarification is valid from
  language-specific topic keywords.
- Guard fallbacks now emit a machine-readable reason: pass-through, empty
  reply, internal leak, premature success, or other fallback.
- Discovery validation exposes known facts, unresolved material fields,
  semantic learning goal, unresolved risk, and confirmation state while
  retaining the deterministic creation gate.
- Optional example conversations no longer block an otherwise complete,
  confirmed brief.
- The first discovery/create model pass receives an explicit mandatory
  planning contract. Recovery remains bounded and is observable rather than
  silently replacing conversation copy.
- Run metadata records graph invocation count and duration for initial,
  planning recovery, evidence retry, creation completion, verification, and
  WhatsApp completion phases.
- Arthur source preflight validates all eight skills, semantic versions,
  frontmatter, bodies, duplicate names, and checksums before writes.
- Config, soul, and all skill activations are reseeded in one database
  transaction. Any immutable checksum conflict rolls back the entire release.
- Reactivated immutable skill rows refresh their live bundle metadata, so every
  active skill can be traced to the currently deployed bundle.
- A read-only release preflight reports Git SHA, dirty paths, image identity,
  bundle checksums, API replica count, and health.

## Verification command

```bash
make install-dev
make test-arthur
# Obsolete for current production: use `python -m arthur_v2.seed` and the
# verification checklist in ARTHUR_V2_VPS_DEPLOY.md.
```

## Rollout boundary

Only the shared application image, API replicas, and Arthur's own system
configuration are in scope. Scheduler, Redis, PgBouncer, WhatsApp services,
Google Workspace services, and every user-created agent remain untouched.
