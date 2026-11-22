"""Bundled launcher, independent of the caller's working directory."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from fusion_swarm.vboard.mcp import main

if __name__ == "__main__":
    raise SystemExit(main())
