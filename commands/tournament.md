---
description: Compare candidates in a seeded bracket using verification before judging.
argument-hint: [task and runtime options]
---

Compare candidates in a seeded bracket using verification before judging. Run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms run tournament --help` for the input flags, then invoke the same entry point with the task, provider IDs, and budget.

Stop with one survivor or at the budget limit. The default call cap is 17. The older engine tournament remains available as a separate pattern. To recover verified work, run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms salvage <run-dir>`.
