import argparse
import json
from pathlib import Path

from .board import HiveBoard
from . import supervision


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fusion terminal supervision and evidence export")
    parser.add_argument("operation", choices=("status", "trace", "export", "doctor"))
    parser.add_argument("--board", type=Path)
    parser.add_argument("--lead")
    parser.add_argument("--run")
    parser.add_argument("--host", choices=("codex", "andrewcode", "claude"))
    parser.add_argument("--executable")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.operation == "doctor":
        if not args.host:
            parser.error("doctor requires --host")
        value = supervision.doctor(args.host, args.executable)
    else:
        if not args.board or not args.board.is_file() or not args.lead or not args.run:
            parser.error("an existing --board, --lead and --run are required")
        with HiveBoard(args.board) as board:
            value = (board.trace(args.lead, args.run) if args.operation == "trace" else
                     supervision.export(board, args.lead, args.run) if args.operation == "export" else supervision.status(board, args.lead, args.run))
    rendered = supervision.render(value) if args.operation == "status" and not args.json else json.dumps(supervision.redact(value), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0
