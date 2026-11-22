# Fusion MetaLoop

**MetaLoop — Fable 5 as CEO + Chief Operator + tiered swarm.** An explicit
Fusion mode (`/fusion:metaloop`) that behaves like an engineering org instead
of "five agents in a room": a CEO who frames every run, one accountable
operator who executes under that frame, senior specialists, a fast execution
bench, and deterministic checks that decide what can be decided mechanically.

Read this page if you plan to run MetaLoop or integrate with its routing and
budget logic. It describes the Python control plane in `metaloop.py`. The
host-native protocol, which is how a Claude Code or Codex session drives a run,
is in [`skills/fusion-metaloop/SKILL.md`](../skills/fusion-metaloop/SKILL.md).
The repository is named `agent-mesh`. The plugin id, `/fusion:*` commands,
`fusion_swarm` package and `FUSION_*` variables keep the `fusion` name.

This is **Phase 1**: real-repo proposal mode, explicit opt-in. The Python module
is a deterministic state and dispatch engine. It does not call the running host
as a subprocess, and no MetaLoop run starts without the host driving it.

## Roles

The tier, family, harness and default model of each provider come from
`adapter.PROVIDER_META`. The role text comes from `metaloop.py` and the skill.

| Plane | Role | Provider / harness | Responsibility |
|---|---|---|---|
| **CEO** | **Board Advisor (CEO)** | `fable` via `scripts/providers/fable.sh` (Claude Code print mode, default model `fable-5.1`) | **Frames every run first**: outcome, decomposition strategy, risk priorities, taste bar. Sets direction. **Not a worker, not a vote.** |
| **Operator** | Chief Operator | `codex` (default model `gpt-5.6-sol`), dispatched at plan, adjudicate and integration-review points when the host is not Codex | Plan under the CEO frame, decompose, keep critical work, adjudicate results, own the outcome |
| **Host** | Executor host | the in-context orchestrator (Claude Code, Codex CLI or AndrewCode) | Runs the mechanics, applies proposals, runs gates, owns the checkout |
| **Worker** | Worker swarm | `andrewcode` (expert tier, family `muse`, up to 4 sessions, default model `meta/muse-spark-1.3-contributor`) | Implementation and parallel subtasks. Preferred nested spawn when a board is set |
| **Expert** | Expert A | `grok` (`grok-4.5`) | Novel diagnosis, adversarial edge cases, alternative architecture, current context |
| **Expert** | Expert B | `opencode` (default `opencode-go/glm-5.2`) | Repo-scale reading, multi-file implementation, broad refactors |
| **Fast** | Fast A + B | two `agy` sessions (Antigravity, default `Gemini 3.5 Flash (High)`) | Inventory, scaffolding, mechanical edits, focused edits, tests, docs |
| **Control** | Deterministic runtime | `python/fusion_swarm/*` and the provider adapters | Contracts, routing, waves, budgets, gates, escalation, ledger |

Model ids are defaults. `config/defaults.env` and the `FUSION_*_MODEL`
variables override them, and `config/defaults.env` currently sets some
different values (for example `FUSION_AGY_MODEL` and `FUSION_OPENCODE_MODEL`).
Run `bash scripts/swarm.sh metaloop "x" --json` to see the roster your
environment resolves.

**Asymmetry is the point.** Fable is the CEO. It frames every run before any
work begins, but it never implements, dispatches or votes. The operator stays
accountable, executes under the frame, and records whether it adopted or
overrode each CEO directive. **A failing deterministic gate outranks every
model opinion, including the CEO's.** Copilot is **not** a MetaLoop worker
(`adapter.WORKER_PROVIDERS` is `andrewcode`, `agy`, `opencode`, `grok`).

Fable runs Claude Code, so `fable.sh` refuses to start unless
`FUSION_ALLOW_CLAUDE=1` is set. Without it the adapter exits with code 3 and
the CEO seat is absent.

## Modules

| File | Purpose |
|---|---|
| `python/fusion_swarm/contracts.py` | `TaskSpec`, `WorkerResult`, `AdvisorMemo`, `GateResult` dataclasses with fail-closed validation (stdlib only). Mirrors [`metaloop.schema.json`](metaloop.schema.json). |
| `python/fusion_swarm/adapter.py` | `PROVIDER_META` (tier, family, harness, model), worker, advisor and operator provider lists, `CORRELATION_GROUPS`, `WORKER_SESSIONS`. |
| `python/fusion_swarm/policy.py` | Hard overrides (host-owned work) first, then the transparent `expert_pressure` score. |
| `python/fusion_swarm/workspaces.py` | Proposal strategy and the scope gate (changed paths against allow and forbid lists). `WorktreeWorkspace` raises `NotImplementedError` unless it is built with an explicit `repo_root` and `authorized=True`. |
| `python/fusion_swarm/metaloop.py` | Run-state machine, dependency waves, correlation-aware family counting, advisor triggers, the bounded escalation ladder, run record, and optional Hive Board registration and nested AndrewCode spawn. |
| `scripts/providers/_common.sh` | `fusion_snapshot_repo` (a populated per-worker repo snapshot for READ) and `fusion_prompt_text` (prompt transport that avoids E2BIG). |
| `scripts/providers/fable.sh` | Read-only structured Fable advisor: prompt on stdin, no `--dangerously-skip-permissions`, write and exec tools disallowed where the CLI supports it, empty scratch cwd. |
| `scripts/providers/agy.sh` | Antigravity fast-worker adapter with capability detection. It fails visibly if the print interface it needs is missing. |

