# Fusion on Kimi Code / AndrewCode

Fusion 3.1 adds `/fusion:sidekick`, `/fusion:genius-boss`, `/fusion:idiot-boss`,
`/fusion:team` and `/fusion:forms`. Follow [coding teams](docs/CODING_TEAMS.md):
keep the selected host and explicit native worker roster. The planner queues
scoped work; native host tools execute it and the lead reviews actual artifacts.

The surface installer includes the stdlib coordination MCP and Python launcher,
while excluding other vendored MCP trees. The installer generates an
inline MCP declaration in both installed Kimi manifests, with the installed
plugin as its working directory and the selected Python directory on PATH.
Loading that declaration still needs a fresh host session; runtime protocol
verification does not prove client loading. Direct Python installation without
`--python` retains the source declarations and needs the configuration described
in [coordination](docs/COORDINATION.md#the-fusion-coordination-mcp-server). Set `FUSION_HOST=kimi` or `andrewcode`
for legacy external panels. Claude/Fable needs an explicit request and
`FUSION_ALLOW_CLAUDE=1`; no native mode adds it implicitly.

Kimi Code and AndrewCode (the Kimi Code fork, whose default home is `~/.andrewcode`)
do **not** read `.claude-plugin/` or `.codex-plugin/`. They need a Kimi
manifest at one of:

```text
<plugin_root>/kimi.plugin.json
<plugin_root>/.kimi-plugin/plugin.json
```

Fusion ships both. `kimi.plugin.json` wins if both exist.

## Install (recommended)

From the plugin directory on Windows:

```powershell
.\install-andrewcode.ps1
# All three hosts: .\install-all.ps1
```

This installs missing Python/Git prerequisites, copies the slim plugin to
`~/.andrewcode/plugins/managed/fusion`, generates its coordination MCP declaration,
preserves other entries in `plugins/installed.json` and verifies the real runtime.
An existing AndrewCode CLI is reused. If it is absent, supply
`-AndrewCodeSource C:\dev\andrew-code`; the fork's full dependency installation
and build run before registration. See [INSTALL.md](INSTALL.md) for options and
failure behavior.

Then in AndrewCode / Kimi:

```text
/plugins reload
/new
/fusion:sidekick
```

## Manual install (no installer)

In a Kimi / AndrewCode session:

```text
/plugins install <path-to-a-slim-copy-of-the-plugin>
```

**Do not do that if this checkout still contains vendor trees.** `/plugins install`
copies the whole directory. Use `install.bat` / `scripts/install-kimi.py` instead,
or pass a slim copy.

Direct Python registration with an existing interpreter:

```powershell
python scripts\install-kimi.py --python C:\Python314\python.exe
```

This copies and registers the slim plugin and configures its MCP; it does not
install dependencies or the AndrewCode program. Override the home with `--home`,
`ANDREWCODE_HOME`, `KIMI_CODE_HOME` or `KIMI_HOME`.

## After install

Plugin commands are namespaced: `/fusion:hive`, `/fusion:designer`, `/fusion:board`,
`/fusion:metaloop`, `/fusion:panel`, …

Nested hive workers still spawn **real** `andrewcode` processes:

```text
node C:/dev/andrew-code/apps/kimi-code/dist/main.mjs -m <alias> --thinking-effort <effort> --prompt-file <file> --output-format stream-json
```

The entry path is the documented Windows default in `python/fusion_swarm/andrewcode_cli.py`.
Override it with `FUSION_ANDREWCODE_ENTRY`.

Hive Board:

```text
python -m fusion_swarm.board --db "$FUSION_BOARD" poll --agent "$FUSION_AGENT_ID"
```

(`PYTHONPATH` must include the plugin's `python/` directory; `scripts/swarm.sh`
and `scripts/board.sh` set that.)

## Verify

```text
/plugins info fusion
/fusion:setup
bash scripts/swarm.sh hive "smoke" --dry-run --json
```

A missing Kimi manifest is the failure mode this file exists to prevent:
`No manifest at kimi.plugin.json or .kimi-plugin/plugin.json`.
