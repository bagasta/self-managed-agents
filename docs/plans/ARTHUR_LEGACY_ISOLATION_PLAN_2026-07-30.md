# Arthur Legacy Isolation Plan

**Status:** proposed — no production behaviour changes in this plan

## Goal

Make the current Arthur implementation a self-contained, disableable legacy
system-agent module. The platform's generic API and dashboard flows for
creating, editing, listing, deleting, and running ordinary agents must remain
independent of Arthur.

This creates a clean boundary for a later, separately designed
`arthur_v2` system agent without deleting the current Arthur data, WhatsApp
session, or implementation prematurely.

## Non-goals

- Do not make Arthur a separate service or container in this work.
- Do not alter existing user-created agents, their API, or their dashboard
  management flow.
- Do not delete Arthur database records, WhatsApp device/session data, build
  drafts, memories, or skills.
- Do not introduce Arthur V2; only establish the extension boundary it will
  use later.

## Target structure

```text
app/
  api/
    agents.py                         # generic platform API only
  core/
    domain/
      agent_ownership.py              # generic owner/LID filtering, no Arthur import
    engine/
      agent_runner.py                 # generic orchestration
      agent_tool_setup.py              # generic tool assembly + system-agent handoff
    system_agents/
      registry.py                      # resolves an enabled system-agent plugin
      contracts.py                     # small, explicit plugin interface
      arthur_legacy/
        plugin.py                      # only entrypoint used by the generic runtime
        runtime/
        tools/
        domain/
        prompts/
        assets/skills/
        seed.py
        lifecycle.py                   # enabled/disabled and maintenance response

scripts/
  arthur_legacy/
    seed.py
    preflight_release.py
    trace_run.py
    wa_e2e.py

tests/
  system_agents/
    arthur_legacy/
```

`arthur_legacy` is an internal Python package, not a new deployable service.
It continues to use the existing API process, database, scheduler, and
production `wa-service` through existing generic interfaces.

## Boundary contract

The generic runtime may know only that an agent can select a registered
system-agent plugin. It must not contain Arthur prompt text, skill paths,
Arthur-specific tool lists, or Arthur-specific feature flags.

The initial plugin contract should be intentionally small:

```python
class SystemAgentPlugin(Protocol):
    key: str

    def matches(self, agent: Agent, tools_config: dict[str, Any]) -> bool: ...
    def is_enabled(self) -> bool: ...
    def disabled_reply(self) -> str: ...
    async def build_tools(self, context: SystemAgentContext) -> list: ...
    def build_prompt_context(self, context: SystemAgentContext) -> str | None: ...
    async def after_run(self, context: SystemAgentContext, result: RunResult) -> None: ...
```

The registration key must be stable and explicit:

```json
{
  "system_plugin": "arthur_legacy"
}
```

Do not use the display name `Arthur` to identify the plugin. A future agent can
be named Arthur without inheriting the legacy policy, tools, or prompts.

## What remains generic

The following stays outside the Arthur legacy package:

- `app/api/agents.py` and all `/v1/agents` CRUD endpoints.
- Agent models, schemas, sessions, runs, messages, subscriptions, and generic
  WhatsApp-device provisioning.
- The generic `run_agent(...)` API, scheduler dispatch, channel ingress, and
  generic tool groups such as memory, custom tools, RAG, sandbox, and skills.
- Generic identity/ownership helpers used by API filtering. In particular,
  move reusable logic currently reached through `builder_identity` into a
  neutral `app/core/domain/agent_ownership.py` (or equivalent) before the
  Arthur package moves.

## Inventory to move behind the plugin

Move code by responsibility, not by a blind directory rename.

| Current area | Destination | Notes |
| --- | --- | --- |
| `app/core/engine/arthur_skill_runtime.py` | `app/core/system_agents/arthur_legacy/runtime/` | Progressive-skill gating and version metadata. |
| `app/core/tools/builder_*.py` | `app/core/system_agents/arthur_legacy/tools/` | Builder-only functions, prompts, creation and management tools. |
| Arthur-specific parts of `agent_runner.py`, `agent_followups.py`, `prompt_builder.py`, `agent_google_routing.py`, and reply guards | `arthur_legacy/runtime/` | Extract first; leave generic orchestration in place. |
| `agent_build_state_service.py` and `builder_confirmation.py` | `arthur_legacy/domain/` where exclusively used by legacy Arthur | Retain neutral shared data models under `app/models/`. |
| `arthur-skills/` | `arthur_legacy/assets/skills/` | Resolve paths using `Path(__file__)`, not project-root assumptions. |
| `scripts/seed_arthur.py` | `arthur_legacy/seed.py`, with a thin CLI wrapper | Seed becomes a module callable and not a root script dependency. |
| `scripts/*arthur*` | `scripts/arthur_legacy/` | Includes preflight, trace, WhatsApp E2E, and test helpers. |
| `tests/test_arthur_*.py` and Arthur E2E scenario | `tests/system_agents/arthur_legacy/` | Keep `make test-arthur` as a compatibility command. |

Do not move `builder_identity` wholesale until its API consumers have been
classified: generic ownership helpers move to the neutral module; legacy-only
policy and tooling remain inside Arthur legacy.

## Phased implementation

