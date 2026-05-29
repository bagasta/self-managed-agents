You are acting as a senior AI platform architect, staff-level backend engineer, and pragmatic product-minded systems designer.

You are reviewing and improving a SaaS product codebase for a general-purpose managed agent platform.

Project path:
`/home/bagas/managed-agents-project`

Core product reality:
This is NOT just a coding-agent platform.
This is a SaaS platform where users can create many kinds of agents depending on their needs, such as:
- customer support agents
- research agents
- workflow/ops agents
- knowledge/RAG agents
- scheduling/reminder agents
- coding/build agents
- channel-integrated assistants
- future specialized agents

So the architecture must remain general-purpose, modular, and SaaS-friendly.

High-level goals:
1. Evolve this project into a stronger general-purpose managed agent platform.
2. Ensure the platform can support different agent capability profiles without becoming biased toward one agent type.
3. Add or improve a main “Agent Builder” agent inside the platform:
   - a first-class platform agent
   - helps users design and create agents according to their needs
   - translates user intent into agent configuration, tool selection, runtime profile, safety policy, and instructions
   - acts as an onboarding / orchestration / meta-agent for agent creation
4. Keep the platform extensible so advanced agents (including coding-style agents) can exist as one profile among many, not as the default architecture for everything.

Critical constraints:
- Preserve existing external behavior unless a change is absolutely necessary.
- Prefer behavior-preserving refactors.
- Do not break current APIs, agent flows, integrations, configs, or expected runtime semantics unless explicitly justified.
- Avoid big-bang rewrites.
- Assume the codebase is already in active use and backward compatibility matters.
- Explicitly flag any recommendation that may change runtime behavior.
- Keep implementations clean, modular, and maintainable.
- Important coding rule: do not produce bloated files.
- Target a maximum of 300 lines per script/module when adding new files or refactoring large responsibilities.
- If a responsibility grows too large, split it into smaller modules with clear boundaries.
- Favor clean, readable, intelligent logic over clever but tangled code.

Your task:
Review the codebase and propose a precise architecture and implementation strategy for evolving this SaaS managed-agent platform in a clean and scalable way.

I want you to analyze and answer the following:

## 1. Product Understanding
Explain what this platform actually is as a SaaS product.
Describe it as a general-purpose managed agent platform, not merely a FastAPI app or coding assistant backend.
Explain what role the platform serves for end users and what kinds of agents it should support.

## 2. Current Architecture
Map the current architecture based on the real repository and code.
Identify:
- API/interface layer
- runtime/orchestration layer
- domain/business logic
- persistence/state
- tool system
- sandbox/execution model
- scheduling/background jobs
- channel integrations
- deployment-related logic
- any agent-building-related patterns already present

Pay special attention to:
- `app/main.py`
- `app/core/agent_runner.py`
- tool builder / tool registry logic
- sandbox-related files
- deployment-related files
- memory / skills / custom tool logic
- scheduler / proactive logic
- channel / WhatsApp integration code

## 3. What Is Already Good
Identify the architectural choices that are already strong and worth preserving.
Be concrete and repo-aware.

## 4. Architecture Problems
Identify weaknesses, technical debt, scaling risks, and structural issues.
Look specifically for:
- god files
- dumping-ground folders
- mixed responsibilities
- weak boundaries between runtime/domain/infrastructure
- fragile tool composition
- unclear capability model
- hard-to-extend agent configuration patterns
- poor separation between general platform concerns and specialized agent concerns
- areas where adding an Agent Builder would become messy
- anything that will hurt maintainability as the SaaS grows

## 5. Capability Model for a General-Purpose SaaS Agent Platform
Design a capability model that is not biased toward one kind of agent.

I want a clear proposal for how the platform should represent:
- agent identity/persona/instructions
- tool access
- runtime profile
- safety/governance policy
- memory behavior
- autonomy level
- integration access
- optional advanced capabilities

The design should support multiple agent classes, for example:
- assistant
- support
- research
- knowledge/RAG
- ops/workflow
- automation
- builder/coding
- privileged internal/system agents

Explain how the platform should stay general while still supporting advanced specialized agents.

## 6. Agent Profile / Runtime Profile Design
Propose a precise architecture for agent profiles or execution modes.

For example, define how the platform might distinguish between:
- lightweight assistant agents
- knowledge/research agents
- workflow/ops agents
- sandbox-enabled builder/coding agents
- privileged system/meta agents

Explain:
- what each profile should be allowed to do
- which tools/capabilities belong to each
- which runtime behavior should differ
- how to avoid over-powering every agent by default

## 7. Main Agent / Agent Builder Design
I want a main platform agent called something like “Agent Builder”.

This agent should help users create agents that fit their needs.
Design this carefully.

