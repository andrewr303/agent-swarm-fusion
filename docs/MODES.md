# Modes and slash commands

This page catalogs every `/fusion:<mode>` slash command: what it does, how the catalog classifies it, when to pick it, and the CLI entry point behind it. Read it to choose a mode, or to find the command-line equivalent when you call Fusion from scripts.

The repository is named `agent-mesh`. The plugin id, the slash commands (`/fusion:*`), the Python package (`fusion_swarm`) and the environment variables (`FUSION_*`) keep the `fusion` name.

## Contents

- [Overview](#overview)
- [Collaboration modes](#collaboration-modes)
- [Hive, designer and board modes](#hive-designer-and-board-modes)
- [Hive — nested AndrewCode swarm on one board](#hive--nested-andrewcode-swarm-on-one-board)
- [Advanced swarm topologies](#advanced-swarm-topologies)
- [Runtime forms](#runtime-forms)
- [Coding teams and leadership](#coding-teams-and-leadership)
- [Control, status and tools](#control-status-and-tools)
- [Engine-backed modes and their rosters](#engine-backed-modes-and-their-rosters)
- [CLI patterns without a slash command](#cli-patterns-without-a-slash-command)
- [Where command text and CLI behavior differ](#where-command-text-and-cli-behavior-differ)

## Overview

The plugin ships 54 slash commands. Each one is a file in [`commands/`](../commands/) with a short frontmatter block (`description`, `argument-hint`) and a body that names the skill or script to run. [`modes_catalog.py`](../python/fusion_swarm/modes_catalog.py) parses those files at runtime, so the catalog cannot drift from the commands. The dashboard and `swarm.sh roster` read the same catalog.

Re-check the count:

```bash
ls commands/*.md | wc -l
PYTHONPATH=python python3 -c "from fusion_swarm import modes_catalog as m; print(len(m.all_modes()))"
```

Both print `54` (run on 2026-11-22).

The groups below are this page's own grouping. The catalog has no groups.

| Group | Commands | Count |
| --- | --- | --- |
| [Collaboration modes](#collaboration-modes) | `fusion`, `panel`, `council`, `debate`, `vote`, `huddle`, `swarm` | 7 |
| [Hive, designer and board](#hive-designer-and-board-modes) | `hive`, `designer`, `board` | 3 |
| [Advanced swarm topologies](#advanced-swarm-topologies) | `moa`, `heavy`, `discuss`, `flow`, `refine`, `bestof`, `reason`, `ladder`, `speclock`, `breaker`, `ballot`, `gate`, `metaloop`, `graph`, `ultraswarm`, `ultimate-review` | 16 |
| [Runtime forms](#runtime-forms) | `refute`, `tournament`, `hedge`, `mapreduce`, `critic-repair`, `pair`, `delphi`, `auction`, `dry`, `blackboard` | 10 |
| [Coding teams and leadership](#coding-teams-and-leadership) | `team`, `sidekick`, `genius-boss`, `idiot-boss`, `forms`, `swarm-compose`, `swarm-control`, `swarm-status` | 8 |
| [Control, status and tools](#control-status-and-tools) | `setup`, `costs`, `learn`, `memory`, `context`, `steer`, `dashboard`, `peers`, `vboard`, `ultracode` | 10 |

### Reading the "Kind" column

The catalog sets an `execution_kind` for only 17 of the 54 commands. The other 37 show `—` in the tables: the catalog leaves them unclassified, and their live roster is decided by the skill the command invokes.

| `execution_kind` | Commands | Meaning in the catalog |
| --- | --- | --- |
| `runtime-form` | 10 runtime-form commands | Backed by an executable form in `python/fusion_swarm/forms/`, with explicit inputs and no adapter defaults. |
| `host-native` | `team`, `sidekick`, `genius-boss`, `idiot-boss` | Roster comes from an explicit TeamSpec. The selected host runs the workers natively. |
| `offline-catalog` | `forms` | Lists the native forms. Makes no model calls. |
| `tool` | `peers`, `vboard` | Inspection and plumbing commands, not deliberation modes. |

Source: `parse_command()` in [`modes_catalog.py`](../python/fusion_swarm/modes_catalog.py). Re-derive the table:

```bash
PYTHONPATH=python python3 -c "
from fusion_swarm import modes_catalog as m
for e in m.all_modes():
    print(e['mode'], e.get('execution_kind', '-'), e['invokes_skill'] or '-', e['engine_pattern'] or '-')"
```

### Shared rules

Every mode follows the honesty rules in [CONCEPTS.md](CONCEPTS.md). Real dispatch only: a mode never invents panelist output. Absent is not agreement: a provider that did not return is reported absent and never counted as a vote. A single-model answer is labeled `solo`, not presented as a panel.

## Collaboration modes

`/fusion:fusion` picks a mode from the task shape and risk. You can force one with `/fusion:<mode>` or with `/fusion:fusion --mode <name>`. The advisor `scripts/route.sh` suggests a default, and the host makes the final call (see [CONCEPTS.md](CONCEPTS.md)). `solo` is not a slash command. It is the label for answering without a panel.

| Mode | Command | Topology | What happens | Use when |
| :--- | :--- | :--- | :--- | :--- |
| **solo** | none (`/fusion:fusion --mode solo`) | `1` | The host answers directly and labels the run `solo`. Costs 1x. | Trivial, saturated or already-certain tasks. |
| **panel** | `/fusion:panel` | `N → 1` | Blind, parallel dispatch of the same neutral prompt to all live CLIs. The host judges: run-the-code for code, a five-section synthesis for research. `--vote` and `--ranked` switch the aggregation. | Research, second opinions, code with one clear deliverable. |
| **council** | `/fusion:council` | `N → N → 1` | Blind round 1, anonymized round 2 with a name-the-flaw directive, a crystallizing round 3, then a chaired synthesis. A counterfactual is forced when agreement exceeds about 70%. Output carries kill criteria, a next step and a minority report when dissent survives. | High-stakes open decisions. |
| **debate** | `/fusion:debate` | `A ⇄ B → judge` | Two panelists from different families argue opposing sides for K rounds, revising after each rebuttal. The host names each side's strongest point and the cruxes, then decides without splitting the difference. | Contested either/or choices. |
| **vote** | `/fusion:vote` | `N → tally` | Dispatches the panel, extracts each answer key (last number, else the normalized last line), takes the majority and reports the agreement ratio. No judge call. A split escalates to a synthesis. | A question with one checkable answer. |
| **swarm** | `/fusion:swarm` | `plan → [W1…Wk] → verify` | Decomposes a large task into independent subtasks, routes each to a suited provider, runs them in dependency waves, then verifies the combined result. The command recommends `hive` for large multi-file coding work. | Large parallelizable work. |

Two more commands belong with these.

| Command | Kind | What happens | CLI / entry point |
| --- | --- | --- | --- |
| `/fusion:fusion` | — | The dynamic entry. Reads the task, runs `fusion.sh route`, reads `lessons.md`, chooses a mode, shows a cost banner, dispatches, judges and records the run. Accepts `--mode sidekick\|genius-boss\|idiot-boss\|team\|panel\|council\|debate\|vote\|swarm\|solo` and `--huddle`. | `bash scripts/fusion.sh route "<task>"` (advisory only) |
| `/fusion:huddle` | — | A scoping step. The panel advises on approach only (framing, decomposition, which mode fits, biggest risk). The host then picks a mode. | `bash scripts/fusion.sh huddle <task_file> <out_dir> [csv]` |

For `/fusion:fusion`, the command text sends Sidekick and the boss modes to the `fusion-sidekick` skill, native coding teams to `fusion-team`, and everything else to `fusion-orchestrate`. The catalog records only the first skill match, `fusion-sidekick`.

Per-mode details:

| Command | Skill | CLI / entry point |
| --- | --- | --- |
| `/fusion:panel` | `fusion-panel` | `bash scripts/fusion.sh panel <prompt_file> <out_dir> [csv]` fans out blind and parallel. |
| `/fusion:council` | `fusion-council` | None. The skill procedure drives the rounds. |
| `/fusion:debate` | `fusion-debate` | None. The skill procedure drives the rounds. |
| `/fusion:vote` | `fusion-panel` (`mode = vote`) | `fusion.sh panel` plus a model-free tally by the host. |
| `/fusion:swarm` | `fusion-swarm` | None for the flat swarm. Nested form: `bash scripts/swarm.sh hive "<goal>" --dry-run`. |

With no provider list, `fusion.sh panel` fans out to every provider that `detect` reports available, except `claude` and the host. A host entry in an explicit list is recorded as `host-native` and not dispatched. The catalog's roster contract for engine patterns uses a base panel of `codex`, `copilot`, `opencode` and `grok` (`BASE_PANEL` in `modes_catalog.py`). Provider details live in [PEERS.md](PEERS.md).

## Hive, designer and board modes

These three commands drive the nested swarm. Use `designer` to produce a spec, `hive` to run it, and `board` to inspect the shared board.

| Mode | Slash Command | Topology | What happens | Use when |
| :--- | :--- | :--- | :--- | :--- |
| **hive** | `/fusion:hive` | `Architect → Captains → Children ⇄ Board` | Registers an architect, an operator, captains and nested AndrewCode children on one Hive Board, then seeds the task. With `--dry-run` it builds the tree and board without spawning anything. | Large coding missions that need delegation across subsystems. |
| **designer** | `/fusion:designer` | `Task → Architect → SwarmSpec` | Builds the design prompt (catalog, live roster, constraints) that the architect answers with a fail-closed `SwarmSpec` JSON. The Python pattern never dispatches a model. | Designing a topology before spending worker tokens. |
| **board** | `/fusion:board` | `Agents ⇄ SQLite WAL` | Inspects, posts to, polls, DMs and acknowledges the Hive Board. There is no daemon. | Looking at live swarm state or steering through the architect seat. |

| Command | Kind | Skill | CLI / entry point |
| --- | --- | --- | --- |
| `/fusion:hive` | — | `fusion-hive` | `bash scripts/swarm.sh hive "<goal>" --dry-run --json`, `bash scripts/hive.sh`, `bash scripts/fusion.sh hive`, `python -m fusion_swarm.hive` |
| `/fusion:designer` | — | `fusion-designer` | `bash scripts/swarm.sh designer "<task>" --json` |
| `/fusion:board` | — | `fusion-board` | `python -m fusion_swarm.board --db PATH <verb>` |

`/fusion:board` lists 13 verbs in its argument hint. The board CLI has 39. The rest cover tasks, delivery receipts, path reservations and traces. See [COORDINATION.md](COORDINATION.md). The Vercel-Board-compatible port is a separate surface: [VBOARD.md](VBOARD.md).

## Hive — nested AndrewCode swarm on one board

A top-level architect talks to the human and designs the swarm. Captains are any live CLI. Nested workers are real AndrewCode processes. Everyone except the architect talks on one Hive Board, a SQLite file in WAL mode with a JSONL sidecar. The full specification is in [HIVE.md](HIVE.md). This section summarizes what the command does and what it enforces.

### Architect latitude

The architect is not a router table. It receives the topology catalog, the live roster and your constraints, and returns a `SwarmSpec`. It may pick a named form or invent a custom topology when none fits.

The architect seat comes from `--architect` (`ARCHITECT_SEATS` in [`hive.py`](../python/fusion_swarm/hive.py)):

| `--architect` | Provider | Model |
| --- | --- | --- |
| `fable` (default) | `fable` | `fable-5.1` (Fable 5.1) |
| `astra` or `codex` | `codex` | `gpt-6-astra` (Astra) |

The hard constraints the architect cannot override:

- User-specified providers and models.
- `SpawnLimits`, whose defaults are `max_depth=2`, `max_children_per_agent=4`, `max_agents=24` ([`identities.py`](../python/fusion_swarm/identities.py)). The limits are checked before any spawn.
- The honesty rules: real dispatch, and absent is never agreement.

[`validate_spec()`](../python/fusion_swarm/designer.py) fails closed. It rejects unknown `SwarmSpec` fields, a missing architect, a duplicate captain id, a captain `spawn` above `max_children_per_agent`, a total agent count above `max_agents`, `max_depth` too small for the spawned layers, `cross_talk` without `board`, a `communication` value outside `open|lineage|need-to-know`, and any provider missing from the live roster. Everything else is the architect's choice: who captains, who spawns, how many children, and which form.

### Example: 4-captain / 8-child

The canonical example (`NESTED_HIVE_EXAMPLE` in `designer.py`) is a 4-captain / 8-child hive. Two captains each spawn 4 nested AndrewCode children, and two spawn none.

| Captain id | Provider and model | Spawns | Spawn via |
| --- | --- | --- | --- |
| `muse` | `andrewcode`, `muse-spark-1.3` | 4 | `andrewcode` |
| `glm` | `opencode`, `glm-5.3` | 4 | `andrewcode` |
| `grok` (captain grok) | `grok`, `grok-4.5` | 0 | `adapter` |
| `copilot` (captain copilot) | `copilot`, `gemini-3.5-flash` | 0 | `adapter` |

Children are AndrewCode processes even when their captain is `opencode`, `grok` or `copilot`. Captains with `spawn_via: adapter` are dispatched through the provider adapters instead. The example uses an open board with cross-talk, so the architect talks to everyone, captains talk to their children, and children of different captains can talk.

Run it offline. This writes the example spec, then builds the tree and board with no subprocesses (run on 2026-11-22):

```bash
PYTHONPATH=python python3 -c "
import json
from fusion_swarm import designer
json.dump(designer.NESTED_HIVE_EXAMPLE, open('spec.json', 'w'))"
PYTHONPATH=python python3 -m fusion_swarm hive "demo task" --spec-file spec.json --dry-run --json 2>/dev/null \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(len(d['agents']), 'agents')
for a in d['agents']:
    print(a['id'], a['role'], a['provider'] + '/' + a['model'])"
```

```text
14 agents
arch architect fable/fable-5.1
copilot captain copilot/gemini-3.5-flash
glm captain opencode/glm-5.3
glm-1 child andrewcode/meta/muse-spark-1.3-contributor
glm-2 child andrewcode/meta/muse-spark-1.3-contributor
glm-3 child andrewcode/meta/muse-spark-1.3-contributor
glm-4 child andrewcode/meta/muse-spark-1.3-contributor
grok captain grok/grok-4.5
muse captain andrewcode/muse-spark-1.3
muse-1 child andrewcode/muse-spark-1.3
muse-2 child andrewcode/muse-spark-1.3
muse-3 child andrewcode/muse-spark-1.3
muse-4 child andrewcode/muse-spark-1.3
operator operator codex/gpt-6-astra
```

That is 1 architect, 1 operator, 4 captains and 8 children. The `glm` children run the default child model `meta/muse-spark-1.3-contributor` (`FUSION_ANDREWCODE_MODEL` overrides it). The `muse` children run the `muse` captain's model.

Without a spec, `--dry-run` builds a smaller default shape: the architect, the operator and two captains (`muse`, `glm`) with 4 children each, 12 agents in total. A live run (no `--dry-run`) with no spec does not call a model. It prints a design packet, and you decide what to run next.

### Using the board

Each agent polls and posts with the board CLI. The agent id and board path come from `FUSION_AGENT_ID` and `FUSION_BOARD`:

```bash
python -m fusion_swarm.board --db "$FUSION_BOARD" poll --agent "$FUSION_AGENT_ID"
```

`poll` returns messages from the channels the agent joined, plus DMs and `@mentions`. On a dry-run board it returns the two seeded task posts. A dry-run board has these channels: `hive`, `architect`, `captains`, `workers`, `system`, `task-main`, and one `lineage-<captain>` per captain. To block until new mail arrives, use the separate `wait` verb with `--timeout` (kqueue on macOS where available, a polling loop otherwise). `poll` has no `--wait` flag.

### Real processes, no sandbox

Nested workers are launched with the exact argv built by [`andrewcode_cli.command()`](../python/fusion_swarm/andrewcode_cli.py):

```text
node C:/dev/andrew-code/apps/kimi-code/dist/main.mjs -m <model> --thinking-effort <effort> --prompt-file <file> --output-format stream-json
```

The path shown is the default entry on Windows. On other systems the code uses the first existing candidate, starting with `~/dev/andrew-code/apps/kimi-code/dist/main.mjs`. Set `FUSION_ANDREWCODE_ENTRY` to point at a different entry script. The prompt always goes through `--prompt-file`. A missing binary is reported as absent and never counted as a vote.

AndrewCode prompt mode has no provider sandbox. Use worktree isolation and path reservations when you let workers write ([PEERS.md](PEERS.md)).

## Advanced swarm topologies

These commands run a named pattern. Most call the Python engine through `scripts/swarm.sh`, which sets `PYTHONPATH` and runs `python -m fusion_swarm <pattern>`. Defaults below are the argparse defaults in [`__main__.py`](../python/fusion_swarm/__main__.py).

| Command | Kind | What happens | Use when | CLI |
| --- | --- | --- | --- | --- |
| `/fusion:moa` | — | Layered Mixture-of-Agents. Every panelist answers, sees the prior layer's answers, and refines across `--layers` (default 2). The host synthesizes. Cost is about `layers` times a panel. | Hard, open-ended work. | `swarm.sh moa "<task>" --layers N --json` |
| `/fusion:heavy` | — | Splits the task into Research, Analysis, Alternatives and Verification questions, answers each in parallel, then integrates. `--loops` (default 1) adds refinement. | Research-grade depth. | `swarm.sh heavy "<task>" --loops N --json` |
| `/fusion:discuss` | — | Multi-round shared-thread discussion. Within a round panelists answer in parallel, and each sees all earlier rounds. `--rounds` defaults to 2. | Brainstorms where ideas should compound. | `swarm.sh discuss "<task>" --rounds N --json` |
| `/fusion:flow` | — | A custom pipeline. Stages run in order and providers within a stage run in parallel: `"codex -> copilot, opencode -> grok"` is codex, then copilot and opencode together, then grok. | You want to control who does what, in what order. | `swarm.sh flow "<task>" --flow "<dsl>" --json` |
| `/fusion:refine` | — | A generator answers, an evaluator scores it 0 to 100 with feedback, and the generator revises until the score clears `--threshold` or `--rounds` runs out. | A scored quality gate. | `swarm.sh refine "<task>" --generator P --evaluator P --threshold N --rounds N --json` |
| `/fusion:bestof` | — | Samples one provider `--samples` times (default 5) and folds the samples into one answer through a rolling champion. | A hardened solo, or only one CLI is live. | `swarm.sh bestof "<task>" --provider P --samples N --json` |
| `/fusion:reason` | — | Wraps a single panelist: `reflexion` (draft, self-critique, revise), `selfconsist` (sample N, majority) or `gkp` (generate knowledge, then answer). | Strengthening one model instead of running a panel. | `swarm.sh reflexion\|selfconsist\|gkp "<task>" --provider P --json` |
| `/fusion:ladder` | — | Escalation ladder. Tries the cheapest tier first and escalates on a gate failure or low confidence, passing the failure log up. Default tiers: `opencode` at minimal effort, `codex` at medium, `codex` at xhigh. Stops at the first tier that passes. | Routine work where flagship rates should be the exception. | `swarm.sh ladder "<task>" [--gate-cmd "<cmd>"] [--cwd DIR] --json` |
| `/fusion:speclock` | — | Writes a hard contract first (types, signatures, test stubs), then fans modules out in parallel against it. A build or typecheck gate is the integrator. | Multi-module coding where integration is the hard part. | `swarm.sh speclock "<task>" --contract-author P [--gate-cmd "<cmd>"] --json` |
| `/fusion:breaker` | — | A builder writes a solution and a breaker writes runnable failing tests, edge cases and security probes. With `--gate-cmd` the test runner is the arbiter. Defaults: builder `codex`, breaker `grok`, 2 rounds. | Robust code, not "looked fine". | `swarm.sh breaker "<task>" --builder P --breaker P --rounds N --json` |
| `/fusion:ballot` | — | Blind proposals are anonymized, each voter casts up to 2 approval votes and cannot vote for itself, votes cite reasons, and ties break on the verification gate. | A winner among proposals without anchoring or self-voting. | `swarm.sh ballot "<task>" [--gate-cmd "<cmd>"] --json` |
| `/fusion:gate` | — | Runs a real check (tests, typecheck, build, lint) and reports pass or fail with logs. Auto-detects a command when none is given. It executes the command, so use non-destructive checks. | A hard arbiter for code. | `swarm.sh gate --gate-cmd "<cmd>" --cwd DIR` |
| `/fusion:metaloop` | — | Fable as CEO frames every run, GPT (Codex) plans and owns the outcome, and a tiered swarm executes in proposal mode. Deterministic gates outrank model opinion. When `FUSION_BOARD` is set, workers register on the Hive Board. See [METALOOP.md](METALOOP.md). | Large gated coding or research runs. | `swarm.sh metaloop "<goal>" [--plan-file plan.json] [--advisor always\|auto\|on-demand\|off]` |
| `/fusion:graph` | — | Graph engineering. Task graphs have typed nodes (`work`, `verify`, `route`, `reduce`, `gate`, `human`) and dataflow scheduling. A knowledge-graph store backs `kg`. `--plan` lints, schedules and costs a graph without dispatching. | The shape of the work is the problem. | `swarm.sh graph "<task>" --spec graph.json --plan`; `swarm.sh kg --op <op> --graph-name N` |
| `/fusion:ultraswarm` | — | Selects OpenCode and Copilot models (defaults `opencode-go/glm-5.2` and `gemini-3.5-flash`), then convenes a five-agent council. The council discusses first, Claude assigns work, and Claude plus Codex integrate. | Large work that needs explicit model selection. | `python scripts/ultraswarm_selector.py --discover` (also `bash scripts/ultraswarm.sh`) |
| `/fusion:ultimate-review` | — | Claude writes a decision-annotated plan, Codex audits the plan before any code exists, Claude adjudicates and locks it, implements, Codex reviews the result, and Claude applies tweaks. No user round-trips in between. | You want it built right the first time. | `bash scripts/ureview.sh init\|plan-review\|impl-review\|status` |

Check any default with `PYTHONPATH=python python3 -m fusion_swarm --help`. Full flag lists are in [CLI.md](CLI.md).

`graph --plan` previews a graph before you spend tokens. Example, run offline on 2026-11-22:

```bash
bash scripts/swarm.sh graph "check files" \
  --nodes "scan=opencode,check=grok:verify,merge=:reduce" \
  --edges "scan>check,check>merge" --plan --json
```

The result begins with `"pattern": "graph"`, the three nodes with kinds `work`, `verify` and `reduce`, the two edges, an empty `lint` list and a `plan` block with the node levels. No provider is called.

`gate` runs your command, so it works as a quick offline check:

```bash
bash scripts/swarm.sh gate --gate-cmd "echo ok" --cwd .
```

```text
=== fusion swarm: gate ===
command: echo ok
passed:  True  (rc=0)
--- log (tail) ---
ok
```

## Runtime forms

The ten runtime forms are executable patterns in [`python/fusion_swarm/forms/`](../python/fusion_swarm/forms/). Each runs under a call budget, keeps a journal, and can be recovered with `salvage`. The catalog maps each command to a form name in `RUNTIME_FORM_COMMANDS`.

| Command | Form name | What happens | Default call cap |
| --- | --- | --- | --- |
| `/fusion:refute` | `refute_panel` | Authors propose, independent refuters try to find reproducible objections. A candidate survives with zero verified refutations and a passing gate. | 20 |
| `/fusion:tournament` | `tournament` | A seeded single-elimination bracket. Each match is judged in both orders, and execution beats judgment. | 17 |
| `/fusion:hedge` | `hedged_race` | Starts a backup provider when the primary is slow. The first verified answer wins. Read-only or sandboxed tasks only. | 3 |
| `/fusion:mapreduce` | `mapreduce` | Deterministic shards, mappers with backups for stragglers, then a reducer that cites shard ids. | 13 |
| `/fusion:critic-repair` | `critic_repair` | A writer drafts, a different-family critic reviews, the writer repairs. The verifier decides, not the critic. | 10 |
| `/fusion:pair` | `pair_swap` | Two providers pair. One writes, the other reviews and repairs. The reliability ledger picks the direction. | 4 |
| `/fusion:delphi` | `delphi` | Blind answers, code-computed feedback, revision. Returns a robust final answer plus a dissent log. | 9 |
| `/fusion:auction` | `auction` | Awards each task to the bidder the ledger favors, with an exploration floor. | 1 |
| `/fusion:dry` | `loop_until_dry` | Repeats a bounded search until consecutive rounds find nothing new. | 6 |
| `/fusion:blackboard` | `blackboard` | Typed entries on a shared notebook. A rule table activates one role per cycle until a cited verdict stands. | 12 |

All ten have catalog kind `runtime-form` and no skill. They share one entry point:

```bash
PYTHONPATH=python python3 -m fusion_swarm.forms list
PYTHONPATH=python python3 -m fusion_swarm.forms plan --task "review the diff" --providers codex,opencode,grok
PYTHONPATH=python python3 -m fusion_swarm.forms run refute_panel --help
PYTHONPATH=python python3 -m fusion_swarm.forms salvage <run-dir>
```

`list` prints the ten forms as JSON. `plan` chooses a form without dispatching a provider. For `--task "review the diff" --providers codex,opencode,grok` it returned the single-dispatch form (`"form": "single"`, `"width": 1`, `"budget": {"max_calls": 3}`) with the ladder `single, hedged_race, tournament, refute_panel`. `run <form> --help` shows each form's inputs. The call caps above are the `default_budget` values from `forms list` (run on 2026-11-22). Mechanics, budgets, the journal and salvage are in [FORMS.md](FORMS.md).

Do not confuse these with the 16 native team forms in [CODING_TEAMS.md](CODING_TEAMS.md). A few names appear in both (`blackboard`, `council`), but they are different things: native forms compile an explicit TeamSpec for host workers, while runtime forms dispatch providers under a budget.

## Coding teams and leadership

Native coding teams compile an explicit roster into a dependency-aware plan that the selected host runs with its own tools. Models are never chosen for you and there is no implicit fallback. Leadership style is independent of the form. The 16 forms are the output of `fusion.py forms --json`: `sidekick`, `genius-boss`, `idiot-boss`, `dag`, `pipeline`, `relay`, `fan-out`, `hub-spoke`, `map-reduce`, `diamond`, `ring`, `mesh`, `blackboard`, `review-pair`, `builder-breaker`, `council`.

| Command | Kind | What happens | Use when | CLI |
| --- | --- | --- | --- | --- |
| `/fusion:team` | `host-native` | Compiles an explicit TeamSpec into one of the native forms with a leadership mode. `--prepare` persists the plan and worker packets and does not dispatch. | A multi-worker coding task with an explicit roster. | `swarm.sh team --spec FILE [--form FORM] [--leadership MODE] [--prepare]`; `python fusion.py team --spec FILE` |
| `/fusion:sidekick` | `host-native` | A selected lead with focused native partners, durable briefs and review. | A lead plus one or two focused helpers. | `swarm.sh sidekick --spec FILE [--prepare] [--board-db FILE]` |
| `/fusion:genius-boss` | `host-native` | The expert lead specifies the design. Scoped workers implement and return evidence. | You want the lead to own decisions. | `swarm.sh genius-boss --spec FILE [--prepare]` |
| `/fusion:idiot-boss` | `host-native` | Light coordination. Explicitly selected expert workers own the technical decisions. | The workers know more than the lead. | `swarm.sh idiot-boss --spec FILE [--prepare]` |
| `/fusion:forms` | `offline-catalog` | Lists the 16 native forms with dependency and communication mechanics. No model calls. It also points to the runtime forms. | Choosing a form. | `swarm.sh forms --json`; `python fusion.py forms` |
| `/fusion:swarm-compose` | — | Composes a native swarm across topology, leadership, communication, roles, review, isolation and bounds. Preparation calls no providers. | Planning a run before launch. | `python fusion.py strategies catalog\|preview\|workflow` |
| `/fusion:swarm-control` | — | Controls a prepared run through durable dispatch ownership and explicit lead review. Pause, resume, budgets and scope changes are versioned and audited. | Operating a prepared run. | `python fusion.py control OPERATION --board FILE --input JSON_FILE` |
| `/fusion:swarm-status` | — | Shows durable status, next operator actions, correlated messages and redacted evidence exports. | Checking a run in flight. | `python fusion.py supervise status\|trace\|export\|doctor` |

Skills: `team` and `forms` use `fusion-team`. `sidekick`, `genius-boss` and `idiot-boss` use `fusion-sidekick`. The three `swarm-*` commands name no skill and point to [SWARM_RUNTIME.md](SWARM_RUNTIME.md).

`python fusion.py` is the PowerShell-friendly entry point. It routes `board`, `comms`, `control`, `strategies` and `supervise` to their modules and everything else to the `fusion_swarm` CLI. The six workflows that `strategies workflow --name` accepts are `ci-repair`, `contract-first-api`, `feature-delivery`, `migration-waves`, `reproduce-build-verify` and `review-and-revise`. They are not slash commands and not runtime forms.

## Control, status and tools

| Command | Kind | Skill | What happens | CLI / entry point |
| --- | --- | --- | --- | --- |
| `/fusion:setup` | — | `fusion-setup` | Detects which provider CLIs are installed and authenticated, optionally runs a one-word live smoke test (`--smoke`), and shows overridable config (`--config`). | `bash scripts/fusion.sh detect`; `bash scripts/fusion.sh providers` |
| `/fusion:costs` | — | none | Read-only cost posture: live panelists, default panel and models, ledger stats. States that a panel costs about 2 to 5 times one call. Dispatches nothing. | `fusion.sh detect`, `fusion.sh providers`, `fusion.sh ledger stats` |
| `/fusion:learn` | — | `fusion-learn` | Shows ledger stats and the leaderboard, regenerates `lessons.md`, and records outcome feedback. | `bash scripts/fusion.sh ledger stats\|leaderboard [type]\|winners <type>\|lessons` |
| `/fusion:memory` | — | `fusion-memory` | Stores, recalls, forgets, lists, injects, compacts, exports and imports persistent agent memory across sessions. | None. Skill procedure over `python/fusion_swarm/memory_service.py`. |
| `/fusion:context` | — | `fusion-agency-context` | Reads and writes shared state (`get`, `set`, `list`, `clear`, `snapshot`) across tools and agents in a session. | None. Skill procedure over `python/fusion_swarm/agency_context.py`. |
| `/fusion:steer` | — | `fusion-steering` | Attaches to a running worker, interrupts it, sends mid-task instructions, shows status or reads its inbox. | None. Skill procedure over `python/fusion_swarm/steering.py`. |
| `/fusion:dashboard` | — | none | Starts the localhost config dashboard. It binds to `127.0.0.1`, prefers port 8765, and opens a browser unless told not to. | `bash scripts/dashboard.sh`; `FUSION_DASHBOARD_PORT=N`, `FUSION_DASHBOARD_NO_OPEN=1` |
| `/fusion:peers` | `tool` | none | Inspects peer routing or calls an explicitly selected CLI peer. Verbs: `policy`, `route`, `ask`, `acp`, `handoff`, `whoami`. | `python -m fusion_swarm.peers <verb>` |
| `/fusion:vboard` | `tool` | `fusion-board-protocol` | Uses the Vercel-Board-compatible local board: claims, leases, cursor waits, a loopback server and a conformance check. | `python -m fusion_swarm.vboard --as <profile> <verb>` |
| `/fusion:ultracode` | — | `fusion-ultracode` | Runs the bundled UltraCode-Shim as a Fusion mode. Defaults to `doctor`. | `bash scripts/ultracode.sh doctor\|test\|launch\|status\|install` |

Notes:

- `learn`: there is no `ledger.sh feedback` verb. The skill records ground-truth outcomes by adding a run record (`ledger.sh record '<json>'`). Several ledger verbs need `jq`.
- `peers`: on a Claude Code host the Codex seat is reached only through AndrewCode. The `codex exec --json` adapter is off by default and needs `FUSION_ALLOW_CODEX_EXEC=1` or a Codex host (`FUSION_HOST=codex`). Details are in [PEERS.md](PEERS.md).
- `vboard`: verbs are `whoami channels actors read post reply edit delete react label tail wait tasks serve sweep conformance`. See [VBOARD.md](VBOARD.md).
- `dashboard`: endpoints and the terminal view are in [COCKPIT.md](COCKPIT.md).
- `ultracode`: `scripts/ultracode.sh` runs the bundled checkout in [`ultracode/`](../ultracode/). `test` is an offline proxy self-test.

## Engine-backed modes and their rosters

For 21 commands the catalog detects an engine pattern, meaning the command body names `swarm.sh <pattern>` or `fusion_swarm <pattern>` for a pattern in `ENGINE_PATTERNS`. `ENGINE_PATTERN_PROVIDERS` is the single roster contract: which providers that pattern dispatches. The dashboard and the per-mode override keys both read it, so neither can show a roster the engine would not run.

| Pattern | Commands | Dispatches |
| --- | --- | --- |
| `moa`, `heavy`, `discuss`, `flow`, `ballot`, `refine`, `speclock` | same-named commands | Base panel: `codex`, `copilot`, `opencode`, `grok` |
| `graph` | `graph` | Base panel |
| `reflexion` | `reason` | Base panel |
| `bestof` | `bestof` | `codex` |
| `ladder` | `ladder` | `opencode`, `codex` |
| `breaker` | `breaker` | `codex`, `grok` |
| `metaloop` | `metaloop` | `codex`, `grok`, `opencode`, `agy`, `fable` |
| `hive` | `hive` | `andrewcode`, `fable`, `codex`, `opencode`, `grok`, `agy` |
| `gate`, `designer` | `gate`, `designer` | None (deterministic or prompt-only) |
| `team`, `sidekick`, `genius-boss`, `idiot-boss`, `forms` | same-named commands | None (explicit user-selected seats) |

The other commands, including `panel`, `council`, `debate`, `vote`, `swarm`, `board` and the three `swarm-*` commands, carry a `roster_note` in the catalog: their live roster is decided dynamically by the skill they invoke, not by a fixed engine roster. Source: [`modes_catalog.py`](../python/fusion_swarm/modes_catalog.py). Guarded by [`tests/test_mode_contract_map.py`](../tests/test_mode_contract_map.py).

### Per-mode model and effort overrides

You can pin a model or reasoning effort for one pattern and one provider with `FUSION_<PATTERN>_<PROVIDER>_MODEL` and `FUSION_<PATTERN>_<PROVIDER>_EFFORT`, for example `FUSION_MOA_OPENCODE_MODEL`. `per_mode_override_specs()` generates the 72 valid keys: a MODEL key for every pattern and provider pair it dispatches, and an EFFORT key only for providers that consume effort (`codex`, `opencode`, `copilot`, `fable`). `grok` and `agy` get no EFFORT key, and `metaloop`, `hive` and `factory` get no keys because they bypass `adapter.dispatch`.

The resolution order, proven by [`tests/test_mode_overrides.py`](../tests/test_mode_overrides.py), is an explicit argument, then an active per-mode key, then the global value. An empty or commented per-mode key does not mask the global value. Because the key follows the engine pattern, `/fusion:reason` reads `FUSION_REFLEXION_*`. Re-check the count with `PYTHONPATH=python python3 -c "from fusion_swarm import modes_catalog as m; print(len(m.per_mode_override_keys()))"`. All variables are listed in [CONFIGURATION.md](CONFIGURATION.md).

## CLI patterns without a slash command

`python -m fusion_swarm` accepts more patterns than there are commands. These have no `/fusion:*` command of their own:

| Pattern | Notes |
| --- | --- |
| `hierarchy` | Director plans and assigns workers. Workers see only their task and injected context. |
| `factory` | Alias of the hive: discover, define, develop, deliver. |
| `race`, `speculate`, `harden`, `evolve`, `island-evolve` | Worktree race, hardening waves and evolutionary search. |
| `tournament` | The older engine tournament. `/fusion:tournament` is the runtime form instead. |
| `ledger-moe` | Wilson-bound routing rankings from `.fusion/runs.jsonl`. |
| `tui` | One-shot terminal snapshot. See [COCKPIT.md](COCKPIT.md). |
| `roster` | Pings every provider CLI live and prints the swarm config for every mode. It runs provider CLIs, so it is not an offline command. |
| `kg`, `reflexion`, `selfconsist`, `gkp` | Reached through `/fusion:graph` and `/fusion:reason`. |

The list is the `pattern` choices in `python -m fusion_swarm --help`. Flags are in [CLI.md](CLI.md).

## Where command text and CLI behavior differ

Checked on 2026-11-22. Where they differ, trust the CLI.

| Command | Command text says | The CLI does |
| --- | --- | --- |
| `/fusion:designer` | First run `swarm.sh designer "<goal>" --packet --json`. Also lists `--constraints` and `--out`. | `--packet` is rejected: `fusion_swarm: error: unrecognized arguments: --packet`. Given a task, the pattern prints the design prompt with `dispatched: false`. Given no task, it prints the topology catalog. `--constraints` and `--out` are not parsed. |
| `/fusion:hive` | The default shape is 4 captains by 4 children. | A spec-less `--dry-run` builds 2 captains (`muse`, `glm`) by 4 children. Use `--captains` or a spec for more. |
| `/fusion:hive` | Children run `andrewcode -p ... --auto --yolo --output-format text`. | The Python spawn path builds the `node ... main.mjs ... --prompt-file ... --output-format stream-json` argv shown in the Hive section. |
| `/fusion:refine` | Defaults are threshold 85 and 3 rounds. | `swarm.sh refine` defaults to threshold 80 and 2 rounds. |
| `/fusion:learn` | Lists a `feedback <run> shipped\|rejected` action. | `ledger.sh` has no `feedback` verb. Record an outcome with `ledger.sh record`. |

## Related

- [HIVE.md](HIVE.md): the Hive specification.
- [FORMS.md](FORMS.md): runtime forms, budgets, journal and salvage.
- [CODING_TEAMS.md](CODING_TEAMS.md): the 16 native team forms.
- [SKILLS.md](SKILLS.md): the skills these commands invoke.
- [CLI.md](CLI.md): every entry point and flag.
