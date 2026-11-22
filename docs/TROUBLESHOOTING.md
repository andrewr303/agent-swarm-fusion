# Troubleshooting

This page maps a symptom to its cause and fix for the Fusion plugin (version `3.3.0+swarm`). Read it when an install, a provider call, a board, a runtime form or a review run does not do what you expected. Every entry names the file that produces the message or behavior.

The repository is named `agent-mesh`. The plugin id, the `/fusion:*` commands, the Python package (`fusion_swarm`), the MCP servers (`fusion-coordination`, `fusion-board`) and the `FUSION_*` variables keep the `fusion` name.

## How the entries were checked

Each entry carries one of two marks.

- **Reproduced**: the command was run in a scratch copy of the repo with `HOME` pointed at a scratch directory, a minimal `PATH` and no provider CLI installed. The output shown is real, with machine-specific paths shortened.
- **Source-reviewed**: the behavior was read from the code or a test and not run, usually because it needs a live provider, a Windows host or a long wait. No output is shown for these.

Commands below assume you are in the repository root. Python commands use `PYTHONPATH=python`. Replace `python3` with the interpreter you installed Fusion with.

## Find your symptom

| Symptom | Go to |
| --- | --- |
| A provider shows `absent`, `timeout` or `error` | [Provider calls](#provider-calls) |
| `'codex' is the host runtime` or exit code 3 | [A provider is refused as the host](#a-provider-is-refused-as-the-host-exit-3) |
| `No panelists selected or detected` | [No panelists](#no-panelists-selected-or-detected) |
| `AndrewCode Node entry is missing` | [AndrewCode](#andrewcode) |
| `peer_not_allowed`, `FUSION_ALLOW_CODEX_EXEC` | [Peer policy refusals](#peer-policy-refusals) |
| `REPO_READ=...` in a review run | [Review runs](#review-runs-fusionultimate-review) |
| `database is locked` | [A board reports it is locked or busy](#a-board-reports-it-is-locked-or-busy) |
| `claim_lost`, `claim_not_held`, other VBoard reasons | [VBoard failures](#vboard-failures) |
| Mail stays undelivered or dead | [HiveBoard mail](#hiveboard-mail) |
| A form run, `--run-dir` or `salvage` error | [Runtime forms](#runtime-forms) |
| Hive `invalid SwarmSpec` or spawn limits | [Hive](#hive) |
| Leftover directories or worktrees | [Leftover workspaces](#leftover-workspaces-after-a-killed-run) |
| Cockpit 403, 421 or an empty terminal view | [Cockpit and terminal view](#cockpit-and-terminal-view) |
| Installer errors, Windows Bash, Codex cache | [Install problems](#install-problems) |

## Collect diagnostics first

Run these before you dig. None of them calls a model or touches the network.

```bash
# Which providers are present, and which one is the host?
bash scripts/fusion.sh detect
bash scripts/fusion.sh providers

# What may the structured peer layer spawn on this host?
PYTHONPATH=python python3 -m fusion_swarm.peers policy

# Is the host or AndrewCode executable installed? (one --version probe, no prompt)
python3 fusion.py supervise doctor --host claude --json
```

What each one tells you:

- `fusion.sh detect` prints `PROVIDER_CHECK_START`, one `name:state` line per provider and `PROVIDER_CHECK_END`. States are `available`, `missing`, `degraded`, `host-native` and `disabled`. It checks `command -v` and never runs the provider. Source: `scripts/providers/detect.sh`.
- `fusion.sh providers` lists the configured model per seat from `config/defaults.env`.
- `peers policy` prints the resolved host, whether `codex exec` and a Claude spawn are allowed, and the route for each peer. Source: `cmd_policy` in `python/fusion_swarm/peers/__main__.py`.
- `fusion.py supervise doctor --host <codex|andrewcode|claude> [--executable PATH] [--json]` runs `<executable> --version` once (15 s limit) and reports eight capability facts. Only `installation` is ever `observed`. The rest stay `unverified` unless a runtime contract is supplied, and the output ends with `provider_calls: 0` and the boundary note "installation and declared capabilities do not prove authenticated execution". Source: `doctor` in `python/fusion_swarm/supervision.py`. The command exists only through `fusion.py`: `python -m fusion_swarm.supervise` has no `__main__` guard and prints nothing.
- `scripts/ultracode.sh doctor` runs the bundled UltraCode config check. See [`commands/ultracode.md`](../commands/ultracode.md).

If the installer ran, its report is `<install root>/logs/latest-install.json` (status `complete` or `failed`, per-host detail and `errors`). Source: `scripts/fusion_install.py`, near `latest-install.json`. The default install root is `~/Library/Application Support/Fusion` on macOS, `$XDG_DATA_HOME/fusion` (else `~/.local/share/fusion`) on Linux and `%LOCALAPPDATA%\Fusion` on Windows (`default_install_root`).

When you file an issue, include the `detect` output, the `peers policy` output, the exact command, its exit code and the contents of any `<provider>.log` described below. Remove tokens and prompts first.

### Reproduced: `doctor` with a missing executable and a stand-in

```bash
python3 fusion.py supervise doctor --host claude --executable nonexistent-cli --json
```

```json
{
  "host": "claude",
  "capabilities": {
    "installation": {
      "state": "unavailable",
      "evidence": "host executable is missing: nonexistent-cli"
    },
    "discovery": { "state": "unverified", "evidence": null }
  }
}
```

The output above is trimmed. With `--host codex` and no AndrewCode entry the evidence reads `AndrewCode Node entry is missing`. With `--executable codex` it reads `standalone Codex CLI is prohibited, including help/version probes; use AndrewCode Node transport` (`executable_argv` in `python/fusion_swarm/host_runtime.py`). `doctor` without `--host` exits 2 with `doctor requires --host`.

## Provider calls

### Status words and exit codes

Every provider adapter in `scripts/providers/` follows one exit-code contract (header of `_common.sh`). The panel, the Python adapter and the review script turn it into a status word.

| Exit code | Meaning | Status word |
| --- | --- | --- |
| 0 | Success with a non-empty answer | `returned` |
| 124 | Wall-clock timeout | `timeout` |
| 127 | CLI not installed | `absent` |
| 3 | The provider is the host runtime, or a gate refused it | `host-conflict` (review script) or `error` (Python adapter) |
| 2 | Unknown provider name (`fusion.sh dispatch`) | `error` |
| anything else | Auth failure, empty output or non-zero exit | `error` |

An exit 0 with an empty output file is downgraded to `error`. Sources: `scripts/fusion.sh` (`cmd_dispatch`, `cmd_panel`), `scripts/ureview.sh` (`status_for_rc`), `python/fusion_swarm/adapter.py` (`dispatch`). An absent or failed panelist is reported as such and never counted as agreement.

### A provider shows `absent` (exit 127)

**Cause.** The CLI is not on `PATH` for the process that runs the adapter. Each adapter prints one line and exits 127.

**Reproduced:**

```bash
bash scripts/fusion.sh dispatch grok prompt.txt out.txt
```

```text
[fusion] grok not installed
```

**Fix.** Install the CLI, or fix `PATH` for the host session, then rerun `fusion.sh detect`. If a `panel-allowlist` file exists the provider is also reported `missing` when it is not listed (see [degraded](#a-provider-shows-degraded-or-missing-though-it-is-installed)). Messages per adapter: `copilot not installed`, `opencode not installed`, `grok not installed`, `kimi not installed`, `agy (Antigravity CLI) not installed`, `claude CLI (for Fable planner) not installed`. The `andrewcode` and `codex` seats need the AndrewCode Node entry instead (see [AndrewCode](#andrewcode)).

### A provider shows `timeout` (exit 124)

**Cause.** The adapter killed the CLI at its wall-clock limit. It sends SIGINT, then SIGTERM after 2 s, then SIGKILL after 2 s more (`fusion_timeout` in `_common.sh`) and prints `[fusion] <provider> timed out after <N>s`.

**Fix.** Raise the limit for that provider. Defaults come from each adapter:

| Variable | Default (s) |
| --- | --- |
| `FUSION_GROK_TIMEOUT` | 600 |
| `FUSION_COPILOT_TIMEOUT` | 600 |
| `FUSION_OPENCODE_TIMEOUT` | 600 |
| `FUSION_AGY_TIMEOUT` | 600 |
| `FUSION_KIMI_TIMEOUT` | 900 |
| `FUSION_FABLE_TIMEOUT` | 900 |
| `FUSION_ANDREWCODE_TIMEOUT` | 600 (the `codex` seat also uses this one) |

`config/defaults.env` mentions `FUSION_CODEX_TIMEOUT`. No code reads it, so setting it changes nothing. See [CONFIGURATION.md](CONFIGURATION.md) for the full variable list. Source-reviewed.

### A provider shows `error` and nothing says why

**Cause.** The Python layer throws the adapter's stderr away. `adapter.dispatch` calls `rc, _, _ = _bash(...)` and keeps only the exit code and the output file, so a `[fusion] <provider> failed (rc=1)` line never reaches you through `python -m fusion_swarm ...`. Source: `python/fusion_swarm/adapter.py` (`dispatch`).

**Fix.** Run the same dispatch through the shell, where stderr is visible:

```bash
bash scripts/fusion.sh dispatch <provider> prompt.txt out.txt
```

The adapter then prints `[fusion] <provider> failed (rc=N)` plus the last six lines of its own log (`fusion_fail` in `_common.sh`). A panel run keeps one `<provider>.log`, `<provider>.out` and `<provider>.status` per provider in its output directory and a `manifest.txt` of `provider:status:seconds` lines (`cmd_panel`). Source-reviewed.

Authentication problems surface here. `fusion.sh detect` only checks that the binary exists, so a logged-out CLI shows `available` and then fails at dispatch. Log in to the provider CLI directly.

### A provider is refused as the host (exit 3)

**Symptom.** `fusion.sh dispatch codex ...` prints `'codex' is the host runtime` and exits 3, even though you are in Claude Code.

**Cause.** The bash scripts default `FUSION_HOST` to `codex` and do not look for Claude markers (`scripts/fusion.sh:38`, `scripts/providers/detect.sh`, `scripts/route.sh:92`). Python resolves the host differently: `CLAUDECODE=1` or `CLAUDE_CODE_ENTRYPOINT` wins over a default `codex` unless `FUSION_HOST_EXPLICIT=1` (`detect_host` in `python/fusion_swarm/peers/policy.py`). On a Claude host the shell verbs therefore treat `codex` as host-native until you set `FUSION_HOST`. `scripts/ureview.sh` is the exception and defaults to `claude`.

**Reproduced:**

```bash
bash scripts/fusion.sh dispatch codex prompt.txt out.txt
```

```text
[fusion] 'codex' is the host runtime — fold its view in-context, not as a subprocess.
```

The exit code is 3. Running `FUSION_HOST=claude bash scripts/fusion.sh detect` instead prints `claude:host-native` and treats `codex` as a normal seat.

**Fix.** Export the real host before calling the bash verbs: `FUSION_HOST=claude`, `codex`, `andrewcode` or `kimi`. Use `none` only when you want every seat dispatchable (the codex CLI is still never run directly, see [Peer policy refusals](#peer-policy-refusals)). `unknown provider 'gemini'` (exit 2) means the name is not a seat: the adapters are `codex`, `copilot`, `opencode`, `grok`, `kimi`, `andrewcode`, `agy` and `fable` (`adapter_for` in `fusion.sh`). There is no Gemini, Ollama or local adapter.

### A provider shows `degraded` or `missing` though it is installed

**Cause.** Two files under the state directory (`$FUSION_STATE_DIR`, default `~/.fusion`) change what `detect` reports.

- `degraded-providers`: a provider named there is `degraded`. `ledger.sh mark-degraded <provider>` writes it and `ledger.sh clear-degraded` empties it.
- `panel-allowlist` (or the file named by `FUSION_ALLOWLIST`): when it exists, any provider not listed is `missing`.

**Reproduced** (a stand-in `grok` script on `PATH`, state directory in scratch):

```text
grok:available
grok:degraded     # after: bash scripts/ledger.sh mark-degraded grok
grok:missing      # after clearing, with panel-allowlist containing only copilot
```

**Fix.** `bash scripts/ledger.sh clear-degraded`, or edit or delete `panel-allowlist`. The `andrewcode` and `codex` seats report `degraded` whenever the Node entry exists and `node` is on `PATH`, because the legacy isolation is unavailable for them (`probe` in `detect.sh`).

### No panelists selected or detected

**Symptom.** A `python -m fusion_swarm moa|heavy|...` run returns immediately with `blocked: true`, empty `panel` and `error: "No panelists selected or detected; no providers dispatched"`.

**Cause.** `adapter.available()` found no provider in state `available` other than the host, or it raised. Two different causes give this same message:

1. No provider CLI is installed (or all are excluded by the allowlist or marked degraded).
2. `FUSION_ANDREWCODE_SELECTION` is set to something that fails validation. `adapter.available()` swallows the `ValueError` and returns an empty list, so a bad selection looks like an empty roster.

**Reproduced** (no provider on `PATH`):

```json
{
  "pattern": "moa",
  "task": "hello",
  "panel": [],
  "transcript": [],
  "returning": [],
  "status": {},
  "blocked": true,
  "synthesis": null,
  "synthesis_ok": false,
  "error": "No panelists selected or detected; no providers dispatched"
}
```

With `FUSION_ANDREWCODE_SELECTION='not json'` the text output was `error: No panelists selected or detected; no providers dispatched`.

**Fix.** Run `bash scripts/fusion.sh detect` first. If a selection variable is set, unset it or correct the JSON. The validator accepts `{"version": 1, "participants": [...1 to 64 bindings with "model" and a valid "effort"], "roles": {...}}` and never echoes the value (`python/fusion_swarm/model_selection.py`). No provider is dispatched in either case.

### AndrewCode

AndrewCode is a separate program. Fusion finds it in two different ways, so a check that passes for one path can fail for the other.

| Path | How AndrewCode is located | Used by |
| --- | --- | --- |
| Node entry | `FUSION_ANDREWCODE_ENTRY`, else the first existing of `C:/dev/andrew-code/apps/kimi-code/dist/main.mjs` (Windows), `~/dev/andrew-code/apps/kimi-code/dist/main.mjs`, a sibling `andrew-code` checkout | `codex.sh`, `andrewcode.sh`, `andrewcode_cli.py`, `detect.sh`, `doctor` |
| `andrewcode` executable | `andrewcode` on `PATH` | `python -m fusion_swarm.peers` transports `andrewcode-oneshot` and `andrewcode-acp` |

The Windows `C:/dev/...` path is a default compiled into `andrewcode_cli.py` and `detect.sh`. Override it with `FUSION_ANDREWCODE_ENTRY`.

**Symptom.** `AndrewCode Node entry is missing; no provider was called` and exit 127.

**Reproduced:**

```bash
bash scripts/fusion.sh dispatch andrewcode prompt.txt out.txt
```

```text
AndrewCode Node entry is missing; no provider was called
```

The exit code is 127, and the same text is written to `out.txt.log`. The same message appears for the `codex` seat when the host is not `codex`.

**Fix.** Build or install AndrewCode, then point `FUSION_ANDREWCODE_ENTRY` at its `dist/main.mjs` and make sure `node` is on `PATH`. Check with `bash scripts/fusion.sh detect`: `andrewcode:degraded` means the entry and `node` were found.

Related AndrewCode messages, all source-reviewed unless marked:

| Message | Cause | Fix |
| --- | --- | --- |
| `The installed AndrewCode build lacks --disable-tools/--ephemeral, so isolation cannot be enforced; no provider was called. Rebuild AndrewCode.` (exit 1) | The consult seat refuses to run unless `--help` advertises both flags (`andrewcode_cli.py:158`) | Rebuild AndrewCode from a version that has them |
| `AndrewCode capability check timed out; no provider was called` | `--help` took over 30 s | Check Node and the checkout, then retry |
| `AndrewCode CLI transport unavailable: verified tools-disabled, filesystem sandbox and ephemeral/non-persistent controls are not available for this legacy seat...` (exit 1) | `andrewcode.sh` is the tool-enabled worker seat. It always fails closed. `FUSION_ANDREWCODE_AUTO` does not bypass it | Use the Hive path (`spawn.py`) or the `codex` consult seat. This is by design |
| `AndrewCode consult timed out after <N>s; owned process stopped; no retry` (exit 124) | The consult exceeded `FUSION_ANDREWCODE_TIMEOUT` | Raise it |
| `AndrewCode exited <N>: authentication`, `model availability` or `CLI failure` | The exit is classified from stderr. Raw diagnostics are withheld on purpose | Log in to AndrewCode, check the model alias, run AndrewCode directly |
| `andrewcode not found on PATH` from `peers ask andrewcode` | The peer layer needs the `andrewcode` executable, not the Node entry | Put `andrewcode` on `PATH` |
| `andrewcode has no sandbox; tools='read' cannot be guaranteed (J-09)` | Read-only tools cannot be enforced for AndrewCode, so the peer layer rejects `--tools read` | Use `--tools none` or an authorized `write` |

Reproduced: `peers ask andrewcode --prompt hi --tools read` returns `{"error": {"code": "invalid_request", "message": "andrewcode has no sandbox; tools='read' cannot be guaranteed (J-09)"}}` with exit 2. Without the executable, `peers ask andrewcode --tools none` returns a result with `status: "absent"`, `rc: 127` and `error: "andrewcode not found on PATH"`.

Security note: AndrewCode has no provider sandbox. A scratch directory, a repo snapshot or `--permission allow_read` does not confine a tool-enabled AndrewCode worker. Use Fusion worktree or tool restrictions that match the authorized task. See [PEERS.md](PEERS.md).

### Peer policy refusals

**Symptom.** `python -m fusion_swarm.peers ask|route` exits 2 with `peer_not_allowed`.

| Message | Cause | Fix |
| --- | --- | --- |
| `host-native` for peer `claude` | You are on a Claude host, so Claude is the host and is not spawned | Fold Claude in-context. This is not an error |
| `claude spawn needs a codex host or FUSION_ALLOW_CLAUDE=1` | Spawning Claude needs a Codex host or explicit authorization | Set `FUSION_ALLOW_CLAUDE=1` only if you authorize it |
| `codex binary exec requires FUSION_ALLOW_CODEX_EXEC=1 or a codex host` | The `codex` binary is never executed unless the gate is open | Set `FUSION_ALLOW_CODEX_EXEC=1`, or accept the default route through AndrewCode |
| `codex help probe requires FUSION_ALLOW_CODEX_EXEC=1 or a codex host` | Same gate for capability probes | Same |
| `peer depth <N> exceeds FUSION_PEER_MAX_DEPTH=<limit>` (`depth_exceeded`) | A peer spawned a peer too many levels deep. The default limit is 2 (`DEFAULT_MAX_DEPTH`) | Flatten the chain, or raise `FUSION_PEER_MAX_DEPTH` knowingly |
| `opencode has no session transport` (`invalid_request`) | `--session` is not available for opencode | Drop `--session` |

On a Claude host with the gate closed, the `codex` peer is not refused. It is routed to `andrewcode-oneshot` with default model `gpt-6.1-sol`. With `FUSION_ALLOW_CODEX_EXEC=1` it routes to `codex-exec`. Exit codes for the peer CLI: 0 ok, 1 peer or board failure, 2 policy or usage error (`python/fusion_swarm/peers/__main__.py`).

**Reproduced** (Claude markers set, nothing on `PATH`):

```bash
CLAUDECODE=1 python3 -m fusion_swarm.peers route claude
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

The exit code is 2. `CLAUDECODE=1 ... route codex` gave `allowed: true`, `transport: andrewcode-oneshot`, `reason: "andrewcode seat (codex exec gated)"`, and adding `FUSION_ALLOW_CODEX_EXEC=1` gave `transport: codex-exec`, `binary: codex`.

The `fable` seat has its own gate: `fusion.sh dispatch fable ...` prints `Fable uses Claude Code; explicit user authorization requires FUSION_ALLOW_CLAUDE=1` and exits 3 (reproduced). `detect` shows `fable:disabled` until that variable is `1`.

### `python: command not found`, or an MCP server that never loads

**Cause.** Several entry points name `python`, not `python3`.

- `mcp.json` and `.mcp.json` start both MCP servers with `"command": "python"`.
- `codex.sh` and `andrewcode.sh` run `"${FUSION_PYTHON:-python}"`.
- `swarm.sh`, `hive.sh` and `board.sh` try `python`, then `py`, then `python3`, and print `[fusion] no python found (set FUSION_PYTHON)` (exit 127) if none exists.

macOS and some Linux systems ship only `python3`.

**Fix.** Put a Python 3.10 or newer on `PATH` under the name `python`, and export `FUSION_PYTHON=/path/to/python3` for the scripts. The installer records the interpreter it found as `FUSION_PYTHON`. Python 3.10 is the installer's guard (`find_python`). No runtime code checks the version, so an older interpreter may fail in unrelated ways. Source-reviewed.

## Review runs (`/fusion:ultimate-review`)

`scripts/ureview.sh` reports whether the reviewer could actually read the repository snapshot it was given. After each `plan-review` or `impl-review` it prints `REVIEW=<status> ...` and `REPO_READ=<state>` and appends both to `manifest.txt` in the run directory.

| `REPO_READ` | Meaning |
| --- | --- |
| `ok` | The stream log shows no sandbox failure, and any `REPO_RECEIPT` the reviewer emitted matched the real file |
| `blocked` | The log matches `CreateProcessAsUserW failed`, `exec error:.*sandbox` or a sandbox denial. The reviewer could not run a shell, so its findings are text-only |
| `none` | The reviewer ran tools-disabled (`mode: isolated-tools-disabled`), so the snapshot was never mounted. This is the normal state for the `codex` consult seat |
| `empty-snapshot` | The snapshot copied 0 files from a non-empty repo |
| `invalid-receipt` | The reviewer cited `REPO_RECEIPT: <path>:<line> <sha>` and the hash matched neither the line nor the file |
| `unverified` | No stream log was kept |
| `n/a` | No `--repo` was passed |

Source: `repo_read_status` and `verify_repo_receipt` in `scripts/ureview.sh`.

**`REPO_READ=blocked`.** The script tells you what to do: treat the findings as a review of your summary only, paste the load-bearing excerpts into the plan, and say so in the report. The log patterns come from a Windows run with a sandboxed CLI. Run the reviewer from an environment where it can start a shell. The old README advertised `REPO_READ:DEGRADED (sandbox_process_spawn_failed)` and a `verify-receipt` subcommand. Neither exists: the state is `blocked`, and receipts are checked automatically. `ureview.sh` accepts `init`, `plan-review`, `impl-review`, `status`, `version` and `help`.

**`REPO_READ=invalid-receipt`.** The reviewer cited a hash that does not match the repository file. Re-dispatch the review, ideally with worktree access, and do not rely on the citation. If neither `shasum` nor `sha256sum` is installed the check is skipped and the state is `ok`.

**`reviewer 'codex' is the host runtime — set FUSION_UREVIEW_REVIEWER to a different family (grok, opencode, copilot).`** The script exits 3. The reviewer must differ from the host. `ureview.sh` defaults `FUSION_HOST` to `claude` and the reviewer to `codex`. If you run under Codex, set `FUSION_UREVIEW_REVIEWER`. Reproduced with `FUSION_HOST=codex ... plan-review ... --dry-run`.

**`<provider>: repo snapshot failed or copied 0 files from non-empty repo (<dir>)`** (exit 1). The adapter could not copy the repo. The snapshot caps are `FUSION_SNAPSHOT_MAX_FILES` (default 4000) and `FUSION_SNAPSHOT_MAX_FILE_KB` (default 512). When files are skipped the snapshot contains `SNAPSHOT-TRUNCATED.md` that names the original path to read. Source: `fusion_snapshot_repo` in `_common.sh`.

## Boards

Fusion has two boards that share no code. HiveBoard is `board.py` behind `fusion-coordination` and VBoard is `python/fusion_swarm/vboard/` behind `fusion-board`. See [COORDINATION.md](COORDINATION.md) and [VBOARD.md](VBOARD.md).

### A board reports it is locked or busy

| Board | Behavior | Source |
| --- | --- | --- |
| HiveBoard | SQLite connection with `timeout=30`. A writer blocked for 30 s fails with `OperationalError: database is locked`. | `python/fusion_swarm/board.py:266` |
| VBoard | Retries `BEGIN IMMEDIATE` for 30 s, then fails with reason `unavailable` and `the board is busy; try again`. `busy_timeout` is 5000 ms per statement. | `python/fusion_swarm/vboard/store.py:246`, `:310` |

**Cause.** Another process holds the write lock for a long time. One write transaction is open at a time in WAL mode, so a crashed or stalled writer, an editor or backup tool holding the file, or a stuck long-running command blocks the rest.

**Reproduced (HiveBoard).** One process held `BEGIN IMMEDIATE` on the board file while another ran `board --db hb2.sqlite post --from a --body x`. After about 31 s it printed:

```json
{"ok": false, "error": "OperationalError: database is locked"}
```

For VBoard, a 9 s hold only delayed the same `post` by about 8 s and then succeeded.

**Fix.** Find and stop the process that has the file open (`lsof <board file>` on macOS and Linux), then retry. `unavailable` is marked retryable, so a caller can simply retry. Do not delete the `-wal` or `-shm` files while any process is still using the board. Check which agents are active with `python3 -m fusion_swarm.board --db <board> agents`. The board file also lives on a local disk only: SQLite WAL is not safe on network shares (general SQLite guidance, not enforced by Fusion).

### HiveBoard mail

Every mail delivery has a receipt in state `pending`, `leased`, `acked`, `dead` or `superseded` (`mailbox.py`, `protocol.py`). `receipts --message-id N` shows them. A receipt records storage and delivery facts only. It does not prove the recipient did the work.

| Message | Cause | Fix |
| --- | --- | --- |
| `delivery is dead or superseded; an explicit retry is required` | The delivery ran out of attempts or passed its deadline, or a newer progress message replaced it. The default budget is 8 attempts (`FUSION_MAIL_MAX_ATTEMPTS`) | `dead-letters --agent ID`, then `retry-delivery --agent ID --message-id N [--extra-attempts K] [--deadline T]`. Only the recipient can retry |
| `stale delivery lease token` | The token does not match the current lease, for example after a redelivery | Claim again and use the new `lease_token` |
| `an active delivery lease token is required` | The lease expired (default 60 s) or no token was sent | Claim again, then acknowledge before the lease ends |
| `sender cannot acknowledge its own message` | The sender tried to ack | Ack from the recipient |
| `message is absent or not visible to this agent` | Wrong message id or a private channel | Check the id and membership |

**Reproduced.** With `FUSION_MAIL_MAX_ATTEMPTS=1`, one `claim` followed by `nack` gave `{"message_id": 1, "agent_id": "b", "state": "dead"}`, and `ack` then failed with `{"ok": false, "error": "delivery is dead or superseded; an explicit retry is required"}` (exit 1). For mail sent with `send-message --envelope-file` (envelope `max_attempts: 1`), `retry-delivery --agent b --message-id 1` returned `{"message_id": 1, "state": "pending"}`.

**Known limitation, observed.** For mail sent with plain `post --to`, `retry-delivery` without `--deadline` failed with `{"ok": false, "error": "TypeError: 'NoneType' object is not subscriptable"}`. The cause is in `retry_delivery`: it reads `policy["deadline"]` from a policy row that only envelope mail has (`python/fusion_swarm/protocol.py:253`). Workaround: resend the message, or use `send-message` for mail that may need a retry.

### MCP calls fail with `board_path ...`

The `fusion-coordination` server is started with `--require-board`, so no board exists until a call names one. Each tool call needs an absolute `board_path`. Errors come back as a normal result with `isError: true`.

**Reproduced** (calls to `mcps/fusion-coordination/server.py --require-board`):

```text
board_path is required; use the prepared team's absolute board_path
board_path must be absolute
unknown tool: nonesuch
```

`board_path differs from this process's fixed board` means the server was started with `--db` or `--workspace` and the call named a different file. `tool actor differs from this process's bound agent` means `--agent-id` was fixed at startup. Fix by passing the prepared team's absolute board path, or by starting the server without a fixed board. The tool list is in [COORDINATION.md](COORDINATION.md).

### VBoard failures

CLI failures are JSON on stderr with `--json` (`{"status": "error", "reason": "<code>", ...}`) or `error: <message>` plus a next-step hint without it. The exit code is 2 for usage-class reasons and 1 for the rest (`_exit_code` in `vboard/cli.py`). The reason table is `_reasons()` in `python/fusion_swarm/vboard/model.py`.

| Reason | HTTP status | Retryable | Typical cause and fix |
| --- | --- | --- | --- |
| `claim_lost` | 409 | no | Your claim was released, expired or taken over. Stop work and do not complete. Claim a ready task again |
| `claim_not_held` | 409 | no | No cached claim for this `--as` profile. Claims are kept per profile, so use the same `--as` you claimed with |
| `task_finished` | 409 | no | The task is already completed, failed or canceled |
| `task_claimed` / `task_waiting` / `task_blocked` | 409 | no | The task is held by someone, waiting for its `needs`, or blocked |
| `task_not_owned` | 403 | no | Only the task owner may do that |
| `nothing_ready` | 404 | no | `tasks claim` found no ready task in that channel |
| `invalid_request` | 400 | no | A bad argument. The message names it, for example `lease: a lease is 30 to 3600 seconds` |
| `revision_conflict` | 409 | no | The message changed since you read it. Re-read, then retry with the new revision |
| `unauthorized` | 401 | no | The HTTP server needs `Authorization: Bearer <token>` |
| `rate_limited` / `unavailable` / `network_error` | 429 / 503 / 503 | yes | Retry with backoff |
| `not_supported` | 501 | no | The engine lacks the feature, or no `vb` engine was found |

**Reproduced: `claim_lost`.** Profile `alice` claimed task 3, released it, `bob` claimed it, and `alice`'s old claim file was put back. Then:

```bash
python3 -m fusion_swarm.vboard --db board.sqlite --as alice tasks complete 3 "done"
```

```text
error: Lost the claim on task #3: you no longer hold this claim
```

The exit code is 1. With `--json` the document is `{"status": "error", "reason": "claim_lost", ..., "data": {"retryable": false}}`. The CLI drops its cached claim when it sees `claim_lost`. In `tasks run`, a `claim_lost` during renewal stops the child process (`vboard/cli.py:1049`). Peer handoffs treat `claim_lost`, `task_finished`, `claim_not_held` and `task_not_owned` as lost and drop the result (`LOST_CODES` in `peers/handoff.py`).

**Reproduced: `claim_not_held`.** Completing a task you never claimed prints `You don't hold a claim on task #1 as alice.` with the hint `Claims are kept per --as profile: use the same --as you claimed with.` and exits 1.

**Lease too short.** `tasks claim --lease 1s` fails with `invalid_request`: a lease is 30 to 3600 seconds. Renew before it ends. Long work should run under `tasks run`, which renews for you.

**`no vb engine found`.** Without `--db` or `--url`, VBoard looks for the real `vb` engine and needs Node 22.18 or newer for it. Fix: pass `--db <file>` for the built-in SQLite board, run `python -m fusion_swarm.vboard.engine install --yes`, or set `FUSION_VBOARD_ENGINE`. Fusion never installs anything or runs `npx` on its own unless `FUSION_VBOARD_ALLOW_NPX=1` (`vboard/engine.py`). `python3 -m fusion_swarm.vboard.engine resolve` printed `{"engine": null}` (exit 1) when none was found. Source-reviewed for the install path.

**Claim files.** Claims are cached at `$XDG_CONFIG_HOME/board/claims/` (default `~/.config/board/claims`), one file per board, task and profile (`vboard/claims.py`). Deleting a file forgets the claim locally. It does not release it on the board.

## Runtime forms

The forms CLI is `python3 -m fusion_swarm.forms` and always prints one JSON document. See [FORMS.md](FORMS.md).

| Symptom | Cause | Fix |
| --- | --- | --- |
| `{"ok": false, "error": "usage", "message": "--run-dir must be empty. Use salvage to recover an existing run."}` (exit 2) | The run directory already has files | Use a new or empty directory. To recover an old run use `salvage` |
| `status: "budget_exhausted"`, `stop_reason: "budget"`, exit 1 | The call, token or deadline budget ran out first. `--budget-calls 0` allows no calls | Raise `--budget-calls`, `--budget-tokens` or `--deadline-s`. `forms plan` shows the default budget |
| `{"ok": false, "error": "usage", "message": "one of the arguments --task --task-file is required"}` (exit 2) | No task | Pass `--task` or `--task-file` |
| `salvage` returns `"status": "empty"` and exit 0 | The directory does not exist, has no journal, or holds unreadable data. `salvage` never raises | Check the path. A usable run gives `"status": "salvaged"` |
| Exit 130, `status: "cancelled"` | SIGINT or SIGTERM. Prior outputs are kept | `salvage <run_dir>` recovers the best recorded result |

Exit codes: 0 for `ok` and `partial` and for the read-only commands, 1 for `failed` or `budget_exhausted` or an error, 2 for usage errors, 130 for `cancelled` (`python/fusion_swarm/forms/__main__.py`).

**Reproduced.** `FUSION_TEST_HOOKS=1 FUSION_FORMS_FAKE_DISPATCH=fx.json python3 -m fusion_swarm.forms run dry --task find --providers mock --budget-calls 0 --run-dir rd1` printed a result with `"status": "budget_exhausted"`, `"stop_reason": "budget"`, `"calls": 0`. Reusing `rd1` then gave the `--run-dir must be empty` error. `salvage rd1` returned `"status": "salvaged"`, `"run_status": "budget_exhausted"`. `salvage` on a missing directory returned `"status": "empty"`. The run directory contains `journal.jsonl`, `checkpoint.json`, `result.json` and `outputs/`.

Without `FUSION_TEST_HOOKS=1` the fake-dispatch fixture is ignored and a run dispatches to real providers. Never set the fixture to rehearse a command you have not read. Run directories hold your task outputs, so treat them as sensitive.

## Hive

`python -m fusion_swarm hive "<task>" --dry-run` builds the board and the agents without launching any process. Live runs need AndrewCode. See [HIVE.md](HIVE.md).

| Message | Cause | Fix |
| --- | --- | --- |
| `unknown architect 'nope'; expected one of ['astra', 'codex', 'fable']` | Bad `--architect` | Use one of the listed names. Reproduced |
| `SwarmSpec: unknown field(s): ['bad']` or `invalid SwarmSpec: ...` | A `--spec` file failed validation. Spec validation fails closed | Fix the fields the message names. Reproduced for the unknown-field case |
| `cannot spawn captain <id>: <reason>` or `cannot spawn child of <id>: <reason>` | A `SpawnLimits` gate refused (depth, children or total agents) | Lower the captains or children, or set limits in the spec. Defaults live in `spawn.py` |
| `depth=<N> exceeds max_depth=<M>` | Spawn depth over the limit | Reduce nesting |
| `fusion_swarm.spawn is required for a live hive run (dry_run=False); it is missing` | A damaged install | Reinstall |

A live run without a spec returns a design packet instead of calling a model. A dry run never spawns a process (`hive.py`). Source-reviewed except where marked.

## Leftover workspaces after a killed run

**Symptom.** Directories named `fusion-wt-*`, `fusion-clone-*`, `fusion-race-*`, `fusion-harden-*` or `fusion-run-*` remain in your temp directory, or `git worktree list` shows paths that no longer exist.

**Cause.** The runtime creates these with `tempfile.mkdtemp` and removes them on a clean exit (`workspaces.py`, `race.py`, `harden.py`, `spawn.py`). The `fusion-spawn-*` work directory of a live spawn is not removed by `spawn.py`. A hard kill skips the cleanup. Only the `worktree` workspace mode registers a detached git worktree in the source repo (`scope.provision_worktree`). Race, harden and clone modes copy files (`clonefile` on macOS, falling back to a plain copy), so they leave only directories. The old README pointed at `.fusion/worktrees/`. Nothing in the code uses that path.

**Fix.** Delete the leftover directories, then in the source repo run `git worktree prune` (or `git worktree remove --force <path>` for one). `WorktreeWorkspace.cleanup` does the same with `git worktree remove --force`. Source-reviewed.

Related messages:

- `worktree mode requires explicit repo_root and authorized=True; use workspace_mode='proposal' in Phase 1.` is a deliberate refusal, not a fault (`workspaces.py:124`).
- `worktree destination must be new and separate` means the target path already exists or overlaps the source (`scope.py:235`). Pick a fresh path.
- `Git worktree capability is unavailable for this workspace` means the workspace has no `.git` or `git` is not installed.

## Cockpit and terminal view

| Symptom | Cause | Fix |
| --- | --- | --- |
| A POST returns 403 `missing or invalid session token` | Mutating endpoints need the per-process `X-Fusion-Token` plus an allowed `Origin`. The token is embedded in the served page and changes on every start | Use the page the server serves, not a saved token. Reproduced: a POST with an allowed origin and no token |
| 403 `origin not allowed` | Missing or foreign `Origin` header | Send the dashboard's own origin |
| 421 `misdirected request: invalid Host header` | The `Host` header was not `127.0.0.1`, `localhost` or `::1` | Open it through `127.0.0.1`. Reproduced with `Host: evil.example` |
| The dashboard is not on port 8765 | The server tries `FUSION_DASHBOARD_PORT` (default 8765) and the next 49 ports, and uses the first free one | Read the `dashboard listening on` line it prints. Python buffers it when stdout is redirected |
| The browser does not open | `FUSION_DASHBOARD_NO_OPEN=1` is set | Open the printed URL |
| The terminal view is empty: `no tasks recorded on board`, `Agents: 0` | The default board is `<workspace>/.fusion/board.sqlite`. A missing file, or any error opening it, gives an empty view with no message (`tui.py`) | Pass `--board-db <file>` (and `--workspace`). Reproduced on an empty workspace |

The server binds `127.0.0.1` only (`HOST` in `scripts/dashboard_server.py`). The old README described `GET /api/stream`, `POST /api/board/post` and a four-pane curses view. The server exposes `/api/events` (SSE) and has no board-post route, and `fusion_swarm tui` prints a one-shot text or JSON snapshot. See [COCKPIT.md](COCKPIT.md).

## Install problems

The installers are `install.sh`, `install.ps1` and the per-host `install-*.command` and `install-*.ps1` wrappers. Options are in [INSTALL.md](../INSTALL.md). A failed run prints `Fusion installation failed: <message>` or `<host> installation failed: <message>`, writes `logs/latest-install.json` with status `failed`, and exits 1. Source: `scripts/fusion_install.py`.

| Message | Cause | Fix |
| --- | --- | --- |
| `Select Git for Windows Bash, not WSL Bash.` (`install.ps1`) | The chosen `bash.exe` is under `System32` or `Sysnative`, which is WSL | Install Git for Windows and pass its Bash (`-GitBash`). The Python adapters also pin Git Bash and honor `FUSION_BASH` (`adapter._find_bash`) |
| `Bash with sed and awk was not found. Use --bash /path/to/bash.` | No usable Bash | Install Bash, or pass `--bash` |
| `The selected Python must be 3.10+ with SQLite support.` or `Python 3.10+ was not found after installation. Rerun in a new terminal or use --python /path/to/python3.` | Python too old, or not yet on `PATH` after an install | Open a new terminal or pass `--python` |
| `Homebrew (brew) is required to install <package> automatically on macOS.` | A dependency is missing and `brew` is absent | Install Homebrew, or install the package yourself and rerun |
| `<package> is required; --skip-dependencies prevents installing it.` | `--skip-dependencies` with a missing tool | Install it, or drop the flag |
| `AndrewCode is a personal fork. Supply --andrewcode-source /path/to/andrew-code with its native installer; upstream Kimi is not substituted.` | No AndrewCode checkout was found | Pass `--andrewcode-source` |
| `AndrewCode requires Node.js. Install Node and rerun.` | No `node` | Install Node |
| `Another installer owns <dir>/.fusion-install.lock; reconcile a stopped installer before removing its lock` | A lock file from a running or killed installer | Confirm no installer is running, then remove that file. The lock lives in the install root, or under `<AndrewCode home>/plugins` for the Kimi install |
| `failed to back up plugin cache entry: Access is denied` (also `os error 5`, `Permission denied`) | Codex has its Fusion plugin cache open | Close Codex fully, then rerun `install-codex.command` (macOS) or `install-codex.ps1` (Windows) from a separate terminal. The installer appends this advice itself and restores the previous marketplace entry |
| `Marketplace <name> is not exclusively owned by Fusion. Existing configuration was preserved.` | A marketplace with the same name is not Fusion's | Resolve the name clash by hand. The installer will not overwrite it |
| `Installed MCP lacks explicit board isolation` | The installed coordination MCP was not started with `--require-board` | Reinstall from a current checkout |

The installer's verification steps check the installed forms, the coordination MCP handshake and the host's own plugin listing, so "installed" means each of those passed. The `--dry-run` flag prints the plan and does not run these checks. Reproduced: `bash install.sh --dry-run` printed `Fusion dry run: source=...`, the install root, the host list and `SkipDependencies=False; SkipJq=False; NoPathUpdate=False` without checking Python or Bash. Installation does not log you in to any provider. The Codex Access-denied case is covered by `tests/test_windows_install.py`.

## Still stuck

1. Run the [diagnostics](#collect-diagnostics-first) and keep the output.
2. Re-run the failing step by its shell form (`fusion.sh dispatch`, `ureview.sh`, `forms run ... --budget-calls 0`) so stderr is visible.
3. Check [CONFIGURATION.md](CONFIGURATION.md) for a variable that overrides the behavior.
4. Open an issue with the command, exit code, `detect` and `peers policy` output and the matching `<provider>.log`, with secrets removed.

## Related

- [GETTING_STARTED.md](GETTING_STARTED.md) for install and the first offline run.
- [PEERS.md](PEERS.md) for providers, transports and the Codex gate.
- [VBOARD.md](VBOARD.md) and [COORDINATION.md](COORDINATION.md) for the two boards.
- [FORMS.md](FORMS.md) for runtime forms, budgets and salvage.
- [COCKPIT.md](COCKPIT.md) for the dashboard and terminal view.
