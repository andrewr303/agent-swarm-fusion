---
description: Ask independent reviewers to find reproducible objections to candidate answers.
argument-hint: [task and runtime options]
---

Ask independent reviewers to find reproducible objections to candidate answers. Run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms run refute_panel --help` for the input flags, then invoke the same entry point with the task, provider IDs, and budget.

Stop after each candidate has a verdict or the budget ends. The default call cap is 20. To recover verified work, run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms salvage <run-dir>`.
