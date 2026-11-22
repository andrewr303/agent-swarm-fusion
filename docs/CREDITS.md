# Credits, Research Provenance & Acknowledgments

This repository is named `agent-mesh`. It publishes the Fusion plugin: the plugin id, the `/fusion:*` slash commands, the `fusion_swarm` Python package and the `FUSION_*` environment variables keep the `fusion` name. "Fusion" below means that plugin.

Fusion builds on academic research, multi-model systems engineering, and open-source agent orchestration projects. The algorithms and coordination primitives in `python/fusion_swarm/` and `scripts/` are written against the Python 3.10+ standard library and local CLI subprocesses (the installers enforce Python 3.10+, see `install.sh`). This page attributes the papers and repositories that inspired each capability. The one exception to "written here" is the vendored `ultracode/` tree, described in [Vendored code](#vendored-code).

---

## 1. Multi-Model Deliberation, Panels & Sidekick Loops

| Capability in Fusion | Primary Sources & Inspiration | How Fusion adapts it |
| :--- | :--- | :--- |
| **Blind Parallel Fan-Out & Off-Panel Judge (`panel`, `fusion.sh`)** | **OpenRouter** — *Fusion: How It Works and When to Use It* & *Surpassing Frontier Performance with Fusion* | Dispatches identical neutral prompts (`in parallel and blind`) across distinct local CLI model families and keeps the synthesizer/judge off the responder panel by default (`CLAUDE.md`, "Judge stays off the panel"). |
| **Anonymized Cross-Review & Anti-Conformity (`council`, `anti-conformity.md`)** | **Andrej Karpathy** — [`karpathy/llm-council`](https://github.com/karpathy/llm-council), **`openfusion`**, **council-of-high-intelligence**, **`nexus-dev`** | Strips provider identities in Round 2 (`Response A`, `Response B`, `Response C`) and enforces the `name-the-flaw` directive so reviewers cannot rubber-stamp peer responses. |
| **Sidekick Feedback Loop & Scoped Worker Briefs (`sidekick.py`, `metaloop.py`, `ureview.sh`)** | **Cognition AI** — *Introducing Fusion in Devin Desktop & CLI* | Separates architectural judgment ("genius-boss" Lead/Operator + cross-family Advisor) from scoped, file-bounded execution briefs ("idiot-boss" anti-micromanagement guardrails). |
| **Host-Neutral Manifests & Nested AndrewCode Runtime (`kimi.plugin.json`, `spawn.py`, `hive.py`)** | **AndrewAgent** (`CLAUDE.md`, `CODEX.md`, `KIMI.md`) | Unified plugin surface across Claude Code, OpenAI Codex CLI, and AndrewCode with strict non-interactive `--prompt-file` subprocess invocation. |

---

## 2. Swarm Topologies, Racing, Hardening & Evolution

| Capability in Fusion | Primary Sources & Inspiration | License / Reference |
| :--- | :--- | :--- |
| **Swarm Topologies (`moa.py`, `heavy.py`, `discuss.py`, `flow.py`, `reasoning.py`)** | **[kyegomez/swarms](https://github.com/kyegomez/swarms)** (Kye Gomez) — Mixture-of-Agents (`MoA`), `HeavySwarm`, `GroupChat`, `AgentRearrange`, and Reflexion/Self-Consistency | Clean-room stdlib re-implementation over local CLI adapters (`scripts/swarm.sh`). |
| **MAP-Elites Quality-Diversity Evolution (`python/fusion_swarm/evolve.py`)** | **[algorithmicsuperintelligence/openevolve](https://github.com/algorithmicsuperintelligence/openevolve)** & **[SakanaAI/ShinkaEvolve](https://github.com/SakanaAI/ShinkaEvolve)** | Implements a 2D `(correctness_bin, conciseness_bin)` MAP-Elites grid, multi-island ring topology migration, and fast-to-slow cascade evaluation (`_cascade_evaluate`: non-empty output, balanced code fences, a heuristic score, then an optional `gate_cmd` check). |
| **Competitive Worktree Racing (`python/fusion_swarm/race.py`)** | **[affaan-m/everything-claude-code](https://github.com/affaan-m/everything-claude-code)** & **[nyldn/claude-octopus](https://github.com/nyldn/claude-octopus)** (MIT) | Spawns parallel provider candidates in isolated APFS copy-on-write workspaces, evaluates each with a 4-stage pipeline (`compile -> test -> scope -> diff`), promotes the first candidate that passes all stages (or the tournament survivor), and cleans up the losing workspaces and process groups. |
| **Adversarial Wave-Hardening (`python/fusion_swarm/harden.py`)** | **[raidium/clean-harbor](https://github.com/raidium/clean-harbor)** & **[procoders/superpowers-v](https://github.com/procoders/superpowers-v)** (MIT) | Alternating execution waves (a builder generates or repairs) and hardening waves (a hardener probes edge cases and invariants). The run stops after `required_clean_waves` (default 2) consecutive clean hardening waves, is capped at `max_waves` (default 4), and rolls back if a repair wave regresses the test gate. |
| **Empirical Wilson-Score MoE Router (`python/fusion_swarm/ledger_moe.py`)** | **[ruvnet/claude-flow](https://github.com/ruvnet/claude-flow)** & **[VoltAgent/awesome-claude-code-subagents](https://github.com/VoltAgent/awesome-claude-code-subagents)** | Computes the Wilson lower confidence bound from `memory/runs.jsonl` (the `--runs-file` default) per provider, with an optional task-type filter. Ties break by sample count, then provider name, and the top hint keeps one provider per model family. |
| **Stigmergic Pheromone Coordination (`stigmergy_traces` in `board.py`)** | **[ruvnet/claude-flow](https://github.com/ruvnet/claude-flow)** (Stigmergic Environment) | Shared SQLite table of expiring, half-life-decayed discoveries (`deposit_trace` and `sense_traces`; the default trace kind is `hotspot`). `tui.py` reads the traces for its snapshot. |

---

## 3. Transactional Boards, Workspaces & Process Supervision

| Capability in Fusion | Primary Sources & Inspiration | License / Reference |
| :--- | :--- | :--- |
| **Durable SQLite Inbox, Idempotency Keys & Claim Leases (`mailbox.py`)** | **[jahwag/agentbus](https://github.com/jahwag/agentbus)** | MIT — Producer idempotency keys, per-recipient delivery receipts, and atomic task claim leases. |
| **File Path Reservations & Threaded Channels (`board.py`, `scope.py`)** | **[Dicklesworthstone/mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail)** | Explicit agent identity, advisory path reservations (`reserve`, with component-wise overlap checks in `coordination.overlaps`), and threaded channel rooms. |
| **Workflow Compilation & Execution States (`execution.py`, `strategies.py`)** | **[AgentWorkforce/relayflows](https://github.com/AgentWorkforce/relayflows)**, **[madison-ai-labs/SwarmWire](https://github.com/madison-ai-labs/SwarmWire)**, **[ChesterRa/cccc](https://github.com/ChesterRa/cccc)** | Separates `stored`, `delivered/read`, and `accepted/completed` execution states across DAG, map-reduce, and blackboard runs. |
| **Worktree Lifecycle, Two-Stage Review & Liveness Supervision (`workspaces.py`, `supervision.py`, `team.py`)** | **[procoders/superpowers-v](https://github.com/procoders/superpowers-v)** (MIT), **[Intrect-io/OpenSwarm](https://github.com/Intrect-io/OpenSwarm)** (MIT), **[Untrivial-ai/agent-orchestrator](https://github.com/Untrivial-ai/agent-orchestrator)** (Apache-2.0) | Baseline-aware SHA-256 workspace snapshots (`scope.snapshot`, `scope.inspect_changes`), staged candidate review (`team.py`), and persisted operator views (`supervision.py`). |
| **macOS `kqueue` Wakeup & Hook-Driven Re-Wake (`board.py`)** | **[mraza007/agmsg](https://github.com/mraza007/agmsg)** & **[awslabs/cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator)** | `select.kqueue` notifications on `<db>.wakeup` (the `wait` command). With `--hook-rewake`, `wait` exits 2 with a stderr notification when new mail arrives. |

---

## 4. Task Graphs, Knowledge Graphs & Scaling Laws

| Capability in Fusion | Primary Sources & Inspiration | Reference |
| :--- | :--- | :--- |
| **Ontology Validation, Bitemporal Knowledge Graphs & Entity Resolution (`kgraph.py`, `fusion:graph`)** | **Southeast University Graduate Knowledge Graph Course** — Prof. Peng Wang ([`npubird/KnowledgeGraphCourse`](https://github.com/npubird/KnowledgeGraphCourse)) | 5-stage KG pipeline (Ontology → Extraction → Entity Resolution → Fusion → Quality & Provenance) with `valid_from`/`valid_until` bitemporal queries and ban on untyped `RELATED_TO` edges. |
| **Dataflow Task Graphs, Stop Rule & Critical-Path Linting (`graph.py`, `task-graphs.md`)** | **Google DeepMind × MIT** (*Towards a Science of Scaling Agent Systems*, Dec 2025) & **Anthropic** (*Building Effective Agents*, Dec 2024) | The Stop Rule (do not use a graph when `solo`, `panel`, or `swarm` suffices) is documented in `task-graphs.md`. `graph.py` lints `fake_edge` dependencies and flags two nodes that write the same file. |
| **Graph Memory Benchmark Honesty (`graph-memory.md`)** | **Zep / Graphiti**, **HippoRAG** (OSU NLP, NeurIPS '24), **Microsoft GraphRAG** | Explicitly distinguishes peer-reviewed retrieval structures from self-benchmarked vendor marketing claims. |

---

## Vendored code

`ultracode/` is a vendored copy of **UltraCode-Shim** by OnlyTerp (<https://github.com/OnlyTerp/UltraCode-Shim>). It is not original to this repository.

| Item | Value |
| :--- | :--- |
| Upstream | `OnlyTerp/UltraCode-Shim` |
| License | MIT, `Copyright (c) 2026 OnlyTerp`. The license text is kept at [`../ultracode/LICENSE`](../ultracode/LICENSE) |
| Location | [`../ultracode/`](../ultracode/) (its own README, `docs/`, `bin/`, `providers/`, `scripts/`, `proxy.py`, `test_proxy.py`) |
| What it is | A local proxy that lets Claude Code's `/model` menu route a pick to another backend with the UltraCode envelope (its README describes this as `effort=xhigh`, adaptive thinking, a large `max_tokens` and one system reminder). Python, standard library only |

How Fusion uses it. The shim is not imported by any module under `python/`.
Fusion only wraps it:

- `scripts/ultracode.sh` is the bridge. Its verbs are `doctor` (runs `ultracode/scripts/doctor.py`), `test` (runs the offline proxy self-test `ultracode/test_proxy.py`), `launch` (runs `ultracode/bin/ultracode`), `status` and `install` (runs `ultracode/install.sh`). With `FUSION_ULTRACODE` set, a non-terminal stdout or `--proxy-only`, `launch` starts the proxy only, so it does not block an agent session.
- `/fusion:ultracode` (`commands/ultracode.md`) calls that bridge. It defaults to `doctor` when no action is given, does not modify global Claude settings directly, and tells the user to start a full session from their own terminal.
- The `fusion-ultracode` skill (`skills/fusion-ultracode/SKILL.md`) sets the working rules: read `ultracode/AGENTS.md` before making setup claims, run the offline `test` first unless it already passed in the session, and follow its no-secrets rule.

What checks it. `tests/validate.sh` only greps the skill and command text for the no-discovery and direct-execution guards. `tests/test_andrewcode_transport.py` loads `ultracode/providers/codex_oauth.py` to assert that it never launches a credential-refresh CLI, and scans every `.py` file under `ultracode/` for subprocess calls without `creationflags` (hidden-window rule). `bash scripts/ultracode.sh test` ran offline in a scratch copy and ended with `ALL TESTS PASSED`.

Anything you change inside `ultracode/` keeps the upstream MIT license and its copyright line.
