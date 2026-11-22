---
description: Resolve questions through typed entries in a shared notebook with optional BoardPort mirroring.
argument-hint: [task and runtime options]
---

Resolve questions through typed entries in a shared notebook with optional BoardPort mirroring. Run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms run blackboard --help` for the input flags, then invoke the same entry point with the task, provider IDs, and budget.

Stop with a verdict and no open question, a repeated state, the cycle cap, or the budget. The default call cap is 12. To recover verified work, run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms salvage <run-dir>`.