## Routing

Hard overrides win first. `policy.HOST_OVERRIDE_MARKERS` are lowercase
substrings matched against the task's `category` **and** `objective`. They cover
auth, secrets and tokens, migrations and backfills, payments and billing,
legal and compliance, public API and data contracts, architecture and
cross-cutting work, production config, merge conflicts and final integration.
A match keeps the task on the host (the Chief Operator). Work that matches
`APPROVAL_MARKERS` (destructive, production, delete, paid-large and similar),
or has `approval_level != "none"`, also sets a human-approval flag.

The match is a plain substring test. A task whose objective is
"Inventory billing modules" is routed to the host because of `billing`, even
though it is low risk. This two-task `plan.json` (`t1` inventory, `t2` invoice
rounding, category `billing`):

```json
[
  {"id": "t1", "objective": "Inventory billing modules", "category": "inventory",
   "acceptance_criteria": ["modules listed"], "complexity": 1, "latency_priority": 2},
  {"id": "t2", "objective": "Fix invoice rounding", "category": "billing",
   "dependencies": ["t1"], "acceptance_criteria": ["rounding fixed"],
   "complexity": 2, "risk": 2}
]
```

run with `bash scripts/swarm.sh metaloop "x" --plan-file plan.json --advisor auto --json`
produced:

| Task | `tier` | `expert_pressure` | `hard_override` | Reason |
|---|---|---|---|---|
| `t1` | `host` | -1 | true | `'billing' is Chief-Operator-owned work` |
| `t2` | `host` | 7 | true | `'billing' is Chief-Operator-owned work` |

with waves `[["t1"], ["t2"]]` and an advisor preflight triggered under
`--advisor auto` ("task t2 is high-risk host work").

Otherwise a single explainable score decides:

```text
expert_pressure = 2*risk + 2*complexity + context_size + novelty + ambiguity
                  + blast_radius - latency_priority - verification_strength
```

`verification_strength` is 2 for a `verification_cmd` plus 1 for any
acceptance criteria, capped at 3. Each input is an integer from 0 to 3.

- `expert_pressure >= 8` goes to the expert lane.
- `expert_pressure <= 3` and `risk <= 1` goes to the fast lane.
- Anything else is left to the host, using strengths, availability and ledger
  evidence.

## Correlation rule

The fast tier is **two `agy` sessions on the same Gemini model**, so they are
one Gemini family by construction. They give throughput and execution
diversity, never two independent votes. `count_model_families` collapses them
to one, and `is_fake_consensus(["agy", "agy"])` is `True`. Copilot is also
Gemini and sits in the same `CORRELATION_GROUPS` entry
(`{"gemini": ["agy", "copilot"]}`), so a mixed panel is counted correctly:
`is_fake_consensus(["agy", "copilot"])` is `True` and
`is_fake_consensus(["agy", "grok"])` is `False` (both checked by running them).
Never present two Gemini outputs as two-model consensus.

When `FUSION_ANDREWCODE_SELECTION` is set, every provider shares one harness
and `model_family` returns `unverified`, so the roster collapses into one
correlation group.

## Workspace (real-repo proposal mode)

MetaLoop workers must read the real codebase, so each worker gets a disposable
per-worker **snapshot** of the repo (`fusion_snapshot_repo`) inside its scratch
dir. It reads, and may edit, only that copy. The user's checkout is never
touched. The worker returns a patch or artifact that the host reconciles. This
keeps the no-user-tree-write invariant and stays safe under concurrency, since
each worker has its own copy.

Pass the repo as the sixth argument to `fusion.sh dispatch` or set
`FUSION_WORKER_REPO`. The snapshot is capped by `FUSION_SNAPSHOT_MAX_FILES`
(default 4000) and `FUSION_SNAPSHOT_MAX_FILE_KB` (default 512). Prompts above
`FUSION_PROMPT_INLINE_MAX` characters (default 16000) become
`work/.fusion-task.md`, which the worker reads from its own cwd instead of
overflowing argv.

## Budgets (defaults)

```text
advisor_policy            always      max_repair_rounds        2   (Fable is the CEO)
max_advisor_calls         2           max_attempts_per_task    3
expert_concurrency        2           max_total_external_calls 12
fast_concurrency          2 (2x agy)  max_wall_seconds         1800
workspace_mode            proposal    risk_threshold           2
```

