---
description: Use ledger history to choose who writes and who reviews a cross-provider pair.
argument-hint: [task and runtime options]
---

Use ledger history to choose who writes and who reviews a cross-provider pair. Run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms run pair_swap --help` for the input flags, then invoke the same entry point with the task, provider IDs, and budget.

Pass a deterministic verifier with `--gate-cmd`. Each direction runs one draft and one review/repair. The default call cap is 4. To recover verified work, run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms salvage <run-dir>`.
