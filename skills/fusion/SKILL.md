---
name: fusion
description: >-
  Main entrypoint for the Fusion Codex plugin. Use when the user says "fusion",
  "use Fusion", "ask the council", "get a second opinion", "run a panel",
  "fusion setup", "fusion gate", or wants Codex to coordinate local CLI
  panelists through the bundled bash and Python backend, or run Sidekick,
  genius-boss, idiot-boss and native coding teams.
---

# Fusion Entrypoint

Route the request to the narrowest Fusion skill:

- Sidekick, genius boss, or idiot boss coding: use `fusion-sidekick`.
- Native coding team, swarm forms, map-reduce, mesh, blackboard, pipeline,
  ring, or builder-breaker: use `fusion-team`.
- Agent inboxes, typed handoffs, file ownership or shared context: use `fusion-board`.
- Setup or health check: use `fusion-setup`.
- General second opinion, panel, council, debate, vote, or dynamic mode choice:
  use `fusion-orchestrate`.
- Python-backed MoA/heavy/discuss/flow/refine/bestof/ladder/speclock/breaker/ballot/gate:
  use `fusion-swarms`.
- Graph engineering — designing the work as a typed-node graph (fan-out, verification,
  routing, reducers, bounded loops, human gates), or building graph memory / GraphRAG
  (ontology, extraction, entity resolution, typed edges): use `fusion-graph`.
- UltraCode bridge: use `fusion-ultracode`.
- UltraSwarm model selection and council: use `fusion-ultraswarm`.

In Codex, resolve `FUSION_PLUGIN_ROOT` from this loaded `SKILL.md` path: it is
two directories above `skills/fusion/SKILL.md`. Invoke backend scripts by
absolute path and keep the user's project directory as the command CWD.

Keep the selected host model and explicit worker roster. Native coding modes
use live native workers; planning/preparation does not execute them. Never
substitute a CLI or invoke Claude/Fable without an explicit user request.

