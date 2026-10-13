# OpenRouter decision routing

Implemented 2026-10-12 using `openai/gpt-6-luna-decisions` and the existing
`OPENROUTER_API_KEY`. This is a separate decision call; the agent's main model
continues planning, writing, and executing tools.

## Runtime behavior

1. After configured tools and MCP tools have been loaded and restricted, the
   agent runner sends the current request, bounded conversation context, and
   available tool descriptions to `https://openrouter.ai/api/alpha/decisions`.
2. Independent questions select a route (`respond`, `tools`, `computer`, or
   `clarify`) and a next tool from the supplied catalog. The computer route is
   offered only when the run exposes the visual screenshot tool.
3. Each answer has its own confidence threshold. Accepted decisions guide the
   main model's next step. They do not execute tools, grant permission, or remove
   tools needed in later phases. Deep Agents' native tools remain available to
   the main model; the router catalog covers the explicit configured/MCP tools.
4. The computer execution contract and final evidence guard use the current
   request's action intent. An accepted decision route is advisory and cannot
   enable that guard by itself. Conversational answers about browser access do
   not trigger the computer contract.
5. `decide_next_action(progress, inspect_computer)` allows another decision after
   new evidence, a failed approach, or uncertainty. Visual deliverables receive
   an instruction to request this review before completion. This is model-driven
   tool use, not a guaranteed hook on every click.
6. With `inspect_computer=true`, the tool captures a fresh image through the
   existing `computer_screenshot` tool, preserving owner checks, computer turns,
   and human private-input priority. It asks whether to continue, replan, finish,
   report a blocker, or mark the evidence uncertain. A user-supplied reference
   image can inform initial routing but is never evaluated as the live desktop.
7. Missing credentials, timeout, invalid responses, or exhausted decision budget
   leave the main model to reason with its existing tools. Each answer with
   `accepted=false` is uncertain advice. A confident visual result can still be
   useful when the separate routing question is uncertain.

The route and tool recommendations are advisory. Existing authorization remains
at execution. The decision model cannot invent callable tools or their arguments.
A visual `complete` result concerns visible criteria; it does not prove an export,
save, publication, or other external operation succeeded.

## Configuration

Defaults are in `app/config.py`; example environment entries are in `.env.example`.

| Setting | Default |
| --- | --- |
| `DECISION_ROUTER_ENABLED` | `true` |
| `DECISION_ROUTER_MODEL` | `openai/gpt-6-luna-decisions` |
| `DECISION_ROUTER_TIMEOUT_SECONDS` | `8` |
| `DECISION_ROUTER_MIN_CONFIDENCE` | `0.7` |
| `DECISION_ROUTER_MAX_CALLS_PER_RUN` | `8` |

The threshold is an initial operating value, not a calibrated accuracy guarantee.
The decision budget limits auxiliary calls, not the agent's execution duration.
Bounded recovery runs with an explicit tool allowlist do not get this extra tool.
Restart an already running API/worker process to load the new code and settings.
No deployment or process restart was performed by this change.

## Observability

`Run.runtime_metadata.decisions` stores up to 16 decision outcomes: requested
model, status, accepted flags, selected choices, probabilities, confidence,
whether an image was supplied, and provider token/cost usage when supplied.
Provider errors store their class and HTTP status without response bodies.
Prompts, screenshots and credentials are not copied into this metadata.
Decision costs are recorded separately from the main model's usage counters.

Every decision now also emits an `INFO` event named
`agent_run.decision_router`. In JSON logs, filter that event and inspect
`decision_model`, `decision_status`, `route`, `route_confidence`,
`route_accepted`, `next_tool`, `next_tool_accepted`, `visual_progress`,
`decision_reason`, `input_tokens`, `cost_usd`, and `has_image`. The event does
not log the user request, conversation context, screenshot data, or credentials.

`GET /v1/runs/{run_id}` now includes the same persisted records under
`decision_events`. This makes completed and failed routing decisions inspectable
without parsing process logs.

## Verification

- 15 focused tests passed across `test_decision_router.py` and
  `test_computer_task_policy.py`; Python syntax and tracked whitespace checks passed.
- Live OpenRouter request: asking to create a music-event poster in Canva selected
  `computer`, confidence 0.84. The screenshot recommendation had confidence 0.63,
  so it remained uncertain without discarding the accepted route.
- Live OpenRouter request: asking only for typography advice for a Canva poster
  selected `respond`, confidence 0.99, and `none` for the next tool.
- Live multimodal request using a synthetic blank canvas succeeded. The visual
  question selected `continue`, confidence 0.91. The route was uncertain, which
  is recorded independently. This validates image transport and parsing, not a
  completed Canva workflow or the quality of a real design.
- Mocked integration verifies that a screenshot blocked by private input is not
  sent to OpenRouter, unknown tool choices are rejected, uncertain advice does
  not force routing, and network failures have bounded fallback behavior.

## Provider references

- [OpenRouter model and endpoint](https://openrouter.ai/openai/gpt-6-luna-decisions)
- [Multimodal decision request format](https://openrouter.ai/docs/guides/community/multimodal-decisions)
- [OpenRouter decision questions and answers](https://openrouter.ai/blog/tutorials/how-to-use-jev)
