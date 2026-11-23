# Fusion Hive

**Hive — nested multi-agent coding swarm on one board.** A top-level architect
talks to the human and may design a custom swarm. Captains are any live CLI.
Nested workers are real AndrewCode processes. Everyone except the architect
talks on one Hive Board (a SQLite WAL file plus a JSONL sidecar, no daemon).

Read this page if you want to run `/fusion:hive`, write a `SwarmSpec`, or
integrate with the board that nested workers use. It is the reference for
`identities.py`, `board.py`, `spawn.py`, `designer.py`, `hive.py` and
`hive_prompts.py` under [`python/fusion_swarm/`](../python/fusion_swarm/).
The repository is named `agent-swarm-fusion`. The plugin id, slash commands
(`/fusion:*`), Python package (`fusion_swarm`) and `FUSION_*` environment
variables keep the `fusion` name.

Hive is not a persona costume and not a simulated panel. Captains dispatch.
Children are AndrewCode subprocesses. Absent processes are reported absent.
The board is greppable JSONL next to the sqlite file, so independent
processes (and you, after the fact) can read what happened.

Related pages: [MODES.md](MODES.md) (mode catalog), [COORDINATION.md](COORDINATION.md)
(the mailbox, task and receipt features of the same board),
[PEERS.md](PEERS.md) (providers and transports), [METALOOP.md](METALOOP.md)
(the tiered org that can reuse nested workers), [../KIMI.md](../KIMI.md)
(installing on AndrewCode).

## Try it offline

A dry run builds the tree and the board and seeds three posts. It starts no
process and calls no model.

```bash
PYTHONPATH=python python3 -m fusion_swarm hive "demo task" --dry-run --json --state-dir /tmp/hive-demo
```

Without `--spec-file` and `--captains`, a dry run builds the default tree: an
architect (`fable` / `fable-5.1`), an operator (`codex` / `in-context`) and two
captains, `muse` and `glm`, each with four children. These are the agents the
command registered when it was run for this page (the `run_id` and board path
differ on every run):

```text
arch      architect  fable      fable-5.1
operator  operator   codex      in-context
muse      captain    andrewcode muse-spark-1.3
muse-1..4 child      andrewcode muse-spark-1.3
glm       captain    opencode   glm-5.3
glm-1..4  child      andrewcode meta/muse-spark-1.3-contributor
```

That is 12 agents and 3 seeded posts (`posts_seeded`). The same command through
the bridge is `bash scripts/swarm.sh hive "demo task" --dry-run --json`. The
JSON result goes to stdout. Unless `--json` is given, a short summary also goes
to stderr (`hive.py:main`).

