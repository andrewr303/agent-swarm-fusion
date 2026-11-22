"""Delivery interruption, recovery, privacy and task-attempt behavior."""
import json
from contextlib import closing
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from fusion_swarm.board import BoardError, HiveBoard
from fusion_swarm import team


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fusion protocol ")
        self.root = Path(self.tmp.name)
        self.db = self.root / "board.sqlite"
        self.board = HiveBoard(self.db)
        for name in ("lead", "worker", "blind"):
            self.board.register({"id": name, "role": "worker", "provider": "native", "model": "selected"})

    def tearDown(self):
        self.board.close()
        self.tmp.cleanup()

    def envelope(self, **changes):
        return {"run_id": "run", "trace_id": "trace", "purpose": "question", "summary": "Which contract?",
                "requested_action": "Choose the contract", **changes}

    def send(self, key="question", **changes):
        return self.board.send_message("lead", "worker", self.envelope(**changes), key)

    def lease(self, message_id):
        return next(row for row in self.board.claim("worker", limit=20) if row["id"] == message_id)["delivery"]["lease_token"]

    def test_producer_retry_is_stable_and_conflicting_reuse_is_rejected(self):
        first = self.send()
        self.assertEqual(self.send()["id"], first["id"])
        with self.assertRaises(BoardError):
            self.send(summary="A different question")
        self.assertEqual(self.board.stats()["messages"], 1)

    def test_consumer_commit_survives_restart_and_deduplicates_lost_reply(self):
        message = self.send()
        token = self.lease(message["id"])
        original = self.board.consume_delivery("worker", message["id"], token, "choose", {"contract": "v2"})
        self.assertFalse(original["duplicate"])
        self.board.close()
        self.board = HiveBoard(self.db)
        repeated = self.board.consume_delivery("worker", message["id"], token, "choose", {"contract": "v2"})
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(self.board._conn.execute("SELECT COUNT(*) FROM message_effects").fetchone()[0], 1)
        self.assertEqual(self.board.inbox("worker"), [])
        with self.assertRaises(BoardError):
            self.board.consume_delivery("worker", message["id"], token, "choose", {"contract": "v3"})

    def test_interrupted_delivery_redelivers_and_old_consumer_cannot_commit(self):
        with patch("fusion_swarm.mailbox.time.time", return_value=100):
            message = self.send()
            old = self.board.claim("worker", lease_seconds=10)[0]["delivery"]["lease_token"]
        with patch("fusion_swarm.mailbox.time.time", return_value=111):
            new = self.board.claim("worker", lease_seconds=10)[0]
            self.assertTrue(new["delivery"]["redelivery"])
            with self.assertRaises(BoardError):
                self.board.consume_delivery("worker", message["id"], old, "choose", {})
            self.board.consume_delivery("worker", message["id"], new["delivery"]["lease_token"], "choose", {})

    def test_retry_budget_dead_letters_and_explicit_reopening_are_bounded(self):
        message = self.send(max_attempts=2)
        for expected in ("pending", "dead"):
            token = self.lease(message["id"])
            self.assertEqual(self.board.nack("worker", message["id"], token, "receiver interrupted")["state"], expected)
        self.assertEqual(self.board.claim("worker"), [])
        self.assertEqual(self.board.dead_letters("worker")[0]["id"], message["id"])
        with self.assertRaises(BoardError):
            self.board.ack("worker", message["id"])
        self.board.retry_delivery("worker", message["id"], extra_attempts=1)
        last = self.board.claim("worker")[0]
        self.assertEqual(last["delivery"]["attempts"], 3)
        self.assertEqual(self.board.nack("worker", message["id"], last["delivery"]["lease_token"])["state"], "dead")

    def test_expired_deadline_needs_explicit_future_deadline(self):
        with patch("fusion_swarm.mailbox.time.time", return_value=100):
            message = self.send(deadline=99)
            self.assertEqual(self.board.claim("worker"), [])
            self.assertEqual(len(self.board.dead_letters("worker")), 1)
            with self.assertRaises(BoardError):
                self.board.retry_delivery("worker", message["id"])
            self.board.retry_delivery("worker", message["id"], deadline=200)
            self.assertEqual(self.board.claim("worker")[0]["id"], message["id"])

    def test_progress_coalescing_preserves_decisions_and_history(self):
        decision = self.send("decision", purpose="decision", summary="Use contract v2")
        old = self.send("p1", purpose="progress", summary="Step 1")
        newest = self.send("p2", purpose="progress", summary="Step 2")
        self.assertEqual({m["id"] for m in self.board.inbox("worker")}, {decision["id"], newest["id"]})
        self.assertEqual(self.board.receipts(old["id"])[0]["state"], "superseded")
        self.assertEqual(len(self.board.trace("lead", "run")["messages"]), 3)

    def test_questions_answers_and_blind_seat_visibility(self):
        question = self.send()
        self.assertEqual(self.board.trace("lead", "run")["unanswered_questions"], [question["id"]])
        self.assertEqual(self.board.trace("blind", "run")["messages"], [])
        answer = self.envelope(purpose="answer", reply_to=question["id"], summary="Use v2", requested_action="Review this answer")
        with self.assertRaises(BoardError):
            self.board.send_message("blind", "lead", answer, "blind-answer")
        with self.assertRaises(BoardError):
            self.board.send_message("worker", "lead", {**answer, "trace_id": "another"}, "wrong-trace")
        self.board.send_message("worker", "lead", answer, "answer")
        self.assertEqual(self.board.trace("lead", "run")["unanswered_questions"], [])

    def test_readiness_preserves_model_handle_and_version(self):
        first = self.board.readiness("worker", "run", "ready", "native-1", "selected", 0)
        with self.assertRaises(BoardError):
            self.board.readiness("worker", "run", "started", "native-1", "substitute", first["version"])
        with self.assertRaises(BoardError):
            self.board.readiness("worker", "run", "started", "native-2", "selected", first["version"])
        with self.assertRaises(BoardError):
            self.board.readiness("worker", "run", "started", "native-1", "selected", 0)
        self.board.readiness("worker", "run", "stopped", "native-1", "selected", first["version"])

    def task(self):
        brief = {"id": "fix", "goal": "Repair", "workspace": str(self.root), "assigned_to": "worker",
                 "success_criteria": ["Repair inspected"]}
        self.board.create_task("lead", brief)
        return self.board.start_task("worker", "fix")

    def test_old_task_attempt_cannot_apply_effect_after_recovery(self):
        self.task()
        message = self.send(task_id="fix", attempt=1)
        token = self.lease(message["id"])
        self.board.recover_task("lead", "fix", 1, "Worker stopped; changes reconciled")
        self.board.start_task("worker", "fix")
        with self.assertRaisesRegex(BoardError, "stale"):
            self.board.consume_delivery("worker", message["id"], token, "apply", {})

    def test_recovery_refuses_an_observed_active_native_handle(self):
        self.task()
        self.board.readiness("worker", "run", "started", "native-1", "selected", 0)
        with self.assertRaisesRegex(BoardError, "stop"):
            self.board.recover_task("lead", "fix", 1, "A lease expired")
        self.board.readiness("worker", "run", "stopped", "native-1", "selected", 1)
        self.assertEqual(self.board.recover_task("lead", "fix", 1, "Stopped and reconciled")["state"], "queued")

    def test_additive_schema_retains_real_legacy_rows_and_receipts(self):
        message = self.board.dm("lead", "worker", "Legacy brief")
        self.board.close()
        with closing(sqlite3.connect(self.db)) as old:
            for name in ("message_protocol", "delivery_policies", "delivery_events", "message_effects", "agent_readiness"):
                old.execute(f"DROP TABLE {name}")
        self.board = HiveBoard(self.db)
        legacy = self.board.inbox("worker")[0]
        self.assertEqual((legacy["id"], legacy["body"]), (message["id"], "Legacy brief"))
        token = self.lease(message["id"])
        self.board.ack("worker", message["id"], token)
        self.assertEqual(self.board.receipts(message["id"])[0]["state"], "acked")

    def test_team_briefs_handoffs_and_decisions_use_the_same_durable_trace(self):
        spec = {"run_id": "run", "task": "Repair", "workspace": str(self.root), "roster": [
            {"id": "lead", "provider": "native", "model": "selected", "role": "lead"},
            {"id": "worker", "provider": "native", "model": "selected", "role": "worker"}],
            "jobs": [{"id": "fix", "goal": "Repair", "assigned_to": "worker", "success_criteria": ["Inspected"]}]}
        team.prepare(team.compile_spec(spec), str(self.db))
        assignment = self.board.inbox("worker")[0]
        self.assertEqual(assignment["envelope"]["purpose"], "assignment")
        self.assertIn("brief", assignment["payload"])
        self.board.start_task("worker", "fix")
        self.board.submit_task("worker", {"task_id": "fix", "attempt": 1, "status": "complete", "summary": "Repaired"})
        self.board.review_task("lead", "fix", 1, "escalate", "Needs an operator decision")
        self.board.review_task("lead", "fix", 1, "cancel", "User cancelled")
        self.assertEqual([m["envelope"]["purpose"] for m in self.board.trace("lead", "run")["messages"]],
                         ["assignment", "handoff", "decision", "decision"])

    def test_stdio_process_restart_and_cli_trace_read_persisted_delivery(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "send_message", "arguments": {
            "board_path": str(self.db), "from_agent": "lead", "to_agent": "worker", "envelope": self.envelope(summary="Unicode café"),
            "idempotency_key": "process-question"}}}
        process = subprocess.run([sys.executable, "-B", str(ROOT / "mcps/fusion-coordination/server.py"), "--require-board"],
            input=json.dumps(request) + "\n", capture_output=True, text=True, encoding="utf-8", timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(process.returncode, 0, process.stderr)
        response = json.loads(process.stdout)
        self.assertFalse(response["result"].get("isError", False), response)
        trace = subprocess.run([sys.executable, "-B", str(ROOT / "fusion.py"), "comms", "--db", str(self.db),
            "trace", "--agent", "worker", "--run-id", "run"], capture_output=True, text=True, encoding="utf-8", timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(trace.returncode, 0, trace.stderr)
        self.assertEqual(json.loads(trace.stdout)["messages"][0]["envelope"]["summary"], "Unicode café")


if __name__ == "__main__":
    unittest.main()
