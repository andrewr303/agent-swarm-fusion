"""PowerShell-friendly entrypoint for the stdlib Fusion runtime."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "python"))

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] in ("board", "comms", "control", "strategies", "supervise"):
        from importlib import import_module
        module = import_module("fusion_swarm." + sys.argv[1])
        raise SystemExit(module.main(sys.argv[2:]))
    else:
        from fusion_swarm.__main__ import main
        raise SystemExit(main())
