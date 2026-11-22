"""Stdlib Fusion packaging, installation and offline verification.

Copies only the plugin surface. Staged replacements restore an existing plugin
if its registry write fails. Never invokes providers, login or model prompts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

PLUGIN_ID = "fusion"
MCP_SERVERS = ("fusion-coordination", "fusion-board")
INCLUDE_DIRS = (
    "skills", "commands", "scripts", "python", "config", "memory", "docs",
    "assets", "tests", ".kimi-plugin", ".claude-plugin", ".codex-plugin",
    "mcps/fusion-coordination", "mcps/fusion-board",
)
INCLUDE_FILES = (
    "kimi.plugin.json", "plugin.json", "SYSTEM.md", "README.md", "INSTALL.md",
    "CODEX.md", "KIMI.md", "LICENSE", "install.sh", "install.command",
    "install-all.command", "install-andrewcode.command",
    "install-claude-code.command", "install-codex.command",
    "install.ps1", "install.bat", "install.cmd", "install-all.ps1",
    "install-andrewcode.ps1", "install-claude-code.ps1", "install-codex.ps1",
    "fusion.py", ".mcp.json", "mcp.json",
)
SKIP_DIR_NAMES = {"__pycache__", ".git", "node_modules", ".omc", ".repowise"}
CHECK_FILES = (
    "fusion.py", "plugin.json", "kimi.plugin.json", ".mcp.json", "mcp.json",
    ".claude-plugin/plugin.json", ".codex-plugin/plugin.json",
    "mcps/fusion-coordination/server.py", "mcps/fusion-board/server.py", "python/fusion_swarm/board.py", "python/fusion_swarm/team.py",
    "python/fusion_swarm/sidekick.py", "python/fusion_swarm/mailbox.py",
    "python/fusion_swarm/coordination.py", "skills/fusion-sidekick/SKILL.md",
    "python/fusion_swarm/protocol.py",
    "python/fusion_swarm/execution.py", "python/fusion_swarm/host_runtime.py",
    "python/fusion_swarm/process_runtime.py", "python/fusion_swarm/controller.py",
    "python/fusion_swarm/scope.py", "python/fusion_swarm/strategies.py",
    "python/fusion_swarm/candidates.py", "python/fusion_swarm/knowledge.py",
    "python/fusion_swarm/supervision.py", "python/fusion_swarm/supervise.py",
    "python/fusion_swarm/control.py", "python/fusion_swarm/mcp_server.py",
    "scripts/dashboard_server.py", "scripts/dashboard_static.html", "scripts/benchmark_swarm.py",
    "skills/fusion-team/SKILL.md", "commands/genius-boss.md", "commands/idiot-boss.md",
    "install.sh", "install.command", "install-all.command",
    "install-andrewcode.command", "install-claude-code.command", "install-codex.command",
    "install.ps1", "install.bat", "install.cmd", "install-all.ps1",
    "install-andrewcode.ps1", "install-claude-code.ps1", "install-codex.ps1",
    "scripts/fusion_install.py", "scripts/install-kimi.py",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def read_object(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object; existing data was preserved")
    return data


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def validate_source(root: Path) -> dict:
    paths = ("plugin.json", "kimi.plugin.json", ".kimi-plugin/plugin.json",
             ".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
    manifests = [read_object(root / item) for item in paths]
    if any(item.get("name") != PLUGIN_ID for item in manifests):
        raise ValueError("Every host manifest must name Fusion")
    if len({item.get("version") for item in manifests}) != 1 or not manifests[0].get("version"):
        raise ValueError("Fusion host manifest versions do not agree")
    market = read_object(root / ".claude-plugin/marketplace.json")
    entries = market.get("plugins")
    if not market.get("name") or not isinstance(entries, list) or len(entries) != 1:
        raise ValueError("Expected a named marketplace containing only Fusion")
    if entries[0].get("name") != PLUGIN_ID or entries[0].get("source") != "./":
        raise ValueError("The Fusion marketplace must reference its own plugin root")
    for item in CHECK_FILES:
        path = root / item
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Missing or escaping plugin file: {path}")
    for field in ("skills", "commands", "systemPromptPath"):
        value = manifests[1].get(field)
        if not isinstance(value, str) or not value.startswith("./"):
            raise ValueError(f"Invalid Kimi manifest path: {field}")
        path = (root / value).resolve()
        if not path.is_relative_to(root.resolve()) or not path.exists():
            raise ValueError(f"Missing or escaping Kimi manifest path: {field}")
    return {"version": manifests[0]["version"], "marketplace": market["name"]}


def linked_path(path: Path) -> bool:
    """Detect Windows junctions too, including on Python before is_junction."""
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except FileNotFoundError:
        return False


def remove_owned_tree(path: Path, parent: Path) -> None:
    """Resolve containment immediately before every recursive deletion."""
    parent = parent.resolve()
    if linked_path(path):
        raise ValueError(f"Refusing to delete a linked installation: {path}")
    target = path.resolve()
    if target == parent or not target.is_relative_to(parent):
        raise ValueError(f"Refusing deletion outside {parent}: {target}")
    if path.exists():
        shutil.rmtree(path)


@contextmanager
def install_lock(parent: Path):
    parent.mkdir(parents=True, exist_ok=True)
    lock = parent / ".fusion-install.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise RuntimeError(f"Another installer owns {lock}; reconcile a stopped installer before removing its lock") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "startedAt": now()}, stream)
        yield
    finally:
        lock.unlink()


def copy_surface(source: Path, target: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        for name in names:
            if name in SKIP_DIR_NAMES:
                continue
            child = Path(directory) / name
            if linked_path(child):
                raise ValueError(f"Linked source files are not packaged: {child}")
        return set(names) & SKIP_DIR_NAMES

    for dirname in INCLUDE_DIRS:
        src = source / dirname
        if src.is_dir():
            if linked_path(src) or not src.resolve().is_relative_to(source):
                raise ValueError(f"Source directory escapes the plugin: {src}")
            shutil.copytree(src, target / dirname, ignore=ignore)
    for filename in INCLUDE_FILES:
        src = source / filename
        if src.is_file():
            if linked_path(src) or not src.resolve().is_relative_to(source):
                raise ValueError(f"Linked source files are not packaged: {src}")
            shutil.copy2(src, target / filename)
    if os.name != "nt":
        for pattern in ("*.sh", "*.command"):
            for script in target.rglob(pattern):
                if script.is_file() and not linked_path(script):
                    script.chmod(0o755)


def configure_runtime(root: Path, final_root: Path, python: str, bash: str | None) -> None:
    """Generate installed config; the checkout's manifests stay portable."""
    py_path = Path(python)
    path_parts = [str(py_path.parent)]
    libexec_bin = py_path.parent.parent / "libexec/bin"
    if os.name != "nt" and libexec_bin.is_dir():
        path_parts.append(str(libexec_bin))
    if os.environ.get("PATH"):
        path_parts.append(os.environ["PATH"])
    env = {"PYTHONUTF8": "1", "FUSION_PYTHON": python,
           "PATH": os.pathsep.join(path_parts)}
    if bash:
        env["FUSION_BASH"] = bash
    legacy = read_object(root / ".mcp.json")
    for server in MCP_SERVERS:
        # fusion-coordination needs an explicit board_path per call; fusion-board resolves its own board.
        extra = ["--require-board"] if server == "fusion-coordination" else []
        legacy["mcpServers"][server] = {
            "command": python,
            "args": ["-B", str(final_root / f"mcps/{server}/server.py"), *extra],
            "env": env,
        }
    atomic_json(root / ".mcp.json", legacy)
    py_cmd = "python" if (os.name == "nt" or (py_path.parent / "python").is_file() or (libexec_bin / "python").is_file()) else py_path.name
    native = read_object(root / "mcp.json")
    for server in MCP_SERVERS:
        native["mcpServers"][server]["command"] = py_cmd
        native["mcpServers"][server]["env"] = env
    atomic_json(root / "mcp.json", native)
    # AndrewCode reads inline MCP declarations from kimi.plugin.json. Its
    # parser requires a PATH command and a contained ./ cwd (args stay relative).
    for relative in ("kimi.plugin.json", ".kimi-plugin/plugin.json"):
        manifest = read_object(root / relative)
        servers = manifest.setdefault("mcpServers", {})
        for server in MCP_SERVERS:
            extra = ["--require-board"] if server == "fusion-coordination" else []
            servers[server] = {
                "transport": "stdio", "command": py_cmd, "cwd": "./",
                "args": ["-B", f"mcps/{server}/server.py", *extra], "env": env,
            }
        atomic_json(root / relative, manifest)


