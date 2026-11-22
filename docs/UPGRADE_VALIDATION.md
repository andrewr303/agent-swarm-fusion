# Fusion 3.2 verification

Verified locally on September 30, 2026. The approved four phases are implemented;
the boundary between local proof and missing live/browser proof is explicit.

## Observed checks

`C:/Python314/python.exe -B -m unittest discover -s tests -q`
passed **753 tests in 67.531 seconds**, with no failures or skips. The suite includes
existing static text/manifest checks; those do not establish model behavior.
Existing HTTP test teardown emitted a socket ResourceWarning, without a test failure.

The behavior checks use actual Windows/Python processes, SQLite restart/migration,
stdio MCP and a localhost HTTP server. Model/host-dispatch fixtures are explicitly
fake; local deterministic workers execute real Python coding/check commands.

| Outcome | Concrete evidence |
| --- | --- |
| Communication survives interruption/restart | producer identity, fresh delivery leases, atomic database effect receipt, dead letters, stale attempts, visible unanswered questions and private traces |
| Execution retains original ownership | stable outbox tickets, once-only claims, restart, original process creation identity, owned cancellation/deadline/supervisor-crash handling |
| Selected models and dependencies remain explicit | model mismatch refusal, accepted prerequisite before dispatch, pause/version guards, same-model retry bounds and infrastructure stop; direct worker submission cannot bypass observed model/scope acceptance |
| Scope and provisioning are inspectable | actual baseline hashes including ignored changes, conflict-preserving directory integration, pre-existing dependencies distinguished from worker changes |
| Candidate reviews refer to exact artifacts | pre-acceptance read-only review, critical resolution evidence, stale digest refusal, review invalidation/rework and read-only input version fencing |
| Strategies alter schedules and communication | all legacy forms, independently composed axes, six workflows, migration fan-out/join, separate selected review seats |
| Knowledge and supervision remain current | cited SHA256 revisions, accepted source requirement, invalidation, search, persisted status/audit, redacted exports and explicit missing usage |
| Dashboard controller affects only its bound run | actual HTTP state/control/export, token/Origin guards, reload-equivalent state readback and stale version refusal |

Eight scenarios from `scripts/benchmark_swarm.py` passed with observed local
timings: coding/restart, delivery interruption, lost dispatch, scope violation,
infrastructure circuit, stale review, stale knowledge and owned cancellation.
Provider usage/cost is unavailable; no model was called. The v2 scenario set can
compare a compatible prior measured round. No historical speed, defect-rate,
quality or cost advantage is inferred from these fixture measurements.

## Native installation

The actual `install.cmd` → Windows PowerShell 5.1 installer passed for all hosts,
using existing dependencies and isolated profiles. The completed native report
ends at `2026-09-30T23:15:05.7977685Z`.

| Host | Actual version | Verification |
| --- | --- | --- |
| AndrewCode | 0.34.0 | managed slim package/registry plus actual Python runtime and stdio MCP smoke |
| Claude Code | 2.1.286 | native marketplace/install/update/list; enabled Fusion 3.2; confirmed local-source loading and current MCP declaration |
| Codex | 0.159.2 | native marketplace/add/list; enabled Fusion 3.2; all selected critical code/install files matched the cache |

Runtime smoke reports **16 forms and 50 coordination MCP tools**. The normal user
profiles were not installed into. `-SkipDependencies -SkipJq -NoPathUpdate` used
existing Python/Git/host CLIs. No authentication or paid provider execution ran.

The final cache check covers 42 critical files, including the board migrations.
After adding that cache guard, all 18 installer cases passed again in 50.188
seconds. The final acceptance guard passed 71 focused tests before the full
753-test round. These checks cover the final code rather than an earlier package.

The first fresh fixture attempt had an absent explicit `CODEX_HOME` directory,
which Codex rejects. Its failed report/log are retained. Creating the empty owned
fixture profile resolved that precondition; repeated native installation and
package verification then passed. Claude's historical marketplace `id` warning
and Codex's refusal to create PATH helpers under a temporary home were nonblocking.

## Browser boundary and retained evidence

BrowserOS tools were absent. The in-app browser, Chrome and Edge entry points all
failed before contact: `failed to write kernel assets: The system cannot find the
path specified. (os error 3)`. The actual source dashboard started on localhost
with a public-controller/actual-process fixture, but no browser opened it.
The owned dashboard server was stopped after the attempts.

The local Acceptance Neo record (`.acceptances/fusion-swarm/20260930-browser-blocked/report.md`,
not retained in this repository) retained three blocked cases for visible run facts, pause/resume and layout/
keyboard access. Its report contract is valid with **0 pass, 3 blocked**; required
screenshots/DOM captures are absent. HTTP checks are supporting proof only.

Raw local release evidence was kept under
`.tmp/swarm-release-evidence/` (not retained in this repository): native success
and initial failed fixture logs/reports, final test-suite output, offline benchmark
JSON and critical-file hashes. The previous 3.1/installer rounds remain separate.
Files suffixed `final-scope` identify the final guarded implementation; earlier
release checks remain preserved with their original scope.

Unverified: clean-machine WinGet/official downloads/AndrewCode build/PATH changes;
fresh authenticated host/client MCP loading; paid live coding/model behavior;
native adapter binding and worker exchange; actual Git worktree/host-managed
execution; browser visuals/keyboard/responsive layout. Shared scope checks remain
cooperative and report other declared scopes as unattributed; they are not an OS
sandbox. Existing pride gate/cross-consult and selected roster contracts remain.
