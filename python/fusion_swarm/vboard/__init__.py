"""vercel-board-compatible local board engine (Fusion's zero-dependency fallback).

Only the data core is imported here. port, http, cli, mcp and conformance belong to other modules and are
imported lazily by their consumers.
"""
from __future__ import annotations

VERSION = "3.3.0+swarm"          # fusion package version (D1)
PROTOCOL = "vercel-board/0.54.0-compatible"

from .model import VBoardError, Caller  # noqa: E402  (re-export)
from .store import BoardStore  # noqa: E402

__all__ = ["VERSION", "PROTOCOL", "VBoardError", "Caller", "BoardStore"]
