# Swarm runtime (introduced in Fusion 3.2, current plugin version 3.3.0+swarm)

This page is for people who compose and run native coding swarms: a lead and
named workers, a durable board, exact-artifact reviews and a lead-controlled
execution outbox. It covers the composer (`strategies`), the execution API
(`control`), supervision (`supervise`) and project knowledge. For the 16 team
forms and the TeamSpec format see [CODING_TEAMS.md](CODING_TEAMS.md).

The coding runtime combines 16 topology strategies, three leadership styles,
five communication policies, eight role types, five isolation modes and six
workflow templates. All seats use the explicit selected roster. Native tools
remain the default; unavailable tools/models block execution. CLI transports and
Claude execution each require explicit invocation. These counts come from
`python fusion.py strategies catalog` (source:
[`strategies.py`](../python/fusion_swarm/strategies.py)); re-run it to re-check them.

This runtime is separate from the 10 executable forms in
[FORMS.md](FORMS.md) (`/fusion:tournament`, `/fusion:delphi` and so on), which
dispatch to provider CLIs. The 16 forms here compile into board tasks that a
host's own native workers execute. Boards here are the native HiveBoard
([COORDINATION.md](COORDINATION.md)), not the Vercel-Board-compatible
[VBOARD.md](VBOARD.md). Provider and transport rules (Codex gate, AndrewCode,
no implicit fallback) are in [PEERS.md](PEERS.md).

## Compose and preview

Run `python fusion.py strategies catalog` for the versioned options. Keep an
ordinary TeamSpec and add a `strategy` object when composing axes:

```json
{
  "version": 1,
  "communication": "peer-consultation",
  "roles": {"implementation-owner": "builder", "verifier": "verifier"},
  "isolation": {"mode": "scoped-shared"},
  "review": {"required_stages": ["artifact"]},
  "bounds": {"max_concurrency": 3, "max_retries": 1, "max_dispatches": 20,
             "timeout_seconds": 900, "auto_retry": false},
  "stages": [["frame"], ["build-a", "build-b"], ["integrate", "review"]]
}
```

The selected seats and job IDs must exist. Stage groups add accepted dependencies
between consecutive groups; the compiler still rejects cycles and unordered
overlapping writers. Bind a job's `role` to its selected `assigned_to`; a role map
can select one seat or a list of seats. Leadership stays independent of topology:
genius-boss puts design decisions with the lead; idiot-boss puts technical choices
with expert workers. Both preserve the same intent, scopes, checks and acceptance.
These are delegation contracts, not measured claims about model intelligence.

```bash
python fusion.py strategies preview --spec swarm.json
python fusion.py strategies workflow --name feature-delivery --spec stages.json
python fusion.py strategies workflow --name feature-delivery --spec stages.json --prepare --board run/board.sqlite
```

`workflow` prints the compiled preview; `--prepare` queues it directly.
`--output-spec` saves the generated raw TeamSpec for later use with `team`.
Do not pass a compiled preview back as a raw TeamSpec. The compiler and preparation
call no models; `prepare` provisions an isolated workspace only when that mode
has explicit authorization. `preview` and `workflow` accept `--allow-claude` for
Claude-backed seats (the flag only permits the declaration).

Without `--board`, `strategies --prepare` writes
`<workspace>/.fusion/<run_id>/board.sqlite`. `python fusion.py team --prepare`
defaults to `~/.fusion/teams/<run_id>/board.sqlite` instead and takes
`--board-db`. Pass an explicit path in both cases to avoid the mismatch.

### Worked example (executed)

The following was run on 2026-11-22 in a scratch copy with a scratch `HOME`. The
spec `wf.json` is a `dag` TeamSpec with a lead and three workers and the five
`feature-delivery` jobs (`plan`, `implement`, `spec-review`, `code-review`,
`verify`), each assigned to a worker, with `implement` the only writer.

```bash
python fusion.py strategies workflow --name feature-delivery --spec wf.json \
  --prepare --board ws/wf.sqlite
```

Reduced output of that command:

