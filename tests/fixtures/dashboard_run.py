"""Create an isolated actual Fusion run for browser acceptance; no providers."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))
from fusion_swarm import team
from fusion_swarm.board import HiveBoard
from fusion_swarm.controller import ExecutionController
from fusion_swarm.process_runtime import ProcessRuntime

parser = argparse.ArgumentParser()
parser.add_argument("destination", type=Path)
args = parser.parse_args()
root = args.destination.resolve()
root.mkdir(parents=True, exist_ok=False)
workspace = root / "workspace"
workspace.mkdir()
spec = {"run_id": "acceptance-swarm", "task": "Build and review a local fixture artifact", "workspace": str(workspace), "form": "pipeline",
    "roster": [{"id": "lead", "model": "fixture", "provider": "native", "role": "lead"}, {"id": "worker", "model": "fixture", "provider": "native"}],
    "jobs": [{"id": "build", "goal": "Produce actual local artifact", "assigned_to": "worker", "writes": ["artifact.txt"], "success_criteria": ["Artifact exists"]},
             {"id": "verify", "goal": "Verify accepted artifact", "assigned_to": "worker", "reads": ["artifact.txt"], "success_criteria": ["Accepted dependency is verified"]}]}
db = root / "board.sqlite"
team.prepare(team.compile_spec(spec), str(db))
runtime = ProcessRuntime(str(root / "runtime"), models=["fixture"], command=lambda packet: {
    "argv": [sys.executable, "-B", str(ROOT / "tests/fixtures/runtime_worker.py")], "stdin": json.dumps({**packet, "write": packet["writes"]})})
with HiveBoard(db) as board:
    controller = ExecutionController(board, runtime, agent_id="lead", run_id=spec["run_id"], owner="browser-acceptance-fixture")
    controller.start()
    ticket = controller.dispatch_ready()[0]
    controller.observe(ticket["ticket_id"])
print(json.dumps({"board": str(db), "run": spec["run_id"], "lead": "lead", "fixture": "actual local process, no model invocation"}))
