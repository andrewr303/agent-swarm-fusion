---
description: Fusion — the selected host coordinates native coding teams, Sidekick or an explicitly requested model panel.
argument-hint: <task or question> [--mode sidekick|genius-boss|idiot-boss|team|panel|council|debate|vote|swarm|solo] [--huddle]
---

For Sidekick or either boss mode invoke the **fusion-sidekick** skill; for native
coding teams or swarm forms invoke **fusion-team**. Otherwise invoke the
**fusion-orchestrate** skill (the Conductor) and run its dynamic procedure
on the task below.

Task: $ARGUMENTS

Rules of engagement:
- This is the **dynamic** entry: read the task, run `fusion.sh route`, read `lessons.md`,
  and **choose** the best collaboration mode — do not default to a rigid pipeline. If the
  user passed `--mode`, honor it. If they passed `--huddle`, run a scoping huddle first.
- Preserve the selected lead and worker models. Native coding modes use native
  tools and durable task review. Never invoke Claude/Fable without an explicit request.
- Show the cost banner before dispatching paid CLIs. Actually dispatch the panel (no
  simulation). Mark non-returning panelists absent.
- Judge with the right track (run-the-code for code, five-section synthesis for research).
- Present the answer in Fusion's voice + the audit trail (mode, panel, consensus, cost, and
  for decisions: kill criteria + one next step).
- Record the run with `fusion.sh ledger record` so Fusion compounds.
