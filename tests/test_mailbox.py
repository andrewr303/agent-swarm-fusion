"""Behavioral regressions for retries, offline recipients and competing consumers."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from fusion_swarm.board import BoardError, HiveBoard


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "board.sqlite")
        self.board = HiveBoard(self.db)
        for agent in ("lead", "worker", "peer"):
            self.board.register({"id": agent, "provider": "native", "model": "selected",
                                 "role": "architect" if agent == "lead" else "worker"})

    def tearDown(self):
        self.board.close()
        self.tmp.cleanup()

    def test_ack_clears_inbox_without_removing_history_or_peer_receipt(self):
        msg = self.board.post(from_agent="lead", body="interface ready")
        self.assertEqual([m["id"] for m in self.board.inbox("worker")], [msg["id"]])
        self.assertIsNotNone(self.board.receipts(msg["id"])[1]["read_at"])
        self.board.ack("worker", msg["id"])
        self.assertEqual(self.board.inbox("worker"), [])
        self.assertEqual(len(self.board.inbox("peer")), 1)
        self.assertEqual(len(self.board.poll("worker")), 1)

    def test_retry_is_one_message_with_independent_recipient_receipts(self):
        for _ in range(3):
            self.board.post(from_agent="lead", body="do work", idempotency_key="brief-1")
        self.assertEqual(self.board.stats()["messages"], 1)
        self.assertEqual(self.board.stats()["deliveries"]["pending"], 2)
        with self.assertRaises(BoardError):
            self.board.post(from_agent="lead", body="different work", idempotency_key="brief-1")

    def test_concurrent_producer_retry_does_not_duplicate(self):
        def send(_):
            with HiveBoard(self.db) as board:
                return board.post(from_agent="lead", body="one", idempotency_key="concurrent")["id"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(send, range(8)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.board.stats()["messages"], 1)

    def test_claim_is_exclusive_and_expiry_fences_the_old_consumer(self):

        self.board.post(from_agent="lead", body="work")
        with patch("fusion_swarm.mailbox.time.time", return_value=100):
            first = self.board.claim("worker", lease_seconds=10)[0]
        with patch("fusion_swarm.mailbox.time.time", return_value=105):
            self.assertEqual(self.board.claim("worker"), [])
        with patch("fusion_swarm.mailbox.time.time", return_value=111):
            second = self.board.claim("worker", lease_seconds=10)[0]
            self.assertEqual(first["id"], second["id"])
            self.assertTrue(second["delivery"]["redelivery"])
            with self.assertRaises(BoardError):
                self.board.ack("worker", first["id"], first["delivery"]["lease_token"])
            self.board.ack("worker", second["id"], second["delivery"]["lease_token"])
        self.assertEqual(self.board.inbox("worker"), [])

    def test_retry_does_not_change_recipients_when_roster_grows(self):
        first = self.board.post(from_agent="lead", body="Future @new-worker can review", idempotency_key="roster-stable")
        self.board.register({"id": "new-worker", "provider": "native", "model": "selected", "role": "worker"})
        second = self.board.post(from_agent="lead", body="Future @new-worker can review", idempotency_key="roster-stable")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.board.inbox("new-worker"), [])

    def test_sentence_punctuation_does_not_swallow_a_registered_mention(self):
        self.board.ensure_channel("isolated", kind="task")
        self.board.join("isolated", "lead")
        message = self.board.post(from_agent="lead", channel="isolated", body="Blocked; need @worker.")
        self.assertEqual(self.board.inbox("worker")[0]["id"], message["id"])
        self.assertEqual(self.board.inbox("peer"), [])

    def test_cli_private_handoff_supports_metadata_multiline_utf8_and_retries(self):
        root = Path(__file__).resolve().parents[1]
        body = Path(self.tmp.name) / "body.txt"
        payload = Path(self.tmp.name) / "payload.json"
        body.write_text("Café fixed\nVerified result", encoding="utf-8")
        payload.write_text(json.dumps({"evidence": "actual output"}), encoding="utf-8")
        command = [sys.executable, "-B", str(root / "fusion.py"), "board", "--db", self.db,
            "dm", "--from", "lead", "--to", "worker", "--body-file", str(body),
            "--kind", "handoff", "--task-id", "fix", "--priority", "urgent",
            "--payload-file", str(payload), "--idempotency-key", "cli-handoff"]
        replies = []
        for _ in range(2):
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=10,
                                    env={**os.environ, "PYTHONIOENCODING": "ascii"})
            self.assertEqual(result.returncode, 0, result.stderr)
            replies.append(json.loads(result.stdout))
        self.assertEqual(replies[0]["id"], replies[1]["id"])
        message = self.board.inbox("worker")[0]
        self.assertEqual(message["body"], body.read_text(encoding="utf-8"))
        self.assertEqual(message["task_id"], "fix")
        self.assertEqual(message["kind"], "handoff")
        self.assertEqual(message["payload"]["evidence"], "actual output")
        self.assertEqual(self.board.inbox("peer"), [])

    def test_competing_connections_claim_once(self):
        self.board.post(from_agent="lead", body="work")
        def claim(_):
            with HiveBoard(self.db) as board:
                return len(board.claim("worker"))
        with ThreadPoolExecutor(max_workers=4) as pool:
            count = sum(pool.map(claim, range(4)))
        self.assertEqual(count, 1)

    def test_nack_retries_with_a_new_token(self):
        msg = self.board.post(from_agent="lead", body="work")
        first = self.board.claim("worker")[0]
        self.board.nack("worker", msg["id"], first["delivery"]["lease_token"])
        second = self.board.claim("worker")[0]
        self.assertNotEqual(first["delivery"]["lease_token"], second["delivery"]["lease_token"])
        self.assertTrue(second["delivery"]["redelivery"])
        with self.assertRaises(BoardError):
            self.board.ack("worker", msg["id"])

    def test_dm_reply_is_private_and_keeps_task_correlation(self):
        msg = self.board.dm("lead", "worker", "brief", task_id="fix")
        reply = self.board.reply("worker", msg["id"], "result")
        self.assertEqual(reply["task_id"], "fix")
        self.assertEqual(reply["to_agent"], "lead")
        self.assertEqual(self.board.poll("peer"), [])
        self.assertEqual(self.board.inbox("peer"), [])
        for action in (lambda: self.board.join(msg["channel"], "peer"),
                       lambda: self.board.reply("peer", msg["id"], "intrusion"),
                       lambda: self.board.ack("peer", msg["id"]),
                       lambda: self.board.post(from_agent="lead", to_agent="worker",
                           channel=msg["channel"], body="@peer private")):
            with self.assertRaises(BoardError):
                action()

    def test_mentions_route_and_priority_precedes_arrival_order(self):
        self.board.ensure_channel("private-work")
        self.board.post(from_agent="lead", channel="private-work", body="@worker finding", priority="low")
        urgent = self.board.post(from_agent="lead", channel="private-work", body="@worker blocked", priority="urgent")
        inbox = self.board.inbox("worker")
        self.assertEqual(inbox[0]["id"], urgent["id"])
        self.assertEqual(self.board.inbox("peer"), [])

    def test_sidecar_failure_does_not_lose_committed_mail(self):
        with patch.object(self.board, "_append_jsonl", side_effect=OSError("disk unavailable")):
            msg = self.board.post(from_agent="lead", body="durable")
        self.assertIn("sidecar_error", msg)
        with HiveBoard(self.db) as reopened:
            self.assertEqual(reopened.inbox("worker")[0]["body"], "durable")

    def test_rolled_back_compound_operation_leaves_no_messages_or_sidecar(self):
        with self.assertRaises(BoardError):
            with self.board._transaction():
                self.board.post(from_agent="lead", body="not committed")
                raise BoardError("reject compound operation")
        self.assertEqual(self.board.stats()["messages"], 0)
        self.assertFalse(Path(self.board.jsonl_path).exists())

    def test_cross_channel_threads_rejected_without_partial_write(self):
        parent = self.board.post(from_agent="lead", body="original")
        with self.assertRaises(BoardError):
            self.board.post(from_agent="worker", channel="workers", thread_id=parent["id"], body="bad root")
        self.assertEqual(self.board.stats()["messages"], 1)

    def test_unknown_recipients_and_non_json_payloads_fail_closed(self):
        for data in ({"to_agent": "absent"}, {"payload": {"x": float("nan")}}, {"priority": "highest"}):
            with self.assertRaises(BoardError):
                self.board.post(from_agent="lead", body="bad", **data)
        self.assertEqual(self.board.stats()["messages"], 0)

    def test_an_independent_cli_process_uses_the_injected_board(self):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "python"),
                   FUSION_BOARD=self.db, PYTHONUTF8="1")
        result = subprocess.run([sys.executable, "-m", "fusion_swarm.board", "post",
                                 "--from", "lead", "--body", "restart-safe"],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        message = json.loads(result.stdout)
        with HiveBoard(self.db) as reopened:
            self.assertEqual(reopened.inbox("worker")[0]["id"], message["id"])


if __name__ == "__main__":
    unittest.main()
