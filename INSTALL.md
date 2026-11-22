# Installing Fusion

The Fusion installers install Fusion's runtime and dependencies, then register the
plugin in AndrewCode, Claude Code and Codex on both **macOS** and **Windows**.
Registration uses each host's native interface. Installing the Claude Code plugin
does not enable Claude as a swarm provider or change the selected models.
For a guided first install and an offline first run, see [Getting started](docs/GETTING_STARTED.md).

## macOS quick start

Run from this checkout (or double-click `install.command` in macOS Finder):

```bash
./install.command
# or from Terminal:
bash install.sh
```

Use a host-specific installer when you only want one integration:

```bash
./install-all.command
./install-andrewcode.command
./install-claude-code.command
./install-codex.command
```

All wrappers accept both POSIX flags (`--hosts`, `--skip-dependencies`, `--dry-run`,
`--no-pause`) and PowerShell-style flags (`-Hosts`, `-SkipDependencies`, `-DryRun`,
`-NoPause`). `install.sh` is the common POSIX entry point and defaults to all three
hosts (`andrewcode`, `claude`, `codex`). `install.command` is double-clickable in
macOS Finder, keeps diagnostics visible and preserves the exit code. Pass
`--no-pause` (or `-NoPause`) for unattended runs.

## Windows quick start

Run from this checkout:

```powershell
.\install-all.ps1
# Double-click friendly equivalent:
.\install.bat
```

If PowerShell blocks local scripts:

```powershell
powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File .\install-all.ps1
```

Use a host-specific installer when you only want one integration:

```powershell
.\install-andrewcode.ps1
.\install-claude-code.ps1
.\install-codex.ps1
```

All four wrappers accept the options below. `install.ps1` is the common entry
point and also accepts `-Hosts andrewcode,claude,codex`. Its default is all three.
`install.bat` calls `install.cmd`, which runs Windows PowerShell 5.1, keeps failures
visible and preserves the exit code. Pass `-NoPause` for unattended batch runs.

## What the installer does

1. Finds Python 3.10+ with SQLite and Bash (`sed`, `awk`). On Windows it installs
   missing Python, Git for Windows Bash and jq through WinGet; on macOS it installs
   missing packages through Homebrew (`brew`). jq supports the legacy learning
   ledger; `--skip-jq` / `-SkipJq` selects its fallback. The Python coordination
   engine uses the stdlib, so it requires no pip packages.
2. Copies only Fusion's plugin surface into `~/Library/Application Support/Fusion/plugin`
   on macOS (`%LOCALAPPDATA%\Fusion\plugin` on Windows, `${XDG_DATA_HOME:-~/.local/share}/fusion/plugin`
   on Linux). Vendor checkouts, node_modules, credentials and local caches are excluded.
   It creates `bin/fusion` (POSIX executable) alongside `bin\fusion.ps1` and
   `bin\fusion.cmd`, and adds the `bin` directory to the user PATH (shell profiles on
   POSIX, User PATH on Windows) unless `--no-path-update` / `-NoPathUpdate` is supplied. On POSIX the installer appends the
   `export PATH` line to `~/.zshrc`, and also to `~/.bash_profile` and `~/.profile` when they exist.
3. Generates installed MCP configurations using the selected Python and Bash.
   The source checkout's portable manifests stay unchanged. It executes the forms
   catalog and the stdio MCP to verify the copied runtime.
4. Finds each requested host CLI. Missing Codex and Claude Code are installed on
   macOS/Linux with `npm install -g` (`@openai/codex`, `@anthropic-ai/claude-code`) when `npm`
   exists, otherwise a Homebrew cask. On Windows Codex uses the official OpenAI installer and
   Claude Code uses WinGet (`Anthropic.ClaudeCode`). Missing AndrewCode requires the local fork source described below.
   Existing CLIs are reused.
