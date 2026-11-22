#!/usr/bin/env python3
"""Install Fusion's slim surface into Kimi Code / AndrewCode's managed store."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fusion_install import INCLUDE_DIRS, INCLUDE_FILES, PLUGIN_ID, SKIP_DIR_NAMES, install_kimi


def _home() -> Path:
    configured = (os.environ.get("ANDREWCODE_HOME") or os.environ.get("KIMI_CODE_HOME")
                  or os.environ.get("KIMI_HOME") or "").strip()
    return Path(configured) if configured else Path.home() / ".andrewcode"


def install(plugin_root: Path, home: Path, *, dry_run: bool = False,
            python: str | None = None, bash: str | None = None) -> Path:
    return install_kimi(plugin_root, home, dry_run=dry_run, python=python, bash=bash)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--home", type=Path, default=_home())
    parser.add_argument("--python", help="Pin Python and configure the bundled MCP for AndrewCode")
    parser.add_argument("--bash", help="Git Bash path for legacy adapters")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        install(args.root, args.home, dry_run=args.dry_run, python=args.python, bash=args.bash)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Fusion installer: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
