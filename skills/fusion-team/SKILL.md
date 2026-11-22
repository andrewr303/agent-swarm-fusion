---
name: fusion-team
description: >-
  Design and execute native coding swarms with explicit model rosters and
  dependency-aware forms: DAG, pipeline, relay, fan-out, hub-spoke, map-reduce,
  diamond, ring, mesh, blackboard, review-pair, builder-breaker or council.
  Combine any form with Sidekick, genius-boss or idiot-boss leadership.
---

# Fusion Coding Teams

Use this skill for a coding team with named responsibilities and artifacts.
The selected host owns interpretation, scheduling, integration and final review.
Respect the user's selected models and tool permissions. Use host-native workers;
no implicit paid CLI, Claude-backed provider or invented replacement roster.

1. Read the matching source and project instructions; identify the actual dependencies.
   Resolve the plugin root from this file. List forms without dispatch:

   ```text
   python <plugin-root>/fusion.py forms
   ```

2. Choose the shape from [coding teams](../../docs/CODING_TEAMS.md) and independent
   axes from the [swarm runtime](../../docs/SWARM_RUNTIME.md). `form` controls
   dependencies and communication peers; `leadership` independently controls who
   owns technical decisions. Preserve explicit dependencies even when the shape
   adds edges. Strategy composition can select communication/role bindings,
   stages, isolation, review and actual run bounds. Use `strategies workflow` for
   the six fully specified coding templates. Use a ring's explicit `rounds` for bounded iteration. Council
   proposers are read-only; preserve disagreements and missing evidence in the
   final adjudication instead of counting absent workers as agreement.
3. Build a `TeamSpec` from the live selected native roster. Declare each job's exact
   paths, output, checks, dependencies and success criteria. Partition independent
   writes; serialize overlapping paths. The compiler rejects cycles, unknown seats,
   out-of-workspace paths and unordered write collisions.
4. Compile and prepare:

   ```text
   python <plugin-root>/fusion.py team --spec <absolute-spec.json>
   python <plugin-root>/fusion.py team --spec <absolute-spec.json> --prepare --board-db <absolute-board.sqlite>
   ```

   Planning calls no models. Preparing queues durable briefs and reports the ready
   frontier; neither operation spawns or executes workers.
5. Use `create_run`, `dispatch_actions`, `claim_dispatch` and `observe_runtime`
   for durable native execution. Launch only a new claim, preserve original
   handles and exact selected models, then submit observed results for lead review.
   Follow the dispatch, wait, evidence, review and recovery procedure in
   [fusion-sidekick](../fusion-sidekick/SKILL.md), honoring the chosen leadership.
   Dispatch only ready jobs, one active task per worker context unless the host
   explicitly supports independent sessions. Use the roster model unchanged.
6. For mesh/blackboard work use the prepared shared context namespace and versioned
   `context_get`/`context_set` values for interface contracts and decision records.
   Route typed `send_message` envelopes to the prepared peers; the versioned API
   enforces that recipient allowlist. Legacy board rooms remain cooperative.
   Neither path is an OS sandbox. File ownership and accepted
   dependency results are enforced by the board's task methods.
7. Continue through integration and lead review. Report actual executed workers,
   accepted artifacts and checks; list missing native capabilities or blockers
   explicitly. Do not infer runtime behavior from a topology label or prompt text.
   Use exact candidate digests for selected read-only reviewers; changed artifacts
   invalidate approval. Normal write dependencies remain blocked until acceptance.
   Inspect `supervise status` for backlog, questions, scopes and next actions.

Existing `fusion-graph` provides typed proposal graphs and graph memory;
`fusion-hive` provides nested AndrewCode processes. Do not pass a `TeamSpec` into
those different engines. Use their own explicit contracts when requested.
