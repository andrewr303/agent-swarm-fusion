---
description: Allocate tasks using verified provider history and a probability of exploring alternatives.
argument-hint: [task and runtime options]
---

Allocate tasks using verified provider history and a probability of exploring alternatives. Run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms run auction --help` for the input flags, then invoke the same entry point with the task, provider IDs, and budget.

Stop when the task list is handled or the budget ends. The default call cap is 1. Set an explicit call budget for multi-task execution. To recover verified work, run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms salvage <run-dir>`.
