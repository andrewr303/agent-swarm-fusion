---
name: fusion-sidekick
description: >-
  Implement coding tasks with Fusion Sidekick, genius boss, or idiot boss.
  Use for focused execution partners, strong lead with narrow workers, or
  light coordination with autonomous expert workers. Preserves the selected
  host model, explicit worker roster, scoped writes, evidence and lead review.
---

# Fusion Sidekick

The selected host leads the task and owns interpretation, integration and final
review. Keep that model. Use only live, explicitly selected native worker models;
never silently substitute a CLI or a model. Do not invoke Claude/Fable without
an explicit user request. A mode name supplies no extra tool or publication authority.

Choose the requested leadership contract:

| Mode | Lead | Worker |
| --- | --- | --- |
| `sidekick` | Frame the task, resolve ambiguity, review and revise | Execute a focused, checkable brief |
| `genius-boss` | Specify interfaces, design, edge cases and acceptance criteria | Follow the design; escalate deviations before changing it |
| `idiot-boss` | Preserve intent, route work and review artifacts | Own exploration and technical decisions; challenge flawed assumptions with evidence |

These names describe delegation styles. They do not measure intelligence,
automatically downgrade the selected lead, or guarantee lower cost.

## Run the work

1. Resolve the plugin root from this file (two parent directories). Read relevant
   project instructions and only the source needed to write a good brief. Discover
   native spawn, message and wait tools from their actual schemas. If native workers
   are unavailable, implement directly or report the missing capability; do not
   present a prepared plan as an executed swarm.
2. Create a `TeamSpec` using the schema and example in
   [coding teams](../../docs/CODING_TEAMS.md). Use the user's selected lead and worker
   models. Each job names its worker, read/write paths, dependencies, requested
   checks and concrete success criteria. Keep one writer per file. Architecture
   choices stay with the lead in Sidekick/genius mode; expert workers receive intent
   and constraints in idiot mode.
3. Plan offline, then queue on one absolute board path:

   ```text
   python <plugin-root>/fusion.py sidekick --spec <absolute-spec.json>
   python <plugin-root>/fusion.py sidekick --spec <absolute-spec.json> --prepare --board-db <absolute-board.sqlite>
   ```

   Replace `sidekick` with `genius-boss` or `idiot-boss` when requested. Set each
   worker's `FUSION_BOARD` and identity, or include the absolute board path in MCP calls.
4. Dispatch only `ready` jobs using native workers with their exact roster models.
   Use the [durable runtime](../../docs/SWARM_RUNTIME.md) outbox when available:
   persist and claim each dispatch before host launch; record actual handle/model.
   A lost reply is uncertain and must be reconciled before another launch.
   Send the prepared `worker_packets[job_id]`, known paths, allowed writes,
   dependencies and success check. Reuse a worker's own context for revisions.
   Workers call `start-task` before writing and retain its attempt number. File
   leases are advisory coordination; host permissions remain authoritative.
5. Use native completion/message notifications and supported wait/join tools.
   At a task boundary read `inbox`; acknowledge after processing. Exchange concise
   findings, blockers, decisions and reports on the board. Include `task_id`, named
   recipients and evidence. Never poll in a loop or relay whole transcripts.
6. Workers submit a real `WorkerReport` with changed paths, command results and
   gaps. The lead inspects the artifact and evidence, then uses `review-task`
   to accept, revise, escalate or cancel. A reported check is not proof it ran;
   inspect the actual output and run any missing necessary verification.
7. Prepare the same spec again to refresh `ready` while preserving progress. Repeat
   dispatch/review until every job is accepted or a specific unresolved blocker
   requires user input. For a stopped/crashed worker, reconcile its changes before
   `recover-task`; this operation never stops a process. Finish with the delivered
   behavior, actual checks and any gaps.

Keep authorization and valid evidence across turns. Do not repeat completed source
investigations or checks unless their inputs changed. Read
[coordination](../../docs/COORDINATION.md) for leases, retry keys and recovery.
