# Windows installer verification

This is the original Fusion 3.1 installer round. The newer Fusion 3.2 native
registration passed on all three hosts with **16 forms and 50 MCP tools**;
see [upgrade verification](UPGRADE_VALIDATION.md). Original evidence below is
retained with its own runtime/version boundaries.

## September 30 installer repair

The normal AndrewCode profile had a registered Fusion entry whose managed
directory was empty. The installer attempted to read a nonexistent `plugin.json`
before staging the package. This was reproduced with an isolated registry and
empty directory, then fixed by allowing empty destination directories. Nonempty
directories still require a Fusion ownership manifest; rollback and unrelated
registry preservation remain covered.

The obsolete marketplace `id` field was removed. Native Claude Code 2.1.286
validation passes without warnings against the installed runtime. Codex 0.159.2
also registered the updated marketplace and plugin in an isolated profile; all
42 critical package files matched the prepared runtime.

Verification for this repair:

- **22 installer tests passed** in 43.792 seconds under Windows PowerShell 5.1,
  including empty-directory recovery, rollback, unowned-directory preservation,
  and the native cache-lock diagnostic. Host-management fixtures are explicitly
  fake; the separate registrations above used real native CLIs.
- **Five Kimi packaging tests passed** in 0.826 seconds.
- Normal-profile AndrewCode installation succeeded with **16 forms and 50 MCP
  tools**. All 20 unrelated registry records and prior Fusion options were
  preserved. The runtime and AndrewCode managed package contain the repaired
  installer.
- Normal-profile Claude registration and installed-runtime validation succeeded
  without the earlier marketplace warning.
- The all-host refresh encountered Codex's native
  `failed to back up plugin cache entry: Access is denied` error. The existing
  Codex Fusion entry remains enabled and its runtime implementation matches the
  current package; its installer refresh is pending. The installer now explains
  how to close Codex and rerun `install-codex.ps1` from a separate PowerShell
  window. No running processes were stopped or active cache files deleted.

Original failure, native repair attempts, isolated Codex verification, the
registry preservation comparison and the final installer test log are retained
locally in the repair evidence directory `"../.tmp/installer-repair-20260930/"`
(desktop-only verification evidence, excluded from plugin packages).
The latest normal-profile all-host report retains the Codex failure; it is not
reported as a successful three-host refresh. These checks do not establish
client MCP reload, authenticated coding execution or a clean-machine bootstrap.

The original Fusion 3.1 installer round below was verified locally on September
30, 2026, separately from the earlier [693-test runtime verification](VALIDATION.md).
It does not claim a fresh full-suite run, live coding-agent execution or client
MCP loading.

## Outcomes observed

The actual `install.bat` → `install.cmd` → Windows PowerShell 5.1 chain completed
with exit 0 using existing dependencies and real native host CLIs:

| Host | Version used | Observed result |
| --- | --- | --- |
| AndrewCode | 0.34.0 | Slim managed package and registry written in the isolated home; real Python runtime and MCP protocol verified |
| Claude Code | 2.1.285 | Native marketplace/install/update/list registration; enabled Fusion 3.1.0+sidekick; confirmed local source path and current MCP declaration |
| Codex | 0.159.2 | Native marketplace/add/list registration; enabled Fusion 3.1.0+sidekick; critical code and installer files matched the cache |

The runtime reported **16 native coding forms and 23 coordination MCP tools**.
The final native run was recorded from `2026-09-30T21:00:27.6461291Z` to
`2026-09-30T21:00:42.9349658Z`, with all three hosts installed and no report errors.

`CODEX_HOME`, `CLAUDE_CONFIG_DIR` and `-AndrewCodeHome` selected isolated temporary
profiles. The command used `-SkipDependencies -SkipJq -NoPathUpdate -NoPause`,
explicit Python/Git Bash paths and existing real host executables. It did not
modify the user's normal plugin profiles, authenticate providers or call models.

The native run exposed and reproduced a Claude-specific distinction: a directory
marketplace with a relative plugin source loads the source in place, while the
inventory can retain a historical cache `installPath`. Verification now checks the
native local marketplace and declared MCP configuration. Codex cache verification
still requires matching critical files. This follows Claude's official
[local editing contract](https://code.claude.com/docs/en/plugin-marketplaces#test-an-edit-to-a-plugin).

Claude validation passed with its nonblocking warning about the historical
marketplace `id` field. Codex reported its known refusal to create helper PATH
aliases under a temporary `CODEX_HOME`; registration and cache verification still
completed. Native stderr was retained separately from machine-readable stdout.

## Supporting checks

- **17 installer tests passed** in 53.080 seconds, with no failures or skips.
  These exercise real Windows PowerShell 5.1 and Python processes, with explicitly
  fake host-management and package-manager CLIs in isolated fixture directories.
- A later junction-boundary hardening change passed **13 focused storage and Kimi
  tests**, including the additional real Windows junction preservation case.
  That gives 18 distinct installer cases covered; unaffected Windows integration
  cases retain the earlier evidence. Junction detection also avoids requiring
  Python's newer `Path.is_junction` API.
- **11 existing Kimi packaging and coordination MCP tests passed**, with no
  failures or skips. They cover real Python stdio transport, explicit board
  isolation, Unicode delivery and durable context updates.
- All five root PowerShell installer scripts parsed successfully. Actual batch
  execution also parsed and executed the common installer under PowerShell 5.1;
  the fixture tests execute the host-specific wrappers under that runtime.

Observable installer boundaries covered: repeated installation without duplicate
or lost unrelated entries; spaces/brackets/Unicode in paths; malformed registries;
locked and unowned destinations; staged package restoration after a registry
write failure; requested host failure with a nonzero overall result and other
hosts still attempted; stale Codex cache rejection; local Claude source versus
historical cache; rejection of an incorrect MCP declaration; safe Fusion-only
marketplace relocation and restoration after failure; shared marketplace
preservation; dry run without writes or host commands; and package-manager
arguments plus actual nonzero process diagnostics.

Reproduce the focused checks from the source checkout:

```powershell
python -B -m unittest discover -s tests -p test_windows_install.py -v
python -B -m unittest discover -s tests -p test_kimi_install.py -v
python -B -m unittest discover -s tests -p test_coordination_mcp.py -v
```

## Evidence and limitations

Original native failure and repaired-run logs, the completed native report, source
hashes and the raw resumed unittest completion are retained locally in
the evidence directory `.tmp/installer-verification-20260930T204656Z/` (not included in this repository).
The failure log is preserved; it was not replaced with the repaired result.
This directory is local verification evidence and is excluded from installed
plugin packages.

The installed `acceptance-neo@acceptance-neo-local` package was inspected and its
requirement/evidence review guidance applied. Its browser workflow and sealing
schema do not apply to this CLI installer and documentation scope. No browser UI
acceptance or independent reviewer is claimed.

Actual dependency downloads, missing-host bootstrapping, the AndrewCode full build,
user PATH mutation and elevated package installation were not executed. Their
code paths and available native contracts were inspected; a fake package-manager
process checked argument preservation and failure handling. These checks do not
prove installation on a clean Windows machine.

The installed skill discovery, MCP loading and coding workflows still require a
fresh running host session. Account authentication, selected-model availability,
paid execution, host project/local scope behavior, macOS/Linux installers and
performance benchmarks were not verified in this installer round.

The [four-repository improvement plan](UPGRADE_PLAN.md) was the original proposed artifact; it is now implemented locally.
Its communication, adapter, strategy and supervision phases were approved for
local implementation and offline verification after this installer round.
