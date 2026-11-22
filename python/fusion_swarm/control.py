"""JSON control API over the same validated MCP methods; no model calls."""
import argparse
import json
import sys
from pathlib import Path

from .mailbox import BoardError
from .mcp_server import CoordinationServer, TOOL_MAP


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=sorted(TOOL_MAP))
    parser.add_argument("--board", required=True)
    parser.add_argument("--input", type=Path, help="JSON arguments file; defaults to stdin")
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.input.read_text(encoding="utf-8-sig") if args.input else sys.stdin.read())
        result = CoordinationServer(str(Path(args.board).resolve())).call(args.operation, data)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (BoardError, ValueError, OSError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
