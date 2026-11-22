"""Regression tests for f1 bugs B2, B5 (protocol side), B6, B9a, B9e, B10, B12, B16 (mailbox/protocol/knowledge)."""
import itertools
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from fusion_swarm import knowledge, protocol, team
from fusion_swarm.board import BoardError, HiveBoard
from fusion_swarm.scope import file_digest

CAPS = {"host": "local", "source": "local-process", "models": ["m"], "operations": ["dispatch"]}


def envelope(**changes):
    return {"run_id": "r1", "trace_id": "t1", "purpose": "handoff", "summary": "s",
            "requested_action": "review", **changes}


class MailFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fusion f1 mail ")
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.db = str(self.root / "board.sqlite")
        self.board = HiveBoard(self.db)
        for name, role in (("lead", "worker"), ("worker", "worker"), ("peer", "worker"), ("outsider", "worker"),
                           ("arch", "architect")):
            self.board.register({"id": name, "role": role, "provider": "native", "model": "m"})
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("FUSION_MAIL_MAX_ATTEMPTS", None)

    def tearDown(self):
        self.board.close()
        self.tmp.cleanup()

    def brief(self, task_id="fix", assigned="worker", **extra):
        return {"id": task_id, "goal": "Repair", "workspace": str(self.ws), "assigned_to": assigned,
                "success_criteria": ["Repair inspected"], **extra}

    # B2
    def test_b02_lead_consumes_handoff_after_accept(self):
        self.board.create_task("lead", self.brief())
        self.board.start_task("worker", "fix")
        message = self.board.send_message("worker", "lead", envelope(task_id="fix", attempt=1), "h1")
        self.board.submit_task("worker", {"task_id": "fix", "attempt": 1, "status": "complete", "summary": "done"})
        held = next(row for row in self.board.claim("lead", limit=10) if row["id"] == message["id"])
        self.board.review_task("lead", "fix", 1, "accept", "ok")
        self.assertEqual(self.board.tasks("fix")[0]["state"], "accepted")
        token = held["delivery"]["lease_token"]
        result = self.board.consume_delivery("lead", message["id"], token, "eff1", {"ok": True})
        self.assertFalse(result["duplicate"])
        self.assertTrue(self.board.consume_delivery("lead", message["id"], token, "eff1", {"ok": True})["duplicate"])
        self.assertEqual(self.board.receipts(message["id"], "lead")[0]["state"], "acked")

    def test_b02_guard_stays_on_send_and_on_stale_attempts(self):
        self.board.create_task("lead", self.brief())
        self.board.start_task("worker", "fix")
        self.board.submit_task("worker", {"task_id": "fix", "attempt": 1, "status": "complete", "summary": "done"})
        self.board.review_task("lead", "fix", 1, "accept", "ok")
        with self.assertRaisesRegex(BoardError, "stale or closed"):
            self.board.send_message("worker", "lead", envelope(task_id="fix", attempt=1), "late")

    # B6
    def test_b06_plain_mail_dead_letters_after_max_attempts(self):
        self.board.dm("worker", "lead", "poison")
        clock = itertools.count(1000, 1)
        attempts = []
        with patch("fusion_swarm.mailbox.time.time", side_effect=lambda: float(next(clock))):
            for _ in range(12):
                got = self.board.claim("lead", limit=1, lease_seconds=0.02)
                if got:
                    attempts.append(got[0]["delivery"]["attempts"])
            dead = self.board.dead_letters("lead")
        self.assertEqual(attempts, list(range(1, 9)))
        self.assertEqual(len(dead), 1)
        reasons = [r[0] for r in self.board._conn.execute("SELECT reason FROM delivery_events WHERE state='dead'")]
        self.assertEqual(reasons, ["max_attempts"])

    def test_b06_budget_follows_environment(self):
        self.board.dm("worker", "lead", "poison")
        clock = itertools.count(1000, 1)
        with patch.dict(os.environ, {"FUSION_MAIL_MAX_ATTEMPTS": "2"}), \
                patch("fusion_swarm.mailbox.time.time", side_effect=lambda: float(next(clock))):
            claims = [self.board.claim("lead", lease_seconds=0.02) for _ in range(5)]
            self.assertEqual([bool(c) for c in claims], [True, True, False, False, False])
            self.assertEqual(len(self.board.dead_letters("lead")), 1)

    def test_b06_nack_of_plain_mail_dead_letters_at_budget(self):
        self.board.dm("worker", "lead", "poison")
        with patch.dict(os.environ, {"FUSION_MAIL_MAX_ATTEMPTS": "2"}):
            first = self.board.claim("lead")[0]
            self.assertEqual(self.board.nack("lead", first["id"], first["delivery"]["lease_token"])["state"], "pending")
            second = self.board.claim("lead")[0]
            self.assertEqual(self.board.nack("lead", second["id"], second["delivery"]["lease_token"])["state"], "dead")

    def test_b06_explicit_policy_still_wins(self):
        message = self.board.send_message("worker", "lead", envelope(purpose="question", max_attempts=1), "q")
        self.board.claim("lead", lease_seconds=0.001)
        with patch("fusion_swarm.mailbox.time.time", return_value=9e9):
            self.assertEqual([m["id"] for m in self.board.dead_letters("lead")], [message["id"]])

    # B9a
    def test_b09_receipts_redacts_other_agents_tokens(self):
        message = self.board.post(from_agent="lead", body="all hands", channel="hive", mentions=["worker", "peer"])
        for name in ("worker", "peer"):
            self.board.claim(name)
        own = {r["agent_id"]: r["lease_token"] for r in self.board._conn.execute(
            "SELECT agent_id,lease_token FROM deliveries WHERE message_id=?", (message["id"],))}
        self.assertTrue(own["worker"] and own["peer"])
        for caller, rows in (("worker", self.board.receipts(message["id"], "worker")),
                             (None, self.board.receipts(message["id"])),
                             ("lead", self.board.receipts(message["id"], "lead"))):
            for row in rows:
                if row["agent_id"] == caller:
                    self.assertEqual(row["lease_token"], own[caller])
                else:
                    self.assertNotIn("lease_token", row)
            self.assertNotIn(own["peer"] if caller != "peer" else "", json.dumps(rows) if caller != "peer" else "x")
        with self.assertRaises(BoardError):
            self.board.receipts(message["id"], "ghost")

    def test_b09_trace_hides_recipient_tokens_from_sender(self):
        sent = self.board.send_message("lead", "worker", envelope(purpose="assignment"), "a1")
        token = self.board.claim("worker")[0]["delivery"]["lease_token"]
        rows = self.board.trace("lead", "r1")["messages"][0]["receipts"]
        self.assertEqual(rows[0]["message_id"], sent["id"])
        self.assertNotIn(token, json.dumps(rows))
        mine = self.board.trace("worker", "r1")["messages"][0]["receipts"]
        self.assertEqual(mine[0]["lease_token"], token)

    # B9e
    def test_b09e_knowledge_scoped_to_workspace_participants(self):
        self.board.create_task("lead", self.brief())
        for call in (lambda a: self.board.knowledge_search(a, str(self.ws), "anything"),
                     lambda a: self.board.knowledge_validate(a, str(self.ws))):
            with self.assertRaises(BoardError):
                call("outsider")
            with self.assertRaises(BoardError):
                call("ghost")
            call("worker")   # assignee
            call("lead")     # creator
            call("arch")     # privileged role
        other = self.root / "other"
        other.mkdir()
        with self.assertRaises(BoardError):
            self.board.knowledge_search("worker", str(other), "anything")
        self.board.knowledge_search("arch", str(other), "anything")

    def test_b09e_reservation_makes_a_participant(self):
        with self.assertRaises(BoardError):
            self.board.knowledge_search("peer", str(self.ws), "x")
        self.board.reserve("peer", str(self.ws), ["a.py"])
        self.board.knowledge_search("peer", str(self.ws), "x")

    # B10
    def test_b10_sender_cannot_ack_own_post(self):
        message = self.board.post(from_agent="lead", body="hello", channel="hive", mentions=["worker"])
        with self.assertRaisesRegex(BoardError, "sender cannot acknowledge"):
            self.board.ack("lead", message["id"])
        self.assertEqual(self.board._conn.execute("SELECT COUNT(*) FROM acks").fetchone()[0], 0)
        self.board.ack("worker", message["id"])
        dm = self.board.dm("lead", "worker", "x")
        with self.assertRaises(BoardError):
            self.board.ack("lead", dm["id"])

    def test_b10_visible_history_ack_without_delivery_still_allowed(self):
        message = self.board.post(from_agent="lead", body="history", channel="hive")
        self.board.register({"id": "late", "role": "worker", "provider": "native", "model": "m"})
        self.board.join("hive", "late")
        self.assertEqual(self.board.ack("late", message["id"])["message_id"], message["id"])

    # B12
    def _seed_leased(self, count):
        with self.board._transaction():
            self.board._conn.execute("INSERT INTO messages(channel,from_agent,to_agent,body,kind,mentions,created_at) "
                                     "SELECT 'hive','lead','worker','x','message','[]','now' FROM (WITH RECURSIVE n(i) AS "
                                     "(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<%d) SELECT i FROM n)" % count)
            self.board._conn.execute("INSERT INTO deliveries(message_id,agent_id,state,attempts,lease_token,lease_until) "
                                     "SELECT id,'worker','leased',1,'t'||id,4e9 FROM messages")
            self.board._conn.execute("INSERT INTO delivery_policies SELECT id,'worker',3,NULL FROM messages")

    def test_b12_expiry_query_is_filtered(self):
        self._seed_leased(1000)
        conn = self.board._conn
        plan = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + protocol.EXPIRY_SQL,
                                                   (8, 1.0, 1.0, 1.0, 1.0, 8)))
        self.assertIn("delivery_policies", plan)
        self.assertRegex(plan, r"USING (COVERING )?INDEX (delivery_policies_|sqlite_autoindex_delivery_policies)")
        self.assertIn("deliveries_lease", plan)
        self.assertNotRegex(plan, r"SCAN d\b|SCAN p\b|SCAN deliveries\b")
        with self.board._transaction():
            self.board._expire_protocol_deliveries_unlocked(1.0)  # one-time index check happens here
        loaded = []
        original = conn.row_factory
        conn.row_factory = lambda cursor, row: (loaded.append(1), original(cursor, row))[1]
        try:
            with self.board._transaction():
                self.board._expire_protocol_deliveries_unlocked(1.0)
        finally:
            conn.row_factory = original
        self.assertEqual(sum(loaded), 0)
        # An expired lease past its budget and a passed deadline are still found.
        with self.board._transaction():
            conn.execute("UPDATE deliveries SET lease_until=0.5,attempts=3 WHERE message_id=1")
            conn.execute("UPDATE delivery_policies SET deadline=0.5 WHERE message_id=2")
            self.board._expire_protocol_deliveries_unlocked(1.0)
        states = {r[0]: r[1] for r in conn.execute("SELECT message_id,state FROM deliveries WHERE message_id<=3")}
        self.assertEqual(states, {1: "dead", 2: "dead", 3: "leased"})
        reasons = {r[0]: r[1] for r in conn.execute("SELECT message_id,reason FROM delivery_events")}
        self.assertEqual(reasons, {1: "max_attempts", 2: "deadline expired"})

    def test_b12_legacy_database_gains_the_indexes(self):
        self.board.close()
        raw = sqlite3.connect(self.db)
        for name in ("deliveries_lease", "delivery_policies_msg", "delivery_policies_deadline"):
            raw.execute(f"DROP INDEX IF EXISTS {name}")
        raw.commit()
        raw.close()
        self.board = HiveBoard(self.db)
        self.board.stats()
        names = {r[0] for r in self.board._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"deliveries_lease", "delivery_policies_msg", "delivery_policies_deadline"} <= names)

    def test_b12_knowledge_file_digests_are_cached_by_stat(self):
        source = self.ws / "contract.txt"
        source.write_text("revision aware contract")
        self.board.create_task("lead", self.brief("kn", reads=["contract.txt"]))
        self.board.start_task("worker", "kn")
        self.board.submit_task("worker", {"task_id": "kn", "attempt": 1, "status": "complete", "summary": "done"})
        self.board.review_task("lead", "kn", 1, "accept", "ok")
        self.board.knowledge_promote("lead", "kn", "contract", "api", "Contract requires revision checks",
                                     [{"path": "contract.txt", "revision": file_digest(source), "line": 1}])
        calls = []
        real = knowledge.file_digest
        with patch.object(knowledge, "file_digest", side_effect=lambda p: (calls.append(p), real(p))[1]):
            for _ in range(3):
                found = self.board.knowledge_search("worker", str(self.ws), "revision")
            self.assertEqual(len(found["entries"]), 1)
            self.assertEqual(len(calls), 1)
            source.write_text("changed source!!")
            result = self.board.knowledge_search("worker", str(self.ws), "revision")
        self.assertEqual(result["entries"], [])
        self.assertEqual(len(calls), 2)

    # B16
    def test_b16_trace_limit_in_sql(self):
        ids = [self.board.send_message("lead", "worker", envelope(purpose="progress", requested_action="",
                                                                  summary=f"s{i}", progress_key=f"k{i}"), f"p{i}")["id"]
               for i in range(7)]
        statements = []
        self.board._conn.set_trace_callback(statements.append)
        try:
            view = self.board.trace("lead", "r1", limit=3)
        finally:
            self.board._conn.set_trace_callback(None)
        self.assertEqual([m["id"] for m in view["messages"]], ids[-3:])
        self.assertTrue(view["truncated"])
        selects = [s for s in statements if "message_protocol" in s and "FROM messages" in s]
        self.assertTrue(selects and all("LIMIT 4" in s for s in selects), selects)
        full = self.board.trace("lead", "r1", limit=7)
        self.assertEqual((len(full["messages"]), full["truncated"]), (7, False))

    def test_b16_trace_pages_past_invisible_messages(self):
        mine = self.board.send_message("lead", "worker", envelope(purpose="question", summary="mine"), "mine")["id"]
        for i in range(6):
            self.board.send_message("peer", "outsider", envelope(purpose="progress", requested_action="",
                                                                 progress_key=f"k{i}"), f"noise{i}")
        view = self.board.trace("lead", "r1", limit=2)
        self.assertEqual([m["id"] for m in view["messages"]], [mine])
        self.assertFalse(view["truncated"])
        self.assertEqual(view["unanswered_questions"], [mine])

    # B5 (protocol side)
    def test_b05_send_message_second_run(self):
        def spec(run_id, prefix):
            return {"run_id": run_id, "task": "t", "workspace": str(self.ws), "form": "sidekick",
                    "roster": [{"id": "boss", "provider": "native", "model": "lead-m", "role": "lead"},
                               {"id": prefix + "w", "provider": "native", "model": "worker-m"}],
                    "jobs": [{"id": prefix + "job", "goal": "g " + prefix, "assigned_to": prefix + "w",
                              "writes": [prefix + ".py"], "success_criteria": ["done"]}]}
        for run, prefix in (("run-a", "a"), ("run-b", "b")):
            team.prepare(team.compile_spec(spec(run, prefix)), self.db, allow_multi_run=True)
        for run, prefix in (("run-a", "a"), ("run-b", "b")):
            sent = self.board.send_message("boss", prefix + "w", envelope(run_id=run, purpose="assignment"), "k-" + run)
            self.assertEqual(sent["envelope"]["run_id"], run)
            self.board.readiness(prefix + "w", run, "registered", "", "worker-m", 0)
        with self.assertRaisesRegex(BoardError, "differs"):
            self.board.send_message("boss", "aw", envelope(run_id="run-zzz", purpose="assignment"), "k-zzz")
        with self.assertRaisesRegex(BoardError, "differs"):
            self.board.readiness("aw", "run-zzz", "registered", "", "worker-m", 0)
        # Peer rules come from the envelope's own run: run-b's worker may not message run-a's worker.
        with self.assertRaisesRegex(BoardError, "peers"):
            self.board.send_message("bw", "aw", envelope(run_id="run-b", purpose="question"), "k-peer")


if __name__ == "__main__":
    unittest.main()