@contextmanager
def replace_surface(source: Path, destination: Path, *, python: str | None = None, bash: str | None = None):
    """Commit replacement after the caller's registry/metadata write succeeds."""
    if linked_path(destination):
        raise ValueError(f"Refusing to replace a linked installation: {destination}")
    caller_dest = destination.expanduser().absolute()
    source, destination = source.resolve(), destination.resolve()
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError("The installation destination must be separate from its source checkout")
    if destination.exists():
        if not destination.is_dir():
            raise ValueError(f"Refusing to replace a path that is not a directory: {destination}")
        # Interrupted or removed installs can leave an empty managed directory.
        # It has no package to identify or user files to overwrite. Nonempty
        # directories still require a Fusion manifest before replacement.
        if any(destination.iterdir()):
            manifest = destination / "plugin.json"
            if (not manifest.is_file() or linked_path(manifest)
                    or read_object(manifest).get("name") != PLUGIN_ID):
                raise ValueError(f"Refusing to replace a directory not owned by Fusion: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".fusion-stage-", dir=destination.parent))
    backup = destination.with_name(f".fusion-backup-{uuid4().hex}")
    switched = False
    try:
        copy_surface(source, staging)
        if python:
            configure_runtime(staging, caller_dest, python, bash)
        validate_source(staging)
        if destination.exists():
            destination.rename(backup)
        staging.rename(destination)
        switched = True
        yield caller_dest
    except BaseException:
        if switched:
            remove_owned_tree(destination, destination.parent)
        if backup.exists():
            backup.rename(destination)
        raise
    finally:
        remove_owned_tree(staging, destination.parent)
    remove_owned_tree(backup, destination.parent)


def install_kimi(source: Path, home: Path, *, dry_run: bool = False,
                 python: str | None = None, bash: str | None = None) -> Path:
    source, home = source.resolve(), home.expanduser().absolute()
    validate_source(source)
    managed = home / "plugins/managed/fusion"
    registry = home / "plugins/installed.json"
    if dry_run:
        print(f"[dry-run] copy plugin surface -> {managed}\n[dry-run] upsert {registry}")
        return managed
    with install_lock(home / "plugins"):
        payload = read_object(registry) if registry.exists() else {"version": 1, "plugins": []}
        if payload.get("version", 1) != 1 or not isinstance(payload.get("plugins"), list):
            raise ValueError(f"Unsupported plugin registry in {registry}; existing data was preserved")
        records = payload["plugins"]
        if not all(isinstance(item, dict) for item in records):
            raise ValueError(f"Invalid plugin records in {registry}; existing data was preserved")
        original = next((item for item in records if item.get("id") == PLUGIN_ID), {})
        record = {**original, "id": PLUGIN_ID, "root": str(managed), "source": "local-path",
                  "enabled": True, "originalSource": str(source), "updatedAt": now()}
        record.setdefault("installedAt", now())
        updated = {**payload, "version": 1,
                   "plugins": [item for item in records if item.get("id") != PLUGIN_ID] + [record]}
        with replace_surface(source, managed, python=python, bash=bash):
            atomic_json(registry, updated)
    print(f"installed Fusion -> {managed}\nrecorded in {registry}")
    print("reload: in AndrewCode / Kimi run /plugins reload then /new")
    return managed


def verify_runtime(root: Path, python: str) -> dict:
    metadata = validate_source(root)
    options = {"cwd": root, "text": True, "encoding": "utf-8", "capture_output": True,
               "timeout": 25, "env": {**os.environ, "PYTHONUTF8": "1"},
               "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0}
    result = subprocess.run([python, "-B", str(root / "fusion.py"), "forms", "--json"], **options)
    if result.returncode:
        raise RuntimeError(f"Installed forms failed: {result.stderr.strip()}")
    forms = json.loads(result.stdout)
    if len(forms.get("forms", [])) < 16:
        raise RuntimeError("The installed coding forms are incomplete")
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    result = subprocess.run([python, "-B", str(root / "mcps/fusion-coordination/server.py"), "--require-board"],
        input="".join(json.dumps(item) + "\n" for item in requests), **options)
    if result.returncode or result.stderr.strip():
        raise RuntimeError(f"Installed coordination MCP failed: {result.stderr.strip()}")
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    if len(replies) != 2 or replies[0].get("result", {}).get("serverInfo", {}).get("name") != "fusion-coordination":
        raise RuntimeError("Installed MCP did not initialize correctly")
    tools = replies[1].get("result", {}).get("tools", [])
    if not tools or not all("board_path" in item["inputSchema"].get("required", []) for item in tools):
        raise RuntimeError("Installed MCP lacks explicit board isolation")
    return {**metadata, "forms": len(forms["forms"]), "mcpTools": len(tools)}


def _is_legacy_fusion_root(root: Path) -> bool:
    manifest = root / "plugin/plugin.json"
    if not manifest.is_file() or linked_path(manifest):
        return False
    try:
        return read_object(manifest).get("name") == PLUGIN_ID
    except Exception:
        return False


def install_runtime(source: Path, destination: Path, python: str, bash: str | None) -> dict:
    if linked_path(destination):
        raise ValueError(f"Refusing to replace a linked installation: {destination}")
    caller_dest = destination.expanduser().absolute()
    source, resolved_dest = source.resolve(), destination.resolve()
    validate_source(source)
    if source.is_relative_to(resolved_dest) or resolved_dest.is_relative_to(source):
        raise ValueError("InstallRoot must be separate from the source checkout")
    marker = caller_dest / "fusion-install.json"
    if marker.exists() and read_object(marker).get("owner") != "fusion-installer":
        raise ValueError(f"InstallRoot is not owned by Fusion: {caller_dest}")
    if not marker.exists() and not _is_legacy_fusion_root(caller_dest) and any(
        (caller_dest / item).exists() for item in ("plugin", "bin", "runtime.json")
    ):
        raise ValueError(f"Existing unowned files in InstallRoot were preserved: {caller_dest}")
    with install_lock(caller_dest):
        with replace_surface(source, caller_dest / "plugin", python=python, bash=bash) as installed:
            verification = verify_runtime(installed, python)
            bin_dir = caller_dest / "bin"
            bin_dir.mkdir(exist_ok=True)
            (bin_dir / "fusion.ps1").write_text(
                "$ErrorActionPreference = 'Stop'\n"
                "$runtime = Get-Content -LiteralPath (Join-Path (Split-Path $PSScriptRoot -Parent) 'runtime.json') -Encoding UTF8 -Raw | ConvertFrom-Json\n"
                "$env:PYTHONUTF8 = '1'\n$env:FUSION_PYTHON = $runtime.python\n"
                "if ($runtime.bash) { $env:FUSION_BASH = $runtime.bash }\n"
                "& $runtime.python -B (Join-Path $runtime.plugin 'fusion.py') @args\nexit $LASTEXITCODE\n", encoding="utf-8")
            (bin_dir / "fusion.cmd").write_text(
                '@echo off\r\n"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
                '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0fusion.ps1" %*\r\nexit /b %ERRORLEVEL%\r\n',
                encoding="utf-8", newline="")
            quoted_py = "'" + python.replace("'", "'\"'\"'") + "'"
            fusion_sh = bin_dir / "fusion"
            fusion_sh.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
                'RUNTIME_JSON="$(dirname "$SCRIPT_DIR")/runtime.json"\n'
                f"DEFAULT_PY={quoted_py}\n"
                'PY_BIN="${FUSION_PYTHON:-$DEFAULT_PY}"\n'
                'if [ ! -x "$PY_BIN" ]; then PY_BIN="$(command -v python3 || command -v python)"; fi\n'
                'exec "$PY_BIN" -B -c \'import json, os, sys; '
                'r = json.load(open(sys.argv[1], encoding="utf-8-sig")); '
                'os.environ["PYTHONUTF8"] = "1"; '
                'os.environ["FUSION_PYTHON"] = r["python"]; '
                'r.get("bash") and os.environ.__setitem__("FUSION_BASH", r["bash"]); '
                'os.execv(r["python"], [r["python"], "-B", os.path.join(r["plugin"], "fusion.py"), *sys.argv[2:]])\' '
                '"$RUNTIME_JSON" "$@"\n',
                encoding="utf-8",
            )
            if os.name != "nt":
                fusion_sh.chmod(0o755)
            atomic_json(caller_dest / "runtime.json", {"python": python, "bash": bash, "plugin": str(installed)})
            atomic_json(marker, {"owner": "fusion-installer", "source": str(source), "updatedAt": now(), **verification})
    return {"root": str(caller_dest / "plugin"), "bin": str(caller_dest / "bin"), **verification}


def compare_installed(source: Path, installed: Path) -> None:
    for relative in CHECK_FILES:
        original, cached = source / relative, installed / relative
        if not cached.is_file() or hashlib.sha256(original.read_bytes()).digest() != hashlib.sha256(cached.read_bytes()).digest():
            raise RuntimeError(f"Installed plugin does not match the prepared package: {cached}")


def verify_host(host: str, listing: Path, source: Path, cache: Path | None, scope: str,
                marketplaces: Path | None = None) -> dict:
    metadata = validate_source(source)
    selector = f"fusion@{metadata['marketplace']}"
    data = json.loads(listing.read_text(encoding="utf-8-sig"))
    entries = data.get("installed", []) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise RuntimeError(f"Unrecognized {host} plugin list output")
    identifier = "pluginId" if host == "codex" else "id"
    matches = [item for item in entries if item.get(identifier) == selector]
    if host == "claude":
        matches = [item for item in matches if item.get("scope") == scope]
    record = next((item for item in matches if item.get("enabled") is True), None)
    if record is None or record.get("version") != metadata["version"]:
        raise RuntimeError(f"{host} did not report an enabled {selector} at the expected version")
    if host == "codex" and record.get("installed") is not True:
        raise RuntimeError("Codex did not confirm that Fusion is installed")
    install_path = record.get("installPath") or record.get("install_path")
    load_mode = "cache"
    local_root = None
    if host == "claude" and marketplaces:
        catalogs = json.loads(marketplaces.read_text(encoding="utf-8-sig"))
        if not isinstance(catalogs, list):
            raise RuntimeError("Unrecognized Claude marketplace list output")
        market = next((item for item in catalogs if item.get("name") == metadata["marketplace"]), None)
        if market and market.get("source") == "directory":
            market_path = Path(market.get("path") or "")
            if market_path.resolve() != source.resolve():
                raise RuntimeError("Claude's local marketplace does not point at the prepared package")
            local_root = market_path
            load_mode = "local-source"
    if local_root:
        # Claude's directory marketplace with a relative source loads in place.
        # Its inventory may retain an obsolete installPath cache from an older
        # install. Verify the current native source registration instead.
        installed = local_root
        declared = record.get("mcpServers")
        expected = read_object(source / ".mcp.json")["mcpServers"]
        if not isinstance(declared, dict) or declared.get("fusion-coordination") != expected["fusion-coordination"]:
            raise RuntimeError("Claude did not report the prepared coordination MCP declaration")
        if "fusion-board" in expected and declared.get("fusion-board") != expected["fusion-board"]:
            raise RuntimeError("Claude did not report the prepared board MCP declaration")
    elif install_path:
        installed = Path(install_path)
    elif host == "codex" and cache:
        installed = cache / metadata["marketplace"] / "fusion" / metadata["version"]
    else:
        raise RuntimeError(f"Cannot verify {host}'s installed plugin path")
    compare_installed(source, installed)
    return {"host": host, "plugin": selector, "installedRoot": str(installed),
            "version": metadata["version"], "loadMode": load_mode}


def default_install_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
        return base / "Fusion"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Fusion"
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share"))
    return base / "fusion"


def default_andrewcode_home() -> Path:
    for var in ("ANDREWCODE_HOME", "KIMI_CODE_HOME", "KIMI_HOME"):
        val = os.environ.get(var)
        if val:
            return Path(val).expanduser().absolute()
    return (Path.home() / ".andrewcode").absolute()


def resolve_executable(name: str | None) -> str | None:
    if not name or not name.strip():
        return None
    candidate = Path(name).expanduser()
    if candidate.is_file():
        return str(candidate.absolute())
    found = shutil.which(name)
    if found:
        return str(Path(found).absolute())
    return None


def add_process_path(directory: str | Path | None) -> None:
    if not directory:
        return
    dir_path = Path(directory).expanduser().absolute()
    if not dir_path.is_dir():
        return
    dir_str = str(dir_path).rstrip("/\\")
    entries = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p.rstrip("/\\") != dir_str]
    os.environ["PATH"] = os.pathsep.join([str(dir_path), *entries])


