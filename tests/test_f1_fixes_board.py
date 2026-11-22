"""Regression tests for f1 B7/B9c/B11/B14/B15/B16 (hb-board)."""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from fusion_swarm import board as board_mod  # noqa: E402
from fusion_swarm.board import AGENT_STATUSES, BoardError, HiveBoard, build_parser  # noqa: E402


def _agent(i, role="worker", **kw):
    d = {"id": i, "role": role, "provider": "native", "model": "selected"}
    d.update(kw)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fusion f1 board ")
        self.db = Path(self.tmp.name) / "board.sqlite"
        self.board = HiveBoard(self.db)

    def tearDown(self):
        self.board.close()
        self.tmp.cleanup()


class B07(Base):
    def test_b07_documented_recipes_parse(self):
        parser = build_parser()
        subs = {"ID": "1", "A": "a", "B": "b", "PATH": "/tmp/x.sqlite"}
        count = 0
        for doc in ("commands/board.md", "skills/fusion-board/SKILL.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            # join backslash continuations
            text = re.sub(r"\\\n\s*", " ", text)
            for line in text.splitlines():
                m = re.search(r"python -m fusion_swarm\.board (.*)$", line.strip())
                if not m or "`" in line and not line.strip().startswith("python"):
                    continue
                rest = m.group(1)
                if "<verb>" in rest:
                    continue
                rest = rest.replace("|", " ")  # status/role alternations: pick first below
                rest = re.sub(r"\[--replace\]", "", rest)
                rest = re.sub(r"(--role|--status) (\w+)(?: \w+)*?(?= --|$)", r"\1 \2", rest)
                rest = rest.replace("<id>", "x").replace("<p>", "p").replace("<m>", "m")
                rest = re.sub(r"<[^>]*>", "x", rest)
                import shlex
                try:
                    argv = shlex.split(rest)
                except ValueError:
                    continue
                argv = ["1" if a == "$MSG_ID" else subs.get(a, a) for a in argv]
                try:
                    parser.parse_args(argv)
                except SystemExit:
                    self.fail(f"{doc}: does not parse: {line.strip()}")
                count += 1
        self.assertGreater(count, 10)

    def test_b07_aliases_and_statuses(self):
        p = build_parser()
        a = p.parse_args(["--db", "x", "dm", "--from-agent", "a", "--to-agent", "b", "--body", "hi"])
        self.assertEqual((a.from_agent, a.to_agent), ("a", "b"))
        a = p.parse_args(["--db", "x", "post", "--from-agent", "a", "--body", "hi"])
        self.assertEqual(a.from_agent, "a")
        a = p.parse_args(["--db", "x", "reply", "--from-agent", "a", "--message-id", "1", "--body", "hi"])
        self.assertEqual(a.from_agent, "a")
        self.board.register(_agent("w"))
        for st in ("blocked", "done"):
            self.assertIn(st, AGENT_STATUSES)
            self.assertEqual(self.board.heartbeat("w", status=st)["status"], st)
        for doc in ("commands/board.md", "skills/fusion-board/SKILL.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            self.assertNotIn("--from-agent", text)
            self.assertNotIn("--to-agent", text)
        skill = (ROOT / "skills/fusion-board/SKILL.md").read_text(encoding="utf-8")
        self.assertIn(" ".join(f"`{s}`" for s in AGENT_STATUSES), skill)


class B09c(Base):
    def test_b09c_register_does_not_silently_overwrite(self):
        self.board.register(_agent("w", provider="a", model="m1"))
        # same identity: allowed, refreshes status
        self.board.register(_agent("w", provider="a", model="m1", status="busy"))
        with self.assertRaisesRegex(BoardError, "different identity"):
            self.board.register(_agent("w", provider="evil", model="m1"))
        with self.assertRaises(BoardError):
            self.board.register(_agent("w", role="captain", provider="a", model="m1"))
        self.assertEqual(self.board.agent("w")["provider"] if hasattr(self.board, "agent") else "a", "a")
        rec = self.board.register(_agent("w", provider="b", model="m2"), replace=True)
        self.assertEqual((rec["provider"], rec["model"]), ("b", "m2"))


class B11(Base):
    def test_b11_reopen_runs_no_executescript(self):
        self.board.close()
        self.assertEqual(sqlite3.connect(self.db).execute("PRAGMA user_version").fetchone()[0],
                         board_mod.BOARD_SCHEMA_VERSION)
        calls = []
        orig = sqlite3.Connection

        class Spy(orig):
            def executescript(self, sql):
                calls.append(sql)
                return super().executescript(sql)

        real_connect = sqlite3.connect
        board_mod.sqlite3.connect = lambda *a, **k: real_connect(*a, factory=Spy, **k)
        try:
            b = HiveBoard(self.db)
            b.register(_agent("x"))
            b.close()
        finally:
            board_mod.sqlite3.connect = real_connect
        self.assertEqual(calls, [])
        # a fresh db still runs the full schema
        fresh = HiveBoard(Path(self.tmp.name) / "fresh.sqlite")
        self.assertIn("hive", {c["name"] for c in fresh.channels()})
        fresh.close()


class B14(Base):
    def test_b14_timestamps_are_utc_iso_ms(self):
        pat = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertRegex(board_mod._now(), pat)
        rec = self.board.register(_agent("w"))
        self.assertRegex(rec["last_seen"], pat)
        self.assertRegex(self.board.heartbeat("w")["last_seen"], pat)
        # old local-time rows remain readable
        self.board._conn.execute("UPDATE agents SET last_seen='2020-01-01T00:00:00' WHERE id='w'")
        self.board._conn.commit()
        self.assertEqual([a["last_seen"] for a in self.board.agents()], ["2020-01-01T00:00:00"])


class B15(Base):
    def test_b15_wakeup_generation_counts_every_touch(self):
        errs = []

        def run():
            try:
                for _ in range(50):
                    self.board._touch_wakeup()
            except Exception as e:  # pragma: no cover
                errs.append(e)

        ts = [threading.Thread(target=run) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(errs, [])
        self.assertEqual(board_mod._read_wakeup_generation(self.board.wakeup_path), 400)


class B16(Base):
    def test_b16_reply_preserves_kind_and_idempotency(self):
        self.board.register(_agent("a"))
        self.board.register(_agent("b"))
        root = self.board.post(from_agent="a", body="q", channel="hive")
        r1 = self.board.reply("b", root["id"], "ans", kind="finding", priority="urgent", idempotency_key="k1")
        self.assertEqual(r1["kind"], "finding")
        r2 = self.board.reply("b", root["id"], "ans", kind="finding", priority="urgent", idempotency_key="k1")
        self.assertEqual(r1["id"], r2["id"])
        plain = self.board.reply("b", root["id"], "plain")
        self.assertEqual(plain["kind"], "message")


if __name__ == "__main__":
    unittest.main()
