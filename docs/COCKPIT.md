# Cockpit: web dashboard and terminal view

The cockpit is the set of local observation tools that ship with the plugin: a loopback web dashboard (`scripts/dashboard_server.py`) and a one-shot terminal snapshot (`python/fusion_swarm/tui.py`). Read this page if you want to start them, understand what they show, or call the dashboard's HTTP API from your own tooling.

Version: `3.3.0+swarm`. Both tools use only the Python standard library. The repository is `agent-mesh`, but the slash command (`/fusion:dashboard`), the Python package (`fusion_swarm`) and the environment variables (`FUSION_*`) keep the `fusion` name.

## What it is, and what it is not

- The **web dashboard** has four tabs: Providers & Models, Config, Swarm Modes and Active Run. It edits `config/defaults.env`, shows provider presence and discovered models, shows every mode's resolved roster, and supervises one prepared execution run.
- The **terminal view** (`fusion_swarm.tui`) prints a plain-text snapshot of board tasks, agents, recent stigmergy traces and a `git diff` of a workspace. It prints once and exits. It is not a `curses` application and has no panes or keys.
- Older docs mention `GET /api/stream`, `POST /api/board/post`, a four-pane `curses` TUI with `q` and `r` keys, and a `--once` flag. None of these exist in the source. The sections below describe what the code does.

## Start the dashboard

Prerequisites: Python 3 on `PATH` (`dashboard.sh` tries `python3`, `python`, then `py`). A provider CLI is not needed to start the server.

```bash
bash scripts/dashboard.sh
```

The server prints its URL and the first eight characters of the session token, then opens the page in your default browser:

```text
[fusion] dashboard listening on http://127.0.0.1:18800/
[fusion] session token (do not share): tkEuLuWn...
```

Stop it with Ctrl-C. It does not daemonize.

### Options

The server reads no command-line arguments. `dashboard.sh` passes `"$@"` through, but `main()` in `scripts/dashboard_server.py` ignores them. Two environment variables control it.

| Variable | Default | Effect |
| --- | --- | --- |
| `FUSION_DASHBOARD_PORT` | `8765` | Preferred port. If it is taken, the server tries the next 49 ports in order and uses the first free one. |
| `FUSION_DASHBOARD_NO_OPEN` | unset | Set to `1` to skip opening a browser tab. |

The `/fusion:dashboard` command maps its arguments onto these variables: `--port N` becomes `FUSION_DASHBOARD_PORT=N` and `--no-open` becomes `FUSION_DASHBOARD_NO_OPEN=1` (see `commands/dashboard.md`). Running `bash scripts/dashboard.sh --port 9000` directly does not change the port.

I confirmed the fallback by starting a dummy listener on port 18820 and then starting the dashboard with `FUSION_DASHBOARD_PORT=18820`. It reported `http://127.0.0.1:18821/`.

### Supervise a run (optional)

The Active Run tab, `/api/run`, `/api/run/export`, `/api/run/control` and `/api/events` operate on one prepared execution run. Bind it with three environment variables before starting the server:

```bash
export FUSION_BOARD=/absolute/path/to/board.sqlite
export FUSION_LEAD=lead
export FUSION_RUN=my-run-id
bash scripts/dashboard.sh
```

`FUSION_BOARD` must be an absolute path to an existing file. Otherwise `/api/run` fails with HTTP 500 and the message `FUSION_BOARD must identify an existing absolute board path`. With any of the three unset, `/api/run` returns `{"available": false, "reason": "Set FUSION_BOARD, FUSION_LEAD and FUSION_RUN ..."}` and control is refused.

The board is a [HiveBoard](COORDINATION.md) SQLite file. The run itself is created by the team runtime (see [SWARM_RUNTIME.md](SWARM_RUNTIME.md)).

## The four tabs

The page is one self-contained HTML file with no external assets. Every control is rendered from `GET /api/schema`, so no model id or enum value is hardcoded in the HTML (enforced by `tests/test_dashboard_html_contract.py`).

