---
description: Repeat a bounded search until rounds stop producing new verified findings.
argument-hint: [task and runtime options]
---

Repeat a bounded search until rounds stop producing new verified findings. Run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms run loop_until_dry --help` for the input flags, then invoke the same entry point with the task, provider IDs, and budget.

Stop on consecutive dry rounds, the round cap, or the budget. The default call cap is 6. To recover verified work, run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms salvage <run-dir>`.