def refresh_process_path(andrewcode_home: Path) -> None:
    for candidate in (
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path.home() / ".local/bin",
        Path.home() / ".cargo/bin",
        andrewcode_home / "bin",
    ):
        add_process_path(candidate)


def add_user_path(directory: str | Path, *, no_path_update: bool) -> None:
    add_process_path(directory)
    if no_path_update or os.name == "nt":
        return
    dir_str = str(Path(directory).expanduser().absolute())
    export_line = f'export PATH="{dir_str}:$PATH"\n'
    profiles = [Path.home() / ".zshrc"]
    for extra in (".bash_profile", ".profile"):
        p = Path.home() / extra
        if p.is_file():
            profiles.append(p)
    for profile in profiles:
        try:
            existing = profile.read_text(encoding="utf-8") if profile.is_file() else ""
            if dir_str not in existing:
                with profile.open("a", encoding="utf-8") as fh:
                    if existing and not existing.endswith("\n"):
                        fh.write("\n")
                    fh.write(f"# Added by Fusion installer\n{export_line}")
        except OSError:
            pass


def run_checked(command: str, args: list[str], *, capture: bool = False, cwd: Path | None = None,
                env: dict[str, str] | None = None, timeout: int = 120) -> str:
    cmd_path = Path(command)
    if cmd_path.is_file() and not os.access(cmd_path, os.X_OK) and cmd_path.suffix in (".sh", ".command"):
        argv = [shutil.which("bash") or "/bin/bash", str(cmd_path), *args]
    else:
        argv = [str(command), *args]
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env or os.environ.copy(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        detail = " ".join(part for part in (out, err) if part)
        raise RuntimeError(f"{command} failed (exit {result.returncode}). {detail}".strip())
    if err:
        print(err)
    if not capture and out:
        print(out)
    return result.stdout or ""


def install_posix_package(package: str, *, cask: bool = False, skip_dependencies: bool = False) -> None:
    if skip_dependencies:
        raise RuntimeError(f"{package} is required; --skip-dependencies prevents installing it.")
    brew = resolve_executable("brew")
    if not brew:
        raise RuntimeError(
            f"Homebrew (brew) is required to install {package} automatically on macOS. "
            f"Install Homebrew or supply {package} and rerun."
        )
    print(f"Installing dependency: {package}")
    args = ["install", "--cask", package] if cask else ["install", package]
    run_checked(brew, args, timeout=600)


def find_python(requested: str | None) -> str | None:
    candidates: list[str] = []
    if requested:
        resolved = resolve_executable(requested)
        if not resolved:
            raise RuntimeError(f"The requested Python executable was not found: {requested}")
        candidates.append(resolved)
    else:
        for name in ("python3", "python", "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"):
            resolved = resolve_executable(name)
            if resolved and resolved not in candidates:
                candidates.append(resolved)
    probe = "import sys,json,sqlite3; assert sys.version_info >= (3,10); print(json.dumps(dict(path=sys.executable,version=list(sys.version_info[:3]))))"
    for candidate in candidates:
        try:
            raw = run_checked(candidate, ["-c", probe], capture=True, timeout=15)
            details = json.loads(raw)
            if details.get("path") and Path(details["path"]).is_file():
                return str(details["path"])
        except Exception as exc:
            if requested:
                raise RuntimeError(f"The selected Python must be 3.10+ with SQLite support. {exc}") from None
    return None


def ensure_python(requested: str | None, *, skip_dependencies: bool) -> str:
    executable = find_python(requested)
    if not executable:
        install_posix_package("python", skip_dependencies=skip_dependencies)
        executable = find_python(requested)
    if not executable:
        raise RuntimeError("Python 3.10+ was not found after installation. Rerun in a new terminal or use --python /path/to/python3.")
    add_process_path(Path(executable).parent)
    return executable


def find_bash(requested: str | None) -> str | None:
    candidates: list[str] = []
    if requested:
        resolved = resolve_executable(requested)
        if not resolved:
            raise RuntimeError(f"The requested Bash executable was not found: {requested}")
        candidates.append(resolved)
    else:
        for name in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", "bash", "/bin/bash"):
            resolved = resolve_executable(name)
            if resolved and resolved not in candidates:
                candidates.append(resolved)
    for candidate in candidates:
        try:
            run_checked(candidate, ["-c", "command -v sed >/dev/null && command -v awk >/dev/null"], capture=True, timeout=10)
            return candidate
        except Exception:
            if requested:
                raise
    return None


def ensure_bash(requested: str | None, *, skip_dependencies: bool) -> str:
    executable = find_bash(requested)
    if not executable:
        install_posix_package("bash", skip_dependencies=skip_dependencies)
        executable = find_bash(requested)
    if not executable:
        raise RuntimeError("Bash with sed and awk was not found. Use --bash /path/to/bash.")
    return executable


def install_andrewcode_cli(source_root: Path, andrewcode_source: str | None, andrewcode_home: Path,
                           install_root: Path, *, skip_dependencies: bool, no_path_update: bool) -> None:
    if skip_dependencies:
        raise RuntimeError("AndrewCode CLI is missing; --skip-dependencies prevents installing it.")
    source_path: Path | None = Path(andrewcode_source).expanduser() if andrewcode_source else None
    if not source_path:
        for candidate in (
            Path.home() / "dev/andrew-code",
            source_root.resolve().parents[1] / "andrew-code",
            Path("C:/dev/andrew-code"),
        ):
            if (candidate / "scripts/install-andrewcode.mjs").is_file():
                source_path = candidate
                break
    if not source_path or not (source_path / "scripts/install-andrewcode.mjs").is_file():
        raise RuntimeError(
            "AndrewCode is a personal fork. Supply --andrewcode-source /path/to/andrew-code "
            "with its native installer; upstream Kimi is not substituted."
        )
    source_path = source_path.resolve()
    node = resolve_executable("node")
    if not node:
        install_posix_package("node", skip_dependencies=skip_dependencies)
        node = resolve_executable("node")
    if not node:
        raise RuntimeError("AndrewCode requires Node.js. Install Node and rerun.")
    env = {**os.environ, "ANDREWCODE_HOME": str(andrewcode_home)}
    if (source_path / "install.sh").is_file():
        run_checked("bash", [str(source_path / "install.sh")], cwd=source_path, env=env, timeout=600)
    else:
        run_checked(node, [str(source_path / "scripts/install-andrewcode.mjs")], cwd=source_path, env=env, timeout=600)
    refresh_process_path(andrewcode_home)


def ensure_host(name: str, override: str | None, *, source_root: Path, andrewcode_source: str | None,
                andrewcode_home: Path, install_root: Path, skip_dependencies: bool, no_path_update: bool) -> str:
    command = resolve_executable(override) if override else None
    if override and not command:
        raise RuntimeError(f"The requested {name} executable was not found: {override}")
    if not command:
        command = resolve_executable(name)
    if not command:
        if skip_dependencies:
            raise RuntimeError(f"{name} CLI is missing; --skip-dependencies prevents installing it.")
        if name == "codex":
            npm = resolve_executable("npm")
            if npm:
                run_checked(npm, ["install", "-g", "@openai/codex"], timeout=300)
            else:
                install_posix_package("codex", cask=True, skip_dependencies=skip_dependencies)
        elif name == "claude":
            npm = resolve_executable("npm")
            if npm:
                run_checked(npm, ["install", "-g", "@anthropic-ai/claude-code"], timeout=300)
            else:
                install_posix_package("claude-code", cask=True, skip_dependencies=skip_dependencies)
        elif name == "andrewcode":
            install_andrewcode_cli(
                source_root, andrewcode_source, andrewcode_home, install_root,
                skip_dependencies=skip_dependencies, no_path_update=no_path_update,
            )
        refresh_process_path(andrewcode_home)
        command = resolve_executable(name)
    if not command:
        raise RuntimeError(f"{name} was not found after setup. Rerun in a new terminal or supply its executable path.")
    version = run_checked(command, ["--version"], capture=True).strip()
    print(f"{name}: {version}")
    return command


def same_path(first: str | Path | None, second: str | Path | None) -> bool:
    if not first or not second:
        return False
    return Path(str(first)).expanduser().resolve() == Path(str(second)).expanduser().resolve()


def set_fusion_marketplace(host_name: str, command: str, current_root: str | None,
                           plugin_root: str, market: str, scope: str, cwd: Path | None = None) -> str | None:
    if current_root and same_path(current_root, plugin_root):
        return None
    flags = ["--json"] if host_name == "codex" else ["--scope", scope]
    if current_root:
        old_manifest = Path(current_root) / ".claude-plugin/marketplace.json"
        if not old_manifest.is_file():
            raise RuntimeError(f"Cannot safely relocate {market} from {current_root}; its local manifest is missing.")
        old = read_object(old_manifest)
        plugins = old.get("plugins")
        if (old.get("name") != market or not isinstance(plugins, list) or len(plugins) != 1
                or plugins[0].get("name") != "fusion" or plugins[0].get("source") != "./"):
            raise RuntimeError(f"Marketplace {market} is not exclusively owned by Fusion. Existing configuration was preserved.")
        run_checked(command, ["plugin", "marketplace", "remove", market, *flags], cwd=cwd)
    try:
        run_checked(command, ["plugin", "marketplace", "add", plugin_root, *flags], cwd=cwd)
    except Exception as exc:
        failure = str(exc)
        if current_root:
            try:
                run_checked(command, ["plugin", "marketplace", "add", current_root, *flags], cwd=cwd)
            except Exception as restore_exc:
                raise RuntimeError(f"{failure} Restoring the prior marketplace also failed: {restore_exc}") from None
        raise RuntimeError(failure) from None
    return current_root


def restore_fusion_marketplace(host_name: str, command: str, previous_root: str | None,
                               market: str, scope: str, cwd: Path | None = None) -> None:
    if not previous_root:
        return
    if host_name == "codex":
        run_checked(command, ["plugin", "marketplace", "remove", market, "--json"], cwd=cwd)
        run_checked(command, ["plugin", "marketplace", "add", previous_root, "--json"], cwd=cwd)
        run_checked(command, ["plugin", "add", f"fusion@{market}", "--json"], cwd=cwd)
    else:
        run_checked(command, ["plugin", "marketplace", "remove", market, "--scope", scope], cwd=cwd)
        run_checked(command, ["plugin", "marketplace", "add", previous_root, "--scope", scope], cwd=cwd)
        run_checked(command, ["plugin", "install", f"fusion@{market}", "--scope", scope], cwd=cwd)


def register_codex(command: str, plugin_root: str, market: str, install_root: Path) -> None:
    raw = run_checked(command, ["plugin", "marketplace", "list", "--json"], capture=True)
    listing = json.loads(raw)
    marketplaces = listing.get("marketplaces", []) if isinstance(listing, dict) else []
    existing = [item for item in marketplaces if isinstance(item, dict) and item.get("name") == market]
    old_root = existing[0].get("root") if existing else None
    relocated = set_fusion_marketplace("codex", command, old_root, plugin_root, market, "user")
    try:
        run_checked(command, ["plugin", "add", f"fusion@{market}", "--json"])
        listing_path = install_root / "logs/codex-plugins.json"
        listing_path.parent.mkdir(parents=True, exist_ok=True)
        listing_out = run_checked(command, ["plugin", "list", "--marketplace", market, "--json"], capture=True)
        listing_path.write_text(listing_out, encoding="utf-8")
        codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        verify_host("codex", listing_path, Path(plugin_root), codex_home / "plugins/cache", "user")
    except Exception as exc:
        failure = str(exc)
        if "failed to back up plugin cache entry" in failure and (
            "Access is denied" in failure or "os error 5" in failure or "Permission denied" in failure
        ):
            failure += (
                " The Fusion cache may be in use. Close Codex completely, then rerun "
                "install-codex.command (or install-codex.ps1 on Windows) from a separate terminal window."
            )
        try:
            restore_fusion_marketplace("codex", command, relocated, market, "user")
        except Exception as restore_exc:
            raise RuntimeError(f"{failure} Restoring the prior Fusion marketplace failed: {restore_exc}") from None
        raise RuntimeError(failure) from None


def register_claude(command: str, plugin_root: str, market: str, install_root: Path,
                    scope: str, project_dir: Path) -> None:
    run_checked(command, ["plugin", "validate", plugin_root], cwd=project_dir)
    raw = run_checked(command, ["plugin", "marketplace", "list", "--json"], capture=True, cwd=project_dir)
    listing = json.loads(raw)
    if not isinstance(listing, list):
        listing = [listing]
    existing = [item for item in listing if isinstance(item, dict) and item.get("name") == market]
    old_root = existing[0].get("path") if existing else None
    relocated = set_fusion_marketplace("claude", command, old_root, plugin_root, market, scope, cwd=project_dir)
    try:
        run_checked(command, ["plugin", "install", f"fusion@{market}", "--scope", scope], cwd=project_dir)
        run_checked(command, ["plugin", "update", f"fusion@{market}", "--scope", scope], cwd=project_dir)
        installed_raw = run_checked(command, ["plugin", "list", "--json"], capture=True, cwd=project_dir)
        installed = json.loads(installed_raw)
        if not isinstance(installed, list):
            installed = [installed]
        enabled = [
            item for item in installed
            if isinstance(item, dict) and item.get("id") == f"fusion@{market}"
            and item.get("scope") == scope and item.get("enabled") is True
        ]
        if not enabled:
            run_checked(command, ["plugin", "enable", f"fusion@{market}", "--scope", scope], cwd=project_dir)
        listing_path = install_root / "logs/claude-plugins.json"
        listing_path.parent.mkdir(parents=True, exist_ok=True)
        listing_path.write_text(
            run_checked(command, ["plugin", "list", "--json"], capture=True, cwd=project_dir),
            encoding="utf-8",
        )
        marketplaces_path = install_root / "logs/claude-marketplaces.json"
        marketplaces_path.write_text(
            run_checked(command, ["plugin", "marketplace", "list", "--json"], capture=True, cwd=project_dir),
            encoding="utf-8",
        )
        verify_host("claude", listing_path, Path(plugin_root), None, scope, marketplaces_path)
    except Exception as exc:
        failure = str(exc)
        try:
            restore_fusion_marketplace("claude", command, relocated, market, scope, cwd=project_dir)
        except Exception as restore_exc:
            raise RuntimeError(f"{failure} Restoring the prior Fusion marketplace failed: {restore_exc}") from None
        raise RuntimeError(failure) from None


def install_all(
    source_root: Path,
    *,
    hosts: list[str],
    install_root: Path | None = None,
    andrewcode_home: Path | None = None,
    andrewcode_source: str | None = None,
    python_arg: str | None = None,
    bash_arg: str | None = None,
    codex_command: str | None = None,
    claude_command: str | None = None,
    andrewcode_command: str | None = None,
    scope: str = "user",
    project_directory: Path | None = None,
    skip_dependencies: bool = False,
    skip_jq: bool = False,
    no_path_update: bool = False,
    no_register: bool = False,
    dry_run: bool = False,
) -> int:
    selected: list[str] = []
    for item in hosts:
        for part in item.split(","):
            clean = part.strip().lower()
            if clean and clean not in selected:
                selected.append(clean)
    if not selected:
        selected = ["all"]
    valid_hosts = {"all", "andrewcode", "claude", "codex", "none"}
    if any(h not in valid_hosts for h in selected):
        print(f"Unsupported host selection: {selected}", file=sys.stderr)
        return 2
    if len(selected) > 1 and ("all" in selected or "none" in selected):
        print("Use all/none alone, or list individual hosts.", file=sys.stderr)
        return 2
    if "all" in selected:
        selected = ["andrewcode", "claude", "codex"]
    if no_register or "none" in selected:
        selected = []

    source_root = source_root.expanduser().absolute()
    target_root = (install_root or default_install_root()).expanduser().absolute()
    ac_home = (andrewcode_home or default_andrewcode_home()).expanduser().absolute()
    proj_dir = (project_directory or Path.cwd()).expanduser().absolute()

    if source_root.resolve().is_relative_to(target_root.resolve()) or target_root.resolve().is_relative_to(source_root.resolve()):
        print("InstallRoot must be separate from the source checkout.", file=sys.stderr)
        return 1

    if dry_run:
        print(f"Fusion dry run: source={source_root}")
        print(f"Install program and fusion command into {target_root}")
        print("Check Python 3.10+, Bash and jq; install missing dependencies where supported.")
        print("Register hosts: " + ", ".join(selected))
        print("Missing Codex/Claude CLIs are installed through their official distributions; AndrewCode uses --andrewcode-source.")
        print(f"AndrewCode home: {ac_home}; Claude scope: {scope}")
        print(f"SkipDependencies={skip_dependencies}; SkipJq={skip_jq}; NoPathUpdate={no_path_update}")
        return 0

    report: dict = {
        "startedAt": now(),
        "source": str(source_root),
        "installRoot": str(target_root),
        "requestedHosts": selected,
        "runtime": "pending",
        "dependencies": [],
        "hosts": {name: {"status": "pending", "detail": ""} for name in selected},
        "errors": [],
    }
    complete = False
    try:
        refresh_process_path(ac_home)
        py_exe = ensure_python(python_arg, skip_dependencies=skip_dependencies)
        bash_exe = ensure_bash(bash_arg, skip_dependencies=skip_dependencies)
        os.environ["PYTHONUTF8"] = "1"
        os.environ["FUSION_PYTHON"] = py_exe
        os.environ["FUSION_BASH"] = bash_exe
        report["dependencies"].append({"name": "python", "path": py_exe, "status": "ready"})
        report["dependencies"].append({"name": "bash", "path": bash_exe, "status": "ready"})
        if not skip_jq:
            jq = resolve_executable("jq")
            if not jq and not skip_dependencies:
                install_posix_package("jq", skip_dependencies=skip_dependencies)
                jq = resolve_executable("jq")
            if jq:
                run_checked(jq, ["--version"])
                report["dependencies"].append({"name": "jq", "status": "ready", "path": jq})
            elif skip_dependencies:
                print("WARNING: jq is absent; the legacy learning ledger will use its fallback.")
                report["dependencies"].append({"name": "jq", "status": "optional-missing"})
            else:
                raise RuntimeError("jq was not found after dependency installation. Use --skip-jq for the fallback ledger.")

        runtime = install_runtime(source_root, target_root, py_exe, bash_exe)
        report["runtime"] = "verified"
        report["verification"] = runtime
        add_user_path(runtime["bin"], no_path_update=no_path_update)
        print(f"Fusion installed: {runtime['forms']} coding forms, {runtime['mcpTools']} coordination tools.")

        for name in selected:
            try:
                if name == "andrewcode":
                    ensure_host(
                        "andrewcode", andrewcode_command,
                        source_root=source_root, andrewcode_source=andrewcode_source,
                        andrewcode_home=ac_home, install_root=target_root,
                        skip_dependencies=skip_dependencies, no_path_update=no_path_update,
                    )
                    install_kimi(Path(runtime["root"]), ac_home, python=py_exe, bash=bash_exe)
                    verify_runtime(ac_home / "plugins/managed/fusion", py_exe)
                elif name == "claude":
                    cmd = ensure_host(
                        "claude", claude_command,
                        source_root=source_root, andrewcode_source=andrewcode_source,
                        andrewcode_home=ac_home, install_root=target_root,
                        skip_dependencies=skip_dependencies, no_path_update=no_path_update,
                    )
                    register_claude(cmd, runtime["root"], runtime["marketplace"], target_root, scope, proj_dir)
                elif name == "codex":
                    cmd = ensure_host(
                        "codex", codex_command,
                        source_root=source_root, andrewcode_source=andrewcode_source,
                        andrewcode_home=ac_home, install_root=target_root,
                        skip_dependencies=skip_dependencies, no_path_update=no_path_update,
                    )
                    register_codex(cmd, runtime["root"], runtime["marketplace"], target_root)
                report["hosts"][name] = {"status": "installed", "detail": "Registry and installed package verified"}
                print(f"Fusion registered with {name}.")
            except Exception as host_exc:
                msg = str(host_exc)
                report["hosts"][name] = {"status": "failed", "detail": msg}
                report["errors"].append(f"{name} : {msg}")
                print(f"{name} installation failed: {msg}", file=sys.stderr)

        complete = len(report["errors"]) == 0
    except Exception as exc:
        msg = str(exc)
        report["errors"].append(msg)
        print(f"Fusion installation failed: {msg}", file=sys.stderr)
    finally:
        report["completedAt"] = now()
        report["status"] = "complete" if complete else "failed"
        try:
            report_path = target_root / "logs/latest-install.json"
            atomic_json(report_path, report)
            print(f"Installation report: {report_path}")
        except Exception as save_exc:
            complete = False
            print(f"Could not save installation report: {save_exc}", file=sys.stderr)

    if not complete:
        return 1
    print("Fusion installation complete. Open a new terminal to use: fusion forms --json")
    if "andrewcode" in selected:
        print("AndrewCode: /plugins reload, then /new, then /fusion:sidekick")
    if "claude" in selected:
        print("Claude Code: restart the client, then /fusion:sidekick")
    if "codex" in selected:
        print("Codex: start a new chat and invoke fusion-sidekick or fusion-team")
    print("Sign in to missing host accounts yourself. This installation made no model calls.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("runtime", "verify", "verify-host", "install"):
        command = sub.add_parser(action)
        command.add_argument("--root", type=Path, required=True)
        if action in ("runtime", "verify"):
            command.add_argument("--python", default=sys.executable)
        if action == "runtime":
            command.add_argument("--destination", type=Path, required=True)
            command.add_argument("--bash")
        if action == "verify-host":
            command.add_argument("--host", choices=("codex", "claude"), required=True)
            command.add_argument("--listing", type=Path, required=True)
            command.add_argument("--cache", type=Path)
            command.add_argument("--marketplaces", type=Path)
            command.add_argument("--scope", default="user")
        if action == "install":
            command.add_argument("--hosts", action="append", default=[])
            command.add_argument("--install-root", type=Path)
            command.add_argument("--andrewcode-home", type=Path)
            command.add_argument("--andrewcode-source")
            command.add_argument("--python")
            command.add_argument("--bash")
            command.add_argument("--codex-command")
            command.add_argument("--claude-command")
            command.add_argument("--andrewcode-command")
            command.add_argument("--scope", choices=("user", "project", "local"), default="user")
            command.add_argument("--project-directory", type=Path)
            command.add_argument("--skip-dependencies", action="store_true")
            command.add_argument("--skip-jq", action="store_true")
            command.add_argument("--no-path-update", action="store_true")
            command.add_argument("--no-register", action="store_true")
            command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "install":
            return install_all(
                args.root,
                hosts=args.hosts or ["all"],
                install_root=args.install_root,
                andrewcode_home=args.andrewcode_home,
                andrewcode_source=args.andrewcode_source,
                python_arg=args.python,
                bash_arg=args.bash,
                codex_command=args.codex_command,
                claude_command=args.claude_command,
                andrewcode_command=args.andrewcode_command,
                scope=args.scope,
                project_directory=args.project_directory,
                skip_dependencies=args.skip_dependencies,
                skip_jq=args.skip_jq,
                no_path_update=args.no_path_update,
                no_register=args.no_register,
                dry_run=args.dry_run,
            )
        if args.action == "runtime":
            result = install_runtime(args.root, args.destination, args.python, args.bash)
        elif args.action == "verify":
            result = verify_runtime(args.root, args.python)
        else:
            result = verify_host(args.host, args.listing, args.root, args.cache, args.scope, args.marketplaces)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Fusion installer: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
