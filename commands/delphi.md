---
description: Collect independent judgments before sharing anonymous rationales for revision.
argument-hint: [task and runtime options]
---

Collect independent judgments before sharing anonymous rationales for revision. Run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms run delphi --help` for the input flags, then invoke the same entry point with the task, provider IDs, and budget.

Stop when the answer distribution stabilizes or the round cap is reached. The default call cap is 9. To recover verified work, run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms salvage <run-dir>`.
