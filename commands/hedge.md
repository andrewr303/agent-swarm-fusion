---
description: Start a backup when the primary is slow and accept the first verified answer.
argument-hint: [task and runtime options]
---

Start a backup when the primary is slow and accept the first verified answer. Run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms run hedged_race --help` for the input flags, then invoke the same entry point with the task, provider IDs, and budget.

Stop at the first verified answer, when all attempts finish, or at the budget limit. The default call cap is 3. Use a read-only task or provide isolated workspaces through the Python API. To recover verified work, run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms salvage <run-dir>`.