5. Copies the AndrewCode plugin into its managed registry. For Claude Code and
   Codex it adds the local `fusion-hive` marketplace pointing at the prepared slim
   package and installs Fusion. It verifies the enabled version and critical
   cached files for Codex. Claude's directory marketplace loads the local source;
   the installer verifies that registration and its declared MCP configuration.
   A rerun refreshes Fusion while preserving unrelated plugins.
6. Writes `logs/latest-install.json`. A requested host failure produces a nonzero
   exit and a failed report; other requested hosts are still attempted.

Package managers or upstream dependency installers may request elevation according to
their own policy. Host accounts are signed in separately. The installer performs
no model calls and does not install optional panel providers such as Copilot, Grok
or OpenCode. Review the report before starting a coding run.

## AndrewCode source installation

AndrewCode is a personal fork, so the installer does not replace it with upstream
Kimi. When AndrewCode is missing, provide its source:

```bash
# macOS:
./install-all.command --andrewcode-source ~/dev/andrew-code
```

```powershell
# Windows:
.\install-all.ps1 -AndrewCodeSource C:\dev\andrew-code
```

`ANDREWCODE_SOURCE` also works. If that parameter is absent, the installer recognizes
`~/dev/andrew-code` (on macOS/POSIX) or `C:\dev\andrew-code` (on Windows) when its
native installer is present.

The managed home defaults to `ANDREWCODE_HOME`, then `KIMI_CODE_HOME`, then
`KIMI_HOME`, then `~/.andrewcode` (`%USERPROFILE%\.andrewcode` on Windows). Use
`--andrewcode-home` / `-AndrewCodeHome` to override it.

## Useful options

| POSIX (`install.sh` / `.command`) | Windows (`install.ps1` / `.bat`) | Effect |
| --- | --- | --- |
| `--dry-run` | `-DryRun` | Prints the intended steps; creates no installation and invokes no host CLI |
| `--skip-dependencies` | `-SkipDependencies` | Offline mode: reuses existing Python, Bash and requested host CLIs; a missing required tool fails |
| `--skip-jq` | `-SkipJq` | Does not require jq; uses the ledger fallback |
| `--no-register` or `--hosts none` | `-NoRegister` or `-Hosts none` | Installs only Fusion and its dependencies |
| `--no-path-update` | `-NoPathUpdate` | Does not add Fusion's `bin` directory to user PATH |
| `--install-root /path/to/Fusion` | `-InstallRoot C:\Tools\Fusion` | Changes Fusion's runtime, launchers and report directory |
| `--python /path/to/python3` | `-Python C:\Python314\python.exe` | Pins the interpreter used by launchers and installed MCP configurations |
| `--bash /path/to/bash` | `-GitBash 'C:\Program Files\Git\bin\bash.exe'` | Pins the Bash executable used by runtime scripts |
| `--codex-command`, `--claude-command`, `--andrewcode-command` | `-CodexCommand`, `-ClaudeCommand`, `-AndrewCodeCommand` | Selects an existing host executable or script explicitly |
| `--scope user\|project\|local` | `-Scope user\|project\|local` | Selects the Claude Code plugin scope; default is user |
| `--project-directory /path/to/project` | `-ProjectDirectory C:\dev\project` | Working directory for Claude project/local registration; default is the invoking directory |
| `--no-pause` | `-NoPause` | Suppresses the interactive launcher's final pause |
| `--smoke` | none | `install.sh` only: after a successful install, runs `tests/smoke-providers.sh`, which makes live provider calls |

For a runtime-only offline install:

```bash
# macOS:
bash install.sh --no-register --skip-dependencies --skip-jq --no-path-update
```

```powershell
# Windows:
.\install.ps1 -NoRegister -SkipDependencies -SkipJq -NoPathUpdate
```

For Codex only with existing dependencies:

```bash
# macOS:
./install-codex.command --skip-dependencies --skip-jq
```

```powershell
# Windows:
.\install-codex.ps1 -SkipDependencies -SkipJq
```

