---
description: Alternate critique and repair while keeping the strongest verified result.
argument-hint: [task and runtime options]
---

Alternate critique and repair while keeping the strongest verified result. Run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms run critic_repair --help` for the input flags, then invoke the same entry point with the task, provider IDs, and budget.

Pass a deterministic verifier with `--gate-cmd`. The run stops on acceptance, a repeated answer, stagnation, regression, or the round cap. The default call cap is 10. To recover verified work, run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms salvage <run-dir>`.
