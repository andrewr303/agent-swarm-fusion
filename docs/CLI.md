# Command-line reference

This page lists every entry point the repository ships, what each one accepts, and which slash command maps to which CLI call. Read it if you drive Fusion from a shell, a script or another tool instead of (or alongside) a host such as Claude Code, Codex CLI or AndrewCode.

The repository is named `agent-swarm-fusion`. The commands, scripts, Python package (`fusion_swarm`) and environment variables (`FUSION_*`) keep the `fusion` name.

Everything below was checked against the source in this repository. Output blocks were produced by running the command in a scratch copy with a scratch `HOME` on 2026-11-22 (Python 3.13, macOS). Commands that would call a paid provider CLI were read from source only and are shown without output.

## Contents

- [Entry points at a glance](#entry-points-at-a-glance)
- [Conventions](#conventions)
- [Slash command to CLI map](#slash-command-to-cli-map)
- [Check providers and get a mode suggestion](#check-providers-and-get-a-mode-suggestion)
- [Run a collaboration pattern](#run-a-collaboration-pattern)
- [Run a nested Hive](#run-a-nested-hive)
- [Plan native coding teams](#plan-native-coding-teams)
- [Run and recover runtime forms](#run-and-recover-runtime-forms)
- [Use the boards](#use-the-boards)
- [Call peers](#call-peers)
- [Read and write the learning ledger](#read-and-write-the-learning-ledger)
- [Cross-family review](#cross-family-review)
- [Dashboard, TUI, UltraSwarm, UltraCode](#dashboard-tui-ultraswarm-ultracode)
- [Install and verify](#install-and-verify)
- [Low-level provider adapters](#low-level-provider-adapters)
- [MCP servers](#mcp-servers)
- [Maintenance and test scripts](#maintenance-and-test-scripts)
- [Commands in older docs that do not exist](#commands-in-older-docs-that-do-not-exist)
- [Keeping this page current](#keeping-this-page-current)

## Entry points at a glance

| Entry point | What it is | Details |
| --- | --- | --- |
| `scripts/fusion.sh` | Bash front door: provider detection, routing advice, single dispatch, blind panel fan-out, ledger passthrough | [Check providers](#check-providers-and-get-a-mode-suggestion) |
| `scripts/swarm.sh` | Bash bridge to `python -m fusion_swarm <pattern>` | [Run a collaboration pattern](#run-a-collaboration-pattern) |
| `fusion.py` and the installed `fusion` command | PowerShell-friendly Python entry, same patterns as `swarm.sh` plus five module entry points | [Conventions](#conventions) |
| `python -m fusion_swarm.forms` | The 10 runtime forms: list, plan, run, salvage, ledger, capacity | [Runtime forms](#run-and-recover-runtime-forms) |
| `scripts/board.sh`, `python -m fusion_swarm.board` | HiveBoard CLI (SQLite) | [Boards](#use-the-boards) |
| `python -m fusion_swarm.comms` | Same CLI, workspace-scoped default board | [Boards](#use-the-boards) |
| `python -m fusion_swarm.vboard` | Vercel-Board-compatible CLI | [Boards](#use-the-boards) |
| `python -m fusion_swarm.peers` | Peer policy, one-shot calls, ACP, board handoff | [Peers](#call-peers) |
| `scripts/hive.sh`, `python -m fusion_swarm.hive` | Nested Hive runner | [Hive](#run-a-nested-hive) |
| `scripts/ledger.sh` | Learning ledger | [Ledger](#read-and-write-the-learning-ledger) |
| `scripts/route.sh` | Mode advisor (also `fusion.sh route`) | [Check providers](#check-providers-and-get-a-mode-suggestion) |
| `scripts/ureview.sh` | Plumbing for `/fusion:ultimate-review` | [Cross-family review](#cross-family-review) |
| `scripts/dashboard.sh` | Localhost cockpit server | [COCKPIT.md](COCKPIT.md) |
| `scripts/ultraswarm.sh`, `scripts/ultracode.sh` | UltraSwarm model selector, bundled UltraCode shim | [Dashboard and others](#dashboard-tui-ultraswarm-ultracode) |
| `install.sh`, `install.ps1`, `scripts/fusion_install.py`, `scripts/install-kimi.py` | Installers | [Install and verify](#install-and-verify) |
| `mcps/fusion-coordination/server.py`, `mcps/fusion-board/server.py` | MCP servers (stdio) | [MCP servers](#mcp-servers) |

## Conventions

**Run from the repository root.** Nothing needs `pip install`. The Python code is standard library only and lives in `python/`.

```bash
PYTHONPATH=python python3 -m fusion_swarm --help
```

`swarm.sh`, `board.sh` and `hive.sh` set `PYTHONPATH` for you and pick an interpreter in this order: `$FUSION_PYTHON`, `python`, `py`, `python3` (source: `scripts/swarm.sh`). `dashboard.sh`, `ultraswarm.sh` and `ultracode.sh` try `python3`, `python`, `py`. The installer requires Python 3.10 or newer (message in `install.sh`).

**`fusion.py` and the installed `fusion` command.** `fusion.py` sends the first argument `board`, `comms`, `control`, `strategies` or `supervise` to the module of that name. Every other first argument goes to `python -m fusion_swarm`. The installer writes `bin/fusion`, `fusion.cmd` and `fusion.ps1` that run the installed copy of `fusion.py` (source: `scripts/fusion_install.py`). So `fusion sidekick --spec FILE` equals `python fusion.py sidekick --spec FILE`. The bash verbs of `fusion.sh` such as `detect` and `route` are not reachable through it:

```text
$ python3 fusion.py detect
fusion_swarm: error: argument pattern: invalid choice: 'detect' (choose from 'moa', 'heavy', ...)
```

`python -m fusion_swarm.strategies` and `python -m fusion_swarm.supervise` print nothing on stdout and exit 0, because those modules have no `__main__` guard (`strategies` also prints a `RuntimeWarning` on stderr). Reach them through `fusion.py strategies ...` and `fusion.py supervise ...`.

**Host detection differs between the bash and Python layers.** The bash scripts default `FUSION_HOST` to `codex` (`ureview.sh` defaults it to `claude`, see [Cross-family review](#cross-family-review)). Python treats `CLAUDECODE=1` or a set `CLAUDE_CODE_ENTRYPOINT` as a Claude host unless `FUSION_HOST_EXPLICIT=1` (source: `peers/policy.py` `detect_host`, `adapter.py` `_resolve_host`). On a Claude host, set `FUSION_HOST=claude` before running the bash scripts, otherwise `codex` is treated as the host and is never dispatched.

**Output and exit codes.** There is no single rule. These are the ones that hold:

| Tool | Output | Exit codes |
| --- | --- | --- |
| `python -m fusion_swarm <pattern>` | text summary, or the full result with `--json` | 0 normally, 1 when the result has `blocked`, 2 for usage errors. A pattern-level `error` field (for example a missing spec file for `hive`) still exits 0, so check the output. |
| `python -m fusion_swarm team/sidekick/...` | JSON | 0, or 1 with `{"ok": false, "error": ...}` |
| `fusion_swarm.forms` | one JSON object | 0 ok, 1 failed or budget exhausted, 2 usage, 130 cancelled |
| `fusion_swarm.board` | JSON | 0, 1 with `{"ok": false, "error": ...}`, argparse errors print `{"ok": false, "error": "invalid arguments"}` |
| `fusion_swarm.peers` | JSON on stdout, errors as JSON on stderr | 0 ok, 1 peer or board failure, 2 policy or usage error |
| `fusion_swarm.vboard` | text or `--json` | 0, 1 failure, 2 usage, 130 interrupted |
| `fusion.sh dispatch`, provider adapters | answer file plus `.log` | 0 returned, 124 timeout, 127 CLI missing, 1 runtime failure, 2 unknown provider, 3 provider is the host |

`python -m fusion_swarm.board` prints a `RuntimeWarning` about `sys.modules` on stderr. It is harmless. `fusion.py board` and `fusion.py comms` do not print it.

**Offline and plan-only options.** These never call a provider: `graph --plan`, `hive --dry-run`, `designer`, `metaloop`, `gate`, `kg`, `ledger-moe`, `tui`, `forms`, `team`/`sidekick`/`genius-boss`/`idiot-boss` without `--prepare`, `strategies`, `fusion.sh detect|providers|route`, and `forms plan|list|capacity`. `roster` and `supervise doctor` run provider CLIs (`roster` probes for model lists, `supervise doctor` runs `<host executable> --version`).

## Slash command to CLI map

Source: `python/fusion_swarm/modes_catalog.py` (54 modes, counted with `len(modes_catalog.all_modes())`) and `commands/*.md`. [MODES.md](MODES.md) describes each mode. This table only gives the shell call.

| Slash command | CLI |
| --- | --- |
| `/fusion:moa` | `swarm.sh moa "<task>" --layers N --json` |
| `/fusion:heavy` | `swarm.sh heavy "<task>" --loops N --json` |
| `/fusion:discuss` | `swarm.sh discuss "<task>" --rounds N --json` |
| `/fusion:flow` | `swarm.sh flow "<task>" --flow "<dsl>" --json` |
| `/fusion:refine` | `swarm.sh refine "<task>" --generator P --evaluator P --threshold N --rounds N --json` |
| `/fusion:bestof` | `swarm.sh bestof "<task>" --provider P --samples N --json` |
| `/fusion:reason` | `swarm.sh reflexion`, `selfconsist` or `gkp "<task>" --provider P` |
| `/fusion:ladder` | `swarm.sh ladder "<task>" [--gate-cmd CMD] [--cwd DIR] --json` |
| `/fusion:speclock` | `swarm.sh speclock "<task>" --contract-author P [--gate-cmd CMD] --json` |
| `/fusion:breaker` | `swarm.sh breaker "<task>" --builder P --breaker P --rounds N --json` |
| `/fusion:ballot` | `swarm.sh ballot "<task>" [--gate-cmd CMD] [--cwd DIR] --json` |
| `/fusion:gate` | `swarm.sh gate --gate-cmd "<cmd>" --cwd DIR` |
| `/fusion:graph` | `swarm.sh graph "<task>" --spec graph.json [--plan]`, `swarm.sh kg --op ...` |
| `/fusion:metaloop` | `swarm.sh metaloop "<goal>" [--plan-file FILE]` (planning report only) |
| `/fusion:designer` | `swarm.sh designer ["<goal>"]` (prints the catalog or the design prompt, never dispatches) |
| `/fusion:hive` | `swarm.sh hive "<goal>" --dry-run --json` |
| `/fusion:team`, `/fusion:sidekick`, `/fusion:genius-boss`, `/fusion:idiot-boss` | `swarm.sh <name> --spec FILE [--prepare] [--board-db FILE]` or `fusion.py <name> ...` |
| `/fusion:forms` | `fusion.py forms` (the 16 native team forms) |
| `/fusion:refute`, `tournament`, `hedge`, `mapreduce`, `critic-repair`, `pair`, `delphi`, `auction`, `dry`, `blackboard` | `python -m fusion_swarm.forms run <form> ...` ([mapping](#run-and-recover-runtime-forms)) |
| `/fusion:swarm-compose` | `fusion.py strategies catalog\|preview\|workflow ...` |
| `/fusion:swarm-status` | `fusion.py supervise status\|trace\|export\|doctor ...` |
| `/fusion:swarm-control` | `fusion.py control <operation> --board FILE --input JSON_FILE` |
| `/fusion:board` | `python -m fusion_swarm.board --db PATH <verb>` |
| `/fusion:vboard` | `python -m fusion_swarm.vboard --as PROFILE <verb>` |
| `/fusion:peers` | `python -m fusion_swarm.peers <policy\|route\|ask\|acp\|handoff\|whoami>` |
| `/fusion:dashboard` | `bash scripts/dashboard.sh` |
| `/fusion:ultimate-review` | `bash scripts/ureview.sh init\|plan-review\|impl-review\|status` |
| `/fusion:ultracode` | `bash scripts/ultracode.sh doctor\|test\|launch\|status\|install` |
| `/fusion:ultraswarm` | `python scripts/ultraswarm_selector.py --discover` (see `commands/ultraswarm.md`) |
| `/fusion:setup`, `/fusion:costs` | `fusion.sh detect`, `fusion.sh providers`, `fusion.sh ledger stats` |
| `/fusion:learn` | `fusion.sh ledger stats\|leaderboard\|lessons` |
| `/fusion:huddle` | `fusion.sh huddle <task_file> <out_dir> [csv]` |
| `/fusion:panel` | `fusion.sh panel <prompt_file> <out_dir> [csv]` for the blind fan-out |

These commands are skill-driven and have no dedicated CLI verb: `/fusion:fusion`, `council`, `debate`, `vote`, `swarm`, `steer`, `memory`, `context`. They use `fusion.sh` and `ledger.sh` as plumbing.

The engine patterns `race`, `speculate`, `tournament`, `harden`, `evolve`, `island-evolve`, `ledger-moe`, `tui`, `hierarchy`, `selfconsist`, `reflexion`, `gkp`, `kg`, `roster` and `factory` exist in the CLI but have no catalog mode of the same name. `tournament` also names a runtime form. They are different things: `swarm.sh tournament` is the engine pattern, `forms run tournament` is the runtime form.

## Check providers and get a mode suggestion

`scripts/fusion.sh` verbs (source: `scripts/fusion.sh`):

| Verb | Purpose |
| --- | --- |
| `detect [--json]` | Provider availability, probed with `command -v` only. Never starts a provider. |
| `providers` | Default panel and models, read from `config/defaults.env` |
| `route "<task>"` | Advisory mode, task type, panel and ledger evidence (`route.sh`) |
| `dispatch <prov> <prompt_file> <out_file> [model] [effort] [workspace]` | Run one adapter |
| `panel <prompt_file> <out_dir> [csv]` | Run a panel concurrently |
| `huddle <task_file> <out_dir> [csv]` | Panel advises on approach before a mode is chosen |
| `ledger <args>` | Passthrough to `ledger.sh` |
| `board <args>`, `hive <args>` | Passthrough to `board.sh`, `hive.sh` |
| `team`, `sidekick`, `genius-boss`, `idiot-boss`, `forms`, `race`, `speculate`, `tournament`, `harden`, `evolve`, `island-evolve`, `ledger-moe`, `tui` | Passthrough to `swarm.sh` |
| `version`, `help` | Version string (`fusion 3.3.0+swarm`), usage |

An unknown verb such as `discover` prints the usage text and exits 0. The usage text lists only some of the verbs above.

### Detect

`detect` reports `available`, `missing`, `degraded`, `host-native` or `disabled` per provider. `degraded` means the provider is present but listed in `$FUSION_STATE_DIR/degraded-providers`. `codex` and `andrewcode` also require `node` and an AndrewCode entry file (`FUSION_ANDREWCODE_ENTRY`, else the default locations in `detect.sh`) and are reported `degraded` when both exist. `fable` stays `disabled` unless `FUSION_ALLOW_CLAUDE=1`. `FUSION_ALLOWLIST` (default `$FUSION_STATE_DIR/panel-allowlist`) restricts the panel to the listed names.

Run with an empty provider PATH so the result is reproducible (your machine will show the CLIs you have installed):

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin bash scripts/fusion.sh detect
```

```text
PROVIDER_CHECK_START
claude:missing
codex:host-native
copilot:missing
opencode:missing
grok:missing
kimi:missing
andrewcode:missing
agy:missing
fable:disabled
PROVIDER_CHECK_END
```

`detect --json` prints the same states as one object with `providers`, `metaloop`, `live_panelists` and `multi_model`. There is no `gemini`, `ollama` or `local` provider in `scripts/providers/`. The adapters that exist are `codex`, `copilot`, `opencode`, `grok`, `kimi`, `andrewcode`, `agy`, `fable` ([PEERS.md](PEERS.md) covers them).

### Route

```bash
bash scripts/fusion.sh route "Fix the bug in parser.py and add a unit test"
```

```text
MODE=panel
TASK_TYPE=code
REASON=code task — panel each produces a candidate; judge runs+merges what works (Track A)
PANEL=copilot,opencode,grok
SIGNALS=code:1 verifiable:0 deliberate:0 debate:0 large:0 graph:0 ureview:0 len:44
LEDGER=none yet
NOTE=advisory default — the Conductor may override based on task nuance + LEDGER
```

`PANEL` is a fixed list, not an availability check: `codex,copilot,opencode,grok`, minus `codex` when `FUSION_HOST` is `codex`. For the native team modes it is `explicit-native-roster`. The advisor can return `panel`, `solo`, `vote`, `debate`, `council`, `swarm`, `graph`, `ureview`, `race`, `harden`, `island-evolve`, `team`, `sidekick`, `genius-boss` or `idiot-boss` (source: `scripts/route.sh`). A task is read from the first argument or from stdin.

### Dispatch and panel

`dispatch` refuses the provider that equals the host and an unknown provider:

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin bash scripts/fusion.sh dispatch codex /dev/null /dev/null
```

```text
[fusion] 'codex' is the host runtime — fold its view in-context, not as a subprocess.
```

That exits 3. An unknown name prints `[fusion] unknown provider 'nope'` and exits 2. A real dispatch (`dispatch grok prompt.txt out.txt`) calls the provider CLI and is not shown here.

`panel` writes `<out_dir>/<provider>.out`, `.status`, `.log` and `.start`, plus `<out_dir>/manifest.txt`. It prints one `provider:status sec=N` line per provider. Status is `returned`, `timeout`, `absent` or `error`. The host is recorded in the manifest as `host-native` and an unknown name as `unknown`. A provider that fails is reported as `absent`, `timeout` or `error` and never merged as agreement. With no CSV it uses the live providers from `detect.sh`, excluding `claude` and the host. `huddle` writes a fixed advise-on-approach prompt and then runs `panel`.

## Run a collaboration pattern

```bash
bash scripts/swarm.sh <pattern> "<task>" [flags] [--json]
python3 fusion.py <pattern> "<task>" [flags] [--json]
PYTHONPATH=python python3 -m fusion_swarm <pattern> "<task>" [flags] [--json]
```

The three forms are equivalent. The pattern is a positional choice, one of the 36 below (source: `choices` in `python/fusion_swarm/__main__.py`). The parser is one flat parser: every flag is accepted by every pattern and ignored by those that do not use it. A task is required unless the pattern is `gate`, `roster`, `kg`, `designer`, `board`, `ledger-moe` or `tui` (positional text or `--task-file`).

```text
moa heavy discuss hierarchy graph flow refine bestof reflexion selfconsist gkp
kg ladder speclock breaker ballot gate metaloop roster hive designer board factory
team sidekick genius-boss idiot-boss forms race speculate tournament harden evolve
island-evolve ledger-moe tui
```

### Common flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--json` | off | Print the full result dict as JSON |
| `--task-file FILE` | none | Read the task from a file |
| `--timeout N` | pattern default | Per-call timeout in seconds |
| `--providers a,b` | live providers | Panel roster |
| `--aggregator P` | `codex` | Provider that synthesizes |
| `--no-record` | off | Do not write the run to the ledger |
| `--gate-cmd CMD` | none | Verification command for patterns that gate |
| `--cwd DIR` | none | Working directory for the gate |

Runs of real patterns are appended to the ledger unless `--no-record` is set. Plan-only runs (`gate`, `metaloop`, `roster`, `kg`, `designer`, `board`, `ledger-moe`, `tui`, a `graph` without dispatch, a `hive` or `factory` dry run) are never recorded.

### Pattern flags

| Pattern | Flags it reads (default) | Notes |
| --- | --- | --- |
| `moa` | `--layers` (2), `--providers`, `--aggregator` | Layered mixture of agents |
| `heavy` | `--loops` (1), `--providers`, `--aggregator` | Role-specialized deep analysis |
| `discuss` | `--rounds` (2), `--providers`, `--aggregator` | Group-chat brainstorm |
| `hierarchy` | `--director` (`codex`), `--providers`, `--aggregator` | Director plans, workers execute |
| `graph` | `--spec FILE` or `--nodes` and `--edges`, `--plan`, `--approve ids`, `--max-workers` (8) | Errors if neither spec nor nodes is given. `--plan` lints, schedules and costs without dispatching |
| `kg` | `--op` (`stats`), `--graph-name` (`default`), `--question`, `--spec`, `--source`, `--as-of`, `--hops` (2), `--apply`, `--head`, `--rel`, `--tail` | Ops: `init`, `stats`, `ontology`, `ingest`, `lint`, `candidates`, `fuse`, `merge`, `query`, `path`, `serialize`. Stores under `$FUSION_GRAPH_HOME/<name>`, default `~/.fusion/graph/<name>`. `--apply` applies auto-merges (the default is a report-only dry run) |
| `flow` | `--flow "a -> b, c"` | Agents must be `codex`, `copilot`, `opencode` or `grok` |
| `refine` | `--generator` (`codex`), `--evaluator` (`copilot`), `--threshold` (80), `--rounds` (2) | Generator to evaluator quality gate |
| `bestof` | `--provider` (`codex`), `--samples` (5) | |
| `reflexion` | `--provider` (`codex`), `--iterations` (2) | |
| `selfconsist` | `--provider` (`codex`), `--samples` (5) | |
| `gkp` | `--provider` (`codex`) | |
| `ladder` | `--gate-cmd`, `--cwd`, `--confidence` (0.7) | |
| `speclock` | `--contract-author` (`codex`), `--gate-cmd`, `--cwd` | |
| `breaker` | `--builder` (`codex`), `--breaker` (`grok`), `--rounds` (2), `--gate-cmd`, `--cwd` | |
| `ballot` | `--providers`, `--gate-cmd`, `--cwd` | |
| `gate` | `--gate-cmd`, `--cwd` | Runs one command. With no `--gate-cmd` it auto-detects one (`npm run typecheck`, `npm test --silent`, `npx --no-install tsc --noEmit`, a pytest run, and so on, from `gate.py`) |
| `metaloop` | `--advisor {off,on-demand,auto,always}`, `--advisor-max`, `--max-repair-rounds`, `--max-attempts-per-task`, `--expert-concurrency`, `--fast-concurrency`, `--workspace-mode {proposal,worktree}`, `--risk-threshold`, `--plan-file FILE`, `--run-id` (`ml_run`) | Prints config, roster and, with a plan file of TaskSpecs, routes and waves. It does not run the loop |
| `roster` | `--force-refresh`, `--json` | Probes every provider CLI for models (cache first) and lists all catalog modes. Starts provider CLIs |
| `designer` | optional task | No task: topology catalog. With a task: the design prompt. Never dispatches. See the note below |
| `hive` | `--dry-run`, `--children` (4), `--captains`, `--architect` (`fable`), `--state-dir`, `--spec-file`/`--spec` | [Hive](#run-a-nested-hive) |
| `factory` | `--dry-run`, `--architect` (`fable`), `--state-dir`, `--spec-file`/`--spec` | Wraps the hive engine with a fixed four-captain discover/define/develop/deliver spec. It ignores `--children` and `--captains` |
| `team`, `sidekick`, `genius-boss`, `idiot-boss`, `forms` | `--spec`, `--form`, `--leadership`, `--prepare`, `--board-db`, `--allow-claude` | [Native teams](#plan-native-coding-teams) |
| `race`, `speculate`, `tournament` | `--providers`, `--workspace` or `--cwd`, `--gate-cmd` | Default roster `codex,opencode,grok`. Unknown provider names exit 2 |
| `harden` | `--builder` (`codex`), `--breaker` (`grok`, used as the hardener), `--workspace`/`--cwd`, `--gate-cmd`, `--max-waves` (4), `--clean-waves` (2) | |
| `evolve`, `island-evolve` | `--providers`, `--islands` (3), `--generations` (2), `--gate-cmd`, `--workspace`/`--cwd` | |
| `ledger-moe` | `--runs-file` (`memory/runs.jsonl`), `--task-type`, `--min-samples` (30), `--providers` | Advisory Wilson-bound ranking from the run records. No task needed |
| `tui` | `--workspace` (current directory), `--board-db`, `--json`, `--tmux` | One-shot text or JSON snapshot, not a live curses screen |
| `board` | none | Prints the pointer to the real board CLI |

`--timeout` is parsed for every pattern but only passed to some. Read the pattern module if the timeout matters.

**`designer` does not take `--packet`, `--constraints` or `--out`.** `commands/designer.md` shows `designer "<goal>" --packet --json`, and argparse rejects it:

```text
fusion_swarm: error: unrecognized arguments: --packet
```

Use `swarm.sh designer "<goal>"` for the design prompt. `--architect` is accepted but the designer branch ignores it, and the branch makes no live architect call.

### Plan-only examples

```bash
bash scripts/swarm.sh graph "scan then check" \
  --nodes "scan=opencode,check=grok:verify,merge=:reduce" \
  --edges "scan>check,check>merge" --plan
```

```text
=== fusion graph: graph ===
nodes=3  critical path=3 stages  max parallel=1  est calls=2 (1 round(s))
  stage 0: scan[work]
  stage 1: check[verify]
  stage 2: merge[reduce]
  deterministic (no model call): merge

(no dispatch — plan only)
```

```bash
bash scripts/swarm.sh gate --gate-cmd "echo gate-ok" --cwd .
```

```text
=== fusion swarm: gate ===
command: echo gate-ok
passed:  True  (rc=0)
--- log (tail) ---
gate-ok
```

## Run a nested Hive

Two front ends reach the same engine:

| Call | Parser | Output |
| --- | --- | --- |
| `swarm.sh hive "<goal>" ...` or `fusion.py hive ...` | `python -m fusion_swarm hive` (the flat parser above) | text summary, JSON with `--json` |
| `scripts/hive.sh "<goal>" ...` or `fusion.sh hive ...` | `python -m fusion_swarm.hive` | JSON always |

Flags of `python -m fusion_swarm.hive` (source: `hive.py` `build_parser`):

| Flag | Default | Meaning |
| --- | --- | --- |
| `task` or `--task-file` | none | The hive goal |
| `--spec-file FILE`, `--spec FILE` | none | A SwarmSpec JSON file. Without one, the default nested example is used |
| `--architect` | `fable` | `fable`, `astra` or `codex` |
| `--children`, `--children-per-captain` | 4 | Children per captain |
| `--captains a,b` | default captains | Captain tokens, for example `muse,glm` |
| `--dry-run` | off | Build the tree and the board and seed the task post, with no provider call |
| `--state-dir DIR` | `$FUSION_STATE_DIR` or `~/.fusion` | Board location: `<state-dir>/hive/<run_id>/board.sqlite` |
| `--timeout N` | none | Seconds |
| `--json` | accepted | Output is JSON anyway |

Always dry-run first:

```bash
bash scripts/swarm.sh hive "demo goal" --dry-run --state-dir /tmp/fusion-demo
```

```text
=== fusion swarm: hive ===
run_id: hive-c24531583a  dry_run: True
board: /tmp/fusion-demo/hive/hive-c24531583a/board.sqlite
agents: 12  posts_seeded: 3
talk on the board: python -m fusion_swarm.board --db /tmp/fusion-demo/hive/hive-c24531583a/board.sqlite poll --agent arch
```

The `run_id` differs on every run. Poll the board it created with the line it prints, for example:

```bash
python -m fusion_swarm.board --db "$FUSION_BOARD" poll --agent "$FUSION_AGENT_ID"
```

[HIVE.md](HIVE.md) covers the architecture, spawn limits and the SwarmSpec contract.

## Plan native coding teams

The native team forms are the 16 forms in the TeamSpec runtime (`fusion.py forms` prints them). They are not the 10 runtime forms. [CODING_TEAMS.md](CODING_TEAMS.md) and [SWARM_RUNTIME.md](SWARM_RUNTIME.md) describe the model.

```bash
python3 fusion.py sidekick --spec docs/examples/sidekick-team.json            # plan only
python3 fusion.py sidekick --spec docs/examples/sidekick-team.json --prepare --board-db /tmp/team/board.sqlite
```

| Flag | Meaning |
| --- | --- |
| `--spec FILE` | TeamSpec JSON. Without it the command prints the form list and a `next_step` note and dispatches nothing. `--prepare` without `--spec` is an error |
| `--form NAME` | Form. Defaults to the command name for `sidekick`, `genius-boss`, `idiot-boss` |
| `--leadership {sidekick,genius-boss,idiot-boss}` | Leadership style |
| `--prepare` | Persist the validated plan and worker packets to a board. Calls no provider |
| `--board-db FILE` | Board for `--prepare`. Default `~/.fusion/teams/<run_id>/board.sqlite` |
| `--allow-claude` | Authorize a Claude-backed seat |

Plan-only and prepare both return JSON; the plan carries `prepared: false` or `true` (executed on the shipped `sidekick-team.json`).

`fusion.py strategies` (source: `strategies.py`):

| Call | Purpose |
| --- | --- |
| `strategies catalog` | Forms, communication modes, isolation, roles, workflows and leadership styles as JSON |
| `strategies preview --spec FILE` | Compile a spec |
| `strategies workflow --name NAME --spec FILE` | Generate and compile one of six workflows: `feature-delivery`, `reproduce-build-verify`, `migration-waves`, `contract-first-api`, `review-and-revise`, `ci-repair` |
| `--prepare`, `--board FILE`, `--output-spec FILE`, `--allow-claude` | Persist to a board (default `<workspace>/.fusion/<run_id>/board.sqlite`), save the generated spec, authorize Claude seat |

The default board path differs from `team --prepare`, which uses `~/.fusion/teams/<run_id>/board.sqlite`.

`fusion.py supervise <status|trace|export|doctor>`: `status`, `trace` and `export` need an existing `--board`, plus `--lead` and `--run`. `export --output FILE` saves redacted evidence. `status` prints text unless `--json`. `doctor --host {codex,andrewcode,claude} [--executable PATH]` runs `<executable> --version` and reports installation and declared capabilities, so it starts a process. Without `--executable`, `codex` and `andrewcode` run `node <AndrewCode entry> --version`, and `claude` runs `claude --version`. `--output FILE` works for every operation.

`fusion.py control <operation> --board FILE [--input JSON_FILE]` calls one of the 51 coordination operations with JSON arguments from the file or stdin and makes no model calls. The operation names are the `fusion-coordination` tool names ([COORDINATION.md](COORDINATION.md)). List them with:

```bash
python3 fusion.py control --help
```

## Run and recover runtime forms

Entry point: `PYTHONPATH=python python3 -m fusion_swarm.forms <command>`. Output is always one JSON object. [FORMS.md](FORMS.md) explains the forms, budgets, journal and salvage.

| Command | Purpose |
| --- | --- |
| `list` | The 10 forms with inputs, stop rule, aggregation and default budget |
| `plan --task T --providers a,b [--kind K] ...` | Pick a form without dispatching |
| `run <form> ...` | Run a form and keep a journal |
| `salvage <run_dir>` | Recover recorded outputs from an interrupted or finished run |
| `ledger show [--task-class C]`, `ledger record ...` | Inspect or record verified outcomes |
| `capacity --width N --service-s S [--tasks N] [--deadline-s S] [--spawn-gap-s S]` | Estimate width, captains and time |

`plan` flags: `--task` and `--providers` are required. Optional: `--kind {implement,review,discovery,estimate,decomposable,allocation,compare,open}`, `--read-only`, `--verifier`, `--items N`, `--difficulty {easy,medium,hard}`, `--stakes {low,high}`, `--latency`, `--isolated`, `--heterogeneous`, `--service-s` (120), `--deadline-s`.

`run` flags shared by every form: one of `--task` or `--task-file`, `--providers a,b`, `--gate-cmd CMD`, `--cwd DIR`, `--budget-calls N`, `--budget-tokens N`, `--deadline-s S`, `--run-dir DIR` (must be empty or new), `--timeout S`. Each form-specific option also has a `-json` twin that takes a JSON value (`--dry-rounds-json 2`); the two are mutually exclusive. `ledger record` requires `--worker`, `--task-class`, `--role`, `--verified` and one of `--success` or `--failure`.

Slash command to form name and aliases (source: `forms/__init__.py` `ALIASES`, `modes_catalog.RUNTIME_FORM_COMMANDS`):

| Slash command | `run` form name | Aliases |
| --- | --- | --- |
| `/fusion:refute` | `refute_panel` | `refute`, `refute-panel` |
| `/fusion:tournament` | `tournament` | none |
| `/fusion:hedge` | `hedged_race` | `hedge`, `race`, `hedged-race` |
| `/fusion:mapreduce` | `mapreduce` | `map` |
| `/fusion:critic-repair` | `critic_repair` | `critic`, `critic-repair` |
| `/fusion:pair` | `pair_swap` | `pair`, `pair-swap` |
| `/fusion:delphi` | `delphi` | none |
| `/fusion:auction` | `auction` | none |
| `/fusion:dry` | `loop_until_dry` | `dry`, `loop-until-dry` |
| `/fusion:blackboard` | `blackboard` | `bb` |

There is also a hidden `single` form (one dispatch plus verifier) that `plan` can choose; it is not one of the 10 listed forms.

Check any form's flags with `python -m fusion_swarm.forms run <form> --help`.

Offline run with the test hook. `FUSION_TEST_HOOKS=1` plus `FUSION_FORMS_FAKE_DISPATCH` pointing at a JSON file that maps provider names to ordered responses replaces provider dispatch (source: `forms/__main__.py` `_fake_dispatch`):

```bash
echo '{"mock": ["[]", "[]"]}' > fixture.json
FUSION_TEST_HOOKS=1 FUSION_FORMS_FAKE_DISPATCH=fixture.json \
  python3 -m fusion_swarm.forms run dry --task find --providers mock --dry-rounds 2 --run-dir run1
python3 -m fusion_swarm.forms salvage run1
```

```text
{"form": "loop_until_dry", "run_id": "loop_until_dry-20261122T073205-59ac64", "status": "partial", "winner": null, "candidates": [], "rounds": 2, "stop_reason": "dry", "calls": 2, "tokens": 0, "elapsed_s": 0.003, "run_dir": "...", "extra": {...}}
{"run_id": "loop_until_dry-20261122T073205-59ac64", "form": "loop_until_dry", "status": "salvaged", "best": {...}, "verified": false, "candidates": [...], "calls": 2, "rounds": 2, "stop_reason": "dry", "run_status": "partial"}
```

(Both lines trimmed with `...`.) The run directory then holds `checkpoint.json`, `journal.jsonl`, `outputs/` and `result.json`. `plan` and `capacity` need no hook:

```bash
python3 -m fusion_swarm.forms plan --task "review this diff" --providers codex,grok --kind review
python3 -m fusion_swarm.forms capacity --width 8 --service-s 60 --tasks 20
```

```text
{"form": "single", "params": {"provider": "codex", "reviewers": ["grok"], "verify": false}, "width": 1, "budget": {"max_calls": 2}, "ladder": ["single", "hedged_race", "tournament", "refute_panel"], "reasons": ["row 10: otherwise -> single dispatch + verifier (ladder stage 0)"], "capacity": {}, "fallbacks": []}
{"width": 8, "captains": 1, "slots_per_captain": 15, "expected_makespan_s": 213.92, "dispatch_ceiling_per_min": 14.151}
```

A missing required argument returns exit 2 with a JSON usage error, not an argparse message:

```text
{"ok": false, "error": "usage", "message": "one of the arguments --task --task-file is required"}
```

## Use the boards

Three tools, two data stores. [COORDINATION.md](COORDINATION.md) covers HiveBoard, [VBOARD.md](VBOARD.md) covers VBoard.

### HiveBoard: `python -m fusion_swarm.board`

`--db PATH` is the SQLite board. Default: `$FUSION_BOARD`, else `${FUSION_STATE_DIR:-~/.fusion}/hive/board.sqlite` (source: `board.py` `default_board_db`). All output is JSON. `fusion.py board ...` and `scripts/board.sh ...` run the same parser.

| Verb | Required flags | Other flags (default) |
| --- | --- | --- |
| `init` | none | creates the schema and the channels `hive`, `architect`, `captains`, `workers`, `system` |
| `register` | `--id`, `--provider`, `--model` | `--display-name`, `--role` (`worker`), `--parent-id`, `--lineage`, `--depth`, `--spawn-budget`, `--max-depth` (2), `--status` (`online`), `--replace` |
| `heartbeat` | `--agent` | `--status` (`online`) |
| `post` | `--from` | `--body` or `--body-file`, `--channel` (`hive`), `--thread-id`, `--mentions`, `--to`, `--kind` (`message`), `--task-id`, `--priority {low,normal,urgent}`, `--payload-file`, `--idempotency-key` |
| `dm` | `--from`, `--to` | `--body` or `--body-file`, `--kind`, `--task-id`, `--priority`, `--payload-file`, `--idempotency-key` |
| `reply` | `--from`, `--message-id`, `--body` | `--kind`, `--priority`, `--idempotency-key` |
| `poll` | `--agent` | `--since-id`, `--limit` (100) |
| `mentions` | `--agent` | `--since-id` |
| `agents`, `tree`, `channels`, `stats`, `reservations` | none | read-only listings |
| `join` | `--channel`, `--agent` | |
| `ack` | `--agent`, `--message-id` | `--lease-token` |
| `inbox` | `--agent` | `--limit` (100), `--task-id` |
| `claim` | `--agent` | `--limit` (1), `--lease-seconds` (60) |
| `nack` | `--agent`, `--message-id`, `--lease-token` | |
| `receipts` | `--message-id` | |
| `reserve` | `--agent`, `--workspace`, `--paths` | `--task-id`, `--lease-seconds` (900) |
| `release` | `--agent`, `--workspace`, `--paths` | `--task-id` |
| `context-get` | `--namespace`, `--key` | |
| `context-set` | `--agent`, `--namespace`, `--key`, `--value-file`, `--expected-version` | |
| `create-task` | `--agent`, `--brief-file` | |
| `tasks` | none | `--task-id` |
| `start-task` | `--agent`, `--task-id` | `--lease-seconds` (900) |
| `submit-task` | `--agent`, `--report-file` | |
| `review-task` | `--agent`, `--task-id`, `--attempt`, `--decision {accept,revise,escalate,cancel}`, `--note` | |
| `recover-task` | `--agent`, `--task-id`, `--attempt`, `--note` | |
| `task-history` | `--task-id` | |
| `send-message` | `--from`, `--to`, `--envelope-file`, `--idempotency-key` | `--priority` |
| `consume-delivery` | `--agent`, `--message-id`, `--lease-token`, `--effect-key`, `--result-file` | |
| `dead-letters` | `--agent` | |
| `retry-delivery` | `--agent`, `--message-id` | `--extra-attempts` (1), `--deadline` |
| `trace` | `--agent`, `--run-id` | `--trace-id`, `--limit` (200) |
| `readiness` | `--agent`, `--run-id`, `--state {registered,ready,started,observed,stopped,error}`, `--native-handle`, `--model`, `--expected-version` | |
| `wait` | none | `--timeout` (30.0), `--agent`, `--since-id`, `--hook-rewake` |
| `trace-deposit` | `--agent`, `--locus` | `--kind` (`hotspot`), `--strength` (1.0), `--half-life` (600), `--ttl` (1800), `--note` |
| `trace-sense` | none | `--locus-prefix`, `--kind`, `--min-strength` (0.05), `--limit` (50) |

Source: `build_parser()` in `python/fusion_swarm/board.py`. Re-check with `python -m fusion_swarm.board <verb> --help`.

`wait` returns when the board's wakeup file changes. It uses kqueue where `select.kqueue` exists (macOS, BSD) and polls every 50 ms otherwise; the JSON reports the `mechanism` (`kqueue`, `poll`, `immediate` or `preexisting`). With `--hook-rewake` it exits 2 and prints new messages on stderr when there are some.

A short session (scratch board, trimmed):

```bash
B=/tmp/demo/board.sqlite
python3 fusion.py board --db "$B" init
python3 fusion.py board --db "$B" register --id arch --role architect --provider fable --model fable-5.1
python3 fusion.py board --db "$B" post --from arch --body "hello" --channel hive
python3 fusion.py board --db "$B" poll --agent arch
python3 fusion.py board --db "$B" stats
```

```text
{"ok": true, "db": "/tmp/demo/board.sqlite", "channels": ["hive", "architect", "captains", "workers", "system"]}
{"id": "arch", "display_name": "arch", "role": "architect", "provider": "fable", "model": "fable-5.1", "parent_id": null, ... "status": "online", ...}
{"id": 1, "channel": "hive", "from_agent": "arch", "to_agent": null, "body": "hello", "kind": "message", ... "priority": "normal", ...}
[{"id": 1, "channel": "hive", "from_agent": "arch", "to_agent": null, "body": "hello", ...}]
{"messages": 1, "deliveries": {"pending": 0, "leased": 0, "acked": 0}, "dead_letters": 0, "superseded": 0, "expired_leases": 0}
```

`python -m fusion_swarm.comms` forwards every argument to the same board CLI. Its default database is `$FUSION_BOARD` if set, else `<cwd>/.andrewcode/comms/board.sqlite`. It also takes `--cwd DIR`, `--dir DIR` and `--db PATH` before the verb.

### VBoard: `python -m fusion_swarm.vboard`

```text
usage: python -m fusion_swarm.vboard [--db PATH | --url URL] [--name N] [--as PROFILE] [--json] <verb> [args]

verbs: whoami channels actors read post reply edit delete react label tail wait
       tasks [create|claim|run|renew|release|complete|fail|block|unblock|reopen|assign|needs|cancel]
       serve sweep conformance
Same verbs, flags and --json output as `vercel board`. Claims are kept per --as profile.
```

Global flags: `--db`, `--url`, `--name`, `--as`, `--json`, `--version`, `--help`. `--version` prints `3.3.0+swarm`. `rm` and `del` are aliases of `delete`. These stock `vercel board` verbs are rejected with `not_supported` (HTTP-style status 501): `grants`, `schemas`, `schedule`, `schedules`, `typing`, `sync`, `inbox`, `sources`, `download`, `hooks`, `inspect`. Per-verb flags are in [VBOARD.md](VBOARD.md); the table of record is `VERB_OPTS` in `python/fusion_swarm/vboard/cli.py`. `python -m fusion_swarm.vboard.engine {resolve|status|start|stop|install [--yes]} [--scope NAME] [--port N]` manages an optional external `vb` engine. `install` prints the `npm i` command and runs it only with `--yes`.

## Call peers

`PYTHONPATH=python python3 -m fusion_swarm.peers <command>` (source: `peers/__main__.py`). [PEERS.md](PEERS.md) covers transports and the Codex gate.

| Command | Flags | Notes |
| --- | --- | --- |
| `policy` | none | Route table for this environment as JSON. Offline |
| `route <peer>` | `--session` | One route. Offline. Exits 2 when the route is not allowed |
| `ask <peer>` | `--prompt` or `--prompt-file`, `--model`, `--effort`, `--cwd`, `--tools {none,read,write}` (`none`), `--json-schema`, `--timeout` (600), `--as` | One-shot structured call. Starts the peer CLI |
| `acp <target>` | `--prompt` or `--prompt-file`, `--model`, `--thinking`, `--cwd`, `--permission {deny,allow_read,allow_all}` (`deny`), `--yes-allow-all`, `--timeout` (600), `--as` | One ACP v1 turn (`andrewcode`, `claude` or `codex`). Starts the peer |
| `handoff submit` | `--channel` and `--key` (both required), `--brief` or `--brief-file`, `--payload-file`, `--to` | Durable handoff over a board. `handoff` also takes `--board`, `--as` |
| `handoff status\|get <job>` | | |
| `handoff wait <job>` | `--timeout` (1800) | |
| `handoff serve <cmd>` | `--channel` (required), `--lease` (300), `--wait`, `--max` (1), `--handler-timeout` (600) | Claim jobs and run `<cmd>` |
| `whoami` | `--board`, `--as` | Resolved identity |

```bash
PYTHONPATH=python python3 -m fusion_swarm.peers route codex
```

```text
{
  "allowed": true,
  "binary": "andrewcode",
  "default_model": "gpt-6.1-sol",
  "peer": "codex",
  "reason": "andrewcode seat (codex exec gated)",
  "transport": "andrewcode-oneshot"
}
```

The output depends on the detected host. This run was inside Claude Code, so the host was `claude`. In a Codex host `policy` reports `codex_exec_allowed: true`.

## Read and write the learning ledger

`scripts/ledger.sh` (also `fusion.sh ledger ...`). It stores `runs.jsonl` and `lessons.md` under `$FUSION_MEMORY_DIR`, default `$FUSION_STATE_DIR/memory`, default `~/.fusion/memory`, and seeds `lessons.md` from the shipped `memory/` on first use. Most reporting verbs need `jq`.

| Verb | Purpose |
| --- | --- |
| `record '<json>'` | Append one run record (must be a JSON object). The file is capped at the last 2000 lines |
| `score <provider>` | Bayesian reliability score, `(successes + 3.5) / (successes + failures + 5)` |
| `rank` | Provider, score and sample count for `codex copilot opencode grok agy fable`, tab separated and sorted by score. Providers with fewer than 5 samples show `0.990` |
| `leaderboard [task_type]`, `winners <task_type>` | Qualitative win rates |
| `mark-degraded <prov>`, `clear-degraded` | Edit the session degraded list that `detect` reads |
| `lessons`, `show-lessons` | Regenerate or print `lessons.md` |
| `path` | Print the memory directory |
| `stats` | One-screen summary |
| `wilson-rank [flags]` | Runs `python -m fusion_swarm.ledger_moe --runs-file ...` |

Panelist `status` in a record counts as success when it is `returned` and as failure when it is `absent`, `fallback`, `timeout` or `error`. Example:

```bash
bash scripts/ledger.sh record '{"run_id":"r1","task_type":"code","mode":"panel","judge":"codex","winner":"grok","panelists":[{"provider":"grok","status":"returned"}]}'
bash scripts/ledger.sh score grok
```

```text
0.750
```

`python -m fusion_swarm.ledger_moe` takes `--runs-file`, `--task-type`, `--min-samples` and `--json`.

## Cross-family review

`scripts/ureview.sh` is the plumbing for `/fusion:ultimate-review`. The skill decides; the script makes the run directory, writes two fixed consult prompts and dispatches the reviewer.

| Verb | Purpose |
| --- | --- |
| `init "<task>" [--dir DIR]` | Create the run directory with `task.txt` and `decisions.md`. Prints `RUN_ID`, `RUN_DIR`, `REVIEWER`, `TASK_FILE`, `DECISION_LOG` |
| `plan-review <run_dir> <plan_file> [--round N] [--repo DIR] [--dry-run]` | Audit the plan before code is written |
| `impl-review <run_dir> <summary_file> [--repo DIR] [--dry-run]` | Review the finished implementation |
| `status <run_dir>` | Artifacts and the dispatch manifest |
| `version`, `help` | `fusion-ureview 1.0.0` |

The reviewer is `$FUSION_UREVIEW_REVIEWER` (default `codex`). Run data goes to `$FUSION_UREVIEW_HOME` (default `~/.fusion/ureview`). Exit codes follow the adapter contract plus `3` for a reviewer that is the host runtime.

`ureview.sh` defaults `FUSION_HOST` to `claude` but does not export it. Its child `fusion.sh dispatch` therefore sees an unset variable and defaults to `codex`. With `FUSION_HOST` unset, a `plan-review` to the default reviewer is refused:

```text
[fusion] 'codex' is the host runtime — fold its view in-context, not as a subprocess.
REVIEW=host-conflict reviewer=codex round=1 sec=0
```

The command also prints `PROMPT=`, `OUT=` and `REPO_READ=n/a` lines, omitted here.

Export `FUSION_HOST` yourself before a real review. `tests/test-ureview.sh` does this (`export FUSION_HOST=claude`). `--dry-run` builds the prompt file and stops before dispatch, and works either way.

## Dashboard, TUI, UltraSwarm, UltraCode

**Dashboard.** `bash scripts/dashboard.sh` runs `scripts/dashboard_server.py`, which takes no command-line flags. It binds `127.0.0.1` and starts at port `FUSION_DASHBOARD_PORT` (default 8765), taking the next free port within 50. `FUSION_DASHBOARD_NO_OPEN=1` stops it opening a browser. It prints the URL and the first 8 characters of a session token. [COCKPIT.md](COCKPIT.md) documents the endpoints.

**TUI.** `python -m fusion_swarm tui [--workspace DIR] [--board-db FILE] [--json] [--tmux]` prints a one-shot snapshot. The module's own parser (`python -m fusion_swarm.tui`) names the board flag `--db` instead of `--board-db` and adds `--dry-run` for the tmux command. Example (scratch workspace):

```text
======================================================================
 FUSION TUI — Active-Instance & Diff Viewer
 Workspace: /tmp/demo
 tmux capability: available=True inside_tmux=False
======================================================================
[Active Tasks: 0 | Agents: 0 | Stigmergy Trails: 0]
  (no tasks recorded on board)

[Active-Instance Diff Tab]
(no git repository at workspace)
```

**UltraSwarm.** `python scripts/ultraswarm_selector.py` (or `bash scripts/ultraswarm.sh`) selects OpenCode and Copilot models.

| Flag | Meaning |
| --- | --- |
| `--opencode X`, `--copilot X` | Model id, 1-based index or `default` |
| `--task TEXT` | Task description, skips the prompt |
| `--discover` | Print the discovered or fallback model lists as JSON and exit. No prompt, no writes. Probes the CLIs when they are installed |
| `--non-interactive` | Fail instead of prompting |

With no provider CLIs on `PATH`, `--discover` prints the curated fallback lists, for example `"defaults": {"opencode": "opencode-go/glm-5.2", "copilot": "gemini-3.5-flash"}`.

**UltraCode.** `bash scripts/ultracode.sh <doctor|test|launch|status|install> [args]` bridges to the vendored `ultracode/` checkout. `launch` starts the proxy only when `FUSION_ULTRACODE` is set, stdout is not a terminal, or `--proxy-only` is passed. `test` is described in its usage text as an offline self-test; `doctor` and `install` run UltraCode's own scripts. An unknown command prints the usage and exits 2.

## Install and verify

[GETTING_STARTED.md](GETTING_STARTED.md) and [INSTALL.md](../INSTALL.md) walk through installation. This is the flag reference.

| Entry | Platform | Notes |
| --- | --- | --- |
| `install.sh` | macOS, Linux | Flags below. `install.command` wraps it for Finder and adds a pause unless `--no-pause` |
| `install-all.command`, `install-codex.command`, `install-claude-code.command`, `install-andrewcode.command` | macOS | `install.sh --hosts <all\|codex\|claude\|andrewcode>` |
| `install.ps1` | Windows PowerShell 5.1 | Same options as PascalCase parameters (`-Hosts`, `-DryRun`, ...) plus `-NoPause`. The Bash path is `-GitBash` |
| `install.cmd`, `install.bat`, `install-*.ps1` | Windows | Wrappers around `install.ps1` |
| `scripts/fusion_install.py` | any | Cross-platform core. Subcommands `runtime`, `verify`, `verify-host`, `install` |
| `scripts/install-kimi.py` | any | Installs the slim surface into AndrewCode (Kimi Code) |

`install.sh` flags (source: `install.sh`): `--hosts a,b` (`all`, `andrewcode`, `claude`, `codex` or `none`; default `all`, and `all` or `none` cannot be combined with other values), `--install-root DIR`, `--andrewcode-home DIR`, `--andrewcode-source PATH` (or `ANDREWCODE_SOURCE`), `--python PATH`, `--bash PATH`, `--codex-command`, `--claude-command`, `--andrewcode-command`, `--scope {user,project,local}` (default `user`), `--project-directory DIR`, `--skip-dependencies`, `--skip-jq`, `--no-path-update`, `--no-register`, `--dry-run`, `--no-pause`, `--smoke`, `-h`. An unknown argument exits 2. `--smoke` runs `tests/smoke-providers.sh` after a successful non-dry install, which makes small paid provider calls.

`install.sh --dry-run` changes nothing. With `HOME` set to a scratch directory:

```bash
bash install.sh --dry-run --hosts codex --skip-dependencies
```

```text
Fusion dry run: source=/path/to/agent-swarm-fusion
Install program and fusion command into /tmp/home/Library/Application Support/Fusion
Check Python 3.10+, Bash and jq; install missing dependencies where supported.
Register hosts: codex
Missing Codex/Claude CLIs are installed through their official distributions; AndrewCode uses --andrewcode-source.
AndrewCode home: /tmp/home/.andrewcode; Claude scope: user
SkipDependencies=True; SkipJq=False; NoPathUpdate=False
```

(Paths trimmed.) `fusion_install.py` subcommands:

| Subcommand | Flags |
| --- | --- |
| `install` | `--root` (required), `--hosts` (repeatable), `--install-root`, `--andrewcode-home`, `--andrewcode-source`, `--python`, `--bash`, `--codex-command`, `--claude-command`, `--andrewcode-command`, `--scope {user,project,local}`, `--project-directory`, `--skip-dependencies`, `--skip-jq`, `--no-path-update`, `--no-register`, `--dry-run` |
| `runtime` | `--root`, `--destination` (required), `--python`, `--bash` |
| `verify` | `--root` (required), `--python` |
| `verify-host` | `--root`, `--host {codex,claude}`, `--listing FILE` (required), `--cache`, `--marketplaces`, `--scope` |

There is no `uninstall` subcommand and no `--target`, `--verify` or `--dry-run` at the top level. `install-kimi.py` takes `--root`, `--home`, `--python`, `--bash`, `--dry-run`.

## Low-level provider adapters

`scripts/providers/<name>.sh` are the adapters `fusion.sh dispatch` and `panel` call. They read a prompt file, write the answer to an out file, and exit with the shared code table in [Conventions](#conventions). Usage lines (from each script):

| Adapter | Arguments |
| --- | --- |
| `codex.sh` | `<prompt_file> <out_file> [model] [effort] [sandbox] [workspace]` |
| `copilot.sh` | `<prompt_file> <out_file> [model]` |
| `opencode.sh` | `<prompt_file> <out_file> [model] [variant] [workspace]` |
| `grok.sh` | `<prompt_file> <out_file> [model] [effort]` |
| `kimi.sh` | `<prompt_file> <out_file> [model] [effort] [workspace]` |
| `andrewcode.sh` | `<prompt_file> <out_file> [model] [effort] [workspace]` |
| `agy.sh` | `<prompt_file> <out_file> [model]` |
| `fable.sh` | `<prompt_file> <out_file> [model] [effort]` |

`fusion.sh dispatch` passes `codex.sh` an empty sandbox slot and puts the workspace in the sixth position. Do not call adapters directly when you can call `fusion.sh dispatch`.

`python -m fusion_swarm.andrewcode_cli <prompt_file> <out_file> <model> <effort> [--sandbox S] [--isolated] [--timeout N]` is the transport behind `codex.sh` and `andrewcode.sh`. Without `--isolated` it fails closed (exit 1) or exits 127 when the AndrewCode Node entry is missing. `--timeout` defaults to `FUSION_ANDREWCODE_TIMEOUT`, else 600. `python -m fusion_swarm.process_runtime --execute X` and `python -m fusion_swarm.cli_progress RAW TEXT` are internal helpers (`cli_progress` reads stdin) and are not meant to be called by hand.

## MCP servers

Both are stdio JSON-RPC servers with stdlib-only launchers (`mcps/*/server.py`), configured in `.mcp.json` and `mcp.json`.

| Server | Command | Flags | Tools |
| --- | --- | --- | --- |
| `fusion-coordination` | `python -B mcps/fusion-coordination/server.py --require-board` | `--db PATH`, `--workspace DIR`, `--require-board` (mutually exclusive), `--agent-id ID` | 51 (`tools/list`) |
| `fusion-board` | `python -B mcps/fusion-board/server.py` | `--backend {local,vercel,http}`, `--db PATH`, `--url URL`, `--as NAME` | 24 (`tools/list`) |

`--require-board` is the plugin default: every call must pass an absolute `board_path`. `fusion-board` defaults its backend from `FUSION_VBOARD`, then the engine selection order, and its identity from `BOARD_PROFILE` or `FUSION_AGENT_ID`. The tool counts come from sending `{"jsonrpc":"2.0","id":1,"method":"tools/list"}` to each server on stdin. The tool tables are in [COORDINATION.md](COORDINATION.md) and [VBOARD.md](VBOARD.md).

## Maintenance and test scripts

| Script | Purpose |
| --- | --- |
| `tests/validate.sh` | Package and contract validator. Makes no API calls |
| `tests/test-ureview.sh` | Offline tests for `ureview.sh` (uses `--dry-run`) |
| `tests/smoke-providers.sh` | Live smoke test. Sends a one-word prompt to each available provider and costs money |
| `scripts/benchmark_swarm.py --output FILE [--baseline FILE]` | Times 8 offline scenarios by running specific unit tests, writes a JSON report, refuses to overwrite an existing output file. Makes no provider calls |

See [../CONTRIBUTING.md](../CONTRIBUTING.md) for how the suites are run.

## Commands in older docs that do not exist

The previous README and some skill prose list the following. None of them exist in this repository. The source of truth is the argparse tables above.

| Old claim | What is true |
| --- | --- |
| `fusion.sh discover` | The verb is `detect`. `discover` falls through to the usage text |
| `fusion.sh council`, `verify`, `doctor` | Not verbs. Same fall-through. Council is a skill-driven mode. Diagnostics live in `supervise doctor` and `ultracode.sh doctor` |
| `fusion.py discover`, `route`, `panel`, `council` | Not patterns. `route` and `panel` are `fusion.sh` verbs |
| `swarm.sh reason`, `swarm.sh ultraswarm` | Not patterns. Use `reflexion`, `selfconsist`, `gkp`, and `ultraswarm.sh` |
| `swarm.sh race --competitors ... --test-cmd ...` | No such flags. Use `--providers` and `--gate-cmd`. argparse reports `unrecognized arguments` |
| `swarm.sh harden --waves N --test-cmd ...` | Use `--max-waves` and `--gate-cmd` |
| `swarm.sh evolve --target FILE` | No `--target`. Flags are `--islands`, `--generations`, `--providers`, `--gate-cmd`, `--workspace` |
| `swarm.sh ledger-moe --role worker` | No `--role` |
| `swarm.sh tui --db .fusion/board.db` | The flag is `--board-db`, and the output is a snapshot, not a 4-pane curses UI |
| `board poll --wait 30` | `poll` has no `--wait`. Use the `wait` verb |
| `board stigmergy put\|list` | No `stigmergy` verb. Use `trace-deposit` and `trace-sense` |
| `.fusion/board.db` as the default board | Defaults are `~/.fusion/hive/board.sqlite` (HiveBoard CLI), `.andrewcode/comms/board.sqlite` (`comms`), `<state-dir>/hive/<run_id>/board.sqlite` (hive) |
| `ureview.sh verify-receipt` | Not a verb. Receipt checking is an internal step of `impl-review` and `plan-review` |
| `dashboard_server.py --root .fusion --port 8765` | The server takes no flags. Use `FUSION_DASHBOARD_PORT` |
| `fusion_install.py --target ... --verify --dry-run` | Use the `install`, `verify` subcommands. `--dry-run` belongs to `install` |
| `designer ... --packet`, `--constraints`, `--out` | Not accepted by `python -m fusion_swarm designer` |

## Keeping this page current

Regenerate the facts from the source rather than editing by hand:

```bash
PYTHONPATH=python python3 -m fusion_swarm --help
PYTHONPATH=python python3 -m fusion_swarm.forms --help
PYTHONPATH=python python3 -m fusion_swarm.board --help
PYTHONPATH=python python3 -m fusion_swarm.peers --help
PYTHONPATH=python python3 -m fusion_swarm.vboard --help
python3 scripts/fusion_install.py install --help
grep -n "usage:" scripts/providers/*.sh
```

## Related

- [MODES.md](MODES.md) for what each slash command does
- [FORMS.md](FORMS.md) for the runtime forms behind `fusion_swarm.forms`
- [CONFIGURATION.md](CONFIGURATION.md) for the environment variables named here
- [PEERS.md](PEERS.md) for providers and the Codex gate
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for failures and exit codes in practice
