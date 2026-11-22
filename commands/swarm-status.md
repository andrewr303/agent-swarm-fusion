---
description: Inspect durable swarm status, next operator actions, visible communication and evidence exports.
argument-hint: --board FILE --lead LEAD --run RUN [--json]
---

Use `python <plugin-root>/fusion.py supervise status $ARGUMENTS` and the
[swarm runtime](../docs/SWARM_RUNTIME.md). `supervise trace` reads visible correlated
messages; `supervise export --output FILE` saves redacted evidence. Readiness and
scope come from persisted host/baseline observations. Missing model identity,
provider usage or native capabilities remain unavailable. The dashboard's Active
Run tab uses the same bound board and audited pause/resume operations.
