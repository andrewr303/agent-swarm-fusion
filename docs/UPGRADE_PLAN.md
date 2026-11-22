# Fusion improvement plan for approval

Status: approved for local implementation and offline verification of all four
phases on September 30, 2026 ("looks good, go"). The Windows installers are
implemented separately. Research snapshots: September 30, 2026. Source inspection
covered the four requested repositories' READMEs,
licenses and selected implementation files; their applications were not run.

The aim is a reliable coding swarm that works through the chosen host, communicates
clearly, and offers useful combinations of execution strategy and leadership.
Fusion already has Sidekick, genius-boss/idiot-boss, 16 native coding forms, typed
briefs, durable inboxes, file reservations, versioned context and accepted task
handoffs. This plan builds on those capabilities. The largest current gap is the
connection between planning, real host execution, recovery and operator visibility.

## What to adapt from each repository

| Source and inspected snapshot | Useful design | Proposed Fusion adaptation |
| --- | --- | --- |
| [superpowers-v](https://github.com/procoders/superpowers-v/tree/66fcee6ccdcbbfe50496afa889546d391bbd8510), MIT | Request sizing, scoped manifests, worktree provisioning and a deterministic check of actual changed files | An explainable task-size recommendation; optional code/docs/domain reconnaissance; baseline-aware scope checks tied to each task's allowed writes |
| [claude-octopus](https://github.com/nyldn/claude-octopus/tree/2bbe1b033d9cfe4ce30a9a9c027b529071a5c1f2), MIT | Discover/Define/Develop/Deliver workflows, structured checkpoints, explicit provider workflows and two-stage review | Reusable coding workflows with spec and artifact reviews, compact host handoffs, and capability checks before executing the selected roster |
| [OpenSwarm](https://github.com/Intrect-io/OpenSwarm/tree/796e3c0d432c1e6516652be16487b5ddecfeb4af), MIT | Durable run ownership, process-liveness reconciliation, bounded retries, worker/reviewer stages and project knowledge | Run supervision, failure classification, pause/resume and restart recovery; artifact-linked project knowledge after acceptance |
| [agent-orchestrator](https://github.com/Untrivial-ai/agent-orchestrator/tree/daeff885b57685b4690ececb25d4fc330b69783d), Apache-2.0 | A host-neutral supervisor, isolated workers, delivery-readiness checks and status derived from actual agent/CI/review facts | A small host adapter interface and an operator view that distinguishes working, awaiting review, blocked, needs input and completed work |

These are design adaptations, not plans to run all four systems inside Fusion.
Implementation should be original and keep the current stdlib Python core. Any
later reuse of source must carry its license, notices and source-to-destination
attribution. The upstream projects' provider defaults, automatic commits, hooks
and background automation do not become Fusion defaults.

## Constraints that apply to every phase

- Preserve the selected lead and the explicitly selected worker roster. A missing
  model produces a visible unavailable state; it does not trigger substitution.
- Keep native execution as the default. External provider workflows and Claude
  adapters require explicit invocation. Installing the Claude Code host does not
  enable paid Claude reviewers or Fable calls.
- Keep ordinary requests dormant. Triage recommends an approach; an explicit
  Fusion invocation or standing user authorization starts orchestration.
- Preserve the existing pride gate, cross consult and absent-is-not-agreement
  contracts. A quorum cannot erase a critical, evidenced unresolved objection.
- Preserve unrelated changes and one writer per scope. Git branches, commits,
  pushes, PRs, merges, messages to people and recurring automation require the
  corresponding user authorization.
- Distinguish planned, dispatched, observed and accepted work in every interface.
  Prompt text and a worker's claim that tests passed are insufficient evidence.
- Extend the board schema additively and test migration of real older rows. File
  leases and scope checks remain cooperative controls; an OS sandbox is a separate
  capability that an adapter must prove when requested.

## Phase 1 — communication and recovery

Priority: highest. Extend `mailbox.py`, `coordination.py`, `comms.py`, `mcp_server.py`
and the Sidekick report contracts. Reuse the existing delivery leases, receipt
tokens, task attempt numbers and compare-and-swap context versions.

Add a versioned message envelope with run/task/attempt IDs, a trace ID, a reply
reference, purpose, concise summary, artifact references and an explicit requested
action. Provide typed handoffs, questions, decisions, blockers, review findings,
progress and stop requests. Keep delivery acknowledgment distinct from a worker
accepting an assignment or the lead accepting a result.

Add consumer-side idempotency records, bounded negative acknowledgment/retry,
delivery deadlines and a dead-letter view. Coalesce routine progress while keeping
decisions and blockers durable. Attach the task attempt to messages so an old
worker cannot complete or alter a new attempt. Define readiness before dispatch:
registered, able to receive the brief, started, producing observations and stopped
are distinct states.

Provide `inspect`, `trace`, `retry-delivery` and `reconcile` operations. Recovery
must check the runtime handle and reconcile outstanding changes before requeuing;
an expired lease alone cannot prove the old worker stopped. Restart restores the
run's ownership and history without launching duplicate workers.

Acceptance: interrupt delivery before and after receipt, restart the controller,
and recover the same task. No accepted decision or handoff is lost; duplicate
delivery creates one effect; stale attempts fail; unanswered questions remain
visible; blind council seats retain their access boundaries. Exercise actual
Python processes and the stdio MCP, then one explicitly authorized native-host
worker exchange. Record any host capability gap.

## Phase 2 — connect plans to real host execution

Add a small `host_runtime` interface for capabilities, dispatch, message delivery,
wait/join, observed status, cancellation and recovery. Build separate AndrewCode,
Codex and Claude Code adapters from their live exposed contracts. Keep native
session handles and model identity in the durable run record. CLI or ACP bridges
remain explicit alternatives when a native operation is unavailable.

Add an execution controller over the existing compiled team plan. Dispatch only
eligible tasks, wait on native completion events when supported, and record the
actual result and checks. Differentiate host error, provider quota, worker failure,
review revision and missing user input. Apply bounded retries to the same selected
model; stop repeated infrastructure failures for a concrete operator decision.

Extend write reservations with observed baseline/diff evidence. Check tracked,
untracked and relevant ignored files against allowed scopes, using explicit
provisioning snapshots to identify pre-existing artifacts. Add isolated workspaces
where the host supports them and the user has authorized that mode. Integration
uses accepted artifacts and explicit conflict handling, with no automatic Git
publication. A branchless directory option supports non-Git projects.

Acceptance: execute one dependent coding workflow, resume it after an interruption,
and show the same native worker identity, exact selected models and reviewed
artifacts. An out-of-scope write fails the scope check. Provisioned dependencies
are distinguished from later worker changes. Two independent tasks can proceed
without overwriting each other, while a blocked prerequisite never unlocks its
dependent. Test unsupported capabilities with visible, specific failures.

## Phase 3 — composable swarm options and stronger Sidekick

Move form behavior into a versioned strategy registry rather than adding more
names to a dispatcher. Retain existing forms and make their mechanics explicit.
Expose a preview/composer that validates the following independent choices:

| Choice | Initial options |
| --- | --- |
| Execution structure | Existing DAG, pipeline, relay, fan-out, hub-spoke, map-reduce, diamond, ring, mesh, blackboard and review forms; composition of stages |
| Leadership | Sidekick, genius-boss, idiot-boss |
| Communication | Parent/child, peer consultation, board-mediated, blind proposals |
| Roles | Planner, implementation owner, reproducer, specialist, integrator, spec reviewer, code reviewer, verifier; bound to selected seats |
| Isolation | Scoped shared checkout, host-managed workspace, authorized Git worktree, branchless directory |
| Review | Lead acceptance; optional separate spec/artifact review and evidenced critical-objection handling |
| Bounds | Worker concurrency, retry count, iteration count, explicit run budget and stop conditions |
| Entry workflow | Feature delivery, bug reproduction/fix, migration, refactor, documentation, security assessment, authorized CI repair |

Add six practical templates through those strategies: feature delivery,
reproduce/build/verify, migration waves, contract-first API work, review-and-revise,
and CI repair. Every template must define dependencies, ownership, real runtime
actions, output evidence, recovery and completion. A template is useful when it
changes behavior, not just its prompt label.

Improve Sidekick's brief quality, intent preservation, scope negotiation and
handoff summaries. Genius-boss keeps technical decisions and detailed decomposition
with the lead. Idiot-boss delegates technical choices to expert workers within the
same intent, scope and acceptance constraints. Leadership can be selected
independently of topology, for example map-reduce with genius-boss leadership.

Introduce read-only review of a submitted candidate artifact identified by digest,
while write dependencies remain blocked until acceptance. This supports spec
review followed by code review without treating unreviewed work as accepted.

Acceptance: inspect and execute representative composed workflows with distinct
dependency and communication behavior. Changing leadership changes decision
ownership while preserving intent and selected models. Review findings refer to
the candidate's exact version; stale approvals fail. Existing specs retain their
meaning through migration and the preview accurately predicts what executes.

## Phase 4 — supervision, knowledge and measured quality

Start with a terminal status/trace view and structured export. Extend the existing
dashboard only after the board exposes reliable runtime facts. Show agents,
dependencies, inbox backlog, ownership, last observed activity, review evidence,
blocked reasons and the next operator action. Readiness comes from the run record
and host events, not an agent's prose summary. Pause/resume, task recovery and scope
changes use explicit controller operations with an audit trail.

Add project-local knowledge with citations, scope, source revision, timestamp and
invalidation. Start with SQLite/FTS where available; embeddings are an optional,
measured extension. Promote only reviewed findings and decisions. Memory is evidence,
not executable instruction, and this feature does not write Codex's global memory
folder or host credentials.

Build a reproducible benchmark set for coding tasks, delivery faults, stale context,
scope violations, restart recovery and review defects. Compare against the current
Fusion baseline using observed completion, defect detection, wasted retries,
latency and reported usage. Missing provider usage is unavailable, not estimated
zero. Add a host doctor and a checked capability matrix covering installation,
discovery, MCP loading, dispatch, messaging, cancellation and recovery separately.

Acceptance: status survives a controller restart and agrees with persisted events;
operator actions affect the intended run once; source changes invalidate relevant
knowledge; exported evidence identifies the actual artifacts and checks. UI claims
require inspected browser evidence through Acceptance Neo and an available supported
browser runtime. Paid-model benchmarks require an explicitly selected roster and
budget; offline fault tests run independently.

## Implementation and approval scope

Recommended order: Phase 1, then Phase 2, then Phase 3, then Phase 4. Each phase
delivers code, migration/compatibility tests, documentation and a concrete acceptance
record before the next depends on it. Use the existing core and tests first;
introduce new modules only for an actual boundary. Refresh host contracts at the
start of an adapter implementation rather than relying on a remembered API.

Approval of this plan authorizes local implementation and offline verification of
the selected phases. External publication, recurring jobs, third-party messaging,
host credential changes and paid live evaluation remain explicit separate actions.
The recommended approval is the four phases in order, with optional integrations
kept opt-in and Phase 1 communication/recovery as the first delivery.

## Inspected implementation anchors

- superpowers-v: [scope checking](https://github.com/procoders/superpowers-v/blob/66fcee6ccdcbbfe50496afa889546d391bbd8510/scripts/compound-v-scope-check.py) and [manifest validation](https://github.com/procoders/superpowers-v/blob/66fcee6ccdcbbfe50496afa889546d391bbd8510/scripts/compound-v-validate-manifest.py). The baseline-aware scope check is the relevant mechanism; automatic model choices and commits are not proposed defaults.
- claude-octopus: [workflow stages](https://github.com/nyldn/claude-octopus/blob/2bbe1b033d9cfe4ce30a9a9c027b529071a5c1f2/scripts/lib/workflows.sh) and [host checkpoint export](https://github.com/nyldn/claude-octopus/blob/2bbe1b033d9cfe4ce30a9a9c027b529071a5c1f2/scripts/handoff.sh). The proposed handoff is a versioned contract with redacted, task-relevant context.
- OpenSwarm: [durable run coordinator](https://github.com/Intrect-io/OpenSwarm/blob/796e3c0d432c1e6516652be16487b5ddecfeb4af/src/automation/durableRunCoordinator.ts) and [pipeline identity/events](https://github.com/Intrect-io/OpenSwarm/blob/796e3c0d432c1e6516652be16487b5ddecfeb4af/src/agents/pipelineCoordination.ts). Process identity and recovery fencing matter more than adopting its entire daemon stack.
- agent-orchestrator: [message delivery readiness](https://github.com/Untrivial-ai/agent-orchestrator/blob/daeff885b57685b4690ececb25d4fc330b69783d/backend/internal/session_manager/message_delivery.go) and [daemon lifetime supervision](https://github.com/Untrivial-ai/agent-orchestrator/blob/daeff885b57685b4690ececb25d4fc330b69783d/backend/internal/daemon/supervisor/supervisor.go). The readiness checks demonstrate why a session handle alone is insufficient delivery evidence.

These sources support the design inspiration. All four phases were approved and
implemented locally; [implementation status](IMPLEMENTATION_STATUS.md) records
verification and the remaining live-host/browser/paid-evaluation gaps.
