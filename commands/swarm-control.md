---
description: Control a prepared Fusion run using durable dispatch ownership, observed native handles and explicit lead review.
argument-hint: <operation> --board FILE --input JSON_FILE
---

Use the [swarm runtime](../docs/SWARM_RUNTIME.md) for $ARGUMENTS. The concrete API is
`python <plugin-root>/fusion.py control OPERATION --board FILE --input JSON_FILE`.
Native host tools supply actual live models and operations. Claim each ticket
once before native launch, record the original handle/model and wait through
exposed completion tools. Preserve uncertain dispatches for reconciliation. Stop
the original worker and inspect changes before recovery. Acceptance requires
actual artifact/check review; pause/resume, budgets and queued scope changes are
versioned, audited lead operations. No automatic provider fallback or publication.