```text
ready:  ['plan']
waves:  [['plan'], ['implement'], ['spec-review', 'verify'], ['code-review']]
review: {'required_stages': ['spec', 'artifact']}
plan         deps []                           role planner
implement    deps ['plan']                     role implementation-owner
spec-review  deps ['implement']                role spec-reviewer   candidate_of implement
code-review  deps ['implement','spec-review']  role code-reviewer   candidate_of implement
verify       deps ['implement']                role verifier
```

The template added the edges, the candidate-review bindings and the default roles.
`ready` shows that only `plan` can start.

| Workflow | Required job IDs | Mechanics |
| --- | --- | --- |
| feature-delivery | plan, implement, spec-review, code-review, verify | accepted framing; spec then code review of an exact candidate; verification waits for accepted implementation |
| reproduce-build-verify | reproduce, fix, verify | accepted reproduction evidence before fixing, then verification |
| migration-waves | inventory, migrate, verify; optional migrate.* | inventory unlocks disjoint migration owners; verification joins accepted migrations; explicit stage groups serialize waves |
| contract-first-api | contract, implement, contract-review, verify | accepted contract before implementation; read-only candidate review before implementation acceptance |
| review-and-revise | build, review, verify | version-bound candidate review, explicit revision/acceptance, then verification |
| ci-repair | reproduce, repair, verify | read-only reproduction, scoped repair, accepted verification; remote CI actions need their own authorization |

Templates require fully specified jobs, real paths/checks and selected seats.
They never invent models, automatically widen scope or publish Git changes.

## Native execution and communication

The same operations are available through stdio MCP and JSON control:

```bash
python fusion.py control create_run --board run/board.sqlite --input start.json
python fusion.py control dispatch_actions --board run/board.sqlite --input run-identity.json
```

`control OPERATION` accepts any tool name in the coordination server's registry
(51 names; the table is in [COORDINATION.md](COORDINATION.md)). Arguments come from
`--input FILE` or stdin. Errors print `{"error": ...}` on stderr and exit 2
([`control.py`](../python/fusion_swarm/control.py)). Re-check the list with
`PYTHONPATH=python python3 -c "from fusion_swarm.mcp_server import TOOL_MAP; print(len(TOOL_MAP))"`.

`start.json` contains `agent_id`, `run_id`, `host`, live `capabilities` and optional
`bounds`. Capabilities identify the host, exact available `models`, actual supported
`operations` and `source: live-runtime`. The selected native host supplies this
inventory; cached lists and planner metadata do not attest availability. The lead's
own model must be in `models` too. When a selected model is missing the run is
created in state `blocked`, not `active`.

Default bounds, when none are given: `max_concurrency` 4 (at most 64),
`max_retries` 1 (0 to 20), `max_dispatches` 100, `timeout_seconds` 900,
`auto_retry` false ([`execution.py`](../python/fusion_swarm/execution.py)
`bounds_from`).

Executed against the board prepared above, with `host: codex` and
`models: ["lead-model", "m1", "m2", "m3"]`:

```text
create_run (lead-model absent from models)  -> state blocked, reason "selected models unavailable: lead-model"
create_run (lead-model listed)              -> state active
dispatch_actions                            -> 1 ticket: task plan, worker a, model m1, attempt 1, timeout 900
supervise status                            -> "plan: running (attempt 1)", "runtime ...: requested | handle uncertain | model=unobserved"
```

`dispatch_actions` called no model. It only queued a ticket that a host must claim
and launch.

1. Prepare the plan, then `create_run`. Use the strategy bounds or explicit limits.
2. `dispatch_actions` persists eligible requests with task attempt, exact selected
   model, workspace, prompt and stable ticket. It starts and reserves each task.
3. `claim_dispatch` returns an ownership token/version. Launch through the host's
   exposed native dispatch **only when `new_claim` is true**. Retain the real handle.
4. Record `observe_runtime` with the actual handle, model, state and a stable
   event key. Use real native messages and bounded wait/join when exposed. A
   worker's presence or prose is insufficient process-liveness evidence.
