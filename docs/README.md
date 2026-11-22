# Documentation index

This is the map of the `agent-swarm-fusion` documentation. The repository ships the Fusion plugin, version `3.3.0+swarm`. The plugin id, the `/fusion:*` commands, the `fusion_swarm` package and the `FUSION_*` variables keep the `fusion` name. Start with the [project README](../README.md) if you have not read it, then pick the row that matches what you are doing.

## Start here

| Page | Read it to |
| --- | --- |
| [Getting started](GETTING_STARTED.md) | Install the plugin into Claude Code, Codex CLI or AndrewCode, verify it, and make a first offline run |
| [Concepts](CONCEPTS.md) | Learn the vocabulary, how a task is routed, and the rules that keep a run honest |
| [Modes and slash commands](MODES.md) | Find the command for a job; covers every file under `commands/` |
| [Troubleshooting](TROUBLESHOOTING.md) | Match a symptom to a cause and a fix |

## Reference

| Page | Covers |
| --- | --- |
| [Command-line reference](CLI.md) | Every entry point and flag: `fusion.py`, `scripts/*.sh`, `python -m fusion_swarm...` |
| [Configuration](CONFIGURATION.md) | Configuration files, every environment variable and the `.fusion/` state layout |
| [Providers and peers](PEERS.md) | The real provider list, model and effort selection, peer transports, the Codex policy |
| [Skills catalog](SKILLS.md) | The skills the hosts load and the files they reference |
| [Architecture](ARCHITECTURE.md) | Layers, a map of every module in `python/fusion_swarm/` and the main data flows |

## Subsystem guides

| Page | Covers |
| --- | --- |
| [Runtime forms](FORMS.md) | The 10 executable forms with call budgets, deadlines, the journal and `salvage` |
| [Swarm runtime](SWARM_RUNTIME.md) | Composing and previewing swarms, native execution, candidate review, supervision and project knowledge |
| [Coding teams](CODING_TEAMS.md) | The 16 native team forms, leadership styles and TeamSpec |
| [Hive](HIVE.md) | The nested architect, captain and child swarm, its board and its spawn limits |
| [MetaLoop](METALOOP.md) | The chief operator and board advisor coding loop |
| [VBoard](VBOARD.md) | The Vercel-Board-compatible board: engines, CLI, HTTP server, MCP tools and conformance |
| [Native coordination](COORDINATION.md) | HiveBoard, mailbox, task states and the `fusion-coordination` MCP server |
| [Cockpit](COCKPIT.md) | The loopback web dashboard and the terminal snapshot |
| [A complete run](EXAMPLE.md) | A captured panel run and the verdict it produced |

## Contributing

[CONTRIBUTING.md](../CONTRIBUTING.md) covers setup, the rules that must not regress, how to run the tests, and how to add a mode, a runtime form, a provider adapter or a skill. [CLAUDE.md](../CLAUDE.md) holds the original developer notes that agents read in this repository.

## Release guide and historical records

[Fusion 3.3.0](V4.md) is the release guide for the current version. [CHANGELOG.md](../CHANGELOG.md) lists what changed. The pages below are retained as evidence of earlier work. Each states its own release and scope, so do not read their counts as the current numbers.

| Page | Record of |
| --- | --- |
| [Upgrade plan](UPGRADE_PLAN.md) | The approved four-phase plan for the 3.2 line |
| [Implementation status](IMPLEMENTATION_STATUS.md) | What each phase delivered, with verification notes up to 3.3.0+swarm |
| [3.2 verification](UPGRADE_VALIDATION.md) | Checks run for the 3.2 delivery |
| [Windows installer verification](INSTALLER_VALIDATION.md) | The original 3.1 installer round |
| [3.1 validation](VALIDATION.md) | The 3.1 baseline |
| [Research provenance](RESEARCH.md) | Where the 3.1 coordination upgrade came from |
| [Credits](CREDITS.md) | Module-by-module attribution and vendored code |
