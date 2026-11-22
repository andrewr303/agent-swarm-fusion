# Configuration

This page lists every place Fusion reads configuration from, which layer wins, every environment variable the code reads, and where each module writes state under `.fusion/`. Read it if you want to change a model, point state at another directory, or find out why an override had no effect.

The repository is named `agent-swarm-fusion`. The plugin id, the `/fusion:*` commands, the Python package `fusion_swarm`, the MCP servers and the `FUSION_*` variables keep the `fusion` name. This page documents version `3.3.0+swarm`.

## What exists and what does not

| Item | Real? | Evidence |
| --- | --- | --- |
| `config/defaults.env` | Yes. Shell file, sourced by every provider adapter | [`scripts/providers/_common.sh`](../scripts/providers/_common.sh) `fusion_load_config` |
| `.fusion/config.json` (per-repository config) | **No.** Nothing in `python/`, `scripts/` or `mcps/` reads a file with that name | `grep -rn 'config.json' python scripts mcps` finds only `tsconfig.json` in `gate.py` |
| Plugin options (`/plugin` then configure) | Only as env bridge. The manifests declare no options | [`_common.sh`](../scripts/providers/_common.sh) maps `CLAUDE_PLUGIN_OPTION_*` / `CODEX_PLUGIN_OPTION_*`; `grep -rni userConfig` over manifests finds nothing |
| `ultracode/config.json` | Yes, but it belongs to the vendored UltraCode proxy, not to `fusion_swarm` | `ultracode/config.example.json` (gitignored copy; holds provider keys, never commit it) |

Older documentation described `.fusion/config.json`. It does not exist. Use `config/defaults.env` or environment variables.

## Configuration sources

