---
description: List Fusion's 16 native coding forms with their dependency and communication mechanics. No model calls.
argument-hint: [--json]
---

Run `swarm.sh forms --json`, or `python <plugin-root>/fusion.py forms`.
For a coding task invoke the **fusion-team** skill with the requested form.

## Runtime forms (3.3.0)

From the plugin root, use `PYTHONPATH=python python3 -m fusion_swarm.forms list`
to see the ten runtime forms. Use `plan --task <text> --providers <a,b>` to
choose a form and estimate width, `run <form> --help` to inspect inputs, or
`salvage <run-dir>` to recover verified output.

[Refute](refute.md), [tournament](tournament.md), [hedge](hedge.md),
[map/reduce](mapreduce.md), [critic-repair](critic-repair.md), [pair](pair.md),
[Delphi](delphi.md), [auction](auction.md), [dry](dry.md), and
[blackboard](blackboard.md).
