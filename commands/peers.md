---
description: Inspect peer routing or call an explicitly selected CLI peer.
argument-hint: [policy | route | ask | acp | handoff | whoami]
---

Use `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.peers --help`. Inspect `policy` and `route` before dispatching. The `handoff` command persists delivery and the result on the board.

On a Claude Code host the Codex seat is reached only through AndrewCode (one-shot or ACP v1). The `codex exec --json` adapter defaults off and requires `FUSION_ALLOW_CODEX_EXEC=1` or a Codex host (`FUSION_HOST=codex`). Codex hosts reach Claude via `claude -p --output-format json`.

AndrewCode has no sandbox. Use Fusion worktree or tool restrictions appropriate to the authorized task and explicit board path reservations.