| Source | Scope | Read by |
| --- | --- | --- |
| Shell environment (`FUSION_*`) | One process tree | Everything |
| [`config/defaults.env`](../config/defaults.env) | Whole install. Path overridable with `FUSION_CONFIG` | Provider adapters (bash) via `_common.sh`; [`config_store.py`](../python/fusion_swarm/config_store.py) (Python) |
| Host plugin options | Whole install, if the host exports them | `fusion_apply_userconfig` in `_common.sh` |
| `FUSION_ANDREWCODE_SELECTION` (JSON) | One process tree | [`model_selection.py`](../python/fusion_swarm/model_selection.py), [`adapter.py`](../python/fusion_swarm/adapter.py) |
| `runtime.json` and generated MCP env | An installed copy | [`scripts/fusion_install.py`](../scripts/fusion_install.py) (see [Installer-managed config](#installer-managed-config-and-backups)) |
| `<state>/panel-allowlist`, `<state>/degraded-providers` | Provider detection | [`scripts/providers/detect.sh`](../scripts/providers/detect.sh), [`scripts/ledger.sh`](../scripts/ledger.sh) |

### `config/defaults.env`

The file header says everything is commented out. That is no longer true: the shipped file activates 26 distinct keys. Count them with:

```bash
PYTHONPATH=python python3 -c "from fusion_swarm import config_store as c; p=c.parse_defaults(); print(len(p), sorted(k for k,v in p.items() if not v['commented']))"
```

Output from the shipped file: 119 distinct keys, 26 active. The 26 active keys are the model and effort seats (`FUSION_CODEX_*`, `FUSION_FABLE_*`, `FUSION_ANDREWCODE_*`, `FUSION_OPENCODE_MODEL`, `FUSION_OPENCODE_VARIANT`, `FUSION_GROK_MODEL`, `FUSION_KIMI_MODEL`, `FUSION_COPILOT_MODEL`, `FUSION_AGY_MODEL`), the MetaLoop worker and operator keys, the Hive limit keys, `FUSION_ANDREWCODE_AUTO`, `FUSION_ARCHITECT_*`, `FUSION_ASTRA_MODEL` and two `FUSION_GRAPH_*_MODEL` keys.

Line forms the parser accepts ([`config_store.py`](../python/fusion_swarm/config_store.py) `_LINE_RE`): `export FUSION_X="value"` and the same line behind `#`. Uncommenting a line is how you override a value.

Two kinds of active line behave differently when the file is sourced:

| Line form | Example | Effect on an existing exported variable |
| --- | --- | --- |
| Self-referential default | `export FUSION_CODEX_MODEL="${FUSION_CODEX_MODEL:-gpt-5.6-sol}"` | Keeps your value |
| Unconditional | `export FUSION_COPILOT_MODEL="gpt-5.6-sol"` | **Overwrites your value** |

Three shipped lines are unconditional: `FUSION_COPILOT_MODEL` (line 18), `FUSION_AGY_MODEL` (line 54) and `FUSION_FABLE_EFFORT` (line 59). Verified in a scratch `HOME`:

```bash
FUSION_FABLE_EFFORT=low FUSION_COPILOT_MODEL=mine FUSION_CODEX_MODEL=mine2 \
  bash -c '. scripts/providers/_common.sh; echo fable_effort=$FUSION_FABLE_EFFORT copilot=$FUSION_COPILOT_MODEL codex=$FUSION_CODEX_MODEL agy=$FUSION_AGY_MODEL'
```

```text
fable_effort=xhigh copilot=gpt-5.6-sol codex=mine2 agy=gemini-3.6-flash-high
```

To make those three respect the environment, rewrite them as `${KEY:-value}` lines or comment them out.

### Precedence for a provider's model and effort

The header of `defaults.env` and the code agree on this order for the bash adapters (`scripts/providers/<name>.sh`):

1. Positional argument to the adapter (`fusion.sh dispatch <provider> <pf> <of> [model] [effort]`).
2. Environment after `defaults.env` is sourced. Shell exports win over `${KEY:-...}` and commented lines, but lose to the unconditional lines above.
3. Host plugin option variables, applied only if the `FUSION_*` variable is still empty.
4. The default baked into the adapter script.

`FUSION_CONFIG` replaces the path of the sourced file. A scratch test with a one-line file showed the replacement takes effect and the shipped file is not read:

```bash
printf 'export FUSION_GROK_MODEL="alt"\n' > "$S/alt.env"
FUSION_CONFIG="$S/alt.env" bash -c '. scripts/providers/_common.sh; echo grok=$FUSION_GROK_MODEL codex=${FUSION_CODEX_MODEL-unset}'
```

```text
grok=alt codex=unset
```

Plugin options are mapped for these keys only: `codex_model`, `codex_effort`, `copilot_model`, `opencode_model`, `opencode_variant`, `grok_model`, `andrewcode_model`, `andrewcode_effort`, `fable_model`, `fable_effort`, `agy_model`. The variable name is `CLAUDE_PLUGIN_OPTION_<key>` (also tried upper-cased) or `CODEX_PLUGIN_OPTION_<key>`. Every one of those keys has an active line in the shipped `defaults.env`, so the variable is never empty when step 3 runs and **plugin options have no effect with the shipped file** (verified: `CLAUDE_PLUGIN_OPTION_grok_model=plug` still gave `grok-4.5`). Comment the line out to let a plugin option through.

### Per-mode overrides (Python layer)

A single `/fusion:<mode>` can override a provider's model or effort without changing the global keys. The key is `FUSION_<PATTERN>_<PROVIDER>_<MODEL|EFFORT>` ([`adapter.mode_override_key`](../python/fusion_swarm/adapter.py)). `<PATTERN>` is the engine pattern of the mode, not always the command name: `/fusion:reason` uses `REFLEXION`.

Resolution in `adapter.dispatch`:

1. An explicit non-empty `model` or `effort` from the caller.
2. An **active** per-mode override: a non-empty value from the environment, or from an uncommented `defaults.env` line. A commented or empty line never masks the global value.
3. Otherwise both stay empty and the bash adapter resolves the value as in the previous section.

The invoked mode travels in `FUSION_INVOKED_MODE`, which `adapter.set_invoked_mode` writes into `os.environ` so worker threads see it. Tests: [`tests/test_mode_overrides.py`](../tests/test_mode_overrides.py).

The catalog defines 72 per-mode keys, derived from `modes_catalog.per_mode_override_specs()`. Recount with:

```bash
PYTHONPATH=python python3 -c "from fusion_swarm import modes_catalog as m; print(len(m.per_mode_override_specs()))"
```

| Pattern (command) | Providers with a `MODEL` key | Providers that also get `EFFORT` |
| --- | --- | --- |
| `moa`, `heavy`, `discuss`, `flow`, `graph`, `ballot`, `refine`, `speclock`, `reflexion` (`reason`) | codex, copilot, opencode, grok | codex, copilot, opencode |
| `bestof` | codex | codex |
| `breaker` | codex, grok | codex |
| `ladder` | opencode, codex | opencode, codex |

`metaloop`, `hive` and `factory` have no per-mode keys: they do not dispatch through `adapter.dispatch` ([`modes_catalog.PER_MODE_UNENFORCEABLE`](../python/fusion_swarm/modes_catalog.py)). Only codex, opencode, copilot and fable accept an effort (`EFFORT_CAPABLE_PROVIDERS`). Grok rejects effort and Antigravity (`agy`) encodes effort in the model name.

### `FUSION_ANDREWCODE_SELECTION`

When this variable is set, `adapter.dispatch` skips the bash adapters and uses AndrewCode for every seat. The value is JSON with `version: 1`, a `participants` list (1 to 64 entries) and an optional `roles` map. Each binding needs a non-empty `model` and an `effort` from `off, on, none, minimal, low, medium, high, xhigh, max, ultra` ([`model_selection.py`](../python/fusion_swarm/model_selection.py)). A set but invalid value fails closed: no seats, an error on dispatch, and the value is never echoed. Each `roles` entry must use a model that is also in `participants`. There is no fallback to a built-in model.

### Inspecting the effective value

[`config_store.effective_value(key)`](../python/fusion_swarm/config_store.py) reports `env`, `defaults.env` (or `defaults.env (baked default via ${VAR:-default})`), `baked_default` for a commented line, or `unknown`. It cannot see plugin options and says so in its `caveat` field. It also reports `env` for the three unconditional keys even though bash would overwrite the exported value, so treat it as advisory for those.

`fusion.sh providers` prints the models a panel would use from the same file. Output from a scratch `HOME` with the shipped config:

```text
Fusion default panel (orchestrator = in-context host: $FUSION_HOST=codex):
  codex       gpt-5.6-sol                      effort xhigh     (OpenAI — dispatchable unless FUSION_HOST=codex)
  kimi        azure/kimi-k3                                     (Moonshot)
  opencode    meta/muse-spark-1.2-contributor  variant xhigh    (OpenCode)
  grok        grok-4.5                         web-search on    (xAI)
  copilot     gpt-5.6-sol                                       (GitHub Copilot)
  andrewcode  meta/muse-spark-1.3-contributor  effort xhigh     (AndrewCode — nested hive workers)
```

The `$FUSION_HOST=codex` is the bash default, not detection. See [Host variables](#host-and-policy).

### Defaults differ between layers

The same seat has different fallback models in different files. With the shipped `defaults.env` sourced, the `defaults.env` column wins for dispatch. The other columns apply only when the line is commented out or `FUSION_CONFIG` points elsewhere.

| Seat | Adapter script default | `defaults.env` | `adapter.PROVIDER_META` |
| --- | --- | --- | --- |
| codex model | `gpt-6.1-sol` ([`codex.sh`](../scripts/providers/codex.sh):14) | `gpt-5.6-sol` | `gpt-5.6-sol` ([`adapter.py`](../python/fusion_swarm/adapter.py):95) |
| copilot model | `gemini-3.5-flash` ([`copilot.sh`](../scripts/providers/copilot.sh):18) | `gpt-5.6-sol` | `gemini-3.5-flash` |
| opencode model | `opencode-go/glm-5.2` ([`opencode.sh`](../scripts/providers/opencode.sh):15) | `meta/muse-spark-1.2-contributor` | `opencode-go/glm-5.2` |
| opencode variant | `high` | `xhigh` | not in `PROVIDER_META` |
| agy model | `Gemini 3.1 Pro (High)` ([`agy.sh`](../scripts/providers/agy.sh):24) | `gemini-3.6-flash-high` | `Gemini 3.5 Flash (High)` |
| grok, kimi, fable, andrewcode | same value in all layers | same | same |

`PROVIDER_META` values feed rosters and display (`mode_roster` uses them as a last resort). Dispatch hands an empty model to bash, so the adapter script decides. The `codex` seat is an AndrewCode compatibility seat: `codex.sh` runs [`andrewcode_cli.py`](../python/fusion_swarm/andrewcode_cli.py), never `codex.exe`.

### Editing `defaults.env` safely

[`config_store.set_value`](../python/fusion_swarm/config_store.py) is what the dashboard Swarm Modes tab uses. It:

- accepts only keys already present in the file (hard allowlist) and rejects values containing `"`;
- edits every occurrence of the key, keeps the `${KEY:-...}` form where a line used it, and preserves line endings (LF or CRLF);
- writes a copy to `config/.backups/defaults.env.<YYYYmmdd-HHMMSS>.bak` and then replaces the file atomically (temp file plus `os.replace`).

Backups are never pruned by the code. Editing by hand is equally valid. The file is installed with the plugin, so an update of the plugin directory replaces your edits (see [installer behaviour](#installer-managed-config-and-backups)). Put durable overrides in the shell environment.

## Environment variable reference

The tables list every variable the code reads or sets, grouped by purpose. Source column is a representative `file:line`. Regenerate the raw name list with:

```bash
grep -rhoE '\b(FUSION|ANDREWCODE|BOARD)_[A-Z0-9_]+' python scripts mcps fusion.py --exclude-dir=__pycache__ | sort -u
```

That command returns 119 names. Sixteen of them are not environment variables: the constants `ANDREWCODE_SH`, `BOARD_POLL_CMD`, `BOARD_SCHEMA_VERSION`, `FUSION_CHAT_V1`, `FUSION_EXTENSION`, `FUSION_LABELS`, `FUSION_MODEL_SELECTION_VERSION`, `FUSION_ROOT`, `FUSION_SH`, `FUSION_VERSION`, the docstring placeholder `FUSION_X`, and the comment prefixes `FUSION_HEAVY_`, `FUSION_METALOOP_`, `FUSION_META_`, `FUSION_MOA_`, `FUSION_REFLEXION_`. Variables from other ecosystems appear below only where Fusion reads them (`CLAUDECODE`, `CODEX_HOME`, `XDG_*`, and similar). Defaults marked "unset" mean the code treats absence as a distinct case.

### Provider seats (model, effort, timeout)

Defaults here are the adapter-script values. See [Defaults differ between layers](#defaults-differ-between-layers) for the shipped overrides.

| Variable | Default | Effect | Source |
| --- | --- | --- | --- |
| `FUSION_CODEX_MODEL` | `gpt-6.1-sol` | Model for the `codex` seat | `scripts/providers/codex.sh:14` |
| `FUSION_CODEX_EFFORT` | `xhigh` | Effort for the `codex` seat | `codex.sh:15` |
| `FUSION_CODEX_SANDBOX` | `workspace-write` | Requested sandbox, passed to `andrewcode_cli.py --sandbox`. `danger-full-access` is refused; `workspace-write` is noted and ignored | `codex.sh:16`, `andrewcode_cli.py:228` |
| `FUSION_COPILOT_MODEL` | `gemini-3.5-flash` | Copilot model | `copilot.sh:18` |
| `FUSION_COPILOT_EFFORT` | unset | Copilot reasoning effort, applied only when non-empty | `copilot.sh:50` |
| `FUSION_COPILOT_TIMEOUT` | `600` s | Wall-clock timeout | `copilot.sh:19` |
| `FUSION_OPENCODE_MODEL` | `opencode-go/glm-5.2` | OpenCode model | `opencode.sh:15` |
| `FUSION_OPENCODE_VARIANT` | `high` | OpenCode `--variant` (its effort knob) | `opencode.sh:16` |
| `FUSION_OPENCODE_TIMEOUT` | `600` s | Wall-clock timeout | `opencode.sh:17` |
| `FUSION_GROK_MODEL` | `grok-4.5` | Grok model. The comment in `grok.sh` says an empty value selects the CLI default, but `:-` treats empty as unset, so `grok-4.5` is used | `grok.sh:16` |
| `FUSION_GROK_EFFORT` | unset | Passed through if set. The `defaults.env` note says grok-build rejects effort (HTTP 400) | `grok.sh:19` |
| `FUSION_GROK_TIMEOUT` | `600` s | Wall-clock timeout | `grok.sh:20` |
| `FUSION_KIMI_MODEL` | `azure/kimi-k3` | Kimi model | `kimi.sh:28` |
| `FUSION_KIMI_TIMEOUT` | `900` s | Wall-clock timeout | `kimi.sh:31` |
| `FUSION_AGY_MODEL` | `Gemini 3.1 Pro (High)` | Antigravity model (effort is part of the name) | `agy.sh:24` |
| `FUSION_AGY_TIMEOUT` | `600` s | Wall-clock timeout | `agy.sh:25` |
| `FUSION_FABLE_MODEL` | `fable-5.1` | Fable (Claude Code print mode) model | `fable.sh:26` |
| `FUSION_FABLE_EFFORT` | `xhigh` | Fable effort | `fable.sh:27` |
| `FUSION_FABLE_TIMEOUT` | `900` s | Wall-clock timeout | `fable.sh:28` |
| `FUSION_ALLOW_CLAUDE` | unset | Must be `1` to dispatch Fable (Claude Code) and to let a non-codex host spawn `claude` | `fable.sh:16`, `discovery.py:317`, `peers/policy.py:58` |
| `FUSION_ANDREWCODE_MODEL` | `meta/muse-spark-1.3-contributor` | Worker model | `andrewcode.sh:11`, `hive.py:38` |
| `FUSION_ANDREWCODE_EFFORT` | `xhigh` | Worker effort | `andrewcode.sh:12`, `spawn.py:521` |
| `FUSION_ANDREWCODE_TIMEOUT` | `600` s | Default of `andrewcode_cli.py --timeout` | `andrewcode_cli.py:220` |
| `FUSION_ANDREWCODE_ENTRY` | auto-detected | Path to AndrewCode's `main.mjs`. Default search order is in [`andrewcode_cli._default_entry`](../python/fusion_swarm/andrewcode_cli.py) (Windows default `C:/dev/andrew-code/apps/kimi-code/dist/main.mjs`; override this with the variable) | `andrewcode_cli.py:24`, `detect.sh:35` |
| `FUSION_ANDREWCODE` | `node` on `PATH` | Node executable for AndrewCode. Must name Node, not a shim | `spawn.py:155` |
| `FUSION_ANDREWCODE_SELECTION` | unset | JSON seat roster (see above) | `model_selection.py:14` |
| `FUSION_<PATTERN>_<PROVIDER>_<MODEL\|EFFORT>` | unset | Per-mode override | `adapter.py:242` |
| `FUSION_INVOKED_MODE` | set by Fusion | Marker for the running engine pattern. Do not set by hand | `adapter.py:216` |
| `CLAUDE_PLUGIN_OPTION_<key>`, `CODEX_PLUGIN_OPTION_<key>` | unset | Host plugin options bridged to `FUSION_*` | `_common.sh:23` |

### Provider scratch and snapshots (MetaLoop workers)

| Variable | Default | Effect | Source |
| --- | --- | --- | --- |
| `FUSION_WORKER_REPO` | unset | Repo a MetaLoop worker snapshots for read context. Set by `fusion.sh dispatch` when a workspace argument is given | `fusion.sh:75`, `_common.sh:230` |
| `FUSION_SNAPSHOT_MAX_FILES` | `4000` | File-count cap for the snapshot | `_common.sh:162` |
| `FUSION_SNAPSHOT_MAX_FILE_KB` | `512` | Per-file size cap | `_common.sh:163` |
| `FUSION_PROMPT_INLINE_MAX` | `16000` | Prompts larger than this bytes are written to `.fusion-task.md` in the worker workspace | `_common.sh:245` |
| `FUSION_CONFIG` | `<root>/config/defaults.env` | Alternate config file sourced by adapters | `_common.sh:40` |

### Host and policy

| Variable | Default | Effect | Source |
| --- | --- | --- | --- |
| `FUSION_HOST` | `codex` in `fusion.sh`, `route.sh`, `detect.sh`; `claude` in `ureview.sh` | Declares the host runtime. A provider equal to the host is never dispatched as a subprocess (exit 3) | `fusion.sh:38`, `route.sh:92`, `detect.sh:58`, `ureview.sh:32` |
| `FUSION_HOST_EXPLICIT` | unset | `1` stops Claude markers from overriding `FUSION_HOST=codex` | `peers/policy.py:43` |
| `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` | set by Claude Code | Markers that make Python resolve the host as `claude` | `peers/policy.py:41` |
| `FUSION_ALLOW_CODEX_EXEC` | unset | `1` opens the gate for spawning the `codex` binary from a non-codex host | `peers/policy.py:53`, `peers/exec_json.py:404` |
| `FUSION_PEER_DEPTH` | `0` | Current peer nesting depth. Incremented in each child | `peers/identity.py:86` |
| `FUSION_PEER_MAX_DEPTH` | `2` | Refuse a spawn deeper than this (`depth_exceeded`) | `peers/identity.py:87` |
| `FUSION_PEERS_KEEP_RAW` | unset | `1` keeps raw peer JSONL under `<FUSION_HOME>/peers/raw/` | `peers/exec_json.py:635` |
| `FUSION_UREVIEW_REVIEWER` | `codex` | Reviewer family for `ureview.sh`. Must differ from the host | `ureview.sh:33` |
| `FUSION_UREVIEW_HOME` | `~/.fusion/ureview` | State dir for `ureview.sh` | `ureview.sh:34` |

Host resolution differs by layer. Python (`peers/policy.detect_host`, `adapter._resolve_host`) accepts `claude`, `codex`, `andrewcode`, `kimi` or `unknown`, lets Claude markers beat an inherited `FUSION_HOST=codex`, and falls back to `FUSION_HOST` or `codex` when still unknown. The bash scripts do not look at Claude markers. On a Claude Code host, export `FUSION_HOST=claude` before using `fusion.sh` verbs, otherwise they treat `codex` as host-native and `claude` as available. `child_env` pins the resolved host into `FUSION_HOST` and then strips the Claude markers for children (`peers/identity.py:102`).

### Locations and state

| Variable | Default | Effect | Source |
| --- | --- | --- | --- |
| `FUSION_STATE_DIR` | `~/.fusion` | Hive boards, model cache, ledger memory, `degraded-providers`, `panel-allowlist`, ultraswarm | `board.py:1076`, `discovery.py:39`, `ledger.sh:35`, `detect.sh:18`, `ultraswarm_selector.py:28`, `hive.py:128` |
| `FUSION_HOME` | `~/.fusion` | Forms runs and reliability ledger, VBoard databases and engine, CLI-help cache, peer raw output | `forms/runtime.py:29`, `vboard/port.py:1151`, `peers/exec_json.py:382` |
| `FUSION_MEMORY_DIR` | `<FUSION_STATE_DIR>/memory` | Run ledger and lessons used by `ledger.sh` | `ledger.sh:36` |
| `FUSION_MEMORY_ROOT` | `~/.fusion/memory` | Root of the scoped memory service | `memory_service.py:36` |
| `FUSION_GRAPH_HOME` | `~/.fusion/graph` | Knowledge-graph store root | `kgraph.py:78` |
| `FUSION_LEDGER_PATH` | `<FUSION_HOME>/ledger/reliability.json` | Reliability ledger file for runtime forms | `forms/ledger.py:33` |
| `FUSION_ALLOWLIST` | `<FUSION_STATE_DIR>/panel-allowlist` | If the file exists, only listed provider names are probed as available | `detect.sh:20` |
| `FUSION_BOARD` | unset; falls back to `<FUSION_STATE_DIR>/hive/board.sqlite` | HiveBoard path. Python `comms.py` falls back to `<cwd>/.andrewcode/comms/board.sqlite` instead | `board.py:1073`, `comms.py:15` |
| `XDG_CONFIG_HOME` | `~/.config` | Base of VBoard claim cache and loopback token files | `vboard/claims.py:28`, `vboard/http.py:260` |
| `XDG_DATA_HOME`, `LOCALAPPDATA` | platform | Default installer root on Linux / Windows | `fusion_install.py:451-455` |

`FUSION_STATE_DIR` and `FUSION_HOME` are independent variables that both default to `~/.fusion`. Setting one does not move the other's files. Likewise `FUSION_MEMORY_DIR` (bash ledger) and `FUSION_MEMORY_ROOT` (Python memory service) are separate and share the default directory.

### Interpreters and tool paths

| Variable | Default | Effect | Source |
| --- | --- | --- | --- |
| `FUSION_PYTHON` | first of `python`, `py`, `python3` in `board.sh`, `hive.sh`, `swarm.sh` (and `python` in provider adapters); `FUSION_PYTHON`, `python3`, `python`, `py` for the JSON helper | Python interpreter for wrappers and adapters | `scripts/board.sh:9`, `_common.sh:75` |
| `FUSION_BASH` | Git Bash on Windows, else `bash` on `PATH` | Bash used by Python to call `fusion.sh` | `adapter.py:292`, `spawn.py:134` |
| `FUSION_VBOARD_ENGINE` | unset | Path to a `vb` binary or `cli.mjs` | `vboard/engine.py:113` |
| `FUSION_VBOARD_ALLOW_NPX` | unset | `1` lets the engine resolver run `npx -y @vercel/board@0.54.0` | `vboard/engine.py:134` |

### Identity and board wiring

Fusion exports these to child processes (`fusion_export_identity` in `_common.sh`, `peers/identity.identity_env`). Workers read them to find their board and name.

| Variable | Effect | Source |
| --- | --- | --- |
| `FUSION_AGENT_ID` | Agent name. Used as the board profile when `BOARD_PROFILE` is unset | `vboard/cli.py:489` |
| `BOARD_PROFILE` | Board profile. Wins over `FUSION_AGENT_ID` | `vboard/cli.py:487`, `port.py:1624` |
| `FUSION_PARENT_ID`, `FUSION_SWARM_ID`, `FUSION_RUN_ID` | Lineage and run identity passed to children | `peers/identity.py:69` |
| `BOARD_TASK` | Task id of the work item a child is running | `peers/identity.py:70`, `vboard/cli.py:1007` |
| `FUSION_VBOARD` | Backend spec: `local`, `local:<path>`, `http(s)://...`, `vercel`, `vercel:<name>`. Default `local` | `vboard/port.py:1633` |
| `FUSION_VBOARD_DB` | SQLite path for the VBoard CLI. Default `<FUSION_HOME>/vboard/<name>.db` | `vboard/cli.py:479` |
| `BOARD_NAME` | Board name for the VBoard CLI and Vercel backend. Default `default` | `vboard/cli.py:473` |
| `BOARD_URL` | Board HTTP URL. Used when no `--db` and no `FUSION_VBOARD_DB` | `vboard/cli.py:474` |
| `BOARD_TOKEN` | Bearer token for the HTTP adapter. Never logged. Otherwise the loopback token file is used | `vboard/port.py:774` |
| `FUSION_VBOARD_CONFORMANCE_TARGET` | empty or `local` (default), or `vercel` | `vboard/cli.py:1194` |
| `FUSION_VBOARD_CONFORMANCE_CHANNEL` | Required throwaway channel for the `vercel` target | `vboard/cli.py:1195` |
| `FUSION_VBOARD_CONFORMANCE_URL` | Opt-in HTTP board URL for the conformance suite | `vboard/cli.py:1196` |
| `FUSION_MAIL_MAX_ATTEMPTS` | Mailbox redelivery budget. Default `8`. Invalid or below 1 gives `8` | `mailbox.py:49` |

See [`VBOARD.md`](VBOARD.md) and [`COORDINATION.md`](COORDINATION.md) for what the boards do.

### MetaLoop

Read by `MetaLoopConfig.from_env` ([`metaloop.py`](../python/fusion_swarm/metaloop.py):144). Defaults below are the code defaults. The dashboard schema documents the same keys.

| Variable | Default | Effect |
| --- | --- | --- |
| `FUSION_META_ADVISOR_POLICY` | `always` | `off`, `on-demand`, `auto` or `always` (per the `defaults.env` comment) |
| `FUSION_META_MAX_ADVISOR_CALLS` | `2` | Cap on advisor consults per run |
| `FUSION_META_MAX_REPAIR_ROUNDS` | `2` | Repair rounds |
| `FUSION_META_MAX_ATTEMPTS_PER_TASK` | `3` | Attempts per task |
| `FUSION_META_EXPERT_CONCURRENCY` | `2` | Concurrent expert-tier calls |
| `FUSION_META_FAST_CONCURRENCY` | `2` | Concurrent fast-tier calls |
| `FUSION_META_MAX_CALLS` | `12` | Total external calls |
| `FUSION_META_MAX_WALL_SECONDS` | `1800` | Wall-clock budget |
| `FUSION_META_WORKSPACE_MODE` | `proposal` | Worker workspace mode (`worktree` is marked Phase 2 in `defaults.env`) |

### Dashboard, discovery, graph and miscellaneous

| Variable | Default | Effect | Source |
| --- | --- | --- | --- |
| `FUSION_DASHBOARD_PORT` | `8765` | Preferred port. Tries the next 49 ports if busy | `dashboard_server.py:394` |
| `FUSION_DASHBOARD_NO_OPEN` | unset | `1` skips opening a browser | `dashboard_server.py:400` |
| `FUSION_LEAD`, `FUSION_RUN` | unset | With `FUSION_BOARD`, bind the dashboard to a prepared execution run | `dashboard_server.py:186` |
| `FUSION_WORKSPACE` | unset | Workspace for the dashboard diff view | `dashboard_server.py:205` |
| `FUSION_MODEL_CACHE_TTL` | `3600` s | Model-discovery cache lifetime on success | `discovery.py:42` |
| `FUSION_MODEL_CACHE_NEG_TTL` | `300` s | Lifetime for degraded or missing results | `discovery.py:43` |
| `FUSION_DISCOVERY_PROBE_TIMEOUT` | `10` s | Per-probe timeout | `discovery.py:44` |
| `FUSION_GRAPH_APPROVED` | empty | Comma-separated node ids or labels the graph scheduler treats as approved | `graph.py:646` |
| `FUSION_PROGRESS_ID`, `_ROLE`, `_MODEL`, `_EFFORT` | id: generated; role: `worker` | Enable and label `FUSION_CHAT_V1` progress packets from AndrewCode runs | `andrewcode_cli.py:160`, `cli_progress.py:23` |
| `FUSION_ULTRACODE` | unset | Any value makes `ultracode.sh launch` start the proxy only | `ultracode.sh:46` |
| `PLANO_SESSION` | unset | `plano` marks a Plano session in `lanes.py` | `lanes.py:118` |
| `TMUX` | unset | Used by the TUI to detect tmux | `tui.py:24` |

### Written by Fusion, not read as settings

| Variable | Where it is set | Source |
| --- | --- | --- |
| `FUSION_CANDIDATE_ID`, `FUSION_CANDIDATE_PROVIDER` | Verifier subprocess environment in runtime forms | `forms/runtime.py:348` |
| `FUSION_ULTRASWARM_SELECTION`, `FUSION_ULTRASWARM_TASK_FILE` | Lines in `<FUSION_STATE_DIR>/ultraswarm/last_selection.env` | `ultraswarm_selector.py:106` |

### Test hooks

| Variable | Effect | Source |
| --- | --- | --- |
| `FUSION_TEST_HOOKS` | Must be `1` for the two hooks below to apply | `forms/__main__.py:237`, `spawn.py:699` |
| `FUSION_FORMS_FAKE_DISPATCH` | Path to a fake dispatcher for `python -m fusion_swarm.forms` | `forms/__main__.py:236` |
| `FUSION_SH_OVERRIDE` | Replace `fusion.sh` in `spawn.py` | `spawn.py:698` |

### Installer-only variables

| Variable | Effect | Source |
| --- | --- | --- |
| `ANDREWCODE_HOME`, `KIMI_CODE_HOME`, `KIMI_HOME` | AndrewCode home, checked in this order. Default `~/.andrewcode` | `fusion_install.py:459-461`, `install-kimi.py:15` |
| `ANDREWCODE_SOURCE` | Source checkout for the AndrewCode install step | `install.sh:30` |
| `CODEX_HOME` | Codex home used to verify its plugin cache. Default `~/.codex` | `fusion_install.py:767` |

### Inert keys

These keys appear in `defaults.env` but no code under `python/`, `scripts/` or `mcps/` reads them as settings:

| Key | Note |
| --- | --- |
| `FUSION_HIVE_MAX_DEPTH`, `FUSION_HIVE_MAX_CHILDREN`, `FUSION_HIVE_MAX_AGENTS` | Hive limits come from `SpawnLimits` defaults (2, 4, 24) or the SwarmSpec ([`identities.py`](../python/fusion_swarm/identities.py):152, `hive.py:547`). `tests/test_hive_wiring.py` only asserts the lines exist |
| `FUSION_ANDREWCODE_AUTO` | Printed by `fusion.sh providers`. `andrewcode.sh` states it does not bypass its safety gate |
| `FUSION_ARCHITECT_PROVIDER`, `FUSION_ARCHITECT_MODEL`, `FUSION_ASTRA_MODEL` | Only the dashboard schema reads them |
| `FUSION_METALOOP_WORKER_PROVIDER`, `_MODEL`, `_EFFORT`, `FUSION_META_MAX_OPERATOR_CALLS`, `FUSION_META_OPERATOR_POLICY` | No code reader (the dashboard schema lists the policy key) |
| `FUSION_CODEX_TIMEOUT` | The `codex` seat runs through AndrewCode and `codex.sh` has no timeout variable |
| `FUSION_GRAPH_KIMI_MODEL`, `FUSION_GRAPH_AGY_MODEL` | Named by [`skills/fusion-graph/SKILL.md`](../skills/fusion-graph/SKILL.md):75 for the host to read; no code reader |

## State layout

Defaults assume neither `FUSION_STATE_DIR` nor `FUSION_HOME` is set. The first column shows which variable moves the path.

| Path | Created by | Contents |
| --- | --- | --- |
| `$FUSION_STATE_DIR/hive/board.sqlite` | `board.py` (`default_board_db`) | Default HiveBoard when `FUSION_BOARD` is unset. Created by `python -m fusion_swarm.board init` (verified) |
| `$FUSION_STATE_DIR/hive/<hive-id>/board.sqlite` | `hive.py` (`prepare`, run id `hive-<10 hex>`) | Per-run Hive board and workdir |
| `~/.fusion/hive/<session_id>/{inbox/,workcards/,swarm.jsonl,hive.db}` | `lanes.py` | Communication lanes. Does not honour `FUSION_STATE_DIR` |
| `$FUSION_STATE_DIR/model-cache.json` | `discovery.py` | Cached provider and model probes |
| `$FUSION_STATE_DIR/memory/{runs.jsonl,lessons.md}` | `scripts/ledger.sh` | Run ledger. `lessons.md` is seeded from `memory/lessons.md` in the plugin on first use (verified by `ledger.sh stats`) |
| `$FUSION_STATE_DIR/degraded-providers` | `ledger.sh`, read by `detect.sh` | Session degraded list |
| `$FUSION_STATE_DIR/panel-allowlist` | You (never created) | One provider name per line, read by `detect.sh` |
| `$FUSION_STATE_DIR/ultraswarm/` | `ultraswarm_selector.py` | `last_selection.json`, `last_selection.env`, `last_task.txt` |
| `$FUSION_HOME/forms/runs/<run_id>/` | `forms/runtime.py`, `forms/journal.py` | `journal.jsonl`, `checkpoint.json`, `result.json`, `outputs/` |
| `$FUSION_HOME/ledger/reliability.json` | `forms/ledger.py` | Provider reliability ledger (or `FUSION_LEDGER_PATH`) |
| `$FUSION_HOME/vboard/<name>.db` | `vboard/cli.py`, `vboard/port.py` | Local VBoard database. `default.db` plus `-wal` and `-shm` (verified) |
| `$FUSION_HOME/board/<scope>/engine.json` | `vboard/engine.py` | State of a running `vb` engine per scope |
| `$FUSION_HOME/engine/node_modules/@vercel/board/` | You (never installed by Fusion) | Optional engine location searched by the resolver |
| `$FUSION_HOME/cache/{cli-help.json,vboard-capabilities.json}` | `peers/exec_json.py`, `vboard/port.py` | Feature-flag probes |
| `$FUSION_HOME/peers/raw/<ms>-<peer>.jsonl` | `peers/exec_json.py` | Only when `FUSION_PEERS_KEEP_RAW=1` |
| `~/.fusion/memory/<scope>/...` | `memory_service.py` (`FUSION_MEMORY_ROOT`) | Scoped markdown memory with `<scope>_index.json` |
| `$FUSION_GRAPH_HOME/<name>/` | `kgraph.py` | Line-oriented JSON knowledge graph |
| `~/.fusion/teams/<run_id>/board.sqlite` | `team.py` `--prepare` | Native team board |
| `<workspace>/.fusion/<run_id>/board.sqlite` | `strategies.py preview --prepare` | Strategy preview board (different default from `team.py`) |
| `<workspace>/.fusion/board.sqlite` | `tui.py` default | Board the terminal snapshot reads |
| `<cwd>/.andrewcode/comms/board.sqlite` | `comms.py` | Fallback board for AndrewCode comms when `FUSION_BOARD` is unset |
| `$XDG_CONFIG_HOME/board/{claims/,local/}` (default `~/.config/board`) | `vboard/claims.py`, `vboard/http.py` | Claim cache (mode 0700) and loopback token files (mode 0600) |
| `config/.backups/defaults.env.<stamp>.bak` | `config_store.py` | Backup before each dashboard edit |

Scratch copies live outside this tree: adapters use `mktemp` directories named `fusion-<tag>.XXXXXX` under `$TMPDIR` or `/tmp` and delete them afterwards.

Run in a scratch `HOME`, `board init`, `ledger.sh stats` and a read-only VBoard `channels` call produced exactly these files:

```text
~/.fusion/vboard/default.db
~/.fusion/vboard/default.db-shm
~/.fusion/vboard/default.db-wal
~/.fusion/memory/runs.jsonl
~/.fusion/memory/lessons.md
~/.fusion/hive/board.sqlite
~/.fusion/hive/board.sqlite-wal
~/.fusion/hive/board.sqlite-shm
```

## Installer-managed config and backups

[`scripts/fusion_install.py`](../scripts/fusion_install.py) never edits the checkout's manifests. It copies the plugin surface into a staging directory, rewrites config there, validates it and swaps it in.

| Item | Path | Notes |
| --- | --- | --- |
| Default install root | Windows `%LOCALAPPDATA%\Fusion`; macOS `~/Library/Application Support/Fusion`; Linux `${XDG_DATA_HOME:-~/.local/share}/fusion` | `default_install_root`. Override with the installer's `--install-root` |
| Installed plugin | `<root>/plugin` | The swapped-in copy |
| Launchers | `<root>/bin/fusion`, `fusion.ps1`, `fusion.cmd` | Read `runtime.json`, export `FUSION_PYTHON` and `FUSION_BASH`, run `fusion.py` |
| Runtime record | `<root>/runtime.json`, `<root>/fusion-install.json` | Pinned Python, bash and plugin path; owner marker `fusion-installer` |
| Host listings | `<root>/logs/*.json` | Output of the host's plugin list commands, kept for verification |
| AndrewCode | `<ANDREWCODE_HOME>/plugins/managed/fusion` and `<ANDREWCODE_HOME>/plugins/installed.json` | `install_kimi` upserts the registry record for id `fusion` |

In the installed copy only, `configure_runtime` rewrites `.mcp.json`, `mcp.json`, `kimi.plugin.json` and `.kimi-plugin/plugin.json` so both MCP servers (`fusion-coordination`, `fusion-board`) run with `PYTHONUTF8=1`, `FUSION_PYTHON`, a `PATH` that starts with the pinned interpreter's directory, and `FUSION_BASH` when a bash was found. `fusion-coordination` gets `--require-board`. The checkout's own [`mcp.json`](../mcp.json) carries only `PYTHONUTF8=1`.

Backups made by the installer:

- During a swap, the old `plugin` directory is renamed to `.fusion-backup-<hex>` next to it. It is deleted after a successful install and renamed back if the install fails.
- Staging uses `.fusion-stage-*`. A lock file `.fusion-install.lock` prevents concurrent installers and must be removed by hand if an installer was killed.
- The installer refuses to replace a directory without a Fusion `plugin.json`, or an install root whose marker is not owned by the installer.
- It does not back up your `config/defaults.env`. Reinstalling replaces the file inside the installed copy. `config_store` backups live in `config/.backups/` of whichever copy the dashboard runs from.

For install commands see [`GETTING_STARTED.md`](GETTING_STARTED.md).

## Related

- [`GETTING_STARTED.md`](GETTING_STARTED.md) for installation and first runs
- [`PEERS.md`](PEERS.md) for providers, the Codex gate and `child_env`
- [`CLI.md`](CLI.md) for the flags that override these values per command
- [`COCKPIT.md`](COCKPIT.md) for the dashboard that edits `defaults.env`
- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) for "my override did nothing" cases
