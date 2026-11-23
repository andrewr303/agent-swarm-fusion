# Fusion 3.1 research and integration provenance

This page records where the Fusion 3.1 coordination and delegation upgrade came
from. It is a historical provenance record. The current plugin version is
`3.3.0+swarm`, and the plugin id, `/fusion:*` commands, `fusion_swarm` package and
`FUSION_*` variables keep the `fusion` name in the `agent-swarm-fusion` repository. For
credits to libraries and vendored code, see [CREDITS.md](CREDITS.md).

The upgrade adapts coordination and delegation concepts into original stdlib
code. External benchmark results are research context, not a measured guarantee
of this plugin's coding quality, cost or speed. No third-party daemon, inference
SDK or paid provider call is introduced into planning or coordination.

## Local OpenCode Fusion materials

Analyzed a local checkout of `opencode-fusion` (an `opencode-fusion/` directory in
the author's AndrewCode workspace, not shipped in this repository), its
Sidekick/routers and diagram, including selectable text from all four PDFs and
visual inspection of the architecture and deliberation pages. The source repository is
[mihneaptu/opencode-fusion](https://github.com/mihneaptu/opencode-fusion), MIT.
Its README says it is no longer maintained and targets OpenCode 1.18, not 2.
OpenCode permission YAML was not transplanted as Codex/OS permission enforcement.

| Local PDF (under `opencode-fusion/`) | Evidence used | Integration |
| --- | --- | --- |
| `Devin Fusion _ Cognition.pdf` | Pages 2–4: separate contexts, briefs/results, focused weak-sidekick tasks | Persistent worker context in the native workflow; typed narrow briefs and escalation |
| `Introducing Fusion in Devin Desktop & CLI _ Cognition.pdf` | Pages 4, 5, 7: strong autonomous workers versus prescriptive delegation | Independent leadership choice: genius-boss or idiot-boss |
| `OpenRouter Fusion_ How It Works and When to Use It — OpenRouter Blog.pdf` | Pages 3, 6, 9: independent panel, structured analyst, caller synthesis | Read-only council proposals, explicit adjudication, dissent and gaps |
| `Surpassing Frontier Performance with Fusion — OpenRouter Blog.pdf` | Pages 5, 7, 8: diversity, selective fusion, evaluation limits | Preserve independent evidence; remove universal benchmark/cost promises |

PDF SHA-256 values, in the same order:

```text
686b473532fdc1edcef08468df232a565b3cc316d7d60bf87fc20c1a5fb26e3b
bdccfa364160714bfd85f4af3861cacaef07e0e48eb9274bb547f3f61480d75b
0e24e5d5828958f8fa36a843b0c356f51c94da99c4d0d5266d9d1360ed66f086
a49fd423415707775189f891e0920764d8b326d5fd1ff8a349ce08596150027e
```

`flow-diagram.png` (part of that local checkout, not shipped here) shows worker exploration → snippets to lead → lead plan →
worker implementation/tests → lead review → worker revisions → final code.
The new Sidekick skill and task states follow that feedback loop. Reported
complete work waits in review; accepted work unlocks dependencies. No model
switch at compaction, provider cache behavior or cost reduction is claimed.

Primary publications: [Cognition Devin Fusion](https://cognition.com/blog/devin-fusion),
[local Fusion](https://cognition.com/blog/local-fusion),
[OpenRouter explainer](https://openrouter.ai/blog/insights/fusion-explainer/),
[OpenRouter benchmark](https://openrouter.ai/blog/announcements/fusion-beats-frontier/).

The original linked social post for the boss terminology was not retrievable.
Mode definitions here are explicit product contracts, not verified claims about
that post's benchmarks or model economics.

## Local AndrewCode comparisons

Compared the `python/fusion_swarm` trees of two sibling AndrewCode checkouts
(`swarm` and `fusion`, also not shipped here) with this plugin. Much of the graph,
Hive, hierarchy and verification-contract machinery is already shared and was
retained rather than duplicated.

- Swarm's workspace communication layer inspired the `comms` entrypoint and
  run-scoped context. Its separate JSONL inbox/mirroring and suppressed failures
  were replaced with the same transactional board store and explicit errors.
- Swarm's configurable default board path was integrated.
- Existing Fusion graph schedules, typed task/risk/gate contracts, hierarchy,
  Hive nesting and factory remain available. Native `TeamSpec` coding forms are
  a separate execution contract; they do not impersonate the older engines.
- A root Python launcher now runs the new modes and board directly on Windows,
  without requiring Bash for native coordination.

## Similar GitHub repositories explored

| Primary repository | Element adapted | Deliberate boundary |
| --- | --- | --- |
| [AgentBus](https://github.com/jahwag/agentbus) | Durable inbox, stable producer retry keys, per-recipient receipts, claim leases and redelivery | Original SQLite implementation; no Go broker or daemon |
| [MCP Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) | Concrete path reservations, threads, explicit identity and scope | Advisory leases; no claim of OS sandboxing or authentication |
| [RelayFlows](https://github.com/AgentWorkforce/relayflows) | DAG, map-reduce, blackboard and other explicit workflow forms | Compiled dependencies and documented communication policy; no workflow SDK copied |
| [CCCC](https://github.com/ChesterRa/cccc) | Distinguish stored, delivered/read and successful execution | Inbox receipts and task acceptance are separate facts |
| [SwarmWire](https://github.com/madison-ai-labs/SwarmWire) | Clear pipeline/map-reduce/blackboard communication patterns | Local cooperative protocol; no claimed hosted routing |
| [OpenAI Swarm](https://github.com/openai/swarm) | Explicit handoffs and narrow responsibilities | Host-native workers and typed files; no Swarm SDK dependency |

For MCP framing and plugin paths the implementation was checked against the
[MCP stdio specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
[Codex plugin documentation](https://developers.openai.com/plugins/build/plugins),
and the primary
[Codex legacy plugin MCP parser](https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/plugin_config.rs),
[native MCP parser](https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/agent_plugin_config.rs)
and [native manifest loader](https://github.com/openai/codex/blob/main/codex-rs/core-plugins/src/agent_plugin_manifest.rs).
Public repositories change; these are the source contracts inspected for this
implementation on 2026-09-30, not a guarantee of future host compatibility.