### Phase 0 — baseline and safety net

1. Record a clean behavioural baseline with `make test-arthur` and focused
   generic-agent API tests.
2. Capture the current Arthur agent ID and its `tools_config` in the target
   environment; no data is changed in this phase.
3. Add characterization tests for:
   - normal agent create/update/list through `/v1/agents`;
   - normal agent runtime without a system plugin;
   - Arthur legacy tool selection and prompt selection;
   - disabled-plugin dispatch behavior.

### Phase 1 — introduce the neutral system-agent seam

1. Add `contracts.py` and `registry.py` under `app/core/system_agents/`.
2. Add one adapter plugin whose behavior delegates to the current Arthur code;
   do not move logic yet.
3. Change `agent_tool_setup.py` and the minimal relevant prompt/follow-up
   integration points to call the registry rather than importing builder tools
   directly.
4. Prove ordinary agents never invoke the registry plugin.

**Exit criterion:** all existing Arthur tests and generic-agent tests pass with
the adapter in place and no behavior difference.

### Phase 2 — remove API-to-Arthur imports

1. Extract generic owner filtering, phone/LID normalization, and generic agent
   metadata helpers out of `app/core/tools/builder_identity.py`.
2. Update `app/api/agents.py`, user/API code, and generic domain services to
   import only neutral helpers.
3. Verify the `app/api/` package has no import path containing `builder_` or
   `arthur_legacy`.

**Exit criterion:** agent CRUD works when Arthur legacy is absent from the
registry.

### Phase 3 — move Arthur implementation in small slices

1. Move the skill runtime and its tests.
2. Move the builder-tool modules and make `plugin.py` their only public entry.
3. Extract Arthur-specific prompt, follow-up, Google-auth repair, attachment,
   and reply-guard branches from generic engine files.
4. Move build-state service code only after determining which parts are not
   shared by normal agents.
5. Use temporary re-export modules at old import paths only during the move;
   delete them after all first-party imports are updated.

Every slice must be followed by import checks and targeted tests. Do not submit
one giant move-only commit.

### Phase 4 — relocate assets, scripts, and test entrypoints

1. Move `arthur-skills/` into the package assets and update the seed loader.
2. Convert `scripts/seed_arthur.py` into a thin CLI wrapper for
   `app.core.system_agents.arthur_legacy.seed`.
3. Move other Arthur-only operational scripts under `scripts/arthur_legacy/`.
4. Move Arthur tests and scenario files; update `make test-arthur` to target
   their new paths.
5. Update Docker build context and `.dockerignore` only as required to include
   package assets. The API entrypoint remains `uvicorn app.main:app`.

### Phase 5 — disable Arthur legacy without deletion

Add `ARTHUR_LEGACY_ENABLED=true` to settings, defaulting to the current
behaviour during the migration. When set to `false`:

- the seed command must refuse to create or update the legacy agent;
- registry matching must return the disabled state before tool/prompt loading;
- an inbound message to the legacy agent receives a stable retirement or
  maintenance reply and creates no build action;
- generic agent creation, dashboard CRUD, scheduler, and WhatsApp services
  continue unchanged;
- no database rows, memories, build drafts, or WhatsApp sessions are deleted.

Add a documented explicit re-enable path: set the flag true, redeploy only the
API/scheduler as needed, then run the legacy seed command deliberately.

### Phase 6 — production rollout

1. Deploy the refactor with `ARTHUR_LEGACY_ENABLED=true`; verify parity first.
2. Test a normal dashboard/API agent create and management flow.
3. Test an existing ordinary WhatsApp agent and a scheduled agent run.
4. Test Arthur legacy once while enabled.
5. Set `ARTHUR_LEGACY_ENABLED=false` and deploy the API component only.
6. Verify Arthur receives the retired response and all generic flows still
   work.

## Validation commands and assertions

Run these at the relevant phase, plus any newly added tests:

```bash
make test-arthur
pytest -q tests
python -m compileall app
rg -n 'app\.core\.tools\.builder_|app\.core\.engine\.arthur_' app/api app/core
```

Expected final import state:

- `app/api/` has no Arthur/builder import.
- generic `app/core/engine/` has only system-agent registry/contract imports,
  not legacy implementation imports.
- all imports of `arthur_legacy` originate from the registry, plugin tests, or
  explicit legacy operational entrypoints.

## Rollback

- For a code regression: revert the last small migration slice; compatibility
  wrappers remain until the final cleanup phase.
- For an unwanted disablement: restore `ARTHUR_LEGACY_ENABLED=true`, redeploy
  the API/scheduler components that read settings, and run no destructive data
  command.
- Never roll back by deleting the Arthur agent, its session, its drafts, or
  shared WhatsApp service data.

## Completion criteria

The work is complete only when:

1. Arthur legacy is contained in one package plus its explicit operational
   scripts/tests/assets.
2. Generic agent CRUD and management have no Arthur import dependency.
3. The generic runner relies on a plugin contract rather than Arthur branches.
4. Arthur legacy can be disabled by configuration without deleting state.
5. Generic dashboard/API, WhatsApp, and scheduler flows are verified while
   Arthur legacy is disabled.
6. A new system agent can later be added as a separate plugin without modifying
   Arthur legacy.
