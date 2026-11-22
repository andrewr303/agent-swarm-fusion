"""Execution ownership, dependencies and scope with actual offline workers."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from fusion_swarm import team
from fusion_swarm.board import HiveBoard, BoardError
from fusion_swarm.controller import ExecutionController
from fusion_swarm.host_runtime import CodexRuntime
from fusion_swarm.process_runtime import ProcessRuntime
from fusion_swarm.scope import snapshot, inspect_changes, provision_directory, integrate_directory


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fusion execution ")
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.spec = {"run_id": "run", "task": "Produce and verify dependent artifacts", "workspace": str(self.workspace), "form": "pipeline",
            "roster": [{"id": "lead", "model": "lead-fixture", "role": "lead", "provider": "native"},
                       {"id": "one", "model": "worker-fixture", "provider": "native"}, {"id": "two", "model": "worker-fixture", "provider": "native"}],
            "jobs": [{"id": "build", "goal": "produce code", "assigned_to": "one", "writes": ["a.py"], "checks": [sys.executable.replace('\\', '/') + ' -B -c "import a; assert a.answer() == 42; print(42)"'], "success_criteria": ["artifact returns expected value"]},
                     {"id": "verify", "goal": "verify accepted code", "assigned_to": "two", "writes": ["b.py"], "checks": [sys.executable.replace('\\', '/') + ' -B -c "import a, b; assert a.answer() == b.answer(); print(42)"'], "success_criteria": ["accepted code verified"]}]}
        team.prepare(team.compile_spec(self.spec), str(self.root / "board.sqlite"))
        self.board = HiveBoard(self.root / "board.sqlite")
        self.runtime = ProcessRuntime(str(self.root / "runtime"), models=["lead-fixture", "worker-fixture"], command=self.command)
        self.controller = ExecutionController(self.board, self.runtime, agent_id="lead", run_id="run", owner="fixture-controller")
        self.controller.start()

    def command(self, packet):
        return {"argv": [sys.executable, "-B", str(ROOT / "tests/fixtures/runtime_worker.py")], "stdin": json.dumps({**packet, "write": packet["writes"]})}

    def tearDown(self):
        for owner in (self.root / "runtime").glob("*/owner.json"):
            self.runtime.cancel("process:" + owner.parent.name)
        self.board.close()
        self.tmp.cleanup()

    def test_dependent_coding_workflow_restarts_and_requires_lead_acceptance(self):
        first = self.controller.dispatch_ready()[0]
        self.assertEqual(self.board.tasks("verify")[0]["state"], "queued")
        self.board.close()
        self.board = HiveBoard(self.root / "board.sqlite")
        controller = ExecutionController(self.board, self.runtime, agent_id="lead", run_id="run", owner="fixture-controller")
        self.assertEqual(controller.dispatch_ready(), [])
        controller.observe(first["ticket_id"])
        self.assertEqual(self.board.run_status("lead", "run")["observed_state"], "awaiting_review")
        self.assertEqual(controller.dispatch_ready(), [])
        self.board.review_task("lead", "build", 1, "accept", "Inspected fixture artifact")
        second = controller.dispatch_ready()[0]
        controller.observe(second["ticket_id"])
        self.board.review_task("lead", "verify", 1, "accept", "Inspected verification artifact")
        status = self.board.run_status("lead", "run")
        self.assertEqual(status["observed_state"], "completed")
        self.assertEqual(first["native_handle"], status["runtimes"][0]["native_handle"])
        self.assertTrue(all(item["observed_model"] == "worker-fixture" for item in status["runtimes"]))
        self.assertIsNone(status["provider_usage"])
        self.assertEqual(self.board.tasks("verify")[0]["report"]["evidence"][0]["output"].strip(), "42")

    def test_dispatch_claim_and_lost_reply_never_launch_duplicate(self):
        actions = self.board.dispatch_actions("lead", "run")
        self.assertEqual(self.board.dispatch_actions("lead", "run"), actions)
        packet = actions[0]
        claimed = self.board.claim_dispatch("lead", packet["ticket_id"], "owner")
        self.assertFalse(self.board.claim_dispatch("lead", packet["ticket_id"], "owner")["new_claim"])
        with self.assertRaises(BoardError):
            self.board.claim_dispatch("lead", packet["ticket_id"], "other")
        self.board.dispatch_uncertain("lead", packet["ticket_id"], claimed["claim_token"], "Lost native reply")
        self.assertEqual(self.board.run_status("lead", "run")["observed_state"], "needs_input")
        self.assertEqual(self.board.dispatch_actions("lead", "run"), [])
        with self.assertRaises(BoardError):
            self.board.reconcile_runtime("lead", packet["ticket_id"], "original-handle", False, "still working")
        self.board.reconcile_runtime("lead", packet["ticket_id"], "not-dispatched:" + packet["ticket_id"], True, "Verified no dispatch occurred and inspected workspace")
        self.assertEqual(self.board.dispatch_actions("lead", "run")[0]["attempt"], 2)

    def test_pause_compare_and_swap_and_missing_capabilities_remain_visible(self):
        self.board.control_run("lead", "run", "paused", "inspect before starting", 1)
        self.assertEqual(self.controller.dispatch_ready(), [])
        with self.assertRaises(BoardError):
            self.board.control_run("lead", "run", "active", "stale", 1)
        self.board.control_run("lead", "run", "active", "ready", 2)
        self.assertEqual(len(self.controller.dispatch_ready()), 1)
        caps = {"host": "local", "source": "local-process", "models": ["lead-fixture"], "operations": ["dispatch"]}
        status = self.board.refresh_capabilities("lead", "run", caps, 3)
        self.assertEqual(status["state"], "blocked")
        with self.assertRaises(BoardError):
            self.board.control_run("lead", "run", "active", "cannot ignore missing model", 4)
        self.board.refresh_capabilities("lead", "run", self.runtime.capabilities(), 4)

    def test_old_attempt_and_model_substitution_cannot_be_accepted(self):
        packet = self.board.dispatch_actions("lead", "run")[0]
        claim = self.board.claim_dispatch("lead", packet["ticket_id"], "owner")
        with self.assertRaises(BoardError):
            self.board.observe_runtime("lead", packet["ticket_id"], claim["claim_token"], 1, "handle", "other-model", "dispatched", "dispatch")
        self.board.reconcile_runtime("lead", packet["ticket_id"], "not-dispatched:" + packet["ticket_id"], True, "not launched")
        self.board.dispatch_actions("lead", "run")
        with self.assertRaises(BoardError):
            self.board.observe_runtime("lead", packet["ticket_id"], claim["claim_token"], 2, "handle", "worker-fixture", "stopped", "late")

    def test_out_of_scope_untracked_and_ignored_changes_fail_observed_check(self):
        first = self.controller.dispatch_ready()[0]
        (self.workspace / ".gitignore").write_text("ignored.log\n")
        (self.workspace / "ignored.log").write_text("out of scope")
        with self.assertRaisesRegex(BoardError, "out-of-scope"):
            self.controller.observe(first["ticket_id"])
        self.assertEqual(self.board.tasks("build")[0]["state"], "running")

    def test_bounded_infrastructure_failures_stop_on_same_model(self):
        with self.board._transaction():
            self.board._conn.execute("UPDATE swarm_runs SET bounds=? WHERE id='run'", (json.dumps({"max_concurrency": 2, "max_retries": 5, "max_dispatches": 10, "timeout_seconds": 900, "auto_retry": True}),))
        for attempt in range(1, 4):
            packet = self.board.dispatch_actions("lead", "run")[0]
            claim = self.board.claim_dispatch("lead", packet["ticket_id"], "owner")
            self.assertEqual(packet["model"], "worker-fixture")
            self.board.observe_runtime("lead", packet["ticket_id"], claim["claim_token"], 1, "handle-" + str(attempt), "", "failed", "failure", {"error": "host unavailable"}, "infrastructure")
        self.assertEqual(self.board.run_status("lead", "run")["state"], "needs_input")
        self.assertEqual(self.board.dispatch_actions("lead", "run"), [])

    def test_provisioned_baseline_and_integration_preserve_other_actor_changes(self):
        (self.workspace / "deps").mkdir()
        (self.workspace / "deps/preexisting.txt").write_text("provisioned")
        provision = provision_directory(str(self.workspace), str(self.root / "isolated"), authorized=True)
        worker = Path(provision["workspace"])
        (worker / "a.txt").write_text("candidate")
        checked = inspect_changes(provision["provisioned_baseline"], ["a.txt"])
        self.assertEqual(checked["changed_paths"], ["a.txt"])
        (self.workspace / "b.txt").write_text("other actor")
        integrate_directory(provision, ["a.txt"], accepted=True)
        self.assertEqual((self.workspace / "b.txt").read_text(), "other actor")
        (worker / "b.txt").write_text("conflict")
        with self.assertRaisesRegex(BoardError, "conflicts"):
            integrate_directory(provision, ["b.txt"], accepted=True)

    def test_usage_only_reports_observed_units_and_survives_later_events(self):
        packet = self.board.dispatch_actions("lead", "run")[0]
        claim = self.board.claim_dispatch("lead", packet["ticket_id"], "owner")
        observed = self.board.observe_runtime("lead", packet["ticket_id"], claim["claim_token"], 1, "handle", "worker-fixture", "working", "usage",
            {"usage": {"scope": "attempt-total", "input_tokens": 10, "output_tokens": 4}})
        self.board.observe_runtime("lead", packet["ticket_id"], claim["claim_token"], observed["version"], "handle", "worker-fixture", "working", "later")
        usage = self.board.run_status("lead", "run")["provider_usage"]
        self.assertEqual(usage["known_totals"]["input_tokens"], 10)
        self.assertIsNone(usage["known_totals"]["cost_usd"])

    def test_direct_worker_submission_cannot_bypass_runtime_scope_acceptance(self):
        packet = self.board.dispatch_actions("lead", "run")[0]
        claim = self.board.claim_dispatch("lead", packet["ticket_id"], "owner")
        launched = self.runtime.dispatch(packet)
        result = self.runtime.wait(launched["native_handle"], 10000)
        self.board.observe_runtime("lead", packet["ticket_id"], claim["claim_token"], 1, launched["native_handle"], result["model"], "stopped", "completion", result)
        self.board.submit_task("one", result["report"])
        with self.assertRaisesRegex(BoardError, "scope validation"):
            self.board.review_task("lead", "build", 1, "accept", "Scope was not checked yet")
        self.board.submit_runtime_result("lead", packet["ticket_id"], result["report"])
        self.board.review_task("lead", "build", 1, "accept", "Actual scope and artifact checked")


if __name__ == "__main__":
    unittest.main()
