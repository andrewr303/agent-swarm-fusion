# Fusion 3.1 validation

Historical baseline record. The newer Fusion 3.2 delivery passed **753 tests**;
see [upgrade validation](UPGRADE_VALIDATION.md) for current execution, installer,
benchmark and blocked browser evidence. The counts below retain their original
3.1 verification scope.

Verified locally on Windows with `C:/Python314/python.exe`, Git Bash and stdlib
unittest on 2026-09-30. Source changes are in this workspace; installation and
live client loading were not performed in that runtime verification round.
The subsequent Windows installer work and isolated native host registration
checks are documented separately in [installer verification](INSTALLER_VALIDATION.md).

## Results

- Full `unittest` discovery of `tests/test_*.py`: **693 tests passed, zero
  failures, errors or skips**. This includes the existing graph, MetaLoop,
  Hive, mode override, dashboard API and package suites.
- **56 new behavioral tests** cover durable mail, transactional task states,
  ownership, stale attempt/lease rejection, concurrent consumers/writers,
  context versions, form schedules, native CLI planning, provider boundaries
  and the actual stdio MCP transport. Tests invoke real Python subprocesses
  where persistence, UTF-8, CLI or transport behavior matters.
- Both native `mcp.json` and legacy `.mcp.json` command configurations start
  the real server and expose explicit-board tool schemas. Tests expand the
  documented plugin-root placeholder; they do not execute Codex's loader.
- Kimi/AndrewCode installation tests copy the actual surface into temporary
  homes, retain the new launcher/server/configs and exclude other MCP vendors.
- Changed Bash scripts pass `bash -n`: `fusion.sh`, `route.sh`, provider
  `detect.sh` and `fable.sh`.
- Both example specs compile offline. Runtime catalog inspection confirms
  **16 native coding forms**, **36 combined designer forms** and **23 MCP tools**;
  all five new command specs map to their actual engine patterns.

The suite also contains static prompt/document and metadata checks. Those verify
packaging and text contracts, not model behavior, coding quality or compliance
with the prompts. Provider adapters in runtime fixtures are mocked/fake; no paid
model, Claude process or production integration was called.

## Reproduce

From the plugin directory:

```text
python -B -m unittest discover -s tests -p "test_*.py"
python -B fusion.py forms
python -B fusion.py sidekick --spec docs/examples/sidekick-team.json
python -B fusion.py team --spec docs/examples/map-reduce-team.json
```

The full run used the same `unittest.defaultTestLoader.discover('tests',
pattern='test_*.py')` through a compact result reporter. Examples use model
placeholders and are planning templates, not authenticated runtime inventories.

## Remaining verification boundaries

No real native coding swarm or paid provider was dispatched. New skills and MCP
registration have not been loaded in a running Codex/Kimi client, and the
dashboard was not exercised in a browser. File leases coordinate cooperative
writers; they are not an OS sandbox. Worker report validation does not prove
commands were executed; the lead must review actual output and artifacts.

Benchmark source claims, relative model capability, cost savings and production
performance were not tested. See [research provenance](RESEARCH.md) and
[coordination](COORDINATION.md) for the implemented boundaries.
