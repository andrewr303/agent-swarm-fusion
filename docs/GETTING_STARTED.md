# Getting started

This page takes you from a fresh clone to a verified Fusion install and a first run that needs no
model account. Read it if you use Claude Code, Codex CLI or AndrewCode and want the Fusion plugin
(version `3.3.0+swarm`) available in that host. For the full option list of the installers see
[INSTALL.md](../INSTALL.md).

The repository is named `agent-mesh`. The plugin id, the slash commands (`/fusion:*`), the Python
package (`fusion_swarm`), the MCP servers (`fusion-coordination`, `fusion-board`) and the
environment variables (`FUSION_*`) keep the `fusion` name.

## Prerequisites

| Requirement | Needed for | Source |
| --- | --- | --- |
| Python 3.10 or newer with `sqlite3` | Everything. The runtime uses only the standard library, so there is nothing to `pip install`. | `scripts/fusion_install.py` (`find_python` probe asserts `sys.version_info >= (3,10)` and imports `sqlite3`) |
| Bash with `sed` and `awk` | The `scripts/*.sh` entry points and provider adapters. macOS system Bash 3.2 passed the installer's probe. | `find_bash` in `scripts/fusion_install.py` |
| `jq` | The legacy learning ledger. Optional: `--skip-jq` selects a fallback. | `install.sh`, `scripts/fusion_install.py` |
| Homebrew (macOS/Linux) or WinGet (Windows) | Only if you let the installer fetch missing Python, Bash, jq, Node or a host CLI. `--skip-dependencies` turns that off. | `install_posix_package`, `Install-WingetPackage` in `install.ps1` |
| Node.js | Only to build or run AndrewCode. | `install_andrewcode_cli` |
| `git` | Only for worktree-based workspaces, `git diff` summaries and the TUI status. The installer does not call it. | `python/fusion_swarm/workspaces.py`, `race.py`, `tui.py` |

You need at least one host CLI to use the slash commands, and none to follow the offline run below.

Provider CLIs are optional and only matter when you dispatch real work. `scripts/providers/detect.sh`
probes these names with `command -v` and does not run them:

| Provider | Probe | Notes |
| --- | --- | --- |
| `copilot`, `opencode`, `grok`, `agy`, `kimi` | binary on `PATH` | `available` or `missing` |
| `codex`, `andrewcode` | `node` plus the AndrewCode entry script (`FUSION_ANDREWCODE_ENTRY`, else the first that exists of `C:/dev/andrew-code/apps/kimi-code/dist/main.mjs`, `~/dev/andrew-code/apps/kimi-code/dist/main.mjs` and a sibling `andrew-code` checkout) | The legacy `codex` seat runs through AndrewCode, not the `codex` binary (`scripts/providers/codex.sh`). Reported as `degraded` at best. |
| `fable` | `claude` binary | `disabled` unless `FUSION_ALLOW_CLAUDE=1` |

Whichever provider matches the host you run is reported `host-native` and is never launched as a
subprocess. Installing Fusion does not sign you in to anything and makes no model calls.

## Install

The installers copy a slim plugin package to an install root, write launchers, verify the copy, and
register it with each host through that host's own plugin commands.