| Tab | Shows | Data source |
| --- | --- | --- |
| Providers & Models | Presence and discovered models per provider, a per-card Refresh, and "Refresh all live" | `/api/providers`, `/api/discovery`, `POST /api/discovery/refresh` |
| Config | Every allowlisted `FUSION_*` key with its effective value and which layer supplies it, with a Save button per field | `/api/schema`, `POST /api/config` |
| Swarm Modes | Every `/fusion:<mode>` with its resolved roster and overridable keys | `/api/modes/resolved` |
| Active Run | Run state, task table, dependency list, runtimes, file ownership, inbox backlog, next actions, a worktree diff drawer, a pause/resume control and an evidence export | `/api/run`, `/api/diff`, `/api/events`, `POST /api/run/control`, `/api/run/export` |

Details worth knowing:

- Model fields offer discovered models only when discovery returned `live` or `degraded` status with a non-curated model list. Otherwise the field shows "discovery unavailable" and only the `Custom…` escape hatch.
- The Config tab shows the precedence chain `explicit env > per-mode key > defaults.env > plugin config > baked default`. If the shell exports the key, the field says edits will not take effect.
- OpenCode models are grouped into "Subscription (opencode-go)" and "Direct API (billed per use)" server-side.
- Selecting a task row in the Active Run tab loads `/api/diff?task_id=<id>`. The task id is echoed back but does not narrow the diff (see `/api/diff` below).
- Opening the Active Run tab starts an `EventSource` on `/api/events`. A `board_wakeup` frame triggers a reload of the run and diff views.

## HTTP API

Source: `scripts/dashboard_server.py` (`Handler.do_GET`, `Handler.do_POST`). Every response except `/` and the SSE stream is JSON with `Cache-Control: no-store`. Every JSON body passes through `_redact`, which replaces the value of any key matching an api-key, secret-key, access-token, auth-token, client-secret, credential, password or passwd pattern with `«redacted»`.

To re-check this table, read `do_GET` and `do_POST`, or run the server and `curl` each path as shown below.

### GET endpoints

| Path | Response |
| --- | --- |
| `/` | The HTML page with the literal `__FUSION_TOKEN__` replaced by this process's session token. |
| `/api/providers` | `{providers, correlation_groups}`. Each provider carries `tier`, `model_family`, `harness`, `model`, `strengths`, `sessions`, `available`, `presence`, `cache_age_seconds`, `selectable_as_worker`. Presence comes from `scripts/providers/detect.sh --json`, a `command -v` style check. |
| `/api/schema` | `{schema, groups, values, precedence, caveat, opencode_family_note}`. `schema` is one descriptor per key, `groups` the ordered UI groups, `values` the effective value and source per key. |
| `/api/discovery` | Cache-only model report for every provider. It never spawns a probe. Add `?provider=<name>` for one provider. |
| `/api/modes/resolved` | `{modes: [...]}`. Each entry has `mode`, `description`, `engine_pattern`, `invokes_skill`, `argument_hint`, `resolution`, `roster`, `roster_note`, `overridable`. |
| `/api/run` | The supervision view of the bound run, or `{available: false, reason}`. |
| `/api/run/export` | The same view plus evidence fields (`tasks`, `communication`, `verification_boundary`). |
| `/api/diff` | `{available, task_id, workspace, diff}`. Query: `task_id`, `workspace`. |
| `/api/events` | Server-Sent Events. Add `?once=1` (or `true`, `yes`) for a single snapshot frame, then close. |

Any other path returns HTTP 404 `{"error": "not found"}`. An unexpected exception returns HTTP 500 with `{"error": "<message>"}` and no stack trace.

### POST endpoints

