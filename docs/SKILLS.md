# Skills catalog

This page lists the skills that ship in [`skills/`](../skills/), what each one is for, which slash commands load it, and how hosts discover and validate them. Read it if you are deciding which skill to invoke, tracing why a command behaves the way it does, or adding a skill. For the commands themselves see [MODES.md](MODES.md).

A skill is a Markdown file named `SKILL.md` with YAML frontmatter (`name`, `description`) and a prose procedure. The host model reads it and follows it. Most Fusion skills tell the host to call the bundled scripts (`scripts/fusion.sh`, `scripts/swarm.sh`, `python3 -m fusion_swarm ...`). The skill is the program, the scripts are the engine.

The plugin id, the `/fusion:*` commands, the `fusion_swarm` package and the `FUSION_*` variables keep the `fusion` name. The repository is `agent-swarm-fusion`.

## Overview

Counts below were derived on 2026-11-22 from the working tree.

| What | Count | How it was counted |
| --- | --- | --- |
| Directories under `skills/` | 24 | `ls skills \| wc -l` |
| `SKILL.md` files | 24 | `find skills -name SKILL.md \| wc -l` |
| Files under `skills/` (all types) | 59 | `find skills -type f \| wc -l` |
| Commands that name a skill (`invokes_skill` is set) | 38 of 54 | `modes_catalog.all_modes()` |
| Commands with no skill (runtime forms, `costs`, `dashboard`, `peers`, `swarm-compose`, `swarm-control`, `swarm-status`) | 16 | same |