Without `--dry-run` and without a spec, `run()` does not call any model. It
returns a design packet instead (see [Hive runner](#hive-runner)).

## Architecture

```text
 human
   │
   ▼
 architect     Fable 5.1  and/or  Astra (Codex gpt-6-astra)
   │           talks to the human; may DESIGN a custom SwarmSpec
   │
   ├── operator    (optional host seat — Chief Operator)
   │
   ├── captain A   any live CLI (codex · opencode/glm · grok · copilot · kimi · andrewcode/muse)
   │     └── child A1..An     REAL AndrewCode Node processes  (-m <alias> --thinking-effort <exact-effort>)
   ├── captain B
   │     └── child B1..Bn
   └── …
```

| Role | Who | Talks to the human? | How they run |
|------|-----|---------------------|--------------|
| **architect** | Fable 5.1 and/or Astra via Codex `gpt-6-astra` | **yes**, the only hive seat that does | designs and steers the swarm, posts on every channel |
| **operator** | host CLI (default identity `codex` / `in-context`) | only if it is the architect/host | owns checkout writes, never impersonates a missing captain |
| **captain** | any live CLI from the roster | **no** | adapter dispatch, or a nested AndrewCode process when the captain's provider is `andrewcode` |
| **worker / child** | AndrewCode process | **no** | `node C:/dev/andrew-code/apps/kimi-code/dist/main.mjs -m <model> --thinking-effort <exact-effort> --prompt-file <UTF8-file> --output-format stream-json` |

Communication is one Hive Board, not a chain of pipes:

- Captains talk to their children on lineage channels.
- Children of different captains can talk when `cross_talk` is on (the default
  for a run without a spec) or through DMs and `@mentions`.
- The architect can talk to everyone (`hive`, DMs, `@mentions`).
- The board is not human-facing except through the architect or host.

The architect and operator are seats the runner registers on the board. It does
not start or call them. `run()` starts only the captains and children.

Identities are `AgentIdentity` (`python/fusion_swarm/identities.py`):

```text
id, display_name, role, provider, model,
parent_id, lineage, depth, spawn_budget, max_depth, status
```

`role` is one of `architect | operator | captain | worker | child`
(`identities.ROLES`). Construction fails closed: an unknown role, an empty id,
a `/` in an id or `depth > max_depth` raises `IdentityError`. `lineage` is a
slash path of ids (`arch/muse/muse-2`). Helpers are `new_id(prefix)`,
`child(parent, **kwargs)` (depth + 1, lineage appended, parent `spawn_budget`
decremented) and `lineage_of(parent, child_id)`.

`SpawnLimits(max_depth=2, max_children_per_agent=4, max_agents=24)` has
`can_spawn(parent, current_total, current_children) -> (ok, reason)`. It refuses
when the parent depth has reached `min(limits.max_depth, parent.max_depth)`,
when the parent already has `max_children_per_agent` children, or when
`current_total` has reached `max_agents`.

The architect is the parent of every captain, so `max_children_per_agent` also
caps the number of captains. With the default of 4, asking for six captains
fails: `python -m fusion_swarm hive "t" --dry-run --captains muse,glm,grok,copilot,kimi,codex`
returned `cannot spawn captain kimi: children 4 >= max_children_per_agent 4`.

### Architect latitude

The architect is not a router table. Given the task, the live roster and the
hard constraints, it emits a `SwarmSpec`. It can pick a named catalog form or
invent a custom topology when the catalog does not fit.

**Hard constraints** (the architect may not override them):

- User-specified models and providers. `designer.design_prompt` states them as
  hard constraints, but nothing in `validate_spec` compares a spec against
  them, so this one is enforced by the prompt and by review, not by code.
- `SpawnLimits` and budget caps in the spec (`max_depth`,
  `max_children_per_agent`, `max_agents`). Code enforces these twice:
  `validate_spec` rejects an over-budget spec and `SpawnLimits.can_spawn` runs
  before each captain and child is created.
- Honesty rules (real dispatch, absent ≠ agreement).

**Everything else is the architect's call**: who captains, who spawns, how
many children, whether cross-talk is open, lineage-only or need-to-know, which
named form (or `custom`) to use, and the notes that captains inherit.

## Example — 4 captains, 8 children

A hive the architect might design for a repo-scale build: four captains, two
of which each spawn four AndrewCode children.

```text
architect (Fable 5.1 / Astra gpt-6-astra)
│
├── captain muse-spark-1.3     (andrewcode / muse)
│     ├── muse-1
│     ├── muse-2
│     ├── muse-3
│     └── muse-4               ← 4 nested AndrewCode processes
│
├── captain glm-5.3            (opencode / glm)
│     ├── glm-1
│     ├── glm-2
│     ├── glm-3
│     └── glm-4                ← 4 nested AndrewCode processes
│                                 (children are andrewcode even when
│                                  the captain itself is opencode)
│
├── captain grok               (grok-4.5, no children)
└── captain copilot            (gemini-3.5-flash, no children)
```

That is 4 captains + 8 children. Captains talk to their children. `muse-2` can
`@mention` `glm-3` on the board, and the architect can talk to everyone.

This exact spec ships as `designer.NESTED_HIVE_EXAMPLE`. It is the example
embedded in the architect's design prompt. To run it as a dry run:

```bash
PYTHONPATH=python python3 -c "import json; from fusion_swarm import designer; print(json.dumps({**designer.NESTED_HIVE_EXAMPLE, 'task': 'demo'}))" > spec.json
PYTHONPATH=python python3 -m fusion_swarm hive "demo" --dry-run --json --spec-file spec.json --state-dir /tmp/hive-demo
```

Result when this was run: 14 registered agents (1 architect, 1 operator,
4 captains, 8 children) and 3 seeded posts. The spec names an operator
(`codex` / `gpt-6-astra`), which the tree above leaves out. `hive.run` registers
an operator for every run: a spec without one gets the default `codex` /
`in-context` seat. The default limits (`max_agents=24`) leave room. The `muse` children ran model
`muse-spark-1.3` and the `glm` children ran `meta/muse-spark-1.3-contributor`.

Which model a child gets (`hive.run`): a child of an `andrewcode` captain uses
that captain's model, and a child of any other captain uses
`DEFAULT_CHILD_MODEL`, which is the `FUSION_ANDREWCODE_MODEL` environment
variable or `meta/muse-spark-1.3-contributor`. Children are always provider
`andrewcode`, even when the captain is `opencode`, `grok`, `copilot` or `kimi`.

Two traps when sizing a hive from the command line:

- `--captains muse,glm,grok,copilot` gives every captain 4 children, not 8
  children in total. A dry run of that command registered 22 agents (16
  children). Use a spec file to give some captains `spawn: 0`.
- `--children-per-captain` (or `--children`) overrides the `spawn` value of
  every captain, including those in a spec file, whenever it is not the
  default 4 or when `--captains` is given (`hive.run`).

## Hive Board

`HiveBoard` (`python/fusion_swarm/board.py`) is the process-shared bus. It is
file plus SQLite, WAL, no daemon. Independent AndrewCode children open the
same db path and talk.

- SQLite at the given path, journal mode **WAL**, `synchronous=NORMAL`.
- Each posted message is also appended as JSONL to `<db>.jsonl` next to the
  sqlite file (greppable, no sqlite needed to audit). A `<db>.wakeup` file
  backs the `wait` command.
- The same board also holds the mailbox, task, reservation, shared-context,
  delivery-receipt, candidate and knowledge tables. They are documented in
  [COORDINATION.md](COORDINATION.md). A fresh board has 34 tables including
  the full-text-search shadow tables (counted with `sqlite_master`).

### Schema overview

The six base tables the Hive was built on (`board._BASE_TABLES`):

| Table | Purpose |
|-------|---------|
| `agents` | registered identities (`AgentIdentity` fields plus `registered_at`, `last_seen`) |
| `channels` | named rooms. `kind` is `room \| dm \| thread \| lineage \| task` (`CHANNEL_KINDS`) |
| `channel_members` | which agents joined which channel |
| `messages` | posts, DMs, replies and system notes |
| `acks` | per-agent acknowledgements of a message id |
| `presence` | last-seen time and status (`online` and the other `AGENT_STATUSES`) |

Default channels created by `init_schema()`:

| Channel | Who is auto-joined on `register` |
|---------|----------------------------------|
| `hive` | everyone |
| `architect` | architect and operator |
| `captains` | captains and the operator |
| `workers` | workers and children |
| `system` | architect and operator (runtime, spawn and heartbeat notes) |

The membership comes from `board._ROLE_ROOMS`. The hive runner also creates:

- `lineage-<captain>`, one lineage channel per captain (architect, operator,
  the captain and its children join it)
- `task-main`, the seeded task (`kind=task`), joined by every agent

A DM uses the canonical channel name `dm:<sorted_a>:<sorted_b>` so A to B and
B to A land in the same room. A third agent cannot join a DM.

### Method surface

```text
HiveBoard(path) / context manager
init_schema()
register(agent: dict | AgentIdentity, *, replace=False) -> dict
heartbeat(agent_id, status="online")
ensure_channel(name, kind="room", created_by="system") -> str
join(channel, agent_id)
post(*, from_agent, body, channel="hive", thread_id=None,
     mentions=None, to_agent=None, kind="message", task_id="",
     priority="normal", payload=None, idempotency_key=None) -> dict
dm(from_agent, to_agent, body, *, kind=..., task_id=..., priority=..., ...) -> dict
reply(from_agent, message_id, body, *, kind=..., priority=..., idempotency_key=None) -> dict
poll(agent_id, since_id=None, limit=100) -> list[dict]
mentions(agent_id, since_id=None) -> list[dict]
agents() -> list[dict]
tree() -> dict          # {"roots", "children", "agents"}, grouped by parent_id
ack(agent_id, message_id, lease_token=None)
format_feed(messages) -> str
close()
```

Rules enforced by the code:

- `register` on an existing id with a different role, provider, model or parent
  raises unless `replace=True`.
- `post` accepts only the kinds in `MESSAGE_KINDS` (`message`, `system`, `ack`,
  `presence`, `task`, `handoff`, `finding`, `blocker`, `decision`, `review`).
  `reply` is threaded to the root message and cannot target a message outside
  the replier's channels.
- `poll` returns messages in the channels the agent joined, plus DMs it is part
  of and posts that `@mention` it or address it with `to_agent`. An agent does
  not see rooms it never joined. `since_id` and `limit` page the result. Reading
  is not acking.
- `ack` marks a message acknowledged. The same call takes a lease token for
  competing consumers (see COORDINATION.md).

Workers should poll, post progress, `@mention` when they need someone, `ack`
what they took, and never talk to the human.

## Spawn tree

`python/fusion_swarm/spawn.py` is the only way nested workers come into
existence.

```text
SpawnHandle: agent_id, provider, model, pid, out_path, log_path, status, returncode

spawn_andrewcode(identity, prompt, board_path, workdir, extra_env=None,
                 timeout=None, fusion_pythonpath=None, runner=None,
                 limits=None, current_total=0, current_children=0,
                 parent=None, swarm_id=None, out_path=None, log_path=None) -> SpawnHandle

spawn_via_adapter(provider, identity, prompt, board_path, workdir=None, ...,
                  dispatch=None, model="", effort="") -> SpawnHandle
    # wraps fusion_swarm.adapter.dispatch for captains that are not nested processes
```

`SpawnHandle.status` mirrors the adapter taxonomy: `returned`, `absent`,
`timeout` or `error`. Return codes map as 0 to `returned`, 124 to `timeout`,
127 to `absent`, anything else to `error`.

Depth and budget are enforced through `SpawnLimits` before spawn
(`can_spawn`, fail closed with `SpawnError`). Tests inject a `runner` callable.
They do not need AndrewCode installed and they make no live paid CLI call.

### Binary and flags

Nested workers are spawned as real AndrewCode processes:

```text
node C:/dev/andrew-code/apps/kimi-code/dist/main.mjs -m <model> --thinking-effort <exact-effort> --prompt-file <UTF8-file> --output-format stream-json
```

`C:/dev/andrew-code/apps/kimi-code/dist/main.mjs` is the documented default
entry on Windows. On macOS and Linux `andrewcode_cli._default_entry` checks
`~/dev/andrew-code/apps/kimi-code/dist/main.mjs`, then
`andrew-code/apps/kimi-code/dist/main.mjs` under the directory four levels above
`andrewcode_cli.py` (`parents[4]`), then the Windows path, and falls back to the
first path when none of them is a file. Set
`FUSION_ANDREWCODE_ENTRY` to point at any other `main.mjs`.

`spawn_andrewcode` launches Node directly with a UTF-8 prompt file for every
prompt size, an exact model, an exact effort and stream-json output. The effort
is the identity's `effort`, else `FUSION_ANDREWCODE_EFFORT`, else `xhigh`. The
model alias is passed unchanged, and the effort must be one of `off`, `on`,
`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`
(`andrewcode_cli.EFFORTS`). The runner reads the stream and keeps only the
final assistant text in `<agent>.out`, with the raw stream in `<agent>.out.raw`
and stderr in `<agent>.log`. A stream with an error event or no final text
counts as a failed run. Python launch sites hide Windows child consoles.

These are authorized, tool-enabled implementation workers. Prompt mode enables
auto permissions and persists sessions. They are **not** sandboxed,
tools-disabled or ephemeral. AndrewCode has no provider sandbox, so a child can
write wherever its user can. Restricted legacy adapters fail closed until
equivalent runtime controls are verified (`andrewcode_cli.CONTROL_GAP`).

### Environment injected into every child

| Variable | Meaning |
|----------|---------|
| `FUSION_BOARD` | absolute path to the hive sqlite file |
| `FUSION_AGENT_ID` | this process's `AgentIdentity.id` |
| `FUSION_PARENT_ID` | captain / parent id (empty for the architect) |
| `FUSION_SWARM_ID` | run id shared by the whole hive |
| `PYTHONPATH` | the plugin's `python/` directory first, so `python -m fusion_swarm.board` works |

The five required keys always win over `extra_env`, so a caller cannot drop the
identity of the process it just spawned. `extra_env` may add other variables.
Children must not inherit a prompt that tells them to speak to the human.

## Designer

`python/fusion_swarm/designer.py` holds the topology catalog and the fail-closed
spec.

Named forms (`designer.catalog()` returns them in this order): `solo`, `panel`,
`council`, `debate`, `vote`, `swarm`, `hierarchy`, `metaloop`, `moa`, `heavy`,
`discuss`, `graph`, `ladder`, `speclock`, `breaker`, `ballot`, `factory`,
`diamond`, `nested-hive`, `ultraswarm`, `ureview`, `custom`. That is 22
hand-written names. `designer.py` then appends every native team form from
`team.FORM_DETAILS` that is not already listed (`sidekick`, `genius-boss`,
`idiot-boss`, `dag`, `pipeline`, `relay`, `fan-out`, `hub-spoke`, `map-reduce`,
`ring`, `mesh`, `blackboard`, `review-pair`, `builder-breaker`), so
`len(designer.catalog())` is 36. `hive_prompts.CATALOG` is a dict form of the
same list: its own legacy entries plus any designer form it lacks.

Each `Topology` has `name`, `when`, `mechanic`, `communication`
(`blind | board | lineage | open`), `nesting` (bool), `default_captains`,
`default_children` and `cost_shape`.

`SwarmSpec` (a dataclass that rejects unknown fields):

```text
name
architect          {provider, model}
operator           {provider, model} or null
captains           list[{id, provider, model, spawn, spawn_via}]
cross_talk         bool           (default false)
board              bool           (default false)
max_depth          (default 2)
max_children_per_agent   (default 4)
max_agents         (default 24)
budget             object
communication      open | lineage | need-to-know   (default open)
task
notes
```

```text
design_prompt(task, live_roster, constraints) -> str
    # prompt for Fable/Astra to emit ONLY SwarmSpec JSON
parse_swarm_spec(text) -> SwarmSpec
    # extracts JSON; tolerates markdown fences
validate_spec(spec, live_roster) -> list[str]
    # fail-closed; empty list means ok
```

`validate_spec` returns every problem it finds. It rejects: a missing
architect, `cross_talk` without `board`, a `communication` outside the allowed
set, a captain `spawn` above `max_children_per_agent`, duplicate captain ids,
`max_depth` too small for the captains or children, an agent count (architect,
operator if set, captains, children) above `max_agents`, and any provider that
is not in the live roster.

`hive.run` loosens the last rule. Before validating it adds every provider the
spec names to the roster, so a dry run is not rejected just because a CLI was
never probed. In a hive run an unknown provider is therefore not a validation
error. A provider that is really absent surfaces as an `absent` spawn status.

The `spawn_via` field is required to be a non-empty string, but `hive.run` does
not read it. A captain whose provider is `andrewcode` becomes a nested process.
Any other captain goes through the adapter named by its provider. Children are
always nested AndrewCode processes.

The design prompt gives the architect latitude to invent a custom topology
(`name` plus `notes`, catalog form `custom`) when no named form fits.

From the CLI, `python -m fusion_swarm designer` prints the catalog, and
`python -m fusion_swarm designer "<task>"` prints the design prompt. Neither
dispatches a model (`__main__.py`).

## Hive runner

`python/fusion_swarm/hive.py`:

```text
run(task, spec=None, spec_file=None, architect="fable",
    children_per_captain=4, captains=None, dry_run=False,
    timeout=None, state_dir=None) -> dict
```

Execution path:

1. If there is no spec and `dry_run` is false, return a **design packet**
   (`design_prompt` plus `roster` and the constraints) instead of calling live
   models. Tests stay offline. A dry run with no spec builds the default
   `muse` + `glm` tree instead.
2. Validate the spec (`validate_spec`) and raise `HiveError` on any problem.
3. Create the board under `<state>/hive/<run_id>/board.sqlite`, where `<state>`
   is `--state-dir`, else `FUSION_STATE_DIR`, else `~/.fusion`, and `run_id` is
   `hive-<10 hex>`. `init_schema()` runs on open.
4. Register the architect (`arch`), the operator (`operator`) and the captains.
   Each captain passes `SpawnLimits.can_spawn` first.
5. Ensure `task-main` and one `lineage-<captain>` channel per captain.
6. Register the children as AndrewCode identities, even if the captain is
   opencode/glm. Each child passes `can_spawn` against its captain.
7. Post the task to `hive` and `task-main` from the architect, and the board
   how-to to `system` from the operator (3 posts).
8. If `dry_run=True`, stop here. Nothing is spawned.
9. Otherwise spawn. Captains and children use separate thread pools of at most
   8 workers each so waiting captains cannot starve the children. Children go
   through `spawn_andrewcode`. A captain whose provider is `andrewcode` does
   too. Every other captain goes through `spawn_via_adapter`.

The built-in captain catalog is `muse` (andrewcode, `muse-spark-1.3`), `glm`
(opencode, `glm-5.3`), `grok` (`grok-4.5`), `copilot` (`gemini-3.5-flash`),
`kimi` (`azure/kimi-k3`) and `codex` (`gpt-6-astra`). Architect tokens are
`fable` (`fable-5.1`), `astra` and `codex` (both Codex `gpt-6-astra`).

Result dict:

```text
pattern=hive, run_id, board_path, spec, agents, tree, posts_seeded, dry_run, task
(+ spawned, the list of spawn handles, on a live run)
```

The CLI flags are `--task-file`, `--spec-file` (alias `--spec`), `--architect`,
`--children` / `--children-per-captain`, `--captains` (comma list), `--dry-run`,
`--timeout`, `--state-dir` and `--json`. The same flags are available through
`python -m fusion_swarm hive`. A rejected run prints `{"ok": false, "error": ...}`
and exits 1 from `python -m fusion_swarm.hive`.

## Prompts

`python/fusion_swarm/hive_prompts.py` returns the strings captains and
children actually see:

| Function | Audience |
|----------|----------|
| `architect_prompt(task, roster, constraints)` | Fable / Astra: emit SwarmSpec JSON |
| `captain_prompt(identity, task, board_howto, children_ids, *, cross_talk=True, spawn_budget=None)` | a captain |
| `worker_prompt(identity, task, board_howto, siblings, cross_talk)` | a nested AndrewCode child |

`board_howto` spells out the exact board CLI, including:

```bash
python -m fusion_swarm.board --db "$FUSION_BOARD" poll --agent "$FUSION_AGENT_ID"
```

The how-to also lists `inbox`, `mentions`, `post`, `dm`, `reply`, `ack`,
`claim`, `heartbeat`, `join`, `agents`, `tree` and `channels`. Workers are
instructed to post progress, `@mention` other agents when they need them, and
**NEVER** talk to the human. With `cross_talk` on, a worker prompt carries an
explicit cross-lineage invitation. With it off, workers are told to stay in
their own lineage.

## CLI

Always print JSON. The board CLI lives in `board.py:main`. Invalid
arguments and errors also print a JSON object (`{"ok": false, "error": ...}`).

```text
python -m fusion_swarm.board --db PATH <cmd>
```

`--db` defaults to `FUSION_BOARD`, else `<FUSION_STATE_DIR or ~/.fusion>/hive/board.sqlite`
(`board.default_board_db`). The Hive commands:

| Command | What |
|---------|------|
| `init` | create/open db, WAL, default channels |
| `register` | upsert an agent (`--id`, `--provider`, `--model` required; `--role`, `--parent-id`, `--lineage`, `--depth`, `--spawn-budget`, `--max-depth`, `--status`, `--replace`) |
| `post` | post a message (`--from`, `--body` or `--body-file`, `--channel`, `--mentions`, `--to`, `--thread-id`, `--kind`, ...) |
| `dm` | canonical DM (`--from`, `--to`, `--body`) |
| `reply` | reply in-thread (`--from`, `--message-id`, `--body`) |
| `poll` | `--agent` `[--since-id]` `[--limit]` |
| `mentions` | `--agent` `[--since-id]` |
| `agents` | roster |
| `tree` | parent_id grouping |
| `heartbeat` | `--agent` `[--status]` |
| `join` | `--channel` `--agent` |
| `channels` | list channels |
| `ack` | `--agent` `--message-id` `[--lease-token]` |

The parser has 39 subcommands in total (`python -m fusion_swarm.board --help`).
The rest (`inbox`, `claim`, `nack`, `receipts`, `reserve`, `release`, tasks,
deliveries, `wait`, `trace-deposit`, `trace-sense` and others) belong to the
mailbox, task and knowledge layers. See [COORDINATION.md](COORDINATION.md).

A short session against a scratch board, with the output that was captured
(timestamps and the db path trimmed):

```bash
python -m fusion_swarm.board --db board.sqlite init
python -m fusion_swarm.board --db board.sqlite register --id arch --role architect --provider fable --model fable-5.1
python -m fusion_swarm.board --db board.sqlite register --id cap --role captain --provider opencode --model glm-5.3 --parent-id arch --lineage arch/cap --depth 1
python -m fusion_swarm.board --db board.sqlite post --from arch --body "hello @cap" --channel hive --mentions cap
python -m fusion_swarm.board --db board.sqlite poll --agent cap
```

```json
{"ok": true, "db": ".../board.sqlite", "channels": ["hive", "architect", "captains", "workers", "system"]}
[{"id": 1, "channel": "hive", "from_agent": "arch", "to_agent": null, "body": "hello @cap", "kind": "message", "thread_id": null, "mentions": ["cap"], "task_id": "", "priority": "normal", "payload": {}, "idempotency_key": null}]
```

Posting as an unregistered agent fails closed: `{"ok": false, "error": "unknown agent: ghost"}`.
On a Python where `fusion_swarm.board` is already imported by the package,
`python -m` prints a harmless `RuntimeWarning` to stderr.

Hive entrypoints (from the plugin root):

```bash
# no spec and no --dry-run: returns a design packet, calls no model
python -m fusion_swarm hive "<task>"

# build the tree and board without spawning anything
python -m fusion_swarm hive "<task>" --dry-run --json

# with a locked spec
python -m fusion_swarm hive "<task>" --spec-file swarm.json --json

# 4 captains, 4 children each (that is 16 children)
python -m fusion_swarm hive "<task>" --captains muse,glm,grok,copilot \
    --children-per-captain 4 --architect fable --json

bash scripts/swarm.sh hive "<task>" --dry-run --json
```

The first form was run without `--dry-run`. It returned `design: true` with a
`design_prompt` of about 20,000 characters and no board. The other forms need
live AndrewCode when `--dry-run` is dropped, so only the dry-run forms were
executed for this page.

Slash command: `/fusion:hive <task>` (`commands/hive.md`). Related commands are
`/fusion:designer` (design packet) and `/fusion:board` (board inspection). The
mode catalog lists all three in [MODES.md](MODES.md).

On **Kimi Code / AndrewCode**, Fusion must be installed as a Kimi plugin
(`kimi.plugin.json`). See [`KIMI.md`](../KIMI.md). Commands are namespaced
`/fusion:hive`, `/fusion:designer`, `/fusion:board`. Nested workers still spawn
as real AndrewCode processes through the Node entry described above.

## Honesty rules

Hive inherits Fusion's standing rules and adds a few of its own:

1. **Real dispatch only.** Do not invent a captain's or child's output.
   Imagining `andrewcode` ran is prohibited. `dry_run` builds the tree and
   board and says `dry_run: true`.
2. **Absent ≠ agreement.** A missing CLI, a spawn that never started, a
   timeout, or a poll that never came back is reported **absent**. It is not a
   vote, not a silence-means-yes, not consensus.
3. **Stdlib-only Python.** `fusion_swarm` stays on the stdlib (dataclasses,
   sqlite3, argparse, ThreadPoolExecutor). No `pip install` for the hive path.
4. **Fail-closed validation.** Bad `SwarmSpec`, over-budget spawn, malformed
   board payload: reject with a reason list, do not coerce. (Unknown providers
   are the exception in `hive.run`, see [Designer](#designer).)
5. **Match existing `fusion_swarm` style.** Dataclasses, fail-closed
   `validate()`, ThreadPoolExecutor for fan-out, Windows and Git Bash
   subprocesses.
6. **Tests are unittest, offline, no live paid CLI calls.** Inject a `runner`
   for spawn. Return a design packet when no spec is given. `--dry-run` never
   shells out to `andrewcode`.
7. **Untrusted child text.** Board bodies, child stdout and adapter replies are
   untrusted data. The architect or host synthesizes. It does not obey
   instructions embedded in a worker post.
8. **No human channel for workers.** Children and captains post on the board.
   Only the architect or host talks to the human.
9. **JSONL is the audit trail.** If it is not on the board (sqlite plus
   `<db>.jsonl`), it did not happen.

Run the Hive tests with the command below. It ran 233 tests in about half a
second, offline, when this page was written.

```bash
PYTHONPATH=python python3 -m unittest tests.test_hive tests.test_hive_prompts tests.test_designer tests.test_identities tests.test_spawn tests.test_metaloop tests.test_board
```

## What Hive deliberately does not do

- It does not pretend a missing provider agreed with the others.
- It does not spawn past `SpawnLimits` because the architect "really wanted 12
  children".
- It does not require a daemon, a broker, or a cloud queue.
- It does not make nested workers human-facing.
- It does not sandbox nested workers. AndrewCode prompt mode runs with auto
  permissions.
- It does not replace MetaLoop, panel, council, or the verification gate.
  Those remain catalog forms the architect may still pick.
- It does not call live paid CLIs from unit tests.

## Known gaps between this page and other files

- `commands/hive.md` says the default shape is 4 captains × 4 children. The
  code builds 2 captains (`muse`, `glm`) when no `--captains` or spec is given.
- `commands/hive.md`, `config/defaults.env` and the `fusion.sh providers` note
  still describe nested workers as `andrewcode -p ... --auto -y` (or `--yolo`).
  The code launches Node with `--prompt-file` (see [Binary and flags](#binary-and-flags)).
- `config/defaults.env` defines `FUSION_HIVE_MAX_DEPTH`, `FUSION_HIVE_MAX_CHILDREN`,
  `FUSION_HIVE_MAX_AGENTS`, `FUSION_ANDREWCODE_AUTO`, `FUSION_ARCHITECT_PROVIDER`,
  `FUSION_ARCHITECT_MODEL` and `FUSION_ASTRA_MODEL`. `tests/test_hive_wiring.py`
  checks that the file mentions them. `hive.py` reads none of them: architect
  seats and limits come from the spec, the `--architect` token and the
  `SpawnLimits` defaults. Only the cockpit schema (`scripts/dashboard_schema.py`)
  reads the architect and Astra keys.
