"""Explicit offline host fixture; it never launches a coding agent."""
import json
import os
import shutil
import sys
from pathlib import Path

kind, *args = sys.argv[1:]
root = Path(os.environ["FUSION_TEST_ROOT"])
state_file = root / f"{kind}-fixture.json"
state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {
    "marketplaces": [{"name": "unrelated", "root": str(root / "unrelated"), "path": str(root / "unrelated")}],
    "installed": [], "commands": [],
}
state["commands"].append(args)


def save():
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def option(name, default=None):
    return args[args.index(name) + 1] if name in args else default


if os.environ.get("FUSION_TEST_FAIL") == kind + ":" + " ".join(args[:2]):
    save()
    print("Injected host installation failure", file=sys.stderr)
    raise SystemExit(7)
if kind == "codex" and args[:2] == ["plugin", "add"] and os.environ.get("FUSION_TEST_CACHE_LOCK"):
    save()
    print("Error: failed to back up plugin cache entry: Access is denied. (os error 5)", file=sys.stderr)
    raise SystemExit(1)
if args == ["--version"]:
    print(f"offline fixture {kind} 1.0")
elif args[:3] == ["plugin", "marketplace", "list"]:
    print("Fixture stderr diagnostic, separate from JSON", file=sys.stderr)
    print(json.dumps({"marketplaces": state["marketplaces"]} if kind == "codex" else state["marketplaces"]))
elif args[:3] == ["plugin", "marketplace", "add"]:
    path = Path(args[3])
    name = json.loads((path / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))["name"]
    existing = next((item for item in state["marketplaces"] if item["name"] == name), None)
    if existing and Path(existing["root"]) != path:
        print("Marketplace already exists at a different path", file=sys.stderr)
        save()
        raise SystemExit(8)
    if not existing:
        state["marketplaces"].append({"name": name, "root": str(path), "path": str(path), "source": "directory"})
    print("Fixture marketplace registered")
elif args[:3] == ["plugin", "marketplace", "remove"]:
    state["marketplaces"] = [item for item in state["marketplaces"] if item["name"] != args[3]]
    print("Fixture marketplace removed")
elif args[:2] in (["plugin", "add"], ["plugin", "install"], ["plugin", "update"]):
    selector = args[2]
    _, market = selector.split("@")
    source = Path(next(item["root"] for item in state["marketplaces"] if item["name"] == market))
    version = json.loads((source / "plugin.json").read_text(encoding="utf-8"))["version"]
    home = Path(os.environ["CODEX_HOME" if kind == "codex" else "CLAUDE_CONFIG_DIR"])
    cached = home / "plugins/cache" / market / "fusion" / version
    in_place = kind == "claude" and os.environ.get("FUSION_TEST_CLAUDE_LOAD_IN_PLACE")
    if not in_place or not cached.exists():
        if cached.exists():
            assert cached.resolve().is_relative_to(root.resolve())
            shutil.rmtree(cached)
        shutil.copytree(source, cached)
    corrupt_root = os.environ.get("FUSION_TEST_CORRUPT_ROOT")
    if os.environ.get("FUSION_TEST_CORRUPT") == kind and (not corrupt_root or source == Path(corrupt_root)):
        (cached / "python/fusion_swarm/team.py").write_text("# stale cached implementation\n", encoding="utf-8")
    identifier = "pluginId" if kind == "codex" else "id"
    record = {identifier: selector, "version": version, "enabled": True, "installed": True,
              "installPath": str(cached), "scope": option("--scope", "user")}
    if kind == "claude":
        record["mcpServers"] = json.loads((source / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    state["installed"] = [item for item in state["installed"] if item.get(identifier) != selector] + [record]
    print("Fixture plugin installed")
elif args[:2] == ["plugin", "list"]:
    print(json.dumps({"installed": state["installed"], "available": []} if kind == "codex" else state["installed"]))
elif args[:2] == ["plugin", "enable"]:
    for item in state["installed"]:
        if item["id"] == args[2]:
            if item["enabled"]:
                print("Plugin is already enabled", file=sys.stderr)
                save()
                raise SystemExit(1)
            item["enabled"] = True
    print("Fixture plugin enabled")
elif args[:2] == ["plugin", "validate"]:
    print("Fixture manifest validated")
else:
    print(f"Unsupported fixture command: {args}", file=sys.stderr)
    save()
    raise SystemExit(9)
save()