Of the 24 `SKILL.md` files, 23 sit at `skills/<name>/SKILL.md`. The 24th is nested at `skills/ultimate-review/skills/ultimate-review/SKILL.md` (see [Vendored: ultimate-review](#vendored-ultimate-review)).

Re-check the mapping from commands to skills with:

```bash
PYTHONPATH=python python3 -c "
from fusion_swarm import modes_catalog as m
for x in m.all_modes(): print(x['mode'], x['invokes_skill'])"
```

The source is [`modes_catalog.py`](../python/fusion_swarm/modes_catalog.py). It sets `invokes_skill` to the first `**name** skill` phrase in the command file, so a command that names several skills shows only the first.

## Choosing a skill

The entrypoint skill `fusion` routes a request to the narrowest skill (see [`skills/fusion/SKILL.md`](../skills/fusion/SKILL.md)):

| Request | Skill |
| --- | --- |
| Sidekick, genius-boss or idiot-boss coding | `fusion-sidekick` |
| Native coding team or swarm form | `fusion-team` |
| Agent inboxes, typed handoffs, file ownership, shared context | `fusion-board` |
| Setup or health check | `fusion-setup` |
| Second opinion, panel, council, debate, vote, dynamic mode choice | `fusion-orchestrate` |
| Python-backed MoA, heavy, discuss, flow, refine, bestof, ladder, speclock, breaker, ballot, gate | `fusion-swarms` |
| Graph engineering, graph memory | `fusion-graph` |
| UltraCode bridge | `fusion-ultracode` |
| UltraSwarm model selection and council | `fusion-ultraswarm` |

The entrypoint's routing list does not mention `fusion-hive`, `fusion-designer`, `fusion-metaloop`, `fusion-ultimate-review` or the memory and steering skills. Those are reached through their own commands or trigger phrases.

## Catalog

Descriptions are condensed from each file's `description` frontmatter. Commands come from `invokes_skill` in the catalog unless noted. "References" lists files inside the skill's own directory.

### Entry and deliberation

| Skill | What it does | Commands | References |
| --- | --- | --- | --- |
| `fusion` | Main entrypoint. Routes the request to one of the skills in the table above. In Codex it resolves `FUSION_PLUGIN_ROOT` two directories above its own `SKILL.md`. | none in the catalog | none |
| `fusion-orchestrate` | The Conductor. Analyzes a non-trivial task, picks a collaboration mode (solo, panel, council, debate, vote, swarm, hive, designer), dispatches live providers, judges and synthesizes one answer, then records the run in the ledger. | `huddle`; also named in [`fusion.md`](../commands/fusion.md) | `anti-conformity.md`, `collaboration-modes.md`, `huddle.md`, `judge-rubric.md`, `learning.md`, `panel-doctrine.md`, `verdict-contract.md` |
| `fusion-panel` | The canonical panel: blind parallel dispatch, then judge and synthesize. Includes the cheap `vote` (model-free majority) and `ranked` variants. Forces `mode = panel` in the Conductor. | `panel`, `vote` | uses `fusion-orchestrate/references/` |
| `fusion-council` | Three rounds (blind, anonymized cross-examination, crystallize) with anti-conformity pressure, a minority report, kill criteria and one next step. Forces `mode = council`. | `council` | uses `fusion-orchestrate/references/` |
| `fusion-debate` | Two panelists from different families argue opposing sides, then the host adjudicates. Forces `mode = debate`. | `debate` | uses `fusion-orchestrate/references/` |
| `fusion-setup` | Detects installed provider CLIs, shows the default panel, runs a live smoke test, and explains model and timeout configuration. | `setup` | none |
| `fusion-learn` | Shows the reliability ledger, leaderboards and per-task winners, regenerates `lessons.md` (live copy in `~/.fusion/memory/`), records outcome feedback. Uses `fusion.sh ledger`. | `learn` | none |

`fusion-panel`, `fusion-council` and `fusion-debate` have no `references/` folder of their own. They point at `fusion-orchestrate/references/collaboration-modes.md` and its siblings, and they are thin wrappers that load the Conductor with a forced mode.

`fusion-setup` step 2 runs `tests/smoke-providers.sh`, which dispatches to live provider CLIs and can spend tokens. The other steps (`fusion.sh detect`, `fusion.sh providers`) are presence checks. See [GETTING_STARTED.md](GETTING_STARTED.md) for an offline first run.

### Coding teams and leadership

| Skill | What it does | Commands | References |
| --- | --- | --- | --- |
| `fusion-sidekick` | Sidekick, genius-boss and idiot-boss leadership. The selected host leads and keeps its model. The skill builds a `TeamSpec`, plans offline with `fusion.py sidekick --spec`, queues with `--prepare --board-db`, dispatches only ready jobs, and reviews each `WorkerReport`. | `sidekick`, `genius-boss`, `idiot-boss`; the catalog also lists `fusion` here because it is the first skill named in [`fusion.md`](../commands/fusion.md) | links [`CODING_TEAMS.md`](CODING_TEAMS.md), [`SWARM_RUNTIME.md`](SWARM_RUNTIME.md), [`COORDINATION.md`](COORDINATION.md) |
| `fusion-team` | Native coding teams with explicit rosters and dependency-aware forms (DAG, pipeline, relay, fan-out, hub-spoke, map-reduce, diamond, ring, mesh, blackboard, review-pair, builder-breaker, council). Lists forms with `fusion.py forms`, compiles with `fusion.py team --spec`, then hands dispatch and review to the `fusion-sidekick` procedure. | `team`, `forms`; also named in [`swarm-compose.md`](../commands/swarm-compose.md) | links `CODING_TEAMS.md` and `SWARM_RUNTIME.md` |

These are the "native team forms". They are separate from the 10 executable runtime forms in [FORMS.md](FORMS.md), which have commands but no skill.

### Hive, designer and boards

| Skill | What it does | Commands | References |
| --- | --- | --- | --- |
| `fusion-hive` | Nested AndrewCode hive. An architect talks to the human, captains are live CLIs, nested workers are real AndrewCode processes, and everyone except the architect talks on one Hive Board. Starts with a dry run. | `hive` | none |
| `fusion-designer` | An architect model must emit a `SwarmSpec` (catalog form or custom) from the live roster and the user's hard constraints before anything is spawned. Includes a form catalog and an example JSON. | `designer` | none |
| `fusion-board` | Hive Board operating manual: post, poll, DM, @mention, lineage and task channels, using `python3 -m fusion_swarm.board --db ...`. | `board` | none |
| `fusion-board-protocol` | Short worker protocol for leased tasks: unique `FUSION_AGENT_ID`, claim, renew at one-third of the lease, idempotency keys, cursor waits, stop on `claim_lost`. Applies to `fusion-board` MCP, `python3 -m fusion_swarm.vboard`, `vercel board` and `fusion-coordination`. | `vboard` | none |
| `fusion-metaloop` | MetaLoop: a strategic planner, a GPT chief operator and AndrewCode workers with deterministic gates that outrank model opinion. Uses the Hive Board when `FUSION_BOARD` is set. | `metaloop` | none |

See [HIVE.md](HIVE.md) and [METALOOP.md](METALOOP.md) for the engines, [VBOARD.md](VBOARD.md) and [COORDINATION.md](COORDINATION.md) for the two boards.

### Advanced swarm topologies and graphs

| Skill | What it does | Commands | References |
| --- | --- | --- | --- |
| `fusion-swarm` | Classic swarm: decompose a large task, route subtasks to providers, run dependency waves in parallel, synthesize and verify. Prefers the hive for large multi-file coding. | `swarm` | none |
| `fusion-swarms` | Python-engine patterns ported from the Swarms framework: MoA, heavy, discuss, hierarchy, graph, flow, refine, best-of-N, reflexion, self-consistency, ladder, speclock, breaker, ballot, gate. Calls `swarm.sh <pattern>`. | `ballot`, `bestof`, `breaker`, `discuss`, `flow`, `gate`, `heavy`, `ladder`, `moa`, `reason`, `refine`, `speclock` | none |
| `fusion-graph` | Graph engineering. Task graphs (typed nodes, dataflow scheduling, verification, reducers, routers, bounded loops, human gates) and knowledge graphs (ontology, extraction, quality gate, fusion, GraphRAG, bitemporal facts). | `graph` | `graph-memory.md`, `knowledge-graphs.md`, `task-graphs.md`, `topologies.md`, `workflows.md`; examples `discovery-loop.json`, `kg-extract.json`, `routed-review.json`, `security-sweep.json` |
| `fusion-ultraswarm` | Model selection and council with Grok, OpenCode, Copilot and optionally an external Codex. Runs `scripts/ultraswarm_selector.py`. | `ultraswarm` | none |
| `fusion-ultracode` | Operates the bundled `ultracode/` checkout through `scripts/ultracode.sh` (`test`, `doctor`, `launch`, `status`, `install`). Run `test` and `doctor` first. | `ultracode` | none |

### Review

| Skill | What it does | Commands | References |
| --- | --- | --- | --- |
| `fusion-ultimate-review` | Plan, cross-model plan audit, adjudication, pride gate, locked plan, implementation, cross-model implementation review, tweaks, with no user round-trips. Drives `scripts/ureview.sh` (`init`, `plan-review`, `impl-review`, `status`). | `ultimate-review` | `adjudication.md`, `implementation-review.md`, `plan-audit.md`, `pride-gate.md`; templates `decision-log.md`, `plan.md`, `review-report.md` |
| `ultimate-review` (vendored) | Post-coding decision audit with a decision log and a pride gate. See below. | none from the plugin's `commands/` | `cross-model-consult.md`, `decision-taxonomy.md`, `pride-gate.md`; templates `decision-log.md`, `review-report.md` |

### State and tools

| Skill | What it does | Commands | References |
| --- | --- | --- | --- |
| `fusion-memory` | Persistent agent memory across sessions: store, recall, forget, list, inject, export, import (the `compact` verb is documented in [`memory.md`](../commands/memory.md), not in the skill). Backed by `fusion_swarm.memory_service`. | `memory` | none |
| `fusion-agency-context` | A shared key-value context for a session so tools and agents pass data without repeating it. Backed by `fusion_swarm.agency_context`. | `context` | none |
| `fusion-steering` | Attach to, interrupt, redirect or check running workers through the worker registry, returning typed `SteeringResult` values. Backed by `fusion_swarm.steering`. | `steer` | none |

## Vendored: ultimate-review

`skills/ultimate-review/` is a standalone plugin tree copied into the repository. It has its own `README.md`, `RELEASE_NOTES.md`, five command files in `skills/ultimate-review/commands/` (`ultimate-review`, `pride-gate`, `decision-audit`, `decision-log`, `cross-consult`) and one skill at `skills/ultimate-review/skills/ultimate-review/SKILL.md`.

Points to know:

- Its `SKILL.md` is two directory levels below `skills/<name>/`, and there is no `SKILL.md` directly in `skills/ultimate-review/`. Whether a host loads it depends on how deep that host scans below the manifest's `skills` path. This was not verified for any host.
- Its five command files are not in the plugin's top-level `commands/` directory, so `/fusion:pride-gate` and the others do not exist as Fusion commands. The Fusion command `/fusion:ultimate-review` loads `fusion-ultimate-review`, a different skill.
- Its release notes mention `.claude-plugin/` and `.codex-plugin/` manifests for the standalone plugin. Those manifests are not part of this vendored copy, and its README install steps refer to the original standalone install location, not this repository.
- Both ultimate-review skills share trigger phrases such as "ultimate review". They differ in scope: `fusion-ultimate-review` runs plan-then-build with a cross-model audit on each side, while the vendored skill audits decisions after the coding work is done.

## How hosts discover skills

Manifests point at the whole `skills/` directory, not at individual files.

| Host | Manifest | Setting |
| --- | --- | --- |
| Codex CLI | [`.codex-plugin/plugin.json`](../.codex-plugin/plugin.json) | `"skills": "./skills/"` |
| Kimi | [`.kimi-plugin/plugin.json`](../.kimi-plugin/plugin.json) and [`kimi.plugin.json`](../kimi.plugin.json) | `"skills": "./skills/"` (also `"commands": "./commands/"`) |
| Root manifest | [`plugin.json`](../plugin.json) | `extensions.com.openai.skills = "./skills/"` |
| Claude Code | [`.claude-plugin/plugin.json`](../.claude-plugin/plugin.json) | no `skills` key; it relies on the Claude Code plugin default of a `skills/` directory at the plugin root. That default is host behavior, not something this repository declares. |

The installers copy `skills/` with the rest of the plugin: `INCLUDE_DIRS` in [`scripts/fusion_install.py`](../scripts/fusion_install.py) starts with `"skills"`, and `tests/test_kimi_install.py` asserts that `skills/fusion-hive/SKILL.md` lands in the Kimi install. For per-host install steps see [GETTING_STARTED.md](GETTING_STARTED.md).

Skills that run scripts tell the host to resolve `FUSION_PLUGIN_ROOT` from the loaded `SKILL.md` path (two directories above `skills/<name>/SKILL.md`) and to call scripts by absolute path. `fusion-hive`, `fusion-ultracode` and others also forbid searching the home directory for the plugin.

## What is enforced

Nothing validates the prose of a skill for correctness. These checks catch structural and contract regressions.

| Check | Where | What it enforces |
| --- | --- | --- |
| Skill frontmatter | `tests/validate.sh`, section `[4]` | every `SKILL.md` under `skills/` (recursive, so the vendored one too) has `name:` and `description:` within its first 12 lines |
| Declared paths exist | `tests/validate.sh`, section `[3]` | the `skills` and `commands` paths in `.codex-plugin/plugin.json` exist |
| Contract invariants | `tests/validate.sh`, section `[6]` | greps for load-bearing strings in `fusion-orchestrate` and its references, `fusion-ultracode`, `fusion-ultraswarm`, `fusion-metaloop`, `fusion-ultimate-review` and its references, and `fusion-graph` and its references, for example `IMMEDIATE ACTION`, `MANDATORY COMPLIANCE`, `Absent .* agreement` |
| Board skill | `tests/test_fusion_board_skill.py` | env vars, the canonical `poll` line, CLI verbs, channel map, roles |
| Designer skill | `tests/test_fusion_designer_skill.py` | `SwarmSpec` fields, pinned models as hard constraints, spawn limits, catalog table rows |
| Hive skill | `tests/test_hive_skill.py` | name, description, dry-run-first, no home-directory discovery, board invocation, spawn limits |
| Board protocol skill | `tests/test_v4_docs.py` | exact frontmatter start of `fusion-board-protocol` |
| Conductor | `tests/test_conductor_docs.py` | `fusion-orchestrate` and `collaboration-modes.md` content |
| MetaLoop and swarm skills | `tests/test_hive_entrypoints.py` | roles, Hive Board, honesty rules and skill invocation of the `metaloop` and `swarm` entrypoints |
| Manifests | `tests/test_hive_docs.py`, `tests/test_kimi_install.py` | `skills` equals `./skills/` in the Codex and Kimi manifests |

Section `[4]` only checks `name` and `description`. Its heading mentions `model`, but the code does not test for it, and no skill declares a `model` key. Verified on 2026-11-22 by running the section `[4]` loop and these modules in a scratch copy:

```bash
PYTHONPATH=python python3 -m unittest tests.test_fusion_board_skill \
  tests.test_fusion_designer_skill tests.test_hive_skill tests.test_v4_docs \
  tests.test_conductor_docs tests.test_hive_entrypoints
```

The result was `Ran 108 tests ... OK`, and the frontmatter loop reported no failures. The full suites are described in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Add a skill

1. Create `skills/<name>/SKILL.md`. Start with frontmatter that has `name` and `description` in the first 12 lines. Keep the name equal to the directory name. Put trigger phrases in the description.
2. Keep reference material in `skills/<name>/references/`, templates in `templates/`, examples in `examples/`.
3. If a command should load it, write `Invoke the **<name>** skill` in `commands/<command>.md`. That phrase is what `invokes_skill` detects.
4. If the skill carries invariants that must not regress, add `check` lines to `tests/validate.sh` section `[6]` or a focused `tests/test_*.py`.
5. Add the skill to the table above, and to the routing list in `skills/fusion/SKILL.md` if it should be reachable from the entrypoint.

The complete list of files, registries and manifests to touch, and the PR checklist, are in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Related

- [MODES.md](MODES.md): every slash command, with execution kind
- [CONCEPTS.md](CONCEPTS.md): the model behind the skills and the honesty invariants they repeat
- [CODING_TEAMS.md](CODING_TEAMS.md): the native team forms used by `fusion-team` and `fusion-sidekick`
- [CONTRIBUTING.md](../CONTRIBUTING.md): how to extend the plugin
