"""Deterministic test worker. Real subprocesses, no model/provider calls."""
import json
import sys
import time
import os
import shlex
import subprocess
from pathlib import Path

packet = json.loads(sys.stdin.read())
time.sleep(packet.get("delay", 0))
if packet.get("fail"):
    print("fixture infrastructure error", file=sys.stderr)
    raise SystemExit(7)
changed = []
for name in packet.get("write", []):
    path = Path(packet["workspace"]) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def answer():\n    return 42\n" if path.suffix == ".py" else "verified fixture artifact\n", encoding="utf-8")
    changed.append(name)
evidence = []
for check in packet.get("checks", []):
    result = subprocess.run(shlex.split(check), cwd=packet["workspace"], capture_output=True, text=True, encoding="utf-8", timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    evidence.append({"command": check, "exit_code": result.returncode, "output": result.stdout + result.stderr})
failed = any(item["exit_code"] != 0 for item in evidence)
print(json.dumps({"model": packet.get("observed_model", packet["model"]), "report": {
    "task_id": packet["task_id"], "attempt": packet["attempt"], "status": "blocked" if failed else "complete",
    "summary": "Deterministic fixture completed", "changed_paths": changed,
    "evidence": evidence, "gaps": ["actual fixture check failed"] if failed else []}}, ensure_ascii=False), flush=True)
