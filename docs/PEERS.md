# Providers and peers

This page explains how Fusion reaches other model CLIs: which providers really exist, how each one is invoked and detected, how models and effort are chosen, and which safety rules gate a call. It is for integrators who dispatch to providers from their own tooling, and for contributors who touch `scripts/providers/` or `python/fusion_swarm/peers/`.

The repository is named `agent-swarm-fusion`. The plugin id, slash commands, Python package (`fusion_swarm`) and `FUSION_*` environment variables keep the `fusion` name.

Everything here was read from source. Where two sources disagree, the page says so (see [Known inconsistencies](#known-inconsistencies)).

## Vocabulary

| Term | Meaning |
| --- | --- |
| host | The harness you run: Claude Code, Codex CLI or AndrewCode. Resolved by `detect_host()` in [`peers/policy.py`](../python/fusion_swarm/peers/policy.py). |
| provider | A model CLI that Fusion dispatches to as a subprocess. |
| seat | A named slot in a roster (`codex`, `fable`, `andrewcode`, ...). A seat is not always the CLI its name suggests. |
| peer | A target of the structured peer layer: `claude`, `codex`, `andrewcode`, `opencode`. |
| transport | The concrete way a peer is called (`claude-print`, `andrewcode-oneshot`, `andrewcode-acp`, `codex-exec`, `claude-acp`, `codex-acp`, `opencode-run`). |

There are two dispatch layers. They share rules but not code:

- The **adapter layer**: `scripts/fusion.sh dispatch <provider>` runs one script from `scripts/providers/`. Python reaches it through `adapter.dispatch` ([`adapter.py`](../python/fusion_swarm/adapter.py)) and `spawn.run_provider` ([`spawn.py`](../python/fusion_swarm/spawn.py)). Panels, councils, MetaLoop and the legacy swarm patterns use this layer.
- The **peer layer**: `python -m fusion_swarm.peers` and [`peers/exec_json.py`](../python/fusion_swarm/peers/exec_json.py) call a CLI directly with structured output, a session transport (ACP) or a board handoff. Form `pair_swap` defaults to it (`forms/pair_swap.py`).

## The real provider list

`scripts/providers/` holds 11 files: 8 adapters (`agy.sh`, `andrewcode.sh`, `codex.sh`, `copilot.sh`, `fable.sh`, `grok.sh`, `kimi.sh`, `opencode.sh`) and 3 helpers (`_common.sh`, `_json_out.py`, `detect.sh`). The adapter table is `adapter_for()` in [`scripts/fusion.sh`](../scripts/fusion.sh).

| Seat | Script | What it really runs | Role in rosters |
| --- | --- | --- | --- |
| `codex` | `codex.sh` | AndrewCode, tools disabled and ephemeral, via `andrewcode_cli.py --isolated`. It never runs the `codex` binary. | Chief Operator seat. Dispatched only when the host is not Codex. |
| `andrewcode` | `andrewcode.sh` | Nothing. Fails closed (see [AndrewCode](#andrewcode)). | Listed for the Hive; nested workers use `spawn.py` instead. |
| `copilot` | `copilot.sh` | `copilot -p ... --model ... --allow-all-tools --no-color -s -C <dir>` | Base panel. Not a MetaLoop worker. |
| `opencode` | `opencode.sh` | `opencode run -m <model>#<variant> --format json --dir <dir> <text>` | Base panel and MetaLoop expert tier. |
| `grok` | `grok.sh` | `grok --prompt-file <f> --output-format plain\|json --always-approve --cwd <dir>` | Base panel and MetaLoop expert tier. |
| `kimi` | `kimi.sh` | `kimi -p <text> -m <model> --output-format text`, or a `--prompt-file` stream-json call when the CLI advertises it | Co-leader seat (Moonshot family). |
| `agy` | `agy.sh` | `agy --print <text> --model <m> --add-dir <dir>` plus flags the installed CLI advertises | MetaLoop fast tier. |
| `fable` | `fable.sh` | `claude -p` in a scratch directory, prompt on stdin | MetaLoop advisor. Needs `FUSION_ALLOW_CLAUDE=1`. |

### Where claude, gemini and local went

Older documentation lists `claude.sh`, `gemini.sh` and `local.sh`. None exists, and `adapter_for()` has no `claude`, `gemini` or `local` case.

- **claude**: the Claude Code host is never a subprocess panelist. `fusion.sh dispatch claude` exits 3 when the host is `claude`. The `claude` CLI is used in two other places: the `fable` adapter (needs `FUSION_ALLOW_CLAUDE=1`) and the peer layer's `claude-print` transport (allowed only on a Codex host or with `FUSION_ALLOW_CLAUDE=1`).
- **gemini**: there is no Gemini CLI adapter. Gemini models are reached through `copilot` and `agy`. `adapter.py` marks both as one correlated `gemini` family (`CORRELATION_GROUPS`). `__main__.py` still accepts `gemini` as a known name when validating `--providers`, but `fusion.sh dispatch gemini` exits 2.
- **local / Ollama**: no adapter. The only `ollama` mentions outside docs are in the vendored `ultracode/` proxy.

### Detection

`scripts/providers/detect.sh` is a fast presence check. It does not authenticate.

| Status | Meaning |
| --- | --- |
| `available` | Binary on `PATH`, not degraded, allowed. |
| `missing` | Binary not found, or the provider is excluded by the allowlist file. |
| `degraded` | Present but listed in `$FUSION_STATE_DIR/degraded-providers` (default `$HOME/.fusion`). |
| `host-native` | The provider is the current host (`FUSION_HOST`). Never counted as a subprocess panelist. |
| `disabled` | `fable` without `FUSION_ALLOW_CLAUDE=1`. |

`codex` and `andrewcode` are probed differently. The check needs `node` on `PATH` and an AndrewCode entry file. Even when both exist the result is `degraded`, never `available`, because the legacy isolation is not verifiable. Entry-file search order in `detect.sh`: `FUSION_ANDREWCODE_ENTRY`, `C:/dev/andrew-code/apps/kimi-code/dist/main.mjs`, `$HOME/dev/andrew-code/apps/kimi-code/dist/main.mjs`, then an `andrew-code` directory inside the plugin's grandparent directory (`$DIR/../../../../andrew-code/...` from `scripts/providers/`).

`FUSION_ALLOWLIST` (default `$FUSION_STATE_DIR/panel-allowlist`) restricts detection to the names listed, one per line.

Executed in a scratch copy with an empty provider `PATH` and `FUSION_HOST=claude`:

```bash
FUSION_HOST=claude bash scripts/providers/detect.sh
```

```text
PROVIDER_CHECK_START
claude:host-native
codex:missing
copilot:missing
opencode:missing
grok:missing
kimi:missing
andrewcode:missing
agy:missing
fable:disabled
PROVIDER_CHECK_END
```

`--json` prints the same data as `{"providers": {...}, "metaloop": {...}, "live_panelists": N, "multi_model": bool}`. `live_panelists` counts only `available` among `codex`, `copilot`, `opencode`, `grok`, `andrewcode`.

## How an adapter call behaves

All adapters share [`_common.sh`](../scripts/providers/_common.sh) and one contract.

Arguments: `<script> <prompt_file> <out_file> [model] [effort] [workspace]`. `codex.sh` has an extra positional `sandbox` before `workspace`, and `fusion.sh dispatch` fills it for you.

Exit codes and the status Python derives from them (`adapter.dispatch`, `spawn.DISPATCH_STATUS`):

| rc | Status | Meaning |
| --- | --- | --- |
| 0 | `returned` | Non-empty answer in `<out_file>`. An empty answer is downgraded to `error`. |
| 124 | `timeout` | Wall-clock budget exceeded. |
| 127 | `absent` | CLI (or AndrewCode entry) not installed. |
| 3 | `blocked` | Refused by policy: provider is the host, or `fable` without `FUSION_ALLOW_CLAUDE=1`. |
| 2 | `error` | Unknown provider name. |
| other | `error` | Auth failure, empty output, non-zero CLI exit. |

`spawn.run_provider` adds `cancelled` with rc 130. A timeout, absent or errored provider is never treated as agreement.

Common behavior:

- The prompt never goes into a shell string. It is passed on stdin, as `--prompt-file`, or as one `"$(cat file)"` argument. Prompts over `FUSION_PROMPT_INLINE_MAX` bytes (default 16000) are copied to `.fusion-task.md` in the worker directory and replaced by a pointer.
- Calls run in a throwaway scratch directory. A scratch directory is not a sandbox. The CLI can still reach anything its own permissions allow.
- Output is cleaned of ANSI escapes. Whitespace-only output counts as empty.
- Timeouts come from `fusion_timeout`, a SIGINT, SIGTERM, SIGKILL ladder with 2 seconds between steps. A sentinel file distinguishes a real timeout from an unrelated 137 or 143.
- Per-provider timeout variables and defaults (seconds): `FUSION_COPILOT_TIMEOUT` 600, `FUSION_OPENCODE_TIMEOUT` 600, `FUSION_GROK_TIMEOUT` 600, `FUSION_KIMI_TIMEOUT` 900, `FUSION_AGY_TIMEOUT` 600, `FUSION_FABLE_TIMEOUT` 900, `FUSION_ANDREWCODE_TIMEOUT` 600 (used by the `codex` seat).
- `copilot`, `opencode`, `grok`, `kimi` and `agy` accept a fifth argument (or `FUSION_WORKER_REPO`) naming a repository. They copy it into the scratch directory as a read snapshot (`fusion_snapshot_repo`, caps `FUSION_SNAPSHOT_MAX_FILES` 4000 and `FUSION_SNAPSHOT_MAX_FILE_KB` 512, with `.fusion-snapshot.txt` recording what was skipped). If the snapshot fails or copies nothing from a non-empty repo, the adapter exits 1. `codex.sh`, `andrewcode.sh` and `fable.sh` take no snapshot.
- Flags are feature-detected from `--help` where the CLI varies (`grok` JSON output, `kimi` stream mode, `agy` print flag and extras, `fable` output and tool flags).

### Per-provider notes

| Seat | Default model | Effort | Notes |
| --- | --- | --- | --- |
| `codex` | `FUSION_CODEX_MODEL` (see [inconsistencies](#known-inconsistencies)) | `FUSION_CODEX_EFFORT`, default `xhigh` | Sandbox argument or `FUSION_CODEX_SANDBOX`: `workspace-write` is noted and ignored, `danger-full-access` is refused. Text-only consult. |
| `copilot` | `FUSION_COPILOT_MODEL` | `--reasoning-effort` for `none minimal low medium high xhigh max`; unknown values are ignored with a warning | No `.meta.json` is written. |
| `opencode` | `FUSION_OPENCODE_MODEL` | Variant appended as `model#variant` unless the model already has `#`. `FUSION_OPENCODE_VARIANT` | Keeps `<out>.log` and `<out>.raw`. Warns when the output has no `{` (possible truncation). |
| `grok` | `FUSION_GROK_MODEL`, default `grok-4.5` | Opt-in only (`FUSION_GROK_EFFORT`). The default model rejects effort with HTTP 400 per the comments in `grok.sh` and `defaults.env`. | Runs with `--always-approve`. |
| `kimi` | `FUSION_KIMI_MODEL`, default `azure/kimi-k3` | `--thinking-effort` only on the stream-json path | The text path strips the `• ` bullet prefix and the resume footer. |
| `agy` | `FUSION_AGY_MODEL` | none | Adds `--dangerously-skip-permissions` when the CLI advertises it. If no print flag is detected from `--help`, it assumes `--print` and warns. |
| `fable` | `FUSION_FABLE_MODEL`, default `fable-5.1`, sent to `claude` as `fable` | `--effort` when advertised. `FUSION_FABLE_EFFORT`, default `xhigh` | Read-only advisor: `--strict-mcp-config` and tool denylist when supported. |

### AndrewCode

AndrewCode is a Node program, started as `node <entry> ...`. Fusion never starts a standalone AndrewCode shim and never starts a standalone `codex` executable.

The default entry is resolved by `_default_entry()` in [`andrewcode_cli.py`](../python/fusion_swarm/andrewcode_cli.py): `FUSION_ANDREWCODE_ENTRY`; on Windows `C:/dev/andrew-code/apps/kimi-code/dist/main.mjs`; elsewhere the first existing of `$HOME/dev/andrew-code/apps/kimi-code/dist/main.mjs`, the same path under the plugin's grandparent directory, or the Windows path. The Windows path is a default for the author's machine. Set `FUSION_ANDREWCODE_ENTRY` to use another checkout.

Three different AndrewCode paths exist and they have different guarantees:

| Path | Entry point | Flags | Guarantee |
| --- | --- | --- | --- |
| Consult seat (`codex` seat) | `codex.sh` then `andrewcode_cli.run_isolated` | `-m`, `--thinking-effort`, `--prompt-file`, `--output-format stream-json`, `--disable-tools`, `--ephemeral` | Tools disabled, non-persistent, empty temp cwd, stdin closed. Fails closed (rc 1, no provider call) unless `--help` advertises both isolation flags. |
| Legacy worker seat | `andrewcode.sh` then `andrewcode_cli.py --sandbox snapshot-only` | none launched | Refuses. rc 127 if Node or the entry is missing, otherwise rc 1 with the `CONTROL_GAP` message. `FUSION_ANDREWCODE_AUTO` does not bypass this. |
| Nested Hive worker | `spawn.spawn_andrewcode` | `command()` without isolation flags | Real process, **no sandbox, tools enabled, sessions persist**. For authorized implementation work only. |

`codex.sh` and `andrewcode.sh` start Python as `${FUSION_PYTHON:-python}`. On a machine with only `python3`, both fail with rc 127 (which reads as "absent") and `python: command not found` in the log. Set `FUSION_PYTHON=python3`.

Executed in a scratch copy where the AndrewCode entry does not exist:

```bash
FUSION_PYTHON=python3 FUSION_HOST=claude bash scripts/fusion.sh dispatch codex "$HOME/p.txt" "$HOME/o.txt"; echo rc=$?
```

```text
rc=127
```

The reason is in `$HOME/o.txt.log`: `AndrewCode Node entry is missing; no provider was called`.

The consult seat passes the model alias through unchanged. AndrewCode validates the alias and the effort. `EFFORTS` is `off on none minimal low medium high xhigh max ultra`. Fusion never translates a model id, drops effort or clamps an unsupported level.

## Choosing models and effort

### Precedence for the adapter layer

For one provider call, the model is the first of:

1. A non-empty explicit `model` argument to `adapter.dispatch` or `fusion.sh dispatch`.
2. An active per-mode override `FUSION_<MODE>_<PROVIDER>_MODEL` (and `_EFFORT`), when `FUSION_INVOKED_MODE` names the running mode. "Active" means set in the environment or uncommented in `config/defaults.env`. The pattern list lives in `modes_catalog.per_mode_override_specs`.
3. The adapter script's own resolution: `FUSION_<PROVIDER>_MODEL` from the environment, then `config/defaults.env`, then host plugin options (`CLAUDE_PLUGIN_OPTION_*` or `CODEX_PLUGIN_OPTION_*`), then the value baked into the script.

`config/defaults.env` is sourced by every adapter through `fusion_load_config`. Most of its keys use `${VAR:-default}`, so your environment wins. Three keys are assigned unconditionally and **override your environment**: `FUSION_COPILOT_MODEL`, `FUSION_AGY_MODEL` and `FUSION_FABLE_EFFORT`. Verified by sourcing `_common.sh` with those three set in the environment:

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin FUSION_COPILOT_MODEL=mine FUSION_AGY_MODEL=mine FUSION_GROK_MODEL=mine FUSION_FABLE_EFFORT=low \
  bash -c '. scripts/providers/_common.sh; echo copilot=$FUSION_COPILOT_MODEL agy=$FUSION_AGY_MODEL grok=$FUSION_GROK_MODEL fable_effort=$FUSION_FABLE_EFFORT'
```

```text
copilot=gpt-5.6-sol agy=gemini-3.6-flash-high grok=mine fable_effort=xhigh
```

To override those three, pass an explicit model or effort argument, use a per-mode override, or point `FUSION_CONFIG` at a different file. With `FUSION_CONFIG=/dev/null` the same `FUSION_COPILOT_MODEL=mine` survives.

### Roster metadata

`PROVIDER_META` in [`adapter.py`](../python/fusion_swarm/adapter.py) records tier, model family, harness and a default model per seat. It is metadata for routing and correlation. It is not what `copilot.sh` or the others actually pass.

| Seat | Tier | Family | Harness |
| --- | --- | --- | --- |
| `codex` | operator | gpt | andrewcode-cli |
| `andrewcode` | expert | muse | andrewcode-cli |
| `grok` | expert | grok | grok-cli |
| `opencode` | expert | glm | opencode |
| `agy` | fast | gemini | antigravity-cli |
| `kimi` | expert | kimi | kimi-code-cli |
| `copilot` | fast | gemini | github-copilot-cli |
| `fable` | advisor | claude | claude-code-print |

Family is how the plugin avoids counting correlated models as independent votes. `agy` and `copilot` are one `gemini` family. `WORKER_PROVIDERS` is `andrewcode agy opencode grok`, `ADVISOR_PROVIDERS` is `fable`, `OPERATOR_PROVIDERS` is `codex`, and `WORKER_SESSIONS` allows 4 `andrewcode`, 2 `agy`, 1 `opencode` and 1 `grok` session per wave.

### An explicit AndrewCode roster: `FUSION_ANDREWCODE_SELECTION`

When `FUSION_ANDREWCODE_SELECTION` is set, every seat is served by AndrewCode with an alias you chose. Source: [`model_selection.py`](../python/fusion_swarm/model_selection.py).

The value is JSON: `{"version": 1, "participants": [{"model": "...", "effort": "..."}, ...], "roles": {...}}`. It needs 1 to 64 participants, an exact effort from the list above, and every role model must be in the roster. The names `selected-1`, `selected-2`, ... become the seats. Seats `fable`, `codex` and `andrewcode` map to the roles `architect`, `synthesis` and `worker` when those roles exist. Other known seats map by position.

Effects, as run with two synthetic aliases:

```text
['selected-1', 'selected-2']
fable {'model': 'alias-a', 'effort': 'high', 'role': 'architect'}
codex {'model': 'alias-b', 'effort': 'low', 'role': ''}
grok {'model': 'alias-a', 'effort': 'high', 'role': ''}
```

With a selection set:

- `adapter.available()` returns the `selected-N` seats and `adapter.dispatch` uses only `andrewcode_cli.run_isolated`. No third-party CLI is called and nothing falls back.
- `model_family()` returns `unverified` for every seat and all seats form one correlation group. Different aliases are not proof of independent model families.
- An invalid value fails closed. `available()` returns an empty list and dispatch returns status `error`. The error text never echoes the value, because aliases may be private (`Invalid AndrewCode model selection JSON`).

## What is recorded about the model, usage and session

Adapters that parse structured output write `<out_file>.meta.json` with `{model, session_id, usage, cost_usd, kind}` (plus `parse_error: true` when parsing failed). [`_json_out.py`](../scripts/providers/_json_out.py) handles the kinds `opencode`, `grok`, `agy`, `claude` and `andrewcode`. `spawn.run_provider` reads that file into its result dict.

| Seat | Meta written |
| --- | --- |
| `opencode`, `grok` (json mode), `agy` (json mode), `fable` (json mode), `kimi` (stream mode) | Parsed from the stream. |
| `grok` plain, `agy` text, `kimi` text, `fable` text | Model requested only, via `fusion_write_meta`. |
| `codex` seat | Meta only (`andrewcode` kind), after a stream was produced. The stream carries no model id, so identity is `requested-only`. |
| `copilot` | None. |

`run_provider` returns `{ok, output, status, provider, role, rc, elapsed_s, model, usage, session_id}`.

The peer layer returns a `PeerResult` (`ok`, `status`, `peer`, `transport`, `text`, `structured`, `model`, `session_id`, `usage`, `cost_usd`, `rc`, `elapsed_s`, `raw_path`, `error`). The `claude-print` parser takes the observed model from `modelUsage`. Other transports report the requested model unless the stream names one. `PeerResult.to_dispatch()` converts it to the same dict shape `adapter.dispatch` callers expect. Set `FUSION_PEERS_KEEP_RAW=1` to keep each raw stream under `$FUSION_HOME/peers/raw/` (default `$HOME/.fusion`).

## Board identity propagation

One name identifies an agent everywhere. [`peers/identity.py`](../python/fusion_swarm/peers/identity.py) resolves it as `BOARD_PROFILE`, then `FUSION_AGENT_ID`, then a host default (`claude`, `codex`, `andrewcode`, `kimi`). Names match `^[a-z][a-z0-9-]{0,31}$`. Invalid environment values are skipped rather than used.

Shell adapters call `fusion_export_identity`, which exports whichever of these are set: `FUSION_AGENT_ID`, `BOARD_PROFILE` (defaults to `FUSION_AGENT_ID`), `FUSION_BOARD`, `FUSION_VBOARD`, `FUSION_PARENT_ID`, `FUSION_SWARM_ID`, `FUSION_RUN_ID`, `BOARD_TASK`.

`child_env()` builds the environment for a peer subprocess:

- Adds the identity variables and increments `FUSION_PEER_DEPTH`. It raises `depth_exceeded` above `FUSION_PEER_MAX_DEPTH` (default 2).
- Pins the resolved host into `FUSION_HOST` and then removes `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT`. Without the pin, a child with those markers stripped but an inherited `FUSION_HOST=codex` would look like a Codex host and open the Codex gate.

Executed:

```python
from fusion_swarm.peers import identity as i
c = i.child_env({"FUSION_PEER_DEPTH": "1", "CLAUDECODE": "1", "PATH": "/usr/bin"}, "Worker-A", board_path="/tmp/b.db", run_id="r1")
```

```text
{'BOARD_PROFILE': 'worker-a', 'FUSION_AGENT_ID': 'worker-a', 'FUSION_BOARD': '/tmp/b.db', 'FUSION_HOST': 'claude', 'FUSION_PEER_DEPTH': '2', 'FUSION_RUN_ID': 'r1'}
```

A second `child_env` from that result raised `depth_exceeded: peer depth 3 exceeds FUSION_PEER_MAX_DEPTH=2`. `worker_name("am1", 7)` returns `uc-am1-7`.

For Hive children, `spawn.spawn_andrewcode` always sets `FUSION_BOARD`, `FUSION_AGENT_ID`, `FUSION_PARENT_ID`, `FUSION_SWARM_ID` and `PYTHONPATH`. Those win over `extra_env`. Spawn limits come from [`identities.py`](../python/fusion_swarm/identities.py): `SpawnLimits` defaults to `max_depth=2`, `max_children_per_agent=4`, `max_agents=24`, checked before any process starts (a refusal raises `SpawnError`). The Hive itself is described in [HIVE.md](HIVE.md).

## The peer layer

### Policy and routing

`python -m fusion_swarm.peers policy` prints the route table for the current environment. Exit codes for the whole CLI: 0 success, 1 peer or board failure, 2 policy or usage error. Errors are one JSON object on stderr: `{"error": {"code", "message"}}`.

| Peer | One-shot transport | Session (ACP) transport | Allowed when |
| --- | --- | --- | --- |
| `andrewcode` | `andrewcode-oneshot` | `andrewcode-acp` (`andrewcode acp`) | Always. |
| `opencode` | `opencode-run` | none (`invalid_request`) | Always. |
| `claude` | `claude-print` | `claude-acp` (`claude-agent-acp`) | Codex host, or `FUSION_ALLOW_CLAUDE=1`. On a Claude host the reason is `host-native`. |
| `codex` | `codex-exec` if the gate is open, else `andrewcode-oneshot` with model `gpt-6.1-sol` | `codex-acp` if the gate is open and `codex-acp` is on `PATH`, else `andrewcode-acp` | See the Codex gate below. |

Executed on a Claude host (`CLAUDECODE=1`):

```bash
CLAUDECODE=1 python3 -m fusion_swarm.peers route codex
```

```json
{
  "allowed": true,
  "binary": "andrewcode",
  "default_model": "gpt-6.1-sol",
  "peer": "codex",
  "reason": "andrewcode seat (codex exec gated)",
  "transport": "andrewcode-oneshot"
}
```

```bash
CLAUDECODE=1 python3 -m fusion_swarm.peers route claude; echo rc=$?
```

```json
{
  "allowed": false,
  "binary": "claude",
  "default_model": null,
  "peer": "claude",
  "reason": "host-native",
  "transport": "claude-print"
}
```

The command printed `rc=2` after that object. With `FUSION_HOST=codex` the same `route codex` returns transport `codex-exec`, binary `codex`, reason `codex exec allowed`.

### Host detection

`detect_host()` reads `FUSION_HOST` if it is one of `claude`, `codex`, `andrewcode`, `kimi`. Claude Code markers (`CLAUDECODE=1`, or any `CLAUDE_CODE_ENTRYPOINT`) win over a legacy `FUSION_HOST=codex` unless `FUSION_HOST_EXPLICIT=1`. With no declared host, the markers give `claude`, otherwise `unknown`. `adapter.py` falls back to `FUSION_HOST` or `codex` when the host is unknown.

### Codex policy

The rule is in `peers/policy.py`: the `codex` binary runs only when `codex_exec_allowed()`, which is `FUSION_ALLOW_CODEX_EXEC=1` or a Codex host. Enforcement is layered:

1. `route()` picks `andrewcode` for the codex seat when the gate is closed.
2. `exec_json.run` and `default_runner` call `assert_exec_allowed(argv[0])` immediately before `Popen`.
3. `feature_flags` refuses even a `codex --help` probe without the gate.
4. `acp.assert_spawnable` rejects any `codex`, `codex-acp` or launcher (`npx`, `bunx`, `pnpx`, `npm`, `pnpm`, `yarn`) found anywhere in argv.
5. `host_runtime.executable_argv` refuses `codex` and `codex-cli` outright, including help and version probes, and `cli_request` for hosts `codex` and `andrewcode` raises the `CONTROL_GAP` message.

Executed:

```bash
python3 -m fusion_swarm.peers handoff --board "local:$HOME/board.db" --as worker-1 serve --channel work/demo -- codex exec x; echo rc=$?
```

```text
{"error": {"code": "peer_not_allowed", "message": "codex / codex-acp spawn requires FUSION_ALLOW_CODEX_EXEC=1 or a codex host"}}
rc=2
```

The Claude peer has its own gate. `claude_spawn_allowed()` is true on a Codex host or with `FUSION_ALLOW_CLAUDE=1`, and false on a Claude host.

### One-shot calls: `peers ask`

```text
python -m fusion_swarm.peers ask <peer> (--prompt TEXT | --prompt-file FILE)
    [--model M] [--effort E] [--cwd DIR] [--tools none|read|write]
    [--json-schema FILE] [--timeout SECONDS] [--as NAME]
```

Defaults: `--tools none`, `--timeout 600`. Prompts are limited to 10,000,000 characters. The command prints a `PeerResult` as JSON and exits 0 only when `ok` is true.

How `--tools` and the transport shape the call ([`exec_json.py`](../python/fusion_swarm/peers/exec_json.py)):

| Transport | Prompt | Notable arguments |
| --- | --- | --- |
| `claude-print` | stdin | `claude -p --output-format json`, `--no-session-persistence` unless resuming, `--model`, `--json-schema`, `--max-budget-usd`. `none`: `--permission-mode dontAsk --disallowedTools "Bash Edit Write MultiEdit NotebookEdit WebFetch"`. `read`: `--allowedTools Read,Grep,Glob`. `write`: `--permission-mode acceptEdits`. |
| `andrewcode-oneshot` | `--prompt-file` | `-m`, `--thinking-effort`, `--output-format stream-json`. `none`: `--disable-tools`. `write`: `--auto`. `--ephemeral` unless a session is requested. A schema is appended to the prompt text. **`--tools read` is rejected** because AndrewCode has no sandbox. |
| `codex-exec` | stdin | `codex --no-daemon exec --json --skip-git-repo-check --ephemeral --color never -s <read-only\|workspace-write> -C <cwd> [-m] [-c model_reasoning_effort=...] [--output-schema] -o <file> -`. Resume is not supported. |
| `opencode-run` | argument | `opencode run [-m] --format json --dir <cwd> <text>`. Over 16000 bytes the text is written to `.fusion-task.md` and replaced by a pointer. |

Unadvertised `--long` flags are dropped with a note in `error`. Advertised flags are learned from `<binary> --help` and cached in `$FUSION_HOME/cache/cli-help.json` (default `$HOME/.fusion`), keyed by binary identity and a hash of the help text. With `andrewcode` on a Claude host and no tools, the call runs in a temp directory.

`status` values: `returned`, `error`, `absent` (binary missing, rc 127), `timeout`, `cancelled`. Policy errors raise instead and exit 2.

Executed with no provider binary on `PATH`:

```bash
python3 -m fusion_swarm.peers ask andrewcode --prompt hello --tools none --timeout 5; echo rc=$?
```

```json
{
  "cost_usd": null,
  "elapsed_s": 0.012,
  "error": "andrewcode not found on PATH",
  "model": null,
  "ok": false,
  "peer": "andrewcode",
  "raw_path": null,
  "rc": 127,
  "session_id": null,
  "status": "absent",
  "structured": null,
  "text": "",
  "transport": "andrewcode-oneshot",
  "usage": null
}
```

The exit code was 1. The same command with `--tools read` exited 2 with `invalid_request: andrewcode has no sandbox; tools='read' cannot be guaranteed (J-09)`. `ask claude` on a Claude host exited 2 with `peer_not_allowed: host-native`.

### Sessions: `peers acp`

```text
python -m fusion_swarm.peers acp {andrewcode|claude|codex} (--prompt TEXT | --prompt-file FILE)
    [--model M] [--thinking LEVEL] [--cwd DIR]
    [--permission deny|allow_read|allow_all] [--yes-allow-all]
    [--timeout SECONDS] [--as NAME]
```

This runs one session with one prompt over ACP v1: newline-delimited JSON-RPC 2.0 on the agent's stdio ([`peers/acp.py`](../python/fusion_swarm/peers/acp.py)).

- The client offers protocol version 1 and aborts if the agent negotiates anything else.
- It advertises no filesystem or terminal capability. Any `fs/*`, `terminal/*` or unknown agent request gets JSON-RPC error `-32601`.
- Permission requests are answered by `--permission`. `deny` is the default. `allow_read` selects an allow option only for tool kinds `read`, `search` and `fetch`. `allow_all` needs `--yes-allow-all` (the CLI and `AcpClient` both enforce it). Each decision is recorded in `permission_decisions`.
- `allow_read` is advisory for AndrewCode, which has no sandbox. A result carries a warning to that effect for any non-`deny` permission.
- `--thinking` maps to ACP's `off|low|high|max`: `off`/`none` to `off`, `minimal`/`low` to `low`, `medium`/`high` to `high`, `xhigh`/`ultra`/`max` to `max`.
- With no `--cwd`, a temp directory is used. The model is set with `session/set_config_option`; the codex seat defaults to `gpt-6.1-sol`.
- Agent stderr is kept only as a short redacted tail for error messages. Secrets-looking strings (`api_key`, `token`, `bearer`, `sk-...`) are replaced with `[redacted]`.

The result includes `ok`, `status`, `stop_reason`, `tool_calls`, `session_id`, `permission_decisions` and `warnings`. `ok` needs `stopReason == "end_turn"` and non-empty text.

### Durable handoff: `peers handoff`

A handoff is a board task labelled `fusion.handoff=1`. One agent submits a brief, another serves it, and the result lives on the board ([`peers/handoff.py`](../python/fusion_swarm/peers/handoff.py)). Board text is untrusted data, message text is capped at 10,000 characters and `board.*` labels are reserved.

```text
python -m fusion_swarm.peers handoff [--board SPEC] [--as NAME] <command>
    submit --channel CH --key KEY (--brief TEXT | --brief-file FILE) [--payload-file JSON] [--to NAME]
    status JOB
    wait JOB [--timeout 1800]
    get JOB
    serve --channel CH [--lease 300] [--wait 0] [--max 1] [--handler-timeout 600] -- COMMAND...
```

`--board` takes `local`, `local:<db>`, `http://...` or `vercel`, and defaults to `FUSION_VBOARD`. See [VBOARD.md](VBOARD.md) for the engines.

Behavior to rely on:

- `submit` is idempotent by `--key`. Replaying it returns the same job id.
- `serve` claims the next ready handoff task, pipes the task text to `COMMAND` on stdin, and posts stdout as the result. The handler runs in its own process group, with a hard `--handler-timeout`, under a lease that renews every third of the lease.
- Results over 1000 characters are posted as a reply first (the board caps a task result at 1000 characters) and the task completes with `see reply #<id>`. `get` follows the pointer and returns the full text.
- It fails closed. Success is reported only after the board accepted completion. If the claim is lost during the handler, nothing is posted and the job is reported `lost`. If delivery cannot be confirmed, it is `undelivered`.
- `wait` exits 0 only for `completed`. `get` exits 1 with `not_ready` while the job is open.

Executed against a local board in a scratch `HOME` (the handler is a local `python3 -c`, no provider):

```bash
h() { python3 -m fusion_swarm.peers handoff --board "local:$HOME/board.db" --as claude "$@"; }
h submit --channel work/demo --brief "summarize the README" --key demo-1 --to worker-1
h --as worker-1 serve --channel work/demo -- python3 -c 'import sys;print("handled: "+sys.stdin.read().split(chr(10))[0])'
h get 3
```

```text
{ "channel": "work/demo", "job_id": "3", "reason": null, "result": null, "state": "ready", "to": "<local identity>+worker-1" }
{ "channel": "work/demo", "job_id": "3", "reason": null, "result": "handled: summarize the README\n", "state": "completed", "to": "<local identity>+worker-1" }
handled: summarize the README
```

The real output is pretty-printed JSON. It is compacted here, and the local identity (derived from the OS user) is replaced with a placeholder. A second `submit` with the same key printed the same `job_id`.

### Other `peers` commands

`whoami [--board SPEC] [--as NAME]` prints `{name, host, peer_depth, max_depth}` plus the board identity if a board is given. On a Claude host with a clean environment it printed name `claude`, host `claude`, depth `0`, max depth `2`.

Regenerate this section with `PYTHONPATH=python python3 -m fusion_swarm.peers --help` and `... <command> --help`.

## No implicit fallback

Fusion does not substitute one provider for another to make a call succeed.

- With `FUSION_ANDREWCODE_SELECTION` set, only AndrewCode runs (see above). A bad selection yields no seats, not a default roster.
- The codex seat is AndrewCode. If AndrewCode is missing, the result is `absent` (rc 127). It does not try `codex`. Tests: `tests/test_andrewcode_transport.py` (for example `test_failed_probe_is_not_live_or_a_model_fallback_call`).
- Native host runtimes in [`host_runtime.py`](../python/fusion_swarm/host_runtime.py) take their model inventory from the live runtime only. An operation not supplied raises `CapabilityUnavailable("... no external fallback was invoked")`. `ClaudeCodeRuntime` needs `explicitly_requested=True`.
- `process_runtime.ProcessRuntime` runs an explicit command or an explicitly chosen host CLI as an owned process. It never picks a provider. A non-local host needs `explicitly_requested`, and Claude needs `allow_claude`.
- An empty explicit panel never triggers rediscovery, and a failed detection blocks before question generation or synthesis (`tests/test_provider_boundaries.py`).
- A provider that does not return is `absent`, `timeout` or `error`, and is excluded from voting. It is never merged as agreement.

## Process groups and cancellation

Every provider process is started in its own session or process group (`start_new_session=True` on POSIX, `CREATE_NEW_PROCESS_GROUP` and `CREATE_NO_WINDOW` on Windows).

- The peer layer's `default_runner` and `AcpClient` kill the whole group with SIGKILL on timeout or cancel, and ACP `close()` sends SIGTERM first.
- `spawn.run_provider`, `spawn._subprocess_runner` and `andrewcode_cli.run_isolated` use `terminate_process_group` from [`process_runtime.py`](../python/fusion_swarm/process_runtime.py): SIGINT, then SIGTERM, then SIGKILL with 2-second grace steps. The process creation identity is recorded first and checked before each signal, so a reused PID is not killed. The ladder always ends with one `killpg(SIGKILL)` so grandchildren that outlive the leader are reaped. On Windows the leader is terminated, then killed.
- `spawn.ProcessCancel` is a cancel token: `set()` kills every registered group, and a process registered after `set()` is killed at once. `run_provider` polls it every 0.1 seconds and returns status `cancelled`, rc 130.
- Shell adapters use `fusion_timeout` as described above.

## Security notes

- **AndrewCode has no provider sandbox.** The nested Hive worker path runs with tools enabled and persistent sessions. A scratch directory, a repo snapshot or `--permission allow_read` does not confine it. Use Fusion worktree or tool restrictions that match the authorized task, and explicit board path reservations. The same warning is in [`commands/peers.md`](../commands/peers.md).
- Only the consult seat (`codex.sh`, `peers ask andrewcode --tools none`) disables tools. Do not use the Hive path for reviews that need that guarantee.
- Several adapters run their CLI with approval prompts off so that it can work unattended: `grok --always-approve`, `copilot --allow-all-tools`, `agy --dangerously-skip-permissions` (when advertised). Their only containment is the throwaway working directory.
- Provider errors are summarized, not echoed. AndrewCode failures report a category (authentication, model availability, CLI failure) and withhold raw diagnostics. ACP stderr is redacted.
- `FUSION_ALLOW_CODEX_EXEC` and `FUSION_ALLOW_CLAUDE` are explicit opt-ins. Installing the plugin is not authorization to run either CLI.
- Do not pass secrets in prompts or `--payload-file`. Handoff briefs are visible to everyone on the board channel.

## Known inconsistencies

These were found while writing this page and are not resolved in code:

- The default Codex-seat model differs by path. `peers/policy.py`, `andrewcode_cli.py` and the fallback in `codex.sh` use `gpt-6.1-sol`. `config/defaults.env`, `adapter.py` (`PROVIDER_META`) and `fusion.sh providers` use `gpt-5.6-sol`. Through `fusion.sh dispatch`, `defaults.env` is sourced first, so the adapter layer runs `gpt-5.6-sol` unless you override it. The peer layer runs `gpt-6.1-sol`.
- Other model defaults differ between `defaults.env` and the script fallbacks: `opencode` (`meta/muse-spark-1.2-contributor` vs `opencode-go/glm-5.2`), `copilot` (`gpt-5.6-sol` vs `gemini-3.5-flash`), `agy` (`gemini-3.6-flash-high` vs `Gemini 3.1 Pro (High)`) and the OpenCode variant (`xhigh` vs `high`). In practice `defaults.env` wins. The header of `defaults.env` says everything in it is commented out, which is no longer true.
- `FUSION_ANDREWCODE_AUTO` appears in `defaults.env` and in the `fusion.sh providers` output as "pass `--auto -y` to nested workers". No code reads it, and `andrewcode_cli.command()` adds no such flag.

## Verify

Source-reviewed, not executed here (they start real provider CLIs): every adapter script, `peers ask` and `peers acp` with a real peer present, and `spawn_andrewcode` with the default runner.

Targeted tests run in a scratch copy (all use fake CLIs or injected runners): `test_peers_core` 17 passed, `test_peers_acp` 23, `test_provider_boundaries` 5, `test_spawn` 23, `test_spawn_run_provider` 13, `test_model_selection` 7, `test_provider_scripts` 7, `test_andrewcode_transport` 20 run with 1 error and 3 skipped. The error is in `test_doctor_probes_node_entry_never_standalone_codex`: the mocked `subprocess.run` was never called. The cause is in `supervision.doctor` (`supervision.py:99-102`): with no AndrewCode entry file on disk it raises `CapabilityUnavailable` before calling `subprocess.run`. The test needs a real entry file at the default path, so it is environment-dependent.

```bash
PYTHONPATH=python python3 -m unittest tests.test_peers_core tests.test_peers_acp tests.test_provider_boundaries tests.test_spawn tests.test_spawn_run_provider tests.test_model_selection
```

## Related

- [HIVE.md](HIVE.md): the nested AndrewCode swarm that uses `spawn.py`.
- [COORDINATION.md](COORDINATION.md): the HiveBoard that carries identity and receipts.
- [VBOARD.md](VBOARD.md): the board engines behind `peers handoff`.
- [CONFIGURATION.md](CONFIGURATION.md): every `FUSION_*` variable and the `.fusion/` layout.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md): provider absent, timeout and gate-refusal symptoms.