I want you to define:
- the role of the Agent Builder inside the product
- what inputs it should gather from the user
- how it should reason about user intent
- how it should translate requirements into:
  - instructions/system prompt
  - tool/capability selection
  - runtime profile
  - model choice
  - safety settings
  - escalation settings
  - memory configuration
- whether it should create draft agents, recommend configs, or fully provision agents
- what tools this Agent Builder should use internally
- how it should avoid creating unsafe or overpowered agents by default
- how it should fit into the existing architecture cleanly

Also explain whether Agent Builder should be:
- a regular agent with privileged tools
- a special system agent
- a service-backed workflow that uses an agent interface
- or a hybrid model

Be opinionated and practical.

## 8. Target Architecture
Propose the target architecture for this SaaS platform.

The architecture must:
- remain general-purpose
- support multiple agent classes
- support a first-class Agent Builder
- preserve compatibility where possible
- reduce coupling
- improve maintainability and extensibility

A structure similar to this is acceptable if it fits:
- API/interface
- application/use cases
- runtime/agent engine
- domain modules
- infrastructure adapters
- workers/background processing
- system agents/meta agents

But do not force this exact structure if the repository suggests a better one.

## 9. Recommended Module / Folder Structure
Suggest a concrete, clean folder/module structure for the next phase of the project.

Important rule:
- keep files small and focused
- target a maximum of 300 lines per script/module where possible
- split large responsibilities into cohesive modules
- do not create abstract layers without real value

Also explain:
- which current files should be split
- what new modules should be introduced
- where Agent Builder logic should live
- where capability profile logic should live
- where runtime policy logic should live

## 10. Safe Refactor Strategy
Because backward compatibility matters, classify all recommendations into:
- Safe refactor (intended to preserve behavior)
- Risky refactor (may affect behavior)
- Intentional product/runtime change

Prefer behavior-preserving refactors first.

For each major recommendation, include:
- purpose
- affected files/modules
- why it is safe or risky
- whether it preserves behavior
- what should be tested afterward

## 11. Implementation Plan
Provide a staged implementation plan with phases.

At minimum, include:
### Phase 1 — high-leverage safe cleanup
### Phase 2 — capability/profile architecture
### Phase 3 — Agent Builder foundation
### Phase 4 — privileged/system agent workflow
### Phase 5 — hardening, observability, and SaaS scalability

For each phase, include:
- goals
- exact areas of the codebase affected
- what should remain backward compatible
- what may change internally
- what tests should be added
- what success criteria define completion

## 12. Agent Builder Implementation Proposal
Go beyond high-level ideas.
Propose a practical implementation design for the first version of Agent Builder.

Include:
- whether it should create agents via existing CRUD flows or new internal services
- how it should generate agent configs safely
- whether it should produce a draft first for user confirmation
- how it should map user requests into profiles/capabilities
- how it should validate instructions and tool selections
- how it should handle unclear requirements
- how it should prevent dangerous configurations
- how it should evolve later into a smarter platform-native builder

## 13. Code Quality and Implementation Rules
When proposing code changes, follow these rules:
- prefer small focused modules
- do not let new scripts exceed 300 lines if avoidable
- favor explicit logic over hidden magic
- avoid monolithic orchestrator files
- avoid giant utility dumping grounds
- keep naming clean and intention-revealing
- keep domain logic out of infrastructure code
- keep platform policy enforcement in code, not only in prompts
- use abstractions only when they simplify real complexity
- write code as if other engineers will maintain it under production pressure

## 14. Top 10 Highest-Leverage Recommendations
End with the 10 changes that would most improve this platform as a SaaS managed-agent product.

## 15. First PR Recommendation
End with a final section titled exactly:

`First PR Recommendation`

In that section, propose the safest highest-impact first implementation step.
Include:
- exact files to create
- exact files to modify
- responsibilities to move
- why this is the best first step
- how it preserves current behavior
- what tests should be written

Additional instructions:
- Base your conclusions on the real repo, not generic assumptions.
- Read the implementation and docs carefully.
- If docs and code disagree, call it out.
- Use concrete file/module references where useful.
- Prefer practical architecture guidance over abstract theory.
- Keep the output clean, structured, and precise.
- Do not ramble.
- Do not produce vague best-practice filler.
- Be accurate, sharp, and implementation-aware.

Output tone:
A serious architecture and implementation review from someone helping turn this repository into a strong SaaS managed-agent platform with a first-class Agent Builder, while keeping the system clean, modular, backward-compatible, and maintainable.

Also include a final appendix titled:
`Draft System Prompt for Agent Builder`

In that appendix, write a first draft of the system prompt for the Agent Builder agent.
The prompt should help Agent Builder:
- understand the user’s desired agent
- ask only the necessary clarifying questions
- recommend the right profile/capabilities
- avoid unsafe defaults
- produce a clear proposed agent specification
- prefer simple and safe agent setups unless advanced capability is clearly needed