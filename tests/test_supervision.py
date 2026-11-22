import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from fusion_swarm import team, supervision
from fusion_swarm.board import HiveBoard, BoardError
from fusion_swarm.mailbox import envelope_fingerprint
from fusion_swarm.scope import file_digest
from fusion_swarm.mcp_server import CoordinationServer


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fusion supervision ")
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.file = self.workspace / "contract.txt"
        self.file.write_text("revision aware contract")
        self.spec = {"run_id": "observed", "task": "Review contract", "workspace": str(self.workspace),
            "roster": [{"id": "lead", "model": "selected", "provider": "native", "role": "lead"}, {"id": "worker", "model": "selected", "provider": "native"}],
            "jobs": [{"id": "review", "goal": "review actual file", "reads": ["contract.txt"], "assigned_to": "worker", "success_criteria": ["contract verified"]}]}
        self.db = self.root / "board.sqlite"
        team.prepare(team.compile_spec(self.spec), str(self.db))
        self.board = HiveBoard(self.db)
        self.board.create_run("lead", "observed", "local", {"host": "local", "source": "local-process", "models": ["selected"], "operations": ["dispatch", "wait"]})

    def tearDown(self):
        self.board.close()
        self.tmp.cleanup()

    def accept(self):
        self.board.start_task("worker", "review")
        self.board.submit_task("worker", {"task_id": "review", "attempt": 1, "status": "complete", "summary": "Reviewed actual file"})
        self.board.review_task("lead", "review", 1, "accept", "Checked file")

    def test_knowledge_requires_acceptance_citations_and_invalidates_source_changes(self):
        kwargs = {"agent_id": "lead", "task_id": "review", "key": "contract", "scope": "api", "content": "Contract requires revision checks", "citations": [{"path": "contract.txt", "revision": file_digest(self.file), "line": 1}]}
        with self.assertRaises(BoardError):
            self.board.knowledge_promote(**kwargs)
        self.accept()
        first = self.board.knowledge_promote(**kwargs)
        self.assertEqual(self.board.knowledge_promote(**kwargs)["id"], first["id"])
        result = self.board.knowledge_search("worker", str(self.workspace), "revision", "api")
        self.assertEqual(result["entries"][0]["id"], first["id"])
        self.assertIn(result["backend"], ("sqlite-fts5", "sqlite-substring"))
        self.file.write_text("changed source")
        result = self.board.knowledge_search("worker", str(self.workspace), "revision")
        self.assertEqual(result["entries"], [])
        self.assertIn("source revision changed", result["validation"]["invalidated"][0]["reason"])
        with self.assertRaises(BoardError):
            self.board.knowledge_promote(**kwargs)

    def test_status_and_once_only_operator_control_survive_restart(self):
        self.board.control_run("lead", "observed", "paused", "inspect contract", 1)
        self.board.control_run("lead", "observed", "paused", "inspect contract", 1)
        view = self.board.supervision("lead", "observed")
        self.assertEqual(len(view["history"]), 1)
        self.assertEqual(view["next_actions"][0]["operation"], "control_run")
        self.assertTrue(view["mail_backlog"])
        self.board.close()
        self.board = HiveBoard(self.db)
        restored = self.board.supervision("lead", "observed")
        self.assertEqual(restored["run"]["version"], view["run"]["version"])
        self.assertEqual(restored["history"], view["history"])
        self.assertIn("paused", supervision.render(restored))
        process = subprocess.run([sys.executable, "-B", str(ROOT / "fusion.py"), "supervise", "status", "--board", str(self.db), "--lead", "lead", "--run", "observed", "--json"], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["run"]["observed_state"], "paused")

    def test_scope_negotiation_fences_stale_briefs_and_is_audited(self):
        digest = envelope_fingerprint(brief=self.board.tasks("review")[0]["brief"])
        changed = self.board.amend_task("lead", "review", digest, {"reads": ["contract.txt"], "constraints": ["preserve revision contract"]}, "Clarified intent")
        self.assertNotEqual(changed["brief_digest"], digest)
        with self.assertRaises(BoardError):
            self.board.amend_task("lead", "review", digest, {"writes": ["different.txt"]}, "stale")
        self.assertTrue(any("amendment" in event["detail"] for event in self.board.task_history("review")))

    def test_mcp_bound_actor_and_exports_do_not_leak_ownership_tokens(self):
        server = CoordinationServer(str(self.db), "worker")
        with self.assertRaises(BoardError):
            server.call("control_run", {"agent_id": "lead", "run_id": "observed", "state": "paused", "note": "unauthorized seat", "expected_version": 1})
        value = supervision.redact({"token": "ownership", "summary": "Bearer abc.def.ghi", "nested": {"api_key": "secret"}})
        self.assertEqual(value["token"], "[redacted]")
        self.assertNotIn("abc.def", value["summary"])
        self.assertEqual(self.board.export_run("lead", "observed")["run"]["provider_usage"], None)

    def test_doctor_distinguishes_installation_and_unexercised_capabilities(self):
        value = supervision.doctor("codex", sys.executable, {"host": "codex", "source": "live-runtime", "models": ["selected"], "operations": ["dispatch"]})
        self.assertEqual(value["capabilities"]["installation"]["state"], "observed")
        self.assertEqual(value["capabilities"]["dispatch"]["state"], "supported-unexercised")
        self.assertEqual(value["capabilities"]["mcp_loading"]["state"], "unverified")
        self.assertEqual(value["provider_calls"], 0)

    def test_expired_run_needs_an_explicit_audited_deadline_renewal(self):
        with patch("fusion_swarm.execution.time.time", return_value=10 ** 12):
            self.assertEqual(self.board.dispatch_actions("lead", "observed"), [])
            run = self.board.run_status("lead", "observed")
            self.assertEqual(run["state"], "needs_input")
            updated = self.board.amend_run_bounds("lead", "observed", run["bounds"], run["version"], "Explicitly extend test run", True)
            self.board.control_run("lead", "observed", "active", "Budget renewed", updated["version"])
            self.assertEqual(len(self.board.dispatch_actions("lead", "observed")), 1)
        self.assertTrue(any(event["action"] == "bounds" for event in self.board.run_history("lead", "observed")))


if __name__ == "__main__":
    unittest.main()
