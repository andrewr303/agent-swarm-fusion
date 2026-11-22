# Approved Fusion upgrade implementation

The user approved all four phases on September 30, 2026 with "looks good, go".
Scope: local implementation and offline verification. No Git publication, paid
provider evaluation, external messaging, recurring automation or credential
changes are included. Preserve the selected lead/worker roster, the existing
pride gate and cross consult, Sidekick and both boss modes.

## Windows installer delivery

The common PowerShell installer, four host wrappers, batch launchers and shared
Python packager are implemented. Actual native registration passed for all three
hosts in isolated profiles under Windows PowerShell 5.1. See
[installer verification](INSTALLER_VALIDATION.md) for the original evidence and
clean-machine/live-client gaps. No normal user profile was installed into.

## Phase 1: communication and recovery

Implemented versioned envelopes, action/trace/reply correlation, task-attempt
fencing, bounded retry/deadline policies, dead letters and explicit retry,
progress coalescing, durable database effect receipts and versioned readiness.
Prepared-team briefs, result handoffs and lead decisions use the same protocol.
Legacy board messages and database rows remain valid. Recovery refuses an
observed active native handle. The CLI and stdio MCP expose the new operations.

Verification: 64 focused tests passed (13 new protocol cases plus 51 existing
mailbox/task/team/MCP cases). Real Python/stdin transport and CLI processes verify
persistence across process boundaries. The database effect receipt guarantees
one stored effect record; external effects require their own idempotency. Native
readiness is a cooperative host observation, not authentication or a live-host
dispatch claim. A live native worker exchange remains unverified.

## Phase 2: execution and scope

Implemented injected Codex/AndrewCode/explicit-Claude native contracts, explicit
CLI requests, durable dispatch ownership and observed controller state, pause/
resume and budget changes, failure classes, bounded retries/circuit, original
process identity and owned Windows process supervision. Actual file baselines
include ignored files and Git identity. Authorized branchless directories and
detached worktree plumbing support explicit provisioning; accepted integration
checks source conflicts. No automatic provider fallback or Git publication.

Actual offline subprocesses exercised dependent Python coding/check commands,
restart without duplicate launch, cancellation, deadline, supervisor crash,
model/stale-attempt refusal and observed scope violations. Directory provisioning
and conflict preservation passed. Live native tool bindings, authenticated CLI
coding and actual Git worktree/host-managed execution remain unverified.

## Phase 3: composer and exact reviews

Implemented a versioned registry preserving all 16 forms, independent leadership/
communication/roles/isolation/review/bounds and composed stage edges. Six coding
workflows define accepted dependencies, candidate gates or migration waves.
Read-only candidate reviews can start before producer acceptance; normal write
dependencies remain gated. Exact artifact digests reject stale approvals,
critical objections need evidenced resolution, and producer revision requeues
invalidated reviews while retaining their history. Accepted read-only inputs
retain source-version bindings. Sidekick handoffs include scope negotiation,
requested decisions, actual checks and next ownership.

## Phase 4: supervision and knowledge

Implemented persisted terminal status/trace/redacted export, audited operator
controls, the existing dashboard's bound Active Run view, cited project knowledge
with revision/acceptance invalidation and SQLite FTS5/fallback search, eight
offline fault/coding benchmarks and a capability doctor. Reported usage retains
coverage and observed units; missing provider cost/usage remains unavailable.
This project knowledge does not write Codex's global memories.

Actual HTTP checks cover state, pause/resume, token/Origin enforcement and stale
control. BrowserOS tools were absent and all supported browser entry points failed
before contact with a kernel assets path error. Three browser acceptance cases
remain blocked; no screenshot, keyboard or responsive-layout pass is claimed.

## Previous release evidence

The previous release recorded **753 passing tests** in 67.531 seconds; **eight
offline benchmark scenarios passed**. The native Windows PowerShell 5.1 batch
chain registered and verified all three hosts in isolated profiles with **16
forms and 50 MCP tools**. See [upgrade verification](UPGRADE_VALIDATION.md),
[runtime usage](SWARM_RUNTIME.md) and the retained historical installer record.

No historical performance improvement or paid-model coding-quality advantage is
claimed. The benchmark supports compatible prior measured rounds; no comparable
historical baseline was supplied. Clean-machine dependency download/build and
fresh authenticated host/client MCP execution were not exercised.

The detailed accepted scope and source anchors are in [UPGRADE_PLAN.md](UPGRADE_PLAN.md).

## Fusion 3.3.0+swarm verification

The full Python suite ran 1,178 tests with 17 skips and no failures. The
plugin validation script passed. Board conformance exercised 72 local
checks, loopback HTTP, and 50 fake-hosted checks. Installer tests initialized
both MCP launchers using temporary profiles.

The export omits legacy MetaLoop and its sibling plugin, so their three
transport checks were skipped. Live hosted-board conformance, authenticated
provider sessions, and installation into normal host profiles were outside
this verification run.