| OS | Default install root | Entry point |
| --- | --- | --- |
| macOS | `~/Library/Application Support/Fusion` | `./install.command` or `bash install.sh` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/fusion` | `bash install.sh` |
| Windows | `%LOCALAPPDATA%\Fusion` | `.\install-all.ps1` or double-click `install.bat` |

Source: `default_install_root` in `scripts/fusion_install.py`. The Linux path is source-reviewed only.
It was not run on Linux for this page, and dependency auto-install uses Homebrew only.

### Choose the hosts

`install.sh` and `install.ps1` default to all three hosts. Each wrapper pins one selection.

| Host | macOS / Linux | Windows |
| --- | --- | --- |
| All three | `./install-all.command` | `.\install-all.ps1` |
| Claude Code | `./install-claude-code.command` | `.\install-claude-code.ps1` |
| Codex CLI | `./install-codex.command` | `.\install-codex.ps1` |
| AndrewCode | `./install-andrewcode.command` | `.\install-andrewcode.ps1` |

Preview before changing anything. A dry run prints the plan and exits 0 without creating files or
calling a host CLI (run here with `HOME` pointed at a scratch directory, paths elided):

```bash
bash install.sh --dry-run
```

```text
Fusion dry run: source=<checkout>
Install program and fusion command into <home>/Library/Application Support/Fusion
Check Python 3.10+, Bash and jq; install missing dependencies where supported.
Register hosts: andrewcode, claude, codex
Missing Codex/Claude CLIs are installed through their official distributions; AndrewCode uses --andrewcode-source.
AndrewCode home: <home>/.andrewcode; Claude scope: user
SkipDependencies=False; SkipJq=False; NoPathUpdate=False
```

An unknown host name exits 2 with `Unsupported host selection: ['foo']`. `all` and `none` cannot be
combined with other names.

### Install the runtime only

This is the safest first install. It stages and verifies the runtime and registers no host. With
`HOME` pointed at a scratch directory and the install root set explicitly, it produced:

```bash
bash install.sh --no-register --skip-dependencies --skip-jq --no-path-update --install-root /tmp/Fusion
```

```text
Fusion installed: 16 coding forms, 51 coordination tools.
Installation report: /tmp/Fusion/logs/latest-install.json
Fusion installation complete. Open a new terminal to use: fusion forms --json
Sign in to missing host accounts yourself. This installation made no model calls.
```

The install root then contains `bin/` (`fusion`, `fusion.cmd`, `fusion.ps1`), `plugin/` (the slim
package), `runtime.json` (the pinned Python and Bash), `fusion-install.json` (ownership marker) and
`logs/latest-install.json` (the report). Without `--no-path-update` the installer appends
`export PATH="<install-root>/bin:$PATH"` to `~/.zshrc`, and to `~/.bash_profile` and `~/.profile` when
those files exist. On Windows the PowerShell installer edits the User `PATH`.

The package leaves out `ultracode/`, vendor trees, `node_modules` and caches
(`INCLUDE_DIRS` in `scripts/fusion_install.py`). `/fusion:ultracode` and `scripts/ultracode.sh` need
the full checkout. From an installed copy the script exits 2 with `bundled ultracode folder not found`.

### Claude Code

Prerequisite: the `claude` CLI on `PATH`, or let the installer install it (`npm install -g
@anthropic-ai/claude-code` when `npm` exists, otherwise a Homebrew cask).

```bash
./install-claude-code.command            # macOS
bash install.sh --hosts claude           # Linux, or any POSIX shell
```

The installer runs `claude plugin validate`, adds the local `fusion-hive` marketplace pointing at
`<install-root>/plugin`, installs `fusion@fusion-hive` at the chosen scope (`--scope user|project|local`,
default `user`), enables it, and checks that Claude reports the prepared MCP servers. Restart Claude
Code, then run `/fusion:sidekick` or `/fusion:team`. Do not move the install root afterward, because
the marketplace points into it.

Manual registration after a runtime-only install (source-reviewed from `register_claude`, not run):

```bash
claude plugin marketplace add "<install-root>/plugin" --scope user
claude plugin install fusion@fusion-hive --scope user
```

`.claude-plugin/plugin.json` declares no skills, commands or MCP paths. Claude Code reads them from
the plugin root, and the installer verifies that Claude's record of the MCP servers matches `.mcp.json`.

### Codex CLI

Prerequisite: the `codex` CLI on `PATH`, or let the installer install it (`npm install -g @openai/codex`
when `npm` exists, otherwise a Homebrew cask; on Windows the installer downloads and runs the official OpenAI install script).

```bash
./install-codex.command                  # macOS
bash install.sh --hosts codex            # Linux, or any POSIX shell
```

The installer registers `fusion@fusion-hive` through `codex plugin marketplace add` and `codex plugin
add`, then compares the cached copy with the prepared package. Codex honors `CODEX_HOME`. Start a new
chat and invoke `fusion-sidekick` or `fusion-team`. Codex loads `skills/` and the MCP servers from
`.codex-plugin/plugin.json`; it does not load `commands/` as slash commands. See [CODEX.md](../CODEX.md).

If Codex reports `failed to back up plugin cache entry` with `Access is denied`, close Codex completely
and rerun the Codex installer from a separate terminal. The installer restores the prior marketplace
before it exits.

### AndrewCode

AndrewCode is a personal fork and the installer never substitutes upstream Kimi. If it is missing, give
the installer the fork's source tree (it must contain `scripts/install-andrewcode.mjs`):

```bash
./install-andrewcode.command --andrewcode-source ~/dev/andrew-code
```

`ANDREWCODE_SOURCE` works too, and `~/dev/andrew-code` is recognized without a flag. The managed home
is `$ANDREWCODE_HOME`, then `$KIMI_CODE_HOME`, then `$KIMI_HOME`, then `~/.andrewcode`. The installer
copies the package to `<home>/plugins/managed/fusion`, adds a `fusion` record to
`<home>/plugins/installed.json` (other entries are preserved) and writes an inline MCP declaration into
both Kimi manifests. In AndrewCode run `/plugins reload`, then `/new`, then `/fusion:sidekick`.
See [KIMI.md](../KIMI.md).

To register without the dependency steps, use the Python installer directly. A real run into a scratch
home printed:

```bash
python3 scripts/install-kimi.py --home /tmp/ac --python "$(command -v python3)"
```

```text
installed Fusion -> /tmp/ac/plugins/managed/fusion
recorded in /tmp/ac/plugins/installed.json
reload: in AndrewCode / Kimi run /plugins reload then /new
```

Do not use AndrewCode's `/plugins install` on a full checkout. It copies the whole directory including
vendor trees.

### Windows

The Windows path is source-reviewed. The PowerShell installers were not run for this page, and
`tests/test_windows_install.py` skipped 12 of its tests on macOS (the targeted run below).

```powershell
.\install-all.ps1
# or double-click .\install.bat
```

`install.bat` calls `install.cmd`, which starts Windows PowerShell 5.1 with `-ExecutionPolicy Bypass`
and keeps failures visible. The installer needs Python 3.10+, Git for Windows Bash and jq, and installs
whatever is missing through WinGet (`Python.Python.3.14`, `Git.Git`, `jqlang.jq`). Use `-Hosts
andrewcode,claude,codex` to pick hosts, and `-DryRun`, `-SkipDependencies`, `-NoPause` as described in
[INSTALL.md](../INSTALL.md#useful-options).

## Verify the install

1. Read the report. `logs/latest-install.json` under the install root has `"status": "complete"`, the
   verified version and the per-host result. A failed requested host gives a nonzero exit and
   `"status": "failed"`.
2. Run the launcher from a new terminal (or by full path). It lists the 16 native coding forms:

   ```bash
   fusion forms --json
   ```

   Real output, reduced to the form names:

   ```text
   ['sidekick', 'genius-boss', 'idiot-boss', 'dag', 'pipeline', 'relay', 'fan-out', 'hub-spoke', 'map-reduce', 'diamond', 'ring', 'mesh', 'blackboard', 'review-pair', 'builder-breaker', 'council']
   ```

3. Confirm the board CLI loads (`fusion comms` is the workspace-scoped HiveBoard CLI):

   ```bash
   fusion comms --help
   ```

   The first line of real output was `usage: fusion_swarm.board [-h] [--db DB]` followed by the
   subcommand list (`init,register,heartbeat,post,...`).
4. Start a new session in each host and check there. The installer's protocol checks and plugin
   inventory do not prove that a running client loaded the plugin or its MCP servers.

   | Host | Check |
   | --- | --- |
   | Claude Code | `/fusion:setup` |
   | Codex | ask for `fusion setup` |
   | AndrewCode | `/plugins info fusion`, then `/fusion:setup` |

The installer's own tests run offline against fake host CLIs in `tests/fixtures/install_host.py`.
Run them with:

```bash
PYTHONPATH=python python3 -m unittest tests.test_kimi_install tests.test_windows_install
```

Result when run on macOS: `Ran 38 tests ... OK (skipped=12)`.

## First offline run

These commands need no host and no provider account. From a checkout, with a scratch state directory so
nothing lands in `~/.fusion`:

```bash
export PYTHONPATH=python FUSION_STATE_DIR="$(mktemp -d)"
```

`PYTHONPATH=python` is only needed when you run `python3 -m fusion_swarm...` from the checkout. The
installed `fusion` launcher and `scripts/*.sh` set it themselves.

### See which providers Fusion can reach

```bash
bash scripts/fusion.sh detect
```

Output depends on your machine. This is what one machine printed with no `FUSION_HOST` set:

```text
PROVIDER_CHECK_START
claude:missing
codex:host-native
copilot:missing
opencode:available
grok:available
kimi:available
andrewcode:missing
agy:available
fable:disabled
PROVIDER_CHECK_END
```

The shell scripts treat `FUSION_HOST=codex` as the default host. Set `FUSION_HOST=claude` (or `kimi`,
`andrewcode`, `none`) to say which harness you are in, for example `FUSION_HOST=claude bash
scripts/fusion.sh detect` printed `claude:host-native` and `codex:missing`. The Python side also
recognizes Claude Code from `CLAUDECODE=1` (`detect_host` in `python/fusion_swarm/peers/policy.py`).

`fusion_swarm.discovery` (the model-listing probe used by the dashboard and `roster`) runs each
provider CLI with a models subcommand, so it is not part of this offline walkthrough.

### Ask for an advisory mode

```bash
bash scripts/fusion.sh route "refactor the auth module across every file"
```

```text
MODE=swarm
TASK_TYPE=code
REASON=large/parallelizable build — decompose and fan subtasks across panelists
PANEL=copilot,opencode,grok
SIGNALS=code:1 verifiable:0 deliberate:0 debate:0 large:1 graph:0 ureview:0 len:42
LEDGER=none yet
NOTE=advisory default — the Conductor may override based on task nuance + LEDGER
```

`route.sh` is a keyword heuristic. It advises and does not decide.

### Plan a runtime form without dispatching

Two different catalogs share the word "forms". `fusion forms` lists the 16 native team forms used by
Sidekick and `/fusion:team` (see [CODING_TEAMS.md](CODING_TEAMS.md)). `python3 -m fusion_swarm.forms`
handles the 10 executable runtime forms (see [FORMS.md](FORMS.md)).

```bash
python3 -m fusion_swarm.forms list
```

`list` printed one JSON array. Its entries' names were:

```text
['refute_panel', 'tournament', 'hedged_race', 'mapreduce', 'critic_repair', 'pair_swap', 'delphi', 'auction', 'loop_until_dry', 'blackboard']
```

```bash
python3 -m fusion_swarm.forms plan --task "review the auth change" --providers grok,opencode,agy --kind review --verifier
```

```json
{"form": "refute_panel", "params": {"authors": ["grok", "opencode"], "refuters": ["agy"], "k": 2, "max_k": 3}, "width": 4, "budget": {"max_calls": 20}, "ladder": ["single", "hedged_race", "tournament", "refute_panel"], "reasons": ["row 5: high-stakes review -> refute_panel (k=2)"], "capacity": {}, "fallbacks": []}
```

`plan` chooses a form and prints the budget. It calls no provider.

### Dry-run a swarm

```bash
bash scripts/swarm.sh hive "smoke" --dry-run --json
```

The dry run builds a SwarmSpec, seeds the board and prints the plan as JSON, with no AndrewCode
process started. In the run for this page the top-level keys were `agents, board_path, dry_run,
pattern, posts_seeded, run_id, spec, task, tree`, the spec named two captains (`muse` on `muse-spark-1.3`, `glm` on
`glm-5.3`, four children each) and `agents` held 12 entries. The board database was created under
`$FUSION_STATE_DIR/hive/<run_id>/board.sqlite`. See [HIVE.md](HIVE.md).

## Update

Rerun the same installer. It refreshes the managed runtime and host packages and keeps unrelated
plugins. Another installer holding `.fusion-install.lock` makes it stop with `Another installer owns
...`. There is no uninstall script in this repository. Remove the host registration with that host's
plugin commands and delete the install root by hand.

## Where to go next

- [CONCEPTS.md](CONCEPTS.md): how hosts, providers and Fusion fit together and why runs stay honest.
- [MODES.md](MODES.md): every `/fusion:*` command and when to use it.
- [FORMS.md](FORMS.md): the runtime forms, their budgets and salvage.
- [CONFIGURATION.md](CONFIGURATION.md): environment variables and the `.fusion/` state layout.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md): symptoms and fixes when an install or run misbehaves.