All POST bodies are JSON. Every POST needs the headers described under [Binding and authentication](#binding-and-authentication).

| Path | Body | Behaviour |
| --- | --- | --- |
| `/api/config` | `{key, value}` | Validates against the schema, then calls `config_store.set_value`. Returns `{ok, result: {key, value, backup, lines_updated}, custom}`. |
| `/api/discovery/refresh` | `{provider}` | Probes one provider live (`discovery.discover(provider, force=True)`). An unknown provider returns 400. |
| `/api/run/control` | `{state, note, expected_version}` | Pauses or resumes the bound run. Returns `{ok, result}`. |

Errors from `config_store.ConfigError` and `BoardError` return HTTP 400. Other exceptions return 500.

### `/api/config` semantics

- A key that is not in the schema is refused with 400 (`'<key>' is not a configurable key`).
- Integer fields must match `-?\d+`, or be empty. Otherwise 400 (`<key> must be an integer`).
- Enum values outside the suggested choices are accepted and flagged `"custom": true`. Model and text fields accept any value, so the server never flags them. The page flags a custom model against the discovery list.
- The write is surgical. Only the target line in `config/defaults.env` changes, and a timestamped copy is saved first under `config/.backups/`. In my run, a write of `FUSION_CODEX_TIMEOUT=900` produced `config/.backups/defaults.env.20261121-234859.bak` and `"lines_updated": 1`.
- Writes go to the plugin's `config/defaults.env`, not to your environment. See [CONFIGURATION.md](CONFIGURATION.md) for the layers.

### `/api/run/control` semantics

The body must contain exactly the three keys `state`, `note` and `expected_version`. Any other shape returns 400 `control needs state, note and expected_version`. The server then calls `HiveBoard.control_run`, which (in `python/fusion_swarm/execution.py`):

- accepts only `state` of `paused` or `active`, and requires a non-empty `note`;
- compares `expected_version` with the run's current `version`. A mismatch returns 400 `stale run control version`, unless the same change was already applied (an identical retry returns the current run);
- refuses to set `active` when the run's recorded capabilities lack `dispatch` or lack a model the plan's roster needs (`resolve unavailable capabilities before activating this run`);
- increments `version` by one and appends a `run_events` row.

A paused run refuses new dispatch (`run dispatch is paused or blocked`, `execution.py`). Nothing in the control path stops a worker that is already running.

### Server-Sent Events frames

`/api/events` sends `Content-Type: text/event-stream`. The frames are:

| Frame | When | `data` |
| --- | --- | --- |
| `event: snapshot` | Once, immediately on connect | The `/api/run` JSON. |
| `event: board_wakeup` | After the board's wakeup file changes (any committed board write) | The `/api/run` JSON, re-read. |
| `: keepalive` (comment line) | After a 15 s wait with no change | none |

When no run is bound, the loop sleeps 15 s between keepalives and never emits `board_wakeup`. The board wait uses `wait_for_board_change("<board>.wakeup", timeout=15.0)` from `python/fusion_swarm/board.py`.

### Binding and authentication

The server binds to `127.0.0.1` only. It checks these on every request:

| Check | Applies to | Failure |
| --- | --- | --- |
| `Host` header host part is `127.0.0.1`, `localhost`, `[::1]` or `::1` (a missing header passes) | GET and POST | 421 `misdirected request: invalid Host header` |
| `Origin`, if present, equals `http://127.0.0.1:<port>` or `http://localhost:<port>` | GET and POST | 403 `origin not allowed` |
| `Origin` header must be present and allowed | POST only | 403 `origin not allowed` |
| `X-Fusion-Token` equals the per-process session token (constant-time compare) | POST only | 403 `missing or invalid session token` |

The token is random per process (`secrets.token_urlsafe(32)`), is embedded in the served page, and is never written to disk. A non-browser client gets it by fetching `/` and reading the `const TOKEN = "..."` line.

Read endpoints need no token, so any local process can read provider presence, config values and run state. Treat the dashboard as a same-user local tool, not a multi-user service.

### Try it with curl

These commands were run against a scratch copy with `HOME` pointed at a temporary directory, on an ephemeral port. Replace the port with the one your server printed.

```bash
B=http://127.0.0.1:18800
curl -s "$B/api/run"
curl -s "$B/api/events?once=1"
curl -s -X POST "$B/api/config" -H "Origin: $B" \
  -d '{"key":"FUSION_CODEX_TIMEOUT","value":"900"}'
```

With no run bound, the first two return:

```text
{
  "available": false,
  "reason": "Set FUSION_BOARD, FUSION_LEAD and FUSION_RUN to supervise a prepared execution run."
}
event: snapshot
data: {"available": false, "reason": "Set FUSION_BOARD, FUSION_LEAD and FUSION_RUN to supervise a prepared execution run."}
```

The POST has the right `Origin` but no token, so it is refused:

```text
{
  "error": "missing or invalid session token"
}
```

Other results from the same session:

| Request | Status | Body |
| --- | --- | --- |
| `POST /api/config` with no `Origin` | 403 | `origin not allowed` |
| `POST /api/config` with `Origin: http://evil.example` and a valid token | 403 | `origin not allowed` |
| `POST /api/config` with a valid token and `FUSION_NOT_REAL` | 400 | `'FUSION_NOT_REAL' is not a configurable key` |
| `POST /api/config` with a valid token and value `abc` for `FUSION_CODEX_TIMEOUT` | 400 | `FUSION_CODEX_TIMEOUT must be an integer` |
| `POST /api/config` with a valid token and `{bad` as body | 400 | `invalid JSON body` |
| `POST /api/discovery/refresh` with provider `bogus` | 400 | `unknown provider 'bogus'` |
| `POST /api/run/control` with no run bound | 400 | `no execution run is bound to this dashboard` |
| `GET /api/run` with `Host: evil.example` | 421 | `misdirected request: invalid Host header` |
| `GET /api/run` with `Origin: http://evil.example` | 403 | `origin not allowed` |
| `GET /nope` | 404 | `not found` |

### A bound run, end to end

`tests/fixtures/dashboard_run.py` builds a real board and a run with a local fixture worker and no model call:

```bash
PYTHONPATH=python python3 tests/fixtures/dashboard_run.py /tmp/cockpit-demo
```

It prints the board path, run id and lead id (`acceptance-swarm`, `lead`). I started the server with those three variables and read `/api/run`. Trimmed, the top-level keys and state were:

```text
keys: available, schema_version, run, readiness, mail_backlog, dependencies,
      ownership, candidate_reviews, next_actions, history, observed_at
run.observed_state: awaiting_review
run.tasks: build (review, attempt 1), verify (queued, attempt 0)
dependencies: verify depends_on [build]
next_actions: review_task build - "Inspect the current artifact, review findings and reported checks"
```

Then a pause, a stale resume and a malformed control body:

```bash
curl -s -X POST "$B/api/run/control" -H "Origin: $B" -H "X-Fusion-Token: $TOKEN" \
  -d '{"state":"paused","note":"inspect first","expected_version":1}'
```

| Control body | Status | Result |
| --- | --- | --- |
| `{"state":"paused","note":"inspect first","expected_version":1}` | 200 | Run `state` becomes `paused`, `version` 2, `history` gains one `paused` event with the note. |
| `{"state":"active","note":"stale","expected_version":1}` | 400 | `stale run control version` |
| `{"state":"paused","note":"x"}` | 400 | `control needs state, note and expected_version` |
| `{"state":"bogus","note":"x","expected_version":2}` | 400 | `run control requires active/paused and a reason` |

An SSE client connected before the pause received the `snapshot` frame, then a `board_wakeup` frame after the control write.

## Run view fields

`GET /api/run` is built by `python/fusion_swarm/supervision.py` (`status`) and passed through `supervision.redact`, which masks secret-shaped keys and bearer or `sk-`/`ghp_` style tokens in strings while keeping numeric usage counters. The page renders these parts:

| Field | Meaning |
| --- | --- |
| `run` | Run record: `id`, `lead_id`, `host`, `capabilities`, `bounds`, `state` (`active` or `paused`), `reason`, `version`, `dispatches`, `deadline_at`, plus derived `observed_state`, `tasks`, `runtimes`, `provider_usage`. |
| `readiness` | Per-agent runtime readiness rows. |
| `mail_backlog` | Inbox counts per agent and state. |
| `dependencies` | Per task: `depends_on`, `candidate_of`, `owner`. |
| `ownership` | Active file reservations (path, workspace, agent, task, expiry). |
| `candidate_reviews`, `history` | Candidate review records and run events. |
| `next_actions` | Operations the lead should consider next, each with a reason. |

`provider_usage` is `null` ("unavailable" on the page) unless a runtime ticket reported usage. The export view adds a `verification_boundary` field that states what the evidence does and does not prove.

## The control schema

`scripts/dashboard_schema.py` is the single declarative description of every control. It maps each key in the `config_store.parse_defaults()` allowlist to a descriptor `{key, widget, group, label, help, provider, choices, unit, custom_allowed, mode, field}`, where `widget` is `model`, `enum`, `int` or `text`.

Counts at the time of writing, from `build_schema()`: 119 descriptors (51 `model`, 40 `enum`, 23 `int`, 5 `text`) in 22 groups, 12 of which are per-mode groups. Regenerate them with:

```bash
PYTHONPATH=python:scripts python3 -c "
import dashboard_schema as d
from collections import Counter
s = d.build_schema()
print(len(s), Counter(x['widget'] for x in s), len(d.group_order()))"
```

Rules the code and `tests/test_dashboard_schema.py` enforce:

- The schema covers exactly the `parse_defaults()` allowlist, with no surplus or missing keys. A key can never render a control the writer would reject, nor be writable without a control.
- `validate_value` re-checks every POST against the same schema. It rejects unknown keys and non-integer ints, and accepts any enum value outside the suggested choices as `custom`.
- Grok effort is a plain text field with a warning, because the source notes that grok's default model rejects reasoning effort with HTTP 400.
- No model inventory lives in the schema. Model choices come only from live discovery.

## Terminal view

Prerequisite: none beyond Python 3. A workspace is optional, and a board file is optional. `git` is used for the diff section when the workspace has a `.git` directory.

There are two equivalent invocations. They differ in the flag name for the board file.

| Invocation | Board flag |
| --- | --- |
| `python3 -m fusion_swarm.tui` | `--db` |
| `python3 -m fusion_swarm tui` or `bash scripts/fusion.sh tui` | `--board-db` |

```bash
export PYTHONPATH=python
python3 -m fusion_swarm.tui --workspace /path/to/workspace --db /path/to/board.sqlite
```

Output against the fixture run from above:

```text
======================================================================
 FUSION TUI — Active-Instance & Diff Viewer
 Workspace: <scratch>/fx/workspace
 tmux capability: available=True inside_tmux=False
======================================================================
[Active Tasks: 2 | Agents: 2 | Stigmergy Trails: 0]
  * build          [review     ] owner=-          
  * verify         [queued     ] owner=-          

[Active-Instance Diff Tab]
(no git repository at workspace)
```

### Flags (`python3 -m fusion_swarm.tui`)

| Flag | Default | Effect |
| --- | --- | --- |
| `--workspace` | `.` | Directory for the diff and for locating the default board. |
| `--db` | empty | Board file. Empty means `<workspace>/.fusion/board.sqlite`. |
| `--json` | off | Print the snapshot as JSON instead of text. Top-level keys: `board`, `db_path`, `diff`, `stigmergy`, `tmux`, `workspace`. |
| `--tmux` | off | Print the `tmux split-window` command and the snapshot. Launches a split pane only when `tmux` exists, you are inside tmux and `--dry-run` is not set. |
| `--dry-run` | off | With `--tmux`, never execute. |

Via `python3 -m fusion_swarm tui` the common flags `--workspace` (or `--cwd`), `--board-db`, `--json`, `--tmux` and `--dry-run` are the shared parser's flags, and `tui` needs no task argument. `python3 fusion.py` forwards `tui` to the same parser.

### Sections

| Section | Source |
| --- | --- |
| Header | `workspace` and `tmux` capability from `shutil.which("tmux")` and `$TMUX`. |
| Active Tasks line | Counts from `HiveBoard.tasks()`, `agents()` and `sense_traces(limit=10)`. At most 12 tasks are printed, each with id, state, owner and title. |
| Stigmergy Pheromone Trails | Printed only when traces exist. Shows at most 6 (locus, kind, strength, agent, note). |
| Active-Instance Diff Tab | `git status --short` (first 30 lines) and `git diff --no-color` (first 120 lines), each with a 10 s timeout. Reports `(no git repository at workspace)` or `(working tree clean)` when applicable. |

### Limits to know

- If the board file does not exist or cannot be opened, the board section is silently empty (the code catches every exception). The text then reads `(no tasks recorded on board)`.
- The `board` object always carries empty `findings`, `questions` and `decisions` lists. They are placeholders and are never filled.
- The `tmux` command line the tool prints does not pass `--db` or `--board-db`, so a pane it opens uses the default board path for that workspace.
- Dashboard diffs and the terminal diff share `tui.collect_workspace_diff`. Both only work when the workspace itself has a `.git` entry, not a parent repository.

## Where the data comes from

| Data | Module |
| --- | --- |
| Provider presence | `scripts/providers/detect.sh --json`, called through `fusion_swarm.adapter` |
| Model lists and cache | `fusion_swarm/discovery.py`, cache-only on reads |
| Config values, precedence, writes and backups | `fusion_swarm/config_store.py` |
| Mode rosters | `fusion_swarm/mode_roster.py`, `fusion_swarm/modes_catalog.py` |
| Run status, export, control | `fusion_swarm/execution.py`, `fusion_swarm/supervision.py` (HiveBoard) |
| Board change wakeups | `fusion_swarm/board.py` (`wait_for_board_change`) |
| Workspace diff | `fusion_swarm/tui.py` (`collect_workspace_diff`) |

`/api/diff` resolves its workspace in this order: the `workspace` query parameter, `FUSION_WORKSPACE`, the directory that holds the bound board's `.fusion/` folder (or the board's own directory), then the plugin root. The `workspace` query parameter accepts any path on the machine, so the dashboard can show a `git diff` of any local repository to any local process that can reach the port.

## Tests

| Test | Covers |
| --- | --- |
| `tests/test_dashboard_api.py` | Schema, cache-only discovery, token and origin gate, schema revalidation, isolated config write |
| `tests/test_dashboard_run.py` | Bound run view, pause and resume, stale control, export |
| `tests/test_dashboard_schema.py` | Allowlist equality, descriptors, `validate_value` |
| `tests/test_dashboard_html_contract.py` | Single file, no external assets, four tabs, no hardcoded models or enums |

```bash
PYTHONPATH=python python3 -m unittest tests.test_dashboard_api tests.test_dashboard_run \
  tests.test_dashboard_schema tests.test_dashboard_html_contract
```

These 34 tests passed when I ran them (2.3 s). The dashboard HTML was not exercised in a browser while writing this page.

## Related

- [CONFIGURATION.md](CONFIGURATION.md) for the config layers the Config tab edits
- [COORDINATION.md](COORDINATION.md) for the HiveBoard behind the Active Run tab
- [SWARM_RUNTIME.md](SWARM_RUNTIME.md) for how runs, bounds and control are created
- [PEERS.md](PEERS.md) for the providers shown on the Providers & Models tab
