# Contributing to Agent Swarm Fusion

This file is for people changing the plugin itself: its Python engine, provider scripts, commands, skills and docs. If you only want to install and use it, start with [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md).

The repository is named `agent-swarm-fusion`. The plugin id, the `/fusion:*` slash commands, the Python package (`fusion_swarm`), the MCP servers (`fusion-coordination`, `fusion-board`) and the `FUSION_*` environment variables keep the `fusion` name. Do not rename them.

[CLAUDE.md](CLAUDE.md) holds the original developer notes that agents read when working in this repo. This page is the human-facing version and is checked against the code.

## Set up

There is nothing to install. The engine is Python standard library only, and the repo has no `pyproject.toml`, `setup.py` or `requirements*.txt`.

Requirements, as enforced by the installers and used by the test run below:

- Python 3.10 or newer with `sqlite3` (`install.sh` line 119 and `scripts/fusion_install.py` line 580 refuse anything older).
- Bash. The suite ran on macOS system bash 3.2.57.
- `jq` is optional. `tests/validate.sh` falls back to `python3`, `py`, `python` or `node` for JSON checks.
- Node.js is optional, and only needed for tests that exercise the AndrewCode entry (see [Known environment-dependent failures](#known-environment-dependent-failures)).

Work from the repository root and put the package on the path:

```bash
export PYTHONPATH="$PWD/python"
python3 -m fusion_swarm --help
```

Never run a real provider CLI (`claude`, `codex`, `gemini`, `grok`, `copilot`, `opencode`, `andrewcode`, `ollama`, `kimi`) to check your change. Use `--dry-run`, fake CLIs under `tests/fixtures/fake_cli/`, or the fake entry script pattern in `tests/test_andrewcode_transport.py`.

## Where things live

| Path | What it holds |
| --- | --- |
| `python/fusion_swarm/` | The engine: patterns (`moa.py`, `heavy.py`, `graph.py` and others), `adapter.py` (provider dispatch), `modes_catalog.py`, `board.py` (HiveBoard), `team.py`, `hive.py`, and the subpackages `forms/`, `peers/`, `vboard/` |
| `scripts/` | Bash bridges (`fusion.sh`, `swarm.sh`, `route.sh`, `ureview.sh`, `ultracode.sh`), installers (`fusion_install.py`, `install-kimi.py`), the dashboard server |
| `scripts/providers/` | One adapter per provider (`codex.sh`, `copilot.sh`, `opencode.sh`, `grok.sh`, `kimi.sh`, `andrewcode.sh`, `agy.sh`, `fable.sh`), plus `_common.sh` and `detect.sh` |
| `commands/` | One `commands/<mode>.md` per `/fusion:<mode>` slash command |
| `skills/` | Skill prose that hosts load. This is the program for the host-driven modes |
| `mcps/` | Launchers for `fusion-coordination` and `fusion-board` (`server.py` each) |
| `config/defaults.env` | Shipped defaults, including the commented per-mode override keys |
| `tests/` | `unittest` modules (`test_*.py`), shell tests, `validate.sh`, and `fixtures/` |
| `docs/` | Documentation. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the module map |
| `ultracode/` | Vendored UltraCode proxy. See [docs/CREDITS.md](docs/CREDITS.md) |

Manifests: `plugin.json`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `.codex-plugin/plugin.json`, `.kimi-plugin/plugin.json`, `kimi.plugin.json`, `.mcp.json`, `mcp.json`. All plugin manifests carry the same version, currently `3.3.0+swarm`.

## Rules that must not regress

These are the invariants the project relies on. Each row names what enforces it today. Where the guard is a prose grep rather than a behavioral test, the table says so.

| Rule | Enforced by |
| --- | --- |
| The conductor never simulates panelists. Dispatch is real, and a panelist with no `.out` is absent. | `tests/validate.sh` step 6 greps for `MANDATORY COMPLIANCE` in `skills/fusion-orchestrate/SKILL.md` (line 76), and the same block in the ultimate-review and graph skills |
| Absent is not agreement. Consensus counts returning panelists only. | `validate.sh` lines 78, 113, 128, 160 (prose grep). `tests/test_conductor_docs.py` line 165 asserts the phrase `Absent ≠ agreement` |
| The judge stays off the panel by default. | `validate.sh` line 79 (prose grep) |
| An empty or failed roster blocks before any hidden synthesis or discovery call and the CLI exits non-zero without recording a run. | `tests/test_provider_boundaries.py` |
| No implicit Claude use. The `fable` provider is skipped in discovery and detection, and its adapter refuses (exit 3) unless `FUSION_ALLOW_CLAUDE=1`. | `python/fusion_swarm/discovery.py` lines 317 and 443, `scripts/providers/detect.sh` line 78, `scripts/providers/fable.sh` line 16, `tests/test_provider_boundaries.py` (`test_claude_discovery_is_disabled_before_cache_or_cli`) |
| The `codex` binary runs only when `codex_exec_allowed()` is true (a Codex host, or `FUSION_ALLOW_CODEX_EXEC=1`). On a Claude host the codex seat goes through AndrewCode. | `python/fusion_swarm/peers/policy.py` (`codex_exec_allowed`), `tests/test_peers_core.py` |
| AndrewCode refuses before launch when its required isolation cannot be enforced, and reports that no provider was called. | `scripts/providers/andrewcode.sh`, `tests/test_hive_wiring.py` (`HiveWiringRuntime`) |
| Prompts never get interpolated into a double-quoted command string. They go through a UTF-8 `--prompt-file`, stdin, or one `"$(cat file)"` argument. | Convention stated in `scripts/providers/_common.sh`. `tests/test_provider_scripts.py` checks each adapter's argv against fake CLIs. There is no single grep that proves the rule, so review adapter changes by eye |
| Diversity is real model families. Correlated harnesses are not counted as independent votes. | `adapter.CORRELATION_GROUPS` and `PROVIDER_META` in `python/fusion_swarm/adapter.py` |
| The graph scheduler is dataflow, not waves. It waits on the first finished future and recomputes readiness per node. | `tests/test_graph.py`, `TestExecution.test_dataflow_not_waves` |
| The graph call cap is checked when a node is submitted, not after spending. `DEFAULT_BUDGET` in `python/fusion_swarm/graph.py` sets `max_nodes` 64 and `max_calls` 200. | The submit-time check is in `python/fusion_swarm/graph.py` (`calls + cost > call_cap`, line 742) and `CLAUDE.md` states the rule. `tests/test_graph.py` has `test_node_cap_is_hard` and `test_call_budget_blocks_before_spending` (the up-front estimate refuses). No test drives the submit-time branch, so review changes to it by eye |
| The knowledge graph keys an entity on type, name and source, so two sources saying "J. Smith" stay two mentions until a fusion step merges them. | `tests/test_graph.py` (the `J. Smith` tests near line 640) |
| `kg` and `gate` have an empty roster in `modes_catalog.py`, so the dashboard grows no controls that do nothing. | `tests/test_mode_contract_map.py` (`test_gate_dispatches_no_panelist`) |
| Per-mode override keys in `config/defaults.env` ship commented out and empty. | `tests/test_dashboard_schema.py` (`test_defaults_env_per_mode_keys_all_commented`) |
| UltraCode stays session-scoped. Do not edit global `~/.claude` settings or commit `ultracode/config.json` or `ultracode.env`. | `ultracode/.gitignore` ignores `/config.json` and `*.env`. The root `.gitignore` does not list them |
| All plugin manifests carry the same version. | `tests/test_hive_docs.py` (`test_version_is_hive`) |
| The plugin has no third-party Python dependency, and no dependency on the `swarms` package. | Absence of any dependency manifest. Review new imports |

Cross-platform notes that bite contributors (from `CLAUDE.md` and `_common.sh`):

- Git Bash has no GNU `timeout`, so `_common.sh` ships a pure-bash `fusion_timeout`. Use it in adapters.
- On Windows, `subprocess.run(["bash", ...])` can resolve to WSL bash. `adapter._find_bash()` pins Git Bash and honors `FUSION_BASH`. `adapter._fs()` forward-slashes every path handed to bash.
- Adapter exit codes are uniform: `0` success, `127` CLI not installed, `124` timeout, `1` runtime failure.

## Run the tests

Four layers, all offline. None of them calls a provider.

```bash
bash tests/validate.sh                                   # structure, JSON, frontmatter, prose invariants, 13 unit modules, ureview tests
bash tests/test-ureview.sh                               # ureview prompt contracts (also run by validate.sh step 9)
PYTHONPATH=python python3 -m unittest discover -s tests  # every tests/test_*.py module
PYTHONPATH=python python3 -m unittest tests.test_graph   # one module while you work
```

`bash tests/smoke-providers.sh` is different. It sends a one-word prompt to every available provider and costs real tokens. Do not run it in CI, and do not run it unless you have verified model, effort and security controls first.

What `validate.sh` checks, by step number in the script:

1. `bash -n` on every `*.sh` under `scripts/`, `tests/` and on `install.sh`.
2. `.codex-plugin/plugin.json`, `kimi.plugin.json`, `.kimi-plugin/plugin.json` and `plugin.json` parse, and `kimi.plugin.json` exists.
3. Every path declared in `.codex-plugin/plugin.json` exists.
4. Every `skills/**/SKILL.md` has `name:` and `description:` in its first 12 lines.
5. Every `commands/*.md` has `description:` in its first 6 lines.
6. Prose invariants: required strings in the orchestrate, ultracode, ultraswarm, metaloop, ultimate-review and graph skills and commands.
7. `py_compile` over `python/fusion_swarm/*.py`.
8. A fixed list of unit modules: `test_metaloop`, `test_graph`, `test_board`, `test_identities`, `test_spawn`, `test_designer`, `test_hive`, `test_hive_integration`, `test_factory`, `test_lanes`, `test_kimi_install`, `test_hive_docs`, `test_council_upgrades`.
9. `tests/test-ureview.sh`.

Step 7 compiles only the top-level modules, not `forms/`, `peers/` or `vboard/`. Step 8 does not run those packages' tests either. Run `unittest discover` for those.

### Last full run

Run on 2026-11-22 in a scratch copy of the repo with `HOME` pointed at an empty directory. macOS (arm64), Python 3.13.17, bash 3.2.57.

| Suite | Result | Wall clock |
| --- | --- | --- |
| `bash tests/validate.sh` | 208 PASS, 1 FAIL (`tests.test_hive_docs`), exit 1 | 23 s |
| `python3 -m unittest discover -s tests` | Ran 1169 tests: 4 failures, 3 errors, 17 skipped | 297 s |

Counts change with every commit. Re-run to get current numbers rather than trusting this table.

### Known environment-dependent failures

The seven failing tests in the run above fall into two groups.

**1. Missing `docs/MODES.md` (2 errors, since resolved).** `tests/test_hive_docs.py` reads `docs/MODES.md` for the mode catalog and Hive section. The scratch copy was taken before that page existed, so `TestReadmeHiveSection.setUpClass` and `TestSliceDoesNotStub.test_readme_still_longer_than_before_hive_insert` raised `FileNotFoundError`, and `validate.sh` step 8 reported `tests.test_hive_docs failed`. With the page in place, `PYTHONPATH=python python3 -m unittest tests.test_hive_docs` ran 31 tests, OK. The table above has not been regenerated, so its counts predate that fix.

**2. No local AndrewCode checkout (5 tests).** These tests assume the AndrewCode entry script exists:

- `tests/test_hive_wiring.py`: `test_auto_default_cannot_bypass_required_controls`, `test_auto_disabled_still_requires_controls`, `test_hive_identity_cannot_bypass_required_controls`, `test_model_passthrough`
- `tests/test_andrewcode_transport.py`: `test_doctor_probes_node_entry_never_standalone_codex`

Without the entry, `python/fusion_swarm/andrewcode_cli.py` returns exit code 127 and `AndrewCode Node entry is missing; no provider was called`, while the tests expect the fail-closed exit code 1. `_default_entry()` looks at `FUSION_ANDREWCODE_ENTRY`, then on Windows at `C:/dev/andrew-code/apps/kimi-code/dist/main.mjs`, and elsewhere at `~/dev/andrew-code/apps/kimi-code/dist/main.mjs`, then at a sibling `andrew-code` checkout, then at the `C:/dev/...` path.

To confirm the cause, the two modules were re-run with the scratch `HOME` containing a symlink to a real AndrewCode checkout. Result: 38 tests, OK, 3 skipped. Setting `FUSION_ANDREWCODE_ENTRY` to a real entry script before running the suite should give the same result. The tests then still refuse before launching a provider, so no tokens are spent.

## Add a mode (slash command)

A mode is a `commands/<mode>.md` file. `modes_catalog.py` parses it, so the catalog never needs a second hand-edited list for the description.

1. Create `commands/<mode>.md` with frontmatter:

   ```markdown
   ---
   description: One sentence on what the mode does.
   argument-hint: <task> [--flag value]
   ---
   ```

   `validate.sh` step 5 requires `description:` in the first six lines. Commands that run a script directly (for example `ultracode.md`, `ultraswarm.md`, `metaloop.md`, `hive.md`) begin the body with a `CRITICAL — DIRECT EXECUTION` block that tells the host to run a relative path immediately and not search the home directory. `validate.sh` greps for it in some of them.
2. If the mode is driven by a skill, name it in the body as `**<skill>** skill` (the pattern `modes_catalog.py` uses to detect `invokes_skill`), and add `skills/<skill>/SKILL.md` (see [Add a skill](#add-a-skill)).
3. If the mode is backed by the Python engine, wire a pattern:
   - Put the pattern name in `ENGINE_PATTERNS` and in `ENGINE_PATTERN_PROVIDERS` in `python/fusion_swarm/modes_catalog.py`. Use an empty tuple when the mode dispatches no panelist.
   - Add the name to the `choices` list in `build_parser()` in `python/fusion_swarm/__main__.py`, and handle it in `main()`.
   - Reference it in the command body as `swarm.sh <pattern>` or `fusion_swarm <pattern>`, which is how `_detect_engine_pattern` finds it.
   - If the pattern dispatches providers, add the generated per-mode keys to `config/defaults.env` as commented, empty lines (`# export FUSION_<PATTERN>_<PROVIDER>_MODEL=""`). Add `_EFFORT` lines only for providers in `EFFORT_CAPABLE_PROVIDERS`. `tests/test_dashboard_schema.py` fails if a generated key is missing or ships uncommented. To list the keys the catalog expects:

     ```bash
     PYTHONPATH=python python3 -c "from fusion_swarm import modes_catalog as m; print('\n'.join(m.per_mode_override_keys()))"
     ```
   - Patterns that do not flow through `adapter.dispatch` belong in `PER_MODE_UNENFORCEABLE` so no override control is generated that nothing reads.
4. Update `skills/fusion-orchestrate/references/collaboration-modes.md` (numbered `## N. \`mode\`` headings, pinned by `tests/test_conductor_docs.py`) and the mode table in `skills/fusion-orchestrate/SKILL.md`.
5. If users should be routed to it from a free-text task, add a signal in `scripts/route.sh`.
6. Add invariant greps to `tests/validate.sh` step 6 for any load-bearing prose, using the `check <file> <regex> <label>` helper. `CLAUDE.md` calls this updating "validate.sh's mode list". The script has no single list, only per-mode `check` blocks.
7. Document the mode in [docs/MODES.md](docs/MODES.md). `tests/test_hive_docs.py` pins some strings in that page and the README, so run it after editing either.
8. Run the gate tests:

   ```bash
   PYTHONPATH=python python3 -m unittest tests.test_mode_contract_map tests.test_mode_overrides tests.test_dashboard_schema tests.test_conductor_docs
   ```

`tests/test_mode_contract_map.py` fails if an engine pattern lacks a roster entry, if a roster names an unknown provider, or if the per-mode key set stops being bounded and unique. In a scratch run, `tests.test_mode_contract_map` and `tests.test_v4_docs` together ran 15 tests, OK.

Check the current command count with `ls commands | wc -l`. At the time of writing it was 54, and `modes_catalog.all_modes()` also returned 54.

## Add a runtime form

Runtime forms are the executable forms in `python/fusion_swarm/forms/` (ten at time of writing). They are different from the 16 native team forms in `docs/CODING_TEAMS.md`. See [docs/FORMS.md](docs/FORMS.md).

1. Create `python/fusion_swarm/forms/<module>.py`. It must export `FORM: FormSpec` (fields used by the listing: `name`, `summary`, `inputs`, `stop_rule`, `aggregation`, `default_budget`) and a `run(...)` that returns a `FormResult`. See `refute_panel.py` for the shape.
2. Register the module in `FORMS` in `python/fusion_swarm/forms/__init__.py`, and add any short names to `ALIASES`. Modules import lazily, so a broken form shows as `"available": false` in `list` instead of breaking the others.
3. Add the form's option parsers to `OPTIONS` in `python/fusion_swarm/forms/__main__.py`.
4. Add `commands/<command>.md` with `description:` and `argument-hint:` in the first six lines, and map the command name to the form in `RUNTIME_FORM_COMMANDS` in `modes_catalog.py`.
5. Add tests. The existing ones are `tests/test_forms_a.py`, `test_forms_b.py`, `test_forms_c.py` and `test_forms_runtime.py`. They use a scripted fake dispatch, a temporary `FUSION_HOME` and a temporary ledger, and avoid wall-clock assertions.
6. `tests/test_v4_docs.py` asserts that `set(RUNTIME_FORM_COMMANDS.values()) == set(FORMS)`, so steps 2 and 4 must agree.
7. Document the form in [docs/FORMS.md](docs/FORMS.md).

Check your registration without spending anything:

```bash
PYTHONPATH=python python3 -m fusion_swarm.forms list
```

## Add a provider adapter

A provider has a bash adapter, a detection entry, engine metadata and a catalog entry. Read `scripts/providers/_common.sh` first for the helpers and the exit-code contract.

1. `scripts/providers/<name>.sh`. The first two arguments are always `<prompt_file> <out_file>`, followed by optional `[model]` and provider-specific extras (`[effort]`, `[workspace]`, and for `codex.sh` `[sandbox]`). Check the `usage:` line of the nearest existing adapter. Source `_common.sh`, call `fusion_export_identity`, apply the timeout helper, and write the final answer to `<out_file>`. Follow the prompt-passing rule from the table above.
2. `scripts/fusion.sh`: add the provider to `adapter_for()`.
3. `scripts/providers/detect.sh`: add a probe line, then add it to both the `--json` object and the text listing.
4. `python/fusion_swarm/adapter.py`: add a `PROVIDER_META` entry (`tier`, `model_family`, `harness`, `model`, `strengths`). If the provider should be dispatched by name rather than only through the shell script, add it to the role lists (`WORKER_PROVIDERS`, `ADVISOR_PROVIDERS`, `OPERATOR_PROVIDERS`) as appropriate. If it shares a harness with another provider, update `CORRELATION_GROUPS`.
5. `python/fusion_swarm/modes_catalog.py`: add the name to `PROVIDER_NAMES`, and to `ENGINE_PATTERN_PROVIDERS` for the patterns that should use it. `test_engine_providers_are_known_providers` fails otherwise.
6. `config/defaults.env`: add `FUSION_<PROVIDER>_MODEL` and, if the CLI honors it, `_EFFORT`.
7. Tests: add a fake CLI under `tests/fixtures/fake_cli/` and cases in `tests/test_provider_scripts.py`. For anything that touches Codex or Claude, also read `python/fusion_swarm/peers/policy.py` and `tests/test_peers_core.py`.

Do not change an adapter's flags without running its offline transport check. `CLAUDE.md` notes two hard limits. Never call the standalone Codex CLI, even for `--help` or `--version`, and never bypass a fail-closed adapter. For the AndrewCode seat, test with a fake entry through `FUSION_ANDREWCODE_ENTRY`, as `tests/test_andrewcode_transport.py` does.

The fusion ledger schema is documented in `skills/fusion-orchestrate/references/learning.md`. If you change the schema, update that file and its record example.

## Add a skill

1. Create `skills/<skill-name>/SKILL.md`. Frontmatter needs `name:` and `description:` in the first 12 lines (`validate.sh` step 4). The description is what hosts use to decide when to load the skill, so list trigger phrases in it.
2. Put long reference material under `skills/<skill-name>/references/` and link it from `SKILL.md`.
3. Hosts find skills by directory. `plugin.json`, `kimi.plugin.json`, `.codex-plugin/plugin.json` and `.kimi-plugin/plugin.json` point at `./skills/`. `.claude-plugin/plugin.json` declares no skills path. The installer copies `skills/` wholesale (`INCLUDE_DIRS` in `scripts/fusion_install.py`), so a new directory needs no registration. Only an individual file listed in `CHECK_FILES` in that script is verified by name.
4. If a command invokes the skill, name it in the command body as `**<skill-name>** skill`.
5. Add invariant greps to `validate.sh` for any string that must not silently disappear, and, for important skills, a contract test in the style of `tests/test_fusion_board_skill.py` or `tests/test_hive_skill.py`.
6. Document it in [docs/SKILLS.md](docs/SKILLS.md).

The nested `skills/ultimate-review/` tree is a separate vendored package with its own `README.md`, `commands/` and `skills/`. `validate.sh` step 4 finds its `SKILL.md` too.

At the time of writing, `find skills -name SKILL.md | wc -l` returned 24.

## Docs that tests pin

Some documentation is asserted by tests, so editing it can fail a build:

| Test | What it pins |
| --- | --- |
| `tests/test_hive_docs.py` | Manifest versions and keywords, the README and `docs/MODES.md` Hive section and mode tables, the contract coverage in `docs/HIVE.md` |
| `tests/test_conductor_docs.py` | The mode table in `skills/fusion-orchestrate/SKILL.md` and the numbered headings in `collaboration-modes.md` |
| `tests/test_v4_docs.py` | Frontmatter of the runtime-form, `vboard` and `peers` commands, the `forms` command text, and the Codex policy statements in `commands/peers.md` |
| `tests/test_slash_hive_designer_board.py`, `test_hive_skill.py`, `test_fusion_board_skill.py`, `test_fusion_designer_skill.py`, `test_hive_entrypoints.py` | Frontmatter and load-bearing strings in the hive, designer and board commands and skills |

When a pinned string is stale, fix the doc and the test together. Do not delete the assertion to get green.

Doc rules for this repo: every flag, default, env var, endpoint, count and path must trace to source. Derive counts by running a command. Use relative links. Do not paste absolute local paths. A path that is a real code default (such as the Windows AndrewCode entry) is the one exception. Label it as a default and say how to override it. Treat `docs/UPGRADE_PLAN.md`, `docs/UPGRADE_VALIDATION.md`, `docs/INSTALLER_VALIDATION.md`, `docs/IMPLEMENTATION_STATUS.md` and `docs/VALIDATION.md` as historical records. Do not update them to describe current behavior.

## Pull request checklist

- [ ] `bash tests/validate.sh` passes, or every failure is explained by one of the known causes above.
- [ ] `PYTHONPATH=python python3 -m unittest discover -s tests` passes, or each failure is explained.
- [ ] No test or script you added calls a live provider or the network. Tests that need an AndrewCode entry use `FUSION_ANDREWCODE_ENTRY` with a fake.
- [ ] New or changed modes: `commands/`, `modes_catalog.py`, `__main__.py`, `defaults.env` keys, `collaboration-modes.md`, `route.sh` and `validate.sh` checks are all consistent, and `tests.test_mode_contract_map` and `tests.test_dashboard_schema` pass.
- [ ] New or changed forms: `FORMS`, `RUNTIME_FORM_COMMANDS` and the command file agree.
- [ ] New or changed provider: adapter, `fusion.sh`, `detect.sh`, `adapter.py`, `modes_catalog.py` and fake-CLI tests are updated.
- [ ] Version-bearing changes keep every manifest on the same version string.
- [ ] Docs you touched are accurate against the code, use relative links, and any pinned strings still hold.
- [ ] `CHANGELOG.md` has an entry for user-visible changes.
- [ ] No secrets, tokens, user emails or machine-specific paths in the diff. `ultracode/config.json` and `ultracode.env` are not committed.

## Related

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): layers and module map
- [docs/MODES.md](docs/MODES.md): the command catalog
- [docs/FORMS.md](docs/FORMS.md): runtime forms
- [docs/PEERS.md](docs/PEERS.md): providers and peer transports
- [docs/SKILLS.md](docs/SKILLS.md): skill catalog