5. After observed completion, `submit_runtime_result` compares actual file hashes
   with the pre-dispatch baseline and submits for review. The lead inspects real
   artifacts/check outputs and explicitly accepts or revises the task.

Python clients can bind actual callable host operations using `CodexRuntime`,
`AndrewCodeRuntime` or explicitly requested `ClaudeCodeRuntime`, then use
`ExecutionController`. These are injected contracts, not hidden discovery of
desktop tools. MCP host skills can perform the same outbox protocol directly.
`ProcessRuntime` supervises explicitly selected local commands or CLI transports
with durable request files, PID creation identity and captured output. It owns a
Windows Job Object on Windows and escalates SIGINT, SIGTERM, SIGKILL over the
owned process group elsewhere
([`process_runtime.py`](../python/fusion_swarm/process_runtime.py)). It never
substitutes a CLI for missing native dispatch.

`host_runtime.cli_request` builds a CLI argv only for the Claude host and only
with `explicitly_requested=True` and `allow_claude=True`. For `codex` and
`andrewcode` it raises the AndrewCode control-gap error, and it refuses to probe
a standalone Codex executable at all. The built request records
`provider_called: false`. This documentation pass did not exercise authenticated
provider flows or live native adapter bindings.

Typed `send_message` envelopes preserve run/task/attempt/trace, reply target,
purpose, artifact references and the requested action. Producer retries use stable
keys. Delivery acknowledgment, assignment ownership and result acceptance are
different facts. `consume_delivery` atomically stores one deduplicated **database
effect receipt** and acknowledges delivery; external effects need their own
idempotency. Failed delivery budgets/deadlines become dead letters; explicit
`retry_delivery` reopens them after the cause is resolved. New progress coalesces
pending progress while decisions/history remain durable. Typed sends enforce the
prepared peer allowlist; legacy channels remain cooperative transport.

An uncertain launch holds its seat and capacity. It cannot be automatically
replaced. Reconcile the **original** native handle and actual changes before
`reconcile_runtime`. A verified not-dispatched ticket can be reconciled explicitly;
an expired file lease alone does not prove the worker stopped. Native observations
are cooperative host assertions, not authentication. Infrastructure retries use
the same selected model, explicit bounds and an opt-in `auto_retry`; the third
recorded infrastructure failure moves the run to `needs_input` with reason
"repeated infrastructure failure". Setting the run active again resets the
count. Authentication, quota,
worker, review and missing-input failures are separate classifications.

`control_run` pauses/resumes new dispatch with a reason and expected run version.
It preserves running workers. `amend_run_bounds` explicitly changes dispatch,
retry, concurrency/time bounds and can renew a deadline. No token/cost budget is
claimed without observed usage. `amend_task` negotiates queued scope/check changes
against the current brief digest and records the decision; it preserves the seat
and dependency graph and rejects unordered writer overlap.

## Candidate review, scope and isolation

A complete submission captures the actual artifact digest. A read-only job can
set `candidate_of` to its producer and `review_stage` to spec, artifact or critical.
Its producer remains in `dependencies`, but that edge unlocks at submitted review
state rather than acceptance. Normal write dependencies still require acceptance.
Separate selected workers record `record_candidate_review` for their bound
digest. Unresolved critical/major findings prevent approval; resolutions need
evidence. Required review stages must be approved and lead-accepted for the current
digest before producer acceptance. Changed candidates reject stale approvals.
Accepted read-only dependency inputs retain source-version bindings during work.

Baselines include tracked, untracked and ignored files and Git HEAD/branch/index
identity. Explicit provisioning records pre-existing artifacts. Shared checkout
changes within another job's declared scope are `shared_unattributed`: the board
cannot determine which OS process wrote them. Ownership checks are cooperative,
not a security sandbox. Use isolation when process attribution is required.

