"""Workspace-aware communication entrypoint, using the same board as Hive.

An injected FUSION_BOARD wins. Otherwise state is scoped to the workspace at
.andrewcode/comms/board.sqlite. There is no mirrored second mailbox store.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def resolve_board(cwd: str | None = None, directory: str | None = None) -> str:
    injected = os.environ.get("FUSION_BOARD", "").strip()
    if injected:
        return injected
    root = Path(directory) if directory else Path(cwd or Path.cwd()) / ".andrewcode" / "comms"
    return str(root.resolve() / "board.sqlite")


def main(argv: list[str] | None = None) -> int:
    from .board import main as board_main
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cwd")
    parser.add_argument("--dir", dest="directory")
    parser.add_argument("--db")
    args, remaining = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    return board_main(["--db", args.db or resolve_board(args.cwd, args.directory), *remaining])


if __name__ == "__main__":
    raise SystemExit(main())