These are the values `bash scripts/swarm.sh metaloop "x" --json` reported. The
environment variables are `FUSION_META_ADVISOR_POLICY` (`off`, `on-demand`,
`auto`, `always`), `FUSION_META_MAX_ADVISOR_CALLS`,
`FUSION_META_MAX_REPAIR_ROUNDS`, `FUSION_META_MAX_ATTEMPTS_PER_TASK`,
`FUSION_META_EXPERT_CONCURRENCY`, `FUSION_META_FAST_CONCURRENCY`,
`FUSION_META_MAX_CALLS` (the total external call cap), `FUSION_META_MAX_WALL_SECONDS`
and `FUSION_META_WORKSPACE_MODE`. The matching CLI flags are `--advisor`,
`--advisor-max`, `--max-repair-rounds`, `--max-attempts-per-task`,
`--expert-concurrency`, `--fast-concurrency`, `--workspace-mode` and
`--risk-threshold`. A flag overrides the environment variable
(`MetaLoopConfig.from_env`). `risk_threshold` has no environment variable.

Advisor policy meanings (`advisor_preflight_trigger`): `always` runs a CEO
preflight on every run, `off` never does, `on-demand` waits for the host to ask,
and `auto` triggers on risky host-owned tasks, plan risk at or above
`risk_threshold`, or plan confidence below 0.6.

`config/defaults.env` also defines `FUSION_META_OPERATOR_POLICY`,
`FUSION_META_MAX_OPERATOR_CALLS` and `FUSION_METALOOP_WORKER_PROVIDER/MODEL/EFFORT`.
The Python engine does not read them. Only the cockpit schema
(`scripts/dashboard_schema.py`) references the operator policy key.

## CLI

```bash
# scaffold: config + live roster (tiers) + correlation groups
bash scripts/swarm.sh metaloop "Refactor billing and add tests"

# with a task DAG: routing decisions + dependency waves + advisor preflight
bash scripts/swarm.sh metaloop "..." --plan-file plan.json --json \
  --advisor auto --advisor-max 2 --max-repair-rounds 2 \
  --expert-concurrency 2 --fast-concurrency 2 --workspace-mode proposal
```

The first form prints the config, the roster (`live` or `absent` per provider,
detected by `scripts/providers/detect.sh`) and the correlation groups. It does
not dispatch a worker. `plan.json` is a JSON array of `TaskSpec` objects. See
[`metaloop.schema.json`](metaloop.schema.json). A plan that fails validation
returns `plan_errors` instead of routes. `--run-id` sets the run id (the
default printed was `ml_run`).

The host-native control protocol, which is how the host actually drives a run,
lives in [`skills/fusion-metaloop/SKILL.md`](../skills/fusion-metaloop/SKILL.md).

## Run states

`metaloop.ALLOWED_TRANSITIONS` defines the legal path. A run starts at
`CREATED` and moves through `SNAPSHOTTED`, `PLANNED_V1`, optionally
`ADVISOR_REVIEWED`, `PLANNED_FINAL`, `DISPATCHING`, `COLLECTING`, optionally
`REPAIRING`, `INTEGRATING`, `FINAL_GATE` and optionally `FINAL_ADVISOR_REVIEW`.
The terminal states are `COMPLETED`, `PARTIAL`, `BLOCKED`, `FAILED` and
`CANCELLED`. An illegal transition raises `StateError`.

## Escalation ladder (bounded)

`metaloop.next_action` handles `approval_required` first, then checks budgets
and attempt caps, so the ladder cannot spin. Once a cap is hit the only moves are stop (synthesize partial
state) or pause for a person.

```text
malformed_output   -> one schema-repair retry, then escalate
timeout/error/absent -> alternate worker, same tier
verification/low-confidence -> fast: promote to expert; expert: host takeover
scope_violation    -> host review (reject + replan)
expert_conflict    -> host adjudicates
safeguard_block    -> host takeover in careful mode
plan_failure       -> Fable systemic consult (within advisor budget) else stop
budget/attempt caps -> stop and synthesize partial state honestly
approval_required  -> pause for a human
```

## Hive Board and nested workers

When `FUSION_BOARD` is set, workers register on that board and the preferred
labor path is a nested `andrewcode` process (`prefer_nested_andrewcode`,
`spawn_nested_andrewcode`). The worker prompt then carries the board how-to and
the rule that workers never talk to the human. Tests inject the board and the
spawn function, so none of this needs a live CLI. Board and spawn details are in
[HIVE.md](HIVE.md).

## What Phase 1 deliberately does NOT do

- No automatic git-worktree writes. `WorktreeWorkspace` refuses to construct
  without an explicit `repo_root` and `authorized=True`, and `proposal` is the
  default mode.
- No automatic selection of MetaLoop by `scripts/route.sh`. That script has no
  MetaLoop path.
- No treating Fable's safeguard fallback as premium advice. It is recorded as
  `fallback` (`contracts.WORKER_STATUSES`).

Tests: `PYTHONPATH=python python3 -m unittest tests.test_metaloop` (part of the
233-test Hive and MetaLoop run in [HIVE.md](HIVE.md)).