Codex respects `CODEX_HOME`; Claude Code respects `CLAUDE_CONFIG_DIR`. These can
select isolated profiles for verification. They are not reassigned by the installer.
A requested host is never silently skipped because it is unavailable.

## Native registration and configuration preservation

Codex and Claude Code use `fusion@fusion-hive`. Their marketplace points at
`<InstallRoot>\plugin`, so do not move that directory after installation. If the
same marketplace already points elsewhere, the installer relocates it only when
its local manifest contains exactly Fusion with source `./`. It uses native
marketplace commands and attempts to restore the prior registration if installation
fails. A shared or unknown marketplace is preserved and reported as a failure.

AndrewCode's `plugins\installed.json` retains other entries and existing Fusion
capability selections. Malformed registry data is preserved with an error. Managed
plugin replacement is staged and restores the previous package if its registry
write fails. An unowned runtime directory or a linked managed destination is
rejected. Do not point `-InstallRoot` at the source checkout.
An empty managed Fusion directory left by an earlier installation is recovered
automatically. A nonempty directory without a Fusion manifest is preserved and
reported as an ownership error.

Claude Code loads relative plugin sources directly from a registered directory
marketplace. Its inventory's `installPath` can still name an older cache; that
cache is not used as the proof of the current local source. The installer checks
the native marketplace path and current MCP declaration instead. See the official
[local plugin editing contract](https://code.claude.com/docs/en/plugin-marketplaces#test-an-edit-to-a-plugin).

For manual registration after a successful runtime-only installation:

```powershell
$pluginRoot = Join-Path $env:LOCALAPPDATA 'Fusion\plugin'
codex plugin marketplace add $pluginRoot --json
codex plugin add fusion@fusion-hive --json
claude plugin marketplace add $pluginRoot --scope user
claude plugin install fusion@fusion-hive --scope user
```

If an existing marketplace uses another root, use the full installer to handle
relocation and verification. Do not use AndrewCode's `/plugins install` on this
checkout while it contains vendor trees; that command copies the whole directory.

Direct Python registration also remains available, using an already installed
interpreter. Passing `--python` generates the inline AndrewCode MCP declaration:

```powershell
python scripts\install-kimi.py --home $env:USERPROFILE\.andrewcode --python C:\Python314\python.exe
```

## Start using Fusion

Open a new terminal for PATH changes. Run:

```powershell
fusion forms --json
fusion comms --help
```

AndrewCode: `/plugins reload`, then `/new`, then `/fusion:sidekick` or
`/fusion:team`. Claude Code: restart the client and use `/fusion:sidekick` or
`/fusion:team`. Codex: start a new chat and invoke `fusion-sidekick` or `fusion-team`.
The existing genius-boss and idiot-boss commands select leadership; the forms
catalog describes the 16 available coding structures.

Runtime protocol checks and native plugin inventory do not establish MCP loading
or agent execution in an already running client. Check those in the fresh host
session. See [coordination](docs/COORDINATION.md), [Codex](CODEX.md),
[AndrewCode](KIMI.md) and [installer verification](docs/INSTALLER_VALIDATION.md).

## Updates and troubleshooting

Rerun the same installer to refresh the managed runtime and host packages. A
nonzero exit names the failed command and retains its diagnostics in the report.
If a new dependency is installed but cannot be resolved, open a fresh terminal or
pass its executable path. Use Git for Windows Bash on Windows.

If Codex reports `failed to back up plugin cache entry: Access is denied`, its
Fusion cache may be in use. Close Codex completely and run `./install-codex.command`
on macOS (or `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-codex.ps1`
on Windows) from a separate terminal window. Keep the existing cache in place until
the native installer can replace it.

Installation does not authenticate providers, validate their quotas or change
model selection. Use the explicitly selected providers when configuring a run.

[The improvement plan](docs/UPGRADE_PLAN.md) is a historical record of the phases that were approved
for local implementation and offline verification. It is not a roadmap.
