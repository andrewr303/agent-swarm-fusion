---
description: Assign independent evidence shards, back up stragglers within the cap, and reconcile the verified results.
argument-hint: [task and runtime options]
---

Assign independent evidence shards, back up stragglers within the cap, and reconcile the verified results. Run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms run mapreduce --help` for the input flags, then invoke the same entry point with the task, provider IDs, and budget.

Reduce once after each shard resolves or exhausts its attempts. The default call cap is 13. To recover verified work, run `PYTHONPATH=<plugin-root>/python python3 -m fusion_swarm.forms salvage <run-dir>`.
