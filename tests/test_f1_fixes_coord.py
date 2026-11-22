"""Regression tests for f1 bugs B3, B4, B5, B12, B13, B16 (coordination, leases, per-run plans)."""
import ast
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from fusion_swarm import coordination, team
from fusion_swarm.board import BoardError, HiveBoard

CAPS = {"host": "local", "source": "local-process", "models": ["lead-m", "worker-m"], "operations": ["dispatch"]}


def spec(run_id, workspace, prefix, **extra):
    return {"run_id": run_id, "task": "t", "workspace": str(workspace), "form": "sidekick",
            "roster": [{"id": "lead", "provider": "native", "model": "lead-m", "role": "lead"},
                       {"id": prefix + "w", "provider": "native", "model": "worker-m"}],
            "jobs": [{"id": prefix + "job", "goal": "do " + prefix, "assigned_to": prefix + "w",
                      "writes": [prefix + ".py"], "success_criteria": ["done"]}], **extra}


class CoordFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fusion f1 coord ")
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.db = str(self.root / "board.sqlite")

    def tearDown(self):
        for board in getattr(self, "boards", []):
            board.close()
        self.tmp.cleanup()

    def open(self):
        board = HiveBoard(self.db)
        self.boards = getattr(self, "boards", []) + [board]
        return board

    def board_for(self, run_id, prefix, bounds=None, multi=False):
        team.prepare(team.compile_spec({**spec(run_id, self.ws, prefix), **({"strategy": {"bounds": bounds}} if bounds else {})}),
                     self.db, allow_multi_run=multi)
        if not getattr(self, "boards", None):
            self.open()
        return self.boards[0]

    # B3
    def test_b03_reservation_ttl_follows_run_timeout(self):
        board = self.board_for("r1", "a")
        board.create_run("lead", "r1", "local", CAPS, {"timeout_seconds": 3600})
        now = time.time()
        board.dispatch_actions("lead", "r1")
        rows = board.reservations()
        self.assertTrue(rows)
        self.assertGreaterEqual(min(r["expires_at"] for r in rows) - now, 3600)

    # B4
    def _running(self):
        board = self.board_for("r1", "a")
        board.start_task("aw", "ajob")
        return board

    def test_b04_running_task_expires_and_requeues(self):
        board = self._running()
        task = board.tasks("ajob")[0]
        self.assertEqual(task["state"], "running")
        self.assertIsNotNone(task["lease_until"])
        self.assertEqual(board.expire_tasks(now=task["lease_until"] - 1), [])
        self.assertEqual(board.expire_tasks(now=task["lease_until"] + 1), ["ajob"])
        after = board.tasks("ajob")[0]
        self.assertEqual((after["state"], after["attempt"]), ("queued", 1))
        self.assertEqual(board.reservations(), [])
        self.assertEqual(board.task_history("ajob")[-1]["state"], "expired")
        report = {"task_id": "ajob", "attempt": 1, "status": "blocked", "summary": "late", "gaps": ["late"]}
        with self.assertRaises(BoardError):
            board.submit_task("aw", report)  # stale attempt rejected
        self.assertEqual(board.start_task("aw", "ajob")["attempt"], 2)

    def test_b04_lazy_expiry_from_tasks(self):
        board = self._running()
        real = time.time
        coordination.time.time = lambda: real() + 10_000
        try:
            self.assertEqual(board.tasks("ajob")[0]["state"], "queued")
        finally:
            coordination.time.time = real

    def test_b04_active_runtime_job_blocks_expiry(self):
        board = self.board_for("r1", "a")
        board.create_run("lead", "r1", "local", CAPS)
        board.dispatch_actions("lead", "r1")
        task = board.tasks("ajob")[0]
        self.assertEqual(task["state"], "running")
        # Runtime activity observed 100 s before the lease ends: inside the lease window, so no expiry.
        with board._transaction():
            board._conn.execute("UPDATE runtime_jobs SET updated_at=?", (task["lease_until"] - 100,))
        self.assertEqual(board.expire_tasks(now=task["lease_until"] + 1), [])
        self.assertEqual(board.tasks("ajob")[0]["state"], "running")
        # Far beyond the lease with no further observation: expires.
        self.assertEqual(board.expire_tasks(now=task["lease_until"] + 100_000), ["ajob"])

    def test_b04_renew_task_extends(self):
        board = self._running()
        before = board.tasks("ajob")[0]
        res_before = board.reservations()[0]["expires_at"]
        time.sleep(0.02)
        renewed = board.renew_task("aw", "ajob", 1, lease_seconds=5000)
        self.assertGreater(renewed["lease_until"], before["lease_until"])
        self.assertGreater(board.reservations()[0]["expires_at"], res_before)
        shorter = board.renew_task("aw", "ajob", 1, lease_seconds=30)
        self.assertEqual(shorter["lease_until"], renewed["lease_until"])
        self.assertGreaterEqual(board.reservations()[0]["expires_at"], renewed["lease_until"])
        with self.assertRaises(BoardError):
            board.renew_task("aw", "ajob", 2)
        with self.assertRaises(BoardError):
            board.renew_task("lead", "ajob", 1)
        self.assertEqual(board.expire_tasks(now=before["lease_until"] + 1), [])

    def test_b04_migration_adds_column_to_existing_board(self):
        import sqlite3
        board = self.board_for("r1", "a")
        board.close()
        self.boards = []
        con = sqlite3.connect(self.db)
        con.execute("ALTER TABLE work_items DROP COLUMN lease_until")
        con.commit()
        con.close()
        board = self.open()
        self.assertIn("lease_until", board.tasks("ajob")[0])

    # B5
    def test_b05_two_runs_one_board(self):
        board = self.board_for("run-a", "a", multi=True)
        self.board_for("run-b", "b", multi=True)
        for run, prefix in (("run-a", "a"), ("run-b", "b")):
            board.create_run("lead", run, "local", CAPS)
            actions = board.dispatch_actions("lead", run)
            self.assertEqual([a["task_id"] for a in actions if a["run_id"] == run], [prefix + "job"])
            status = board.run_status("lead", run)
            self.assertEqual([t["id"] for t in status["tasks"]], [prefix + "job"])
        self.assertEqual(board._team_plan_unlocked("run-b")["run_id"], "run-b")
        self.assertEqual(board._team_plan_unlocked("run-a")["run_id"], "run-a")
        with self.assertRaises(BoardError):
            board._team_plan_unlocked("missing")

    def test_b05_second_run_refused_without_opt_in(self):
        self.board_for("run-a", "a")
        with self.assertRaises(BoardError):
            team.prepare(team.compile_spec(spec("run-b", self.ws, "b")), self.db)

    def test_b05_legacy_only_board_still_resolves(self):
        board = self.board_for("run-a", "a")
        plan = board._team_plan_unlocked("run-a")
        with board._transaction():
            board._conn.execute("DELETE FROM shared_context WHERE key=?", (coordination.team_plan_key("run-a"),))
        self.assertEqual(board._team_plan_unlocked("run-a")["run_id"], plan["run_id"])
        with self.assertRaises(BoardError):
            board._team_plan_unlocked("other")

    # B12
    def test_b12_candidate_status_ignores_other_runs(self):
        board = self.board_for("run-a", "a", multi=True)
        self.board_for("run-b", "b", multi=True)
        # A task of run B that (wrongly) names run A's task as its candidate must not grant access.
        board.register({"id": "intruder", "provider": "native", "model": "worker-m", "role": "worker"})
        with board._transaction():
            brief = board.tasks("bjob")[0]["brief"]
            import json
            brief = {**brief, "candidate_of": "ajob"}
            board._conn.execute("UPDATE work_items SET brief=?,assigned_to='intruder' WHERE id='bjob'", (json.dumps(brief),))
        with self.assertRaises(BoardError):
            board.candidate_status("intruder", "ajob")
        scanned = board._plan_tasks_unlocked("ajob")
        self.assertEqual([t["id"] for t in scanned], ["ajob"])

    # B13
    def test_b13_case_insensitive_reservation_collides(self):
        (self.ws / "src").mkdir()
        swapped = Path(str(self.ws).swapcase())
        if not (swapped.exists() and os.path.samefile(self.ws, swapped)):
            self.skipTest("filesystem is case-sensitive")
        board = self.open()
        for agent in ("one", "two"):
            board.register({"id": agent, "provider": "native", "model": "m", "role": "worker"})
        board.reserve("one", str(self.ws), ["src/Main.py"])
        with self.assertRaises(BoardError):
            board.reserve("two", str(self.ws), ["SRC/main.PY"])
        # Persisted path keeps the caller's original case; only comparison folds.
        self.assertTrue(board.reservations()[0]["path"].endswith("Main.py"))
        board.release("one", str(self.ws), ["SRC/MAIN.py"])
        self.assertEqual(board.reservations(), [])

    def test_b13_sibling_prefix_not_overlapping(self):
        self.assertFalse(coordination.overlaps("/w/src", "/w/src2"))
        self.assertFalse(coordination.overlaps("/w/src2/a", "/w/src"))
        self.assertTrue(coordination.overlaps("/w/src", "/w/src/a.py"))
        self.assertTrue(coordination.overlaps("/w/src", "/w/src"))
        (self.ws / "src").mkdir()
        (self.ws / "src2").mkdir()
        board = self.open()
        for agent in ("one", "two"):
            board.register({"id": agent, "provider": "native", "model": "m", "role": "worker"})
        board.reserve("one", str(self.ws), ["src"])
        board.reserve("two", str(self.ws), ["src2"])
        with self.assertRaises(BoardError):
            board.reserve("two", str(self.ws), ["src/x.py"])

    def test_b13_probe_never_writes_into_workspace(self):
        coordination._CASE_CACHE.clear()
        before = sorted(os.listdir(self.ws))
        coordination.fs_case_insensitive(str(self.ws))
        self.assertEqual(sorted(os.listdir(self.ws)), before)

    # B16
    def test_b16_execution_imports_copy(self):
        tree = ast.parse((ROOT / "python/fusion_swarm/execution.py").read_text())
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertIn("copy", imported)
        coord = ast.parse((ROOT / "python/fusion_swarm/coordination.py").read_text())
        names = [a.name for n in ast.walk(coord) if isinstance(n, ast.ImportFrom) and n.module == "mailbox"
                 for a in n.names if a.name == "envelope_fingerprint"]
        self.assertEqual(len(names), 1)


if __name__ == "__main__":
    unittest.main()