The five isolation modes in `strategy.isolation.mode` are `scoped-shared`
(default, the shared checkout), `host-managed`, `git-worktree`, `directory` and
`apfs-clone`. Every mode except `scoped-shared` needs `isolation.workspace`.
`directory`, `apfs-clone` and `git-worktree` also need `authorized: true` and a
new destination that is not inside or above the source. Worktrees need a selected
`ref` and a source with `.git` and `git` on `PATH`. `directory` and `apfs-clone`
copy files, using a copy-on-write clone per file where the filesystem offers it
(`clone_mechanism` reports `apfs-clone` or `copy`). Both refuse workspaces that
contain symlinks. Host-managed workspaces must already exist and differ from the
source. Preparing persists provisioning once.
`scope.integrate_directory` integrates accepted concrete files only and
refuses source changes since provisioning; it preserves unrelated actor work.
Integration is an explicit lead operation, with no staging, commits, branches or
publication. Source: [`scope.py`](../python/fusion_swarm/scope.py),
[`team.py`](../python/fusion_swarm/team.py) `prepare`. Worktree plumbing is
implemented. This documentation pass did not execute a host-managed or Git
worktree run.

## Supervision and project knowledge

```bash
python fusion.py supervise status --board run/board.sqlite --lead lead --run run
python fusion.py supervise trace  --board run/board.sqlite --lead lead --run run
python fusion.py supervise export --board run/board.sqlite --lead lead --run run --output evidence.json
python fusion.py supervise doctor --host codex
```

`status`, `trace` and `export` need an existing `--board` plus `--lead` and `--run`.
`status` prints text unless `--json` is given. Status shows persisted
identities/readiness, dependencies, inbox backlog, scopes, review state, blockers
and concrete next operations. Export redacts ownership and credential-shaped
fields. Reported attempt usage is retained with coverage; missing units/cost remain
unavailable. The doctor distinguishes installation, declared discovery, MCP loading
and unexercised execution capabilities. Its `--host` choices are `codex`,
`andrewcode` and `claude`, with optional `--executable`.

For hosts `codex` and `andrewcode` the doctor probes the AndrewCode Node entry
(`node <entry> --version`), never a standalone Codex CLI. Run in a scratch
`HOME` with no AndrewCode checkout, `supervise doctor --host codex` reported
`installation: unavailable` with evidence `AndrewCode Node entry is missing` and
`discovery`, `dispatch` and the other facts `unverified`. The report always ends with
`provider_calls: 0` and the boundary note that installation and declared
capabilities do not prove authenticated execution. Do not run it against a real
install unless you accept the `--version` subprocess.

The cockpit dashboard's **Active Run** tab binds one existing board through
`FUSION_BOARD` (absolute path of an existing board), `FUSION_LEAD` and `FUSION_RUN`.
It serves `/api/run` and `/api/run/export` and accepts versioned pause/resume at
`POST /api/run/control` with the body keys `state`, `note` and `expected_version`.
Every POST needs the per-process session token (`X-Fusion-Token`) and a matching
`Origin` header. Routes, startup and verification are in [COCKPIT.md](COCKPIT.md).

`knowledge_promote` requires a lead-accepted task and concrete source citations
with current SHA256 revisions. Knowledge is project-scoped evidence in the board,
not global Codex memory or executable instruction. `knowledge_search` validates
source revisions/acceptance first, invalidates stale entries and searches SQLite
FTS5, with a substring fallback. Citations, scope, author, timestamps and versions
remain inspectable. Legacy memory features keep their existing separate contract.

Run `python scripts/benchmark_swarm.py --output measurements.json` (`--output` is
required) for the eight deterministic fault/coding scenarios: `coding-restart`,
`delivery-interruption`, `lost-dispatch`, `scope-violation`,
`infrastructure-circuit`, `stale-review`, `stale-knowledge`, `owned-cancellation`.
Each runs one named unit test, so it calls no provider. Actual local timings and
results are recorded, with provider usage/cost unavailable. `--baseline` compares a
compatible previous measured round. No historical baseline improvement or
paid-model quality, speed/cost advantage is claimed without that evidence.

Executed on 2026-11-22: the command printed `"scenarios": 8, "passed": 8,
"provider_calls": 0` and finished in about 1.7 seconds.
