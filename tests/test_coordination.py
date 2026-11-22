"""Task lifecycle, ownership and compare-and-swap behavior across connections."""
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from fusion_swarm.board import BoardError, HiveBoard


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        self.board = HiveBoard(self.root / "board.sqlite")
        for agent in ("lead", "worker", "peer"):
            self.board.register({"id": agent, "provider": "native", "model": "selected", "role": "worker"})
        self.brief = {"id": "fix", "goal": "fix a defect", "workspace": str(self.root), "assigned_to": "worker",
                      "writes": ["src/main.py"], "checks": ["python -m unittest"], "success_criteria": ["regression passes"]}

    def tearDown(self):
        self.board.close()
        self.tmp.cleanup()

    def report(self, **changes):
        return {"task_id": "fix", "attempt": 1, "status": "complete", "summary": "defect fixed",
                "changed_paths": ["src/main.py"], "evidence": [{"command": "python -m unittest", "exit_code": 0, "output": "1 test passed"}],
                "gaps": [], **changes}

    def start(self):
        self.board.create_task("lead", self.brief)
        return self.board.start_task("worker", "fix")

    def test_result_waits_for_lead_review_before_dependencies_start(self):
        self.start()
        self.board.create_task("lead", {**self.brief, "id": "integrate", "assigned_to": "peer",
                                      "dependencies": ["fix"], "writes": ["src/main.py"]})
        self.board.submit_task("worker", self.report())
        with self.assertRaises(BoardError):
            self.board.start_task("peer", "integrate")
        self.board.review_task("lead", "fix", 1, "accept", "Inspected diff and actual verification evidence")
        next_task = self.board.start_task("peer", "integrate")
        self.assertEqual(next_task["state"], "running")
        self.assertEqual(self.board.reservations()[0]["agent_id"], "peer")

    def test_rework_fences_previous_reports(self):
        self.start()
        self.board.submit_task("worker", self.report())
        self.board.review_task("lead", "fix", 1, "revise", "cover the missing edge case")
        self.assertEqual(self.board.start_task("worker", "fix")["attempt"], 2)
        with self.assertRaises(BoardError):
            self.board.submit_task("worker", self.report())
        self.board.submit_task("worker", self.report(attempt=2))
        with self.assertRaises(BoardError):
            self.board.review_task("lead", "fix", 1, "accept", "stale review")
        self.board.review_task("lead", "fix", 2, "accept", "Reviewed new attempt")
        self.assertEqual([e["state"] for e in self.board.task_history("fix")],
                         ["queued", "running", "review", "queued", "running", "review", "accepted"])

    def test_complete_rejects_missing_failed_or_duplicate_check_evidence(self):

        self.start()
        for report in (self.report(evidence=[]), self.report(gaps=["not verified"]),
                       self.report(evidence=[{"command": "python -m unittest", "exit_code": 1, "output": "failed"}]),
                       self.report(evidence=self.report()["evidence"] * 2)):
            with self.assertRaises(BoardError):
                self.board.submit_task("worker", report)
        self.assertEqual(self.board.tasks("fix")[0]["state"], "running")

    def test_recovery_releases_scopes_and_fences_stopped_attempt(self):
        self.start()
        for agent, attempt in (("worker", 1), ("lead", 2)):
            with self.assertRaises(BoardError):
                self.board.recover_task(agent, "fix", attempt, "worker stopped; changes inspected")
        self.board.recover_task("lead", "fix", 1, "worker stopped; changes inspected")
        self.assertEqual(self.board.reservations(), [])
        with self.assertRaises(BoardError):
            self.board.submit_task("worker", self.report())
        self.assertEqual(self.board.start_task("worker", "fix")["attempt"], 2)
        with self.assertRaises(BoardError):
            self.board.submit_task("worker", self.report())
        self.board.submit_task("worker", self.report(attempt=2))

    def test_lost_report_or_review_reply_can_be_retried_without_duplicate_mail(self):
        self.start()
        first = self.board.submit_task("worker", self.report())
        message_count = self.board.stats()["messages"]
        self.assertEqual(self.board.submit_task("worker", self.report()), first)
        self.assertEqual(self.board.stats()["messages"], message_count)
        decision = self.board.review_task("lead", "fix", 1, "accept", "Inspected actual result")
        event_count = len(self.board.task_history("fix"))
        message_count = self.board.stats()["messages"]
        self.assertEqual(self.board.review_task("lead", "fix", 1, "accept", "Inspected actual result"), decision)
        self.assertEqual(len(self.board.task_history("fix")), event_count)
        self.assertEqual(self.board.stats()["messages"], message_count)
        with self.assertRaises(BoardError):
            self.board.submit_task("worker", self.report(summary="different report"))

    def test_out_of_scope_report_and_outside_workspace_are_rejected(self):
        self.start()
        for path in ("src/other.py", "../outside.py"):
            with self.assertRaises(BoardError):
                self.board.submit_task("worker", self.report(changed_paths=[path]))
        with self.assertRaises(BoardError):
            self.board.reserve("peer", str(self.root), ["../outside.py"])

    def test_relative_workspace_is_persisted_absolute_for_another_process_cwd(self):
        previous = Path.cwd()
        other = self.root / "other"
        other.mkdir()
        try:
            os.chdir(self.root)
            item = self.board.create_task("lead", {**self.brief, "workspace": "."})
            self.assertEqual(item["brief"]["workspace"], str(self.root.resolve()))
            os.chdir(other)
            self.board.start_task("worker", "fix")
            self.assertEqual(Path(self.board.reservations()[0]["path"]), self.root.resolve() / "src" / "main.py")
        finally:
            os.chdir(previous)

    def test_blocker_notifies_the_lead_privately_and_cannot_be_accepted(self):
        self.start()
        self.board.submit_task("worker", self.report(status="escalate", evidence=[], changed_paths=[],
            decision_request="Which public interface must be retained?"))
        self.assertEqual(self.board.inbox("peer"), [])
        handoff = self.board.inbox("lead")[0]
        self.assertEqual(handoff["priority"], "urgent")
        self.assertEqual(handoff["payload"]["report"]["status"], "escalate")
        with self.assertRaises(BoardError):
            self.board.review_task("lead", "fix", 1, "accept", "looks fine")

    def test_worker_cannot_self_accept_or_report_for_another_worker(self):
        self.start()
        with self.assertRaises(BoardError):
            self.board.submit_task("peer", self.report())
        self.board.submit_task("worker", self.report())
        with self.assertRaises(BoardError):
            self.board.review_task("worker", "fix", 1, "accept", "self accepted")

    def test_retrying_a_brief_preserves_running_task_and_delivers_once(self):
        self.start()
        again = self.board.create_task("lead", self.brief)
        self.assertEqual(again["state"], "running")
        self.assertEqual(len(self.board.inbox("worker")), 1)
        with self.assertRaises(BoardError):
            self.board.create_task("lead", {**self.brief, "goal": "different scope"})

    def test_parent_directory_reservation_blocks_children_and_has_one_owner(self):
        self.board.reserve("worker", str(self.root), ["src"])
        with self.assertRaises(BoardError):
            self.board.reserve("peer", str(self.root), ["src/main.py"])
        with self.assertRaises(BoardError):
            self.board.release("peer", str(self.root), ["src"])
        self.board.release("worker", str(self.root), ["src"])
        self.board.reserve("peer", str(self.root), ["src/main.py"])

    def test_expired_writer_cannot_claim_success(self):
        self.board.create_task("lead", self.brief)
        with patch("fusion_swarm.coordination.time.time", return_value=100):
            self.board.start_task("worker", "fix", lease_seconds=10)
        with patch("fusion_swarm.coordination.time.time", return_value=111):
            with self.assertRaises(BoardError):
                self.board.submit_task("worker", self.report())
            self.board.reserve("worker", str(self.root), ["src/main.py"], task_id="fix")
            self.board.submit_task("worker", self.report())

    def test_context_conflicts_do_not_overwrite_another_agents_decision(self):
        self.board.context_set("lead", "run-1", "contract", {"version": "v1"}, 0)
        with HiveBoard(self.board.path) as second:
            with self.assertRaises(BoardError):
                second.context_set("peer", "run-1", "contract", {"version": "stale"}, 0)
            second.context_set("peer", "run-1", "contract", {"version": "v2"}, 1)
        self.assertEqual(self.board.context_get("run-1", "contract")["value"], {"version": "v2"})
        self.assertFalse(self.board.context_get("other-run", "contract")["found"])

    def test_simultaneous_file_reservations_have_one_winner(self):
        def reserve(agent):
            with HiveBoard(self.board.path) as connection:
                try:
                    connection.reserve(agent, str(self.root), ["src/main.py"])
                    return True
                except BoardError:
                    return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(reserve, ("worker", "peer"))), 1)

    def test_unknown_dependency_does_not_persist_an_unstartable_task(self):
        with self.assertRaises(BoardError):
            self.board.create_task("lead", {**self.brief, "dependencies": ["missing"]})
        self.assertEqual(self.board.tasks(), [])
        self.assertEqual(self.board.stats()["messages"], 0)

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            try:
                os.symlink(outside, self.root / "link", target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with self.assertRaises(BoardError):
                self.board.reserve("worker", str(self.root), ["link/secret.py"])


if __name__ == "__main__":
    unittest.main()
