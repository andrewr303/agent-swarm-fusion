"""Tests for the board port (python/fusion_swarm/vboard/port.py): Local, Http and VercelCli adapters.

stdlib unittest; no network (loopback servers only), no real `vercel`, no Node. The VercelCliAdapter is driven
through tests/fixtures/fake_vercel.py (a stdlib script emulating `vercel board ... --json --as X` over a
LocalEngineAdapter) or through injected runners. Every database, claim cache and cache file lives in a temp dir.
Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_vboard_port -v   (from the plugin root)
"""
from __future__ import annotations

import http.server
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from fusion_swarm.vboard import cli as vcli  # noqa: E402
from fusion_swarm.vboard import port as vport  # noqa: E402
from fusion_swarm.vboard.claims import ClaimCache, board_key  # noqa: E402
from fusion_swarm.vboard.http import serve  # noqa: E402
from fusion_swarm.vboard.model import VBoardError, iso  # noqa: E402
from fusion_swarm.vboard.port import (Capabilities, HttpBoardAdapter, LocalEngineAdapter, VercelCliAdapter,  # noqa: E402
                                      open_port, parse_failure_document, probe, renew_interval)
from fusion_swarm.vboard.store import BoardStore  # noqa: E402

FAKE = ROOT / "tests" / "fixtures" / "fake_vercel.py"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.xdg = self.tmp / "xdg"
        self.home = self.tmp / "fusion-home"
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg), "FUSION_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("BOARD_PROFILE", "FUSION_AGENT_ID", "FUSION_VBOARD", "BOARD_TOKEN", "BOARD_URL", "BOARD_NAME"):
            os.environ.pop(var, None)
        self._closers: list = []
        self.addCleanup(self._close_all)

    def _close_all(self) -> None:
        for fn in reversed(self._closers):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    def local(self, name: str = "b", as_: str | None = "w1") -> LocalEngineAdapter:
        port = LocalEngineAdapter(self.tmp / f"{name}.db", as_=as_)
        self._closers.append(port.close)
        return port

    # -- fake vercel plumbing
    def fake_env(self, db: Path, **extra: str) -> dict[str, str]:
        env = dict(os.environ)
        env.update({"FAKE_VERCEL_DB": str(db), "PYTHONDONTWRITEBYTECODE": "1"})
        env.update(extra)
        return env

    def fake_runner(self, calls: list | None = None):
        def runner(argv, **kw):
            if calls is not None:
                calls.append(list(argv))
            return subprocess.run([sys.executable, str(FAKE), *argv[1:]], **kw)

        return runner

    def vercel(self, db: Path | None = None, *, as_: str = "w1", calls: list | None = None,
               style: str = "compact", extra: dict | None = None, **kw) -> VercelCliAdapter:
        db = db or (self.tmp / "v.db")
        env = self.fake_env(db, FAKE_VERCEL_STYLE=style, **(extra or {}))
        kw.setdefault("cache_path", self.tmp / "caps.json")
        kw.setdefault("sleep", lambda s: None)
        return VercelCliAdapter(as_=as_, runner=self.fake_runner(calls), env=env, **kw)


def done(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["vercel"], rc, out, err)


def failure(reason: str, message: str = "boom", status: int = 409, **extra) -> str:
    doc = {"status": "error", "reason": reason, "message": message, "next": [],
           "data": {"error": {"code": reason, "message": message, "status": status, **extra}, "retryable": False}}
    return json.dumps(doc)


def keys(doc) -> list:
    return sorted(doc.keys())


# ===================================================================================== local adapter + parity
class LocalAdapterTests(Base):
    def test_documents_have_cli_shapes(self):
        p = self.local()
        post = p.post("dev", "hello @nobody")
        self.assertEqual(keys(post), ["mentions", "message", "outcome"])
        mid = post["message"]["id"]
        self.assertEqual(keys(p.reply(mid, "re")), ["mentions", "message", "outcome"])
        self.assertEqual(p.react(mid, "eyes"), {"message": {"id": mid}, "outcome": "reacted"})
        self.assertEqual(p.react(mid, "eyes", remove=True)["outcome"], "unreacted")
        self.assertEqual(p.label(mid, set={"k": "v"}), {"message": {"id": mid}, "outcome": "labeled"})
        self.assertEqual(p.edit(mid, "hello again")["outcome"], "edited")
        self.assertEqual(keys(p.read("dev")), ["cursor", "older", "threads"])
        self.assertEqual(keys(p.read(mid)), ["cursor", "message", "replies", "schedules"])
        self.assertEqual(keys(p.channels()), ["channels", "next"])
        t = p.task_create("work/x", "do it", claim=True, lease=60)
        self.assertEqual(keys(t), ["claim", "outcome", "task"])
        self.assertEqual(keys(t["claim"]), ["epoch", "until"])
        self.assertNotIn("token", json.dumps(t))
        renewed = p.task_renew(t["task"]["id"])
        self.assertEqual(keys(renewed), ["claim", "task"])
        done_doc = p.task_complete(t["task"]["id"], "ok")
        self.assertEqual(done_doc["outcome"], "completed")
        self.assertEqual(p.delete(mid)["outcome"], "deleted")

    def test_wait_wakes_on_post_and_times_out(self):
        p = self.local()
        p.post("dev", "first")
        cursor = p.head()
        other = self.local(as_="w2")
        t = threading.Timer(0.05, lambda: other.post("dev", "second"))
        t.start()
        self.addCleanup(t.join, 2)
        ev = p.wait("dev", after=cursor, timeout=5)
        self.assertEqual(ev["type"], "message.posted")
        self.assertIsNone(p.wait("dev", after=ev["cursor"], timeout=0.1))

    def test_unknown_settle_raises_claim_not_held(self):
        p = self.local()
        t = p.task_create("work/x", "x")
        with self.assertRaises(VBoardError) as cm:
            p.task_complete(t["task"]["id"])
        self.assertEqual(cm.exception.code, "claim_not_held")

    def test_renew_interval_bounds(self):
        import random
        rng = random.Random(7)
        for _ in range(50):
            self.assertTrue(120 <= renew_interval(300, rng=rng) <= 144)
            self.assertLessEqual(renew_interval(60, rng=rng), 48)

    def test_capabilities_local(self):
        caps = self.local().capabilities()
        self.assertIsInstance(caps, Capabilities)
        self.assertEqual((caps.backend, caps.batch_max, caps.lease_cap_s, caps.priority_native,
                          caps.conditional_needs), ("local", 100, 3600, True, True))
        self.assertEqual(probe(self.local("c")).backend, "local")


def exercise(port) -> dict:
    """Run the same operations on any adapter and return the key sets of every document."""
    out: dict = {}
    post = port.post("work/parity", "hello")
    out["post"] = keys(post)
    out["post.message"] = keys(post["message"])
    out["reply"] = keys(port.reply(post["message"]["id"], "re"))
    read = port.read("work/parity")
    out["read.channel"] = keys(read)
    out["read.thread"] = keys(port.read(post["message"]["id"]))
    out["channels"] = keys(port.channels())
    t1 = port.task_create("work/parity", "task one")
    out["task_create"] = keys(t1)
    out["task_create.task"] = keys(t1["task"])
    out["task_create.task.task"] = keys(t1["task"]["task"])
    out["tasks"] = keys(port.tasks("work/parity"))
    out["tasks.item"] = keys(port.tasks("work/parity")["tasks"][0])
    claim = port.task_claim(t1["task"]["id"], lease=120)
    out["claim"] = keys(claim)
    out["claim.claim"] = keys(claim["claim"])
    out["renew"] = keys(port.task_renew(t1["task"]["id"]))
    done_doc = port.task_complete(t1["task"]["id"], "ok")
    out["complete"] = keys(done_doc)
    t2 = port.task_create("work/parity", "task two")
    nxt = port.task_claim_next("work/parity", respect_priority=False)
    out["claim_next"] = keys(nxt)
    assert nxt["task"]["id"] == t2["task"]["id"]
    out["fail"] = keys(port.task_fail(t2["task"]["id"], "nope"))
    out["claim_next.empty"] = port.task_claim_next("work/parity", respect_priority=False)
    out["react"] = keys(port.react(post["message"]["id"], "eyes"))
    out["label"] = keys(port.label(post["message"]["id"], set={"a": "b"}))
    out["edit"] = keys(port.edit(post["message"]["id"], "hello2"))
    return out


class ParityTests(Base):
    def test_local_and_fake_vercel_return_identical_key_sets(self):
        local = exercise(self.local("parity-local"))
        fake = exercise(self.vercel(self.tmp / "parity-vercel.db"))
        self.assertEqual(local, fake)
        self.assertIsNone(local["claim_next.empty"])

    def test_http_adapter_key_sets_match_local(self):
        server = serve(BoardStore(self.tmp / "http.db"))
        self._closers += [server.stop, server.store.close]
        server.write_token_file(self.xdg)
        http = HttpBoardAdapter(server.url, as_="w1", env=dict(os.environ))
        self._closers.append(http.close)
        self.assertEqual(exercise(self.local("parity-local2")), exercise(http))


# ===================================================================================== vercel adapter
class VercelAdapterTests(Base):
    def test_vercel_failure_line_parsed_to_vboard_error(self):
        runner = mock.Mock(return_value=done(1, "", failure("task_claimed", "task 4 is already claimed", 409) + "\n"))
        p = VercelCliAdapter(as_="w1", runner=runner, sleep=lambda s: None)
        with self.assertRaises(VBoardError) as cm:
            p.task_claim("4")
        err = cm.exception
        self.assertEqual((err.code, err.status, err.message), ("task_claimed", 409, "task 4 is already claimed"))
        self.assertEqual(runner.call_count, 1)                    # a conflict is never retried
        runner = mock.Mock(return_value=done(1, "", "Segmentation fault\n"))
        with self.assertRaises(VBoardError) as cm:
            VercelCliAdapter(as_="w1", runner=runner).whoami()
        self.assertEqual(cm.exception.code, "unexpected_error")

    def test_exit2_not_retried(self):
        sleeps: list = []
        runner = mock.Mock(return_value=done(2, "", failure("invalid_arguments", "bad lease", 400)))
        p = VercelCliAdapter(as_="w1", runner=runner, sleep=sleeps.append)
        with self.assertRaises(VBoardError) as cm:
            p.tasks("work/x")
        self.assertEqual(cm.exception.code, "invalid_arguments")
        self.assertEqual((runner.call_count, sleeps), (1, []))
        # even a reason that is normally retryable is not retried on exit 2
        runner = mock.Mock(return_value=done(2, "", failure("network_error", "x", 503)))
        with self.assertRaises(VBoardError):
            VercelCliAdapter(as_="w1", runner=runner, sleep=sleeps.append).tasks("work/x")
        self.assertEqual(runner.call_count, 1)

    def test_rate_limited_retry_after_honored(self):
        sleeps: list = []
        limited = done(1, "", failure("rate_limited", "slow down", 429, retryAfter=7))
        runner = mock.Mock(side_effect=[limited, limited, done(0, json.dumps({"tasks": [], "cursor": "1"}))])
        p = VercelCliAdapter(as_="w1", runner=runner, sleep=sleeps.append)
        self.assertEqual(p.tasks("work/x")["cursor"], "1")
        self.assertEqual(sleeps, [7.0, 7.0])
        runner = mock.Mock(return_value=limited)
        sleeps.clear()
        with self.assertRaises(VBoardError) as cm:
            VercelCliAdapter(as_="w1", runner=runner, sleep=sleeps.append).tasks("work/x")
        self.assertEqual((cm.exception.code, runner.call_count, len(sleeps)), ("rate_limited", 4, 3))

    def test_network_error_backoff_only_for_repeatable_calls(self):
        sleeps: list = []
        net = done(1, "", failure("network_error", "unreachable", 503))
        runner = mock.Mock(side_effect=[net, net, net, done(0, json.dumps({"tasks": [], "cursor": "1"}))])
        VercelCliAdapter(as_="w1", runner=runner, sleep=sleeps.append).tasks("work/x")
        self.assertEqual(sleeps, [1.0, 2.0, 4.0])
        runner = mock.Mock(return_value=net)
        with self.assertRaises(VBoardError):                       # a post without an idempotency key is not repeated
            VercelCliAdapter(as_="w1", runner=runner, sleep=sleeps.append).post("dev", "once")
        self.assertEqual(runner.call_count, 1)

    def test_vercel_requires_as_and_never_passes_sync(self):
        for bad in ("", None, "9lives", "has space"):
            with self.assertRaises(VBoardError) as cm:
                VercelCliAdapter(as_=bad)                           # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "invalid_profile")
        calls: list = []
        p = self.vercel(calls=calls, name="team-board")
        p.post("dev", "hi", labels={"a": "b"}, idempotency_key="k1")
        t = p.task_create("work/x", "-dash first", claim=True, lease=900)
        p.task_complete(t["task"]["id"], "r")
        p.tasks("work/x")
        p.read("dev")
        self.assertTrue(calls)
        for argv in calls:
            if argv[1] != "board" or "--help" in argv:
                continue
            self.assertNotIn("--sync", argv)
            self.assertNotIn("--scope", argv)
            self.assertEqual(argv[0:2], ["vercel", "board"])
            self.assertEqual(argv[argv.index("--as") + 1], "w1")
            self.assertIn("--json", argv)
            self.assertEqual(argv[argv.index("--name") + 1], "team-board")
        create = next(a for a in calls if a[2:4] == ["tasks", "create"])
        self.assertIn("--", create)                                  # a text starting with `-` goes after `--`
        self.assertLess(create.index("--as"), create.index("--"))
        self.assertEqual(vport.VercelCliAdapter(as_="W1").argv("whoami"),
                         ["vercel", "board", "whoami", "--json", "--as", "w1"])

    def test_lease_sent_as_duration_string(self):
        calls: list = []
        p = self.vercel(calls=calls)
        t = p.task_create("work/x", "a")
        t2 = p.task_create("work/x", "b")
        t3 = p.task_create("work/x", "c")
        p.task_claim(t["task"]["id"], lease=900)                    # clamped to the hosted cap
        p.task_claim(t2["task"]["id"], lease=120.7)
        p.task_claim(t3["task"]["id"])
        leases = [a[a.index("--lease") + 1] for a in calls if "--lease" in a]
        self.assertEqual(leases, ["300s", "120s", "300s"])
        for lease in leases:
            self.assertRegex(lease, r"^\d+s$")
        # the stock CLI (and our fake) reject a bare integer with exit 2 invalid_arguments
        env = self.fake_env(self.tmp / "v.db")
        proc = subprocess.run([sys.executable, str(FAKE), "board", "tasks", "claim", t["task"]["id"], "--lease",
                               "300", "--json", "--as", "w1"], capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stderr)["reason"], "invalid_arguments")
        got = p.capabilities()                                      # until is read back from the answer
        self.assertEqual(got.lease_cap_s, 300)

    def test_vercel_failure_document_pretty_stdout_and_stderr(self):
        p = self.vercel(style="pretty66")
        with self.assertRaises(VBoardError) as cm:
            p.task_complete("999")
        self.assertEqual(cm.exception.code, "claim_not_held")
        proc = subprocess.run([sys.executable, str(FAKE), "board", "tasks", "complete", "999", "--json", "--as", "w1"],
                              capture_output=True, text=True, env=self.fake_env(self.tmp / "v.db",
                                                                                 FAKE_VERCEL_STYLE="pretty66"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("\n  ", proc.stdout)                           # pretty-printed on stdout ...
        self.assertIn("\n  ", proc.stderr)                           # ... and on stderr
        doc = parse_failure_document(proc.stderr, proc.stdout)
        self.assertEqual((doc["reason"], doc["cliVersion"]), ("claim_not_held", "66.2.0"))
        self.assertIn("invocationId", doc)
        only_stdout = parse_failure_document("", proc.stdout)
        self.assertEqual(only_stdout["reason"], "claim_not_held")
        noisy = "warning: update available\n" + json.dumps({"reason": "task_claimed", "message": "m"}) + "\nbye\n"
        self.assertEqual(parse_failure_document(noisy, "")["reason"], "task_claimed")
        self.assertIsNone(parse_failure_document("no json here", ""))

    def test_wait_timeout_is_none(self):
        runner = mock.Mock(return_value=done(1, "", failure("timeout", "Nothing matched within 5s.", 408)))
        p = VercelCliAdapter(as_="w1", runner=runner, sleep=lambda s: None)
        self.assertIsNone(p.wait("dev", after="3", timeout=5))
        argv = runner.call_args[0][0]
        self.assertEqual(argv[argv.index("--timeout") + 1], "5s")
        runner = mock.Mock(return_value=done(0, json.dumps({"event": {"type": "message.posted", "cursor": "9"}})))
        self.assertEqual(VercelCliAdapter(as_="w1", runner=runner).wait("dev", timeout=5)["cursor"], "9")

    def test_conditional_needs_not_supported_on_vercel(self):
        p = self.vercel()
        first = p.task_create("work/x", "a")["task"]["id"]
        with self.assertRaises(VBoardError) as cm:
            p.task_create("work/x", "b", needs=[{"id": first, "if": "failed"}])
        self.assertEqual(cm.exception.code, "not_supported")
        with self.assertRaises(VBoardError):
            p.task_needs(first, [{"id": "7", "if": "failed"}])
        with self.assertRaises(VBoardError):
            p.task_create_batch("work/x", [{"text": "z", "needs": [{"id": first, "if": "failed"}]}],
                                idempotency_key="K")
        plain = p.task_create("work/x", "c", needs=[first])          # plain needs are fine
        self.assertEqual(plain["task"]["task"]["state"], "waiting")
        local = self.local("cond")
        a = local.task_create("work/x", "a")["task"]["id"]
        b = local.task_create("work/x", "b", needs=[{"id": a, "if": "failed"}])
        self.assertEqual(b["task"]["task"]["needsIf"], {a: "failed"})

    def test_capability_probe_cached_by_help_hash(self):
        calls: list = []
        cache = self.tmp / "caps.json"
        p1 = self.vercel(calls=calls, cache_path=cache)
        caps = p1.capabilities()
        self.assertEqual((caps.backend, caps.version, caps.batch, caps.claim_wait, caps.idempotency),
                         ("vercel", "66.2.0", True, True, True))
        self.assertEqual((caps.lease_cap_s, caps.batch_max, caps.priority_native, caps.conditional_needs,
                          caps.subtree_read), (300, 15, False, False, False))
        self.assertRegex(caps.help_hash, r"^[0-9a-f]{64}$")
        self.assertIs(p1.capabilities(), caps)                       # memoized per adapter
        self.assertEqual(len(calls), 3)                              # --version, board --help, board tasks --help
        entries = json.loads(cache.read_text())["entries"]
        self.assertEqual(list(entries), [caps.help_hash])
        # a second adapter on the same cache hits the entry stored under the hash (tampered value proves it)
        entries[caps.help_hash]["file"] = False
        cache.write_text(json.dumps({"entries": entries}))
        caps2 = self.vercel(cache_path=cache).capabilities()
        self.assertEqual((caps2.help_hash, caps2.batch), (caps.help_hash, False))
        # different help text -> different hash -> features re-derived and stored next to the old entry
        caps3 = self.vercel(cache_path=cache, extra={"FAKE_VERCEL_HELP_EXTRA": "new flag"}).capabilities()
        self.assertNotEqual(caps3.help_hash, caps.help_hash)
        self.assertTrue(caps3.batch)
        self.assertEqual(len(json.loads(cache.read_text())["entries"]), 2)

    def test_renew_is_floored_for_hosted_hygiene(self):
        now = [1_000_000.0]
        mono = [0.0]
        until = lambda: iso(int((now[0] + 300) * 1000))  # noqa: E731
        task = {"id": "5", "task": {"state": "claimed"}}
        calls: list = []

        def runner(argv, **kw):
            calls.append(argv)
            if "renew" in argv:
                return done(0, json.dumps({"task": task, "claim": {"epoch": 1, "until": until()}}))
            return done(0, json.dumps({"task": task, "claim": {"epoch": 1, "until": until()}}))

        p = VercelCliAdapter(as_="w1", runner=runner, clock=lambda: now[0], monotonic=lambda: mono[0])
        p.task_claim("5")
        mono[0] = 30
        early = p.task_renew("5")
        self.assertEqual(len(calls), 1)                              # too soon: answered locally, no CLI process
        self.assertEqual(keys(early), ["claim", "task"])
        mono[0] = 130
        now[0] += 130
        p.task_renew("5")
        self.assertEqual(len(calls), 2)

    def test_native_claim_ready_and_missing_queue(self):
        calls: list = []
        p = self.vercel(calls=calls)
        with self.assertRaises(VBoardError) as cm:                  # the stock CLI: a channel must exist
            p.task_claim_next("work/q", respect_priority=False)
        self.assertEqual(cm.exception.code, "channel_not_found")
        p.task_create("work/q", "one")
        got = p.task_claim_next("work/q", respect_priority=False, wait=0)
        self.assertEqual(got["task"]["task"]["state"], "claimed")
        self.assertIsNone(p.task_claim_next("work/q", respect_priority=False))
        native = [a for a in calls if "--ready" in a]
        self.assertEqual(len(native), 3)
        with self.assertRaises(VBoardError) as cm:
            p.tasks("work/")
        self.assertEqual(cm.exception.code, "not_supported")

    def test_priority_emulation_matches_local_order(self):
        def build(creator):
            future = iso(int((time.time() + 3600) * 1000))
            ids = {}
            ids["low"] = creator.task_create("work/prio", "low")["task"]["id"]
            ids["hi1"] = creator.task_create("work/prio", "hi1", labels={"fusion.priority": "5"})["task"]["id"]
            ids["hi2"] = creator.task_create("work/prio", "hi2", labels={"fusion.priority": "5"})["task"]["id"]
            ids["mid"] = creator.task_create("work/prio", "mid", labels={"fusion.priority": "2"})["task"]["id"]
            ids["deferred"] = creator.task_create("work/prio", "later", labels={
                "fusion.priority": "9", "fusion.defer_until": future})["task"]["id"]
            ids["other"] = creator.task_create("work/prio", "theirs", for_="somebody-else")["task"]["id"]
            return ids

        def drain(port, ids):
            order = []
            while True:
                got = port.task_claim_next("work/prio", respect_priority=True)
                if got is None:
                    return order
                order.append(got["task"]["id"])

        local_db = self.local("prio-local", as_="w1")
        lids = build(self.local("prio-local", as_="boss"))
        local_order = drain(local_db, lids)
        vdb = self.tmp / "prio-vercel.db"
        boss = LocalEngineAdapter(vdb, as_="boss")
        self._closers.append(boss.close)
        vids = build(boss)
        remote = self.vercel(vdb, as_="w1")
        remote_order = drain(remote, vids)
        names = {v: k for k, v in lids.items()}
        self.assertEqual([names[i] for i in local_order], ["hi1", "hi2", "mid", "low"])
        self.assertEqual(local_order, remote_order)
        self.assertNotIn(lids["deferred"], local_order)
        # respect_priority=False is plain oldest-first on every adapter
        plain = self.local("prio-plain", as_="boss")
        pids = build(plain)
        first = plain.task_claim_next("work/prio", respect_priority=False)
        self.assertEqual(first["task"]["id"], pids["low"])

    def test_batch_chunks_at_15_with_offset_keys_and_cross_chunk_needs(self):
        log = self.tmp / "calls.jsonl"
        p = self.vercel(extra={"FAKE_VERCEL_LOG": str(log)})
        plan = [{"key": "root", "text": "root"}] + [{"text": f"t{i}", "needs": [{"key": "root"}]} for i in range(1, 36)]
        out = p.task_create_batch("work/b", plan, idempotency_key="K")
        self.assertEqual((out["outcome"], len(out["tasks"])), ("created", 36))
        argvs = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
        sent = [a[a.index("--idempotency-key") + 1] for a in argvs if "--file" in a]
        self.assertEqual(sent, ["K:0", "K:15", "K:30"])
        root_id = out["tasks"][0]["id"]
        self.assertEqual(out["tasks"][20]["task"]["needs"], [root_id])  # rewritten from {"key": "root"}
        # replaying the same call creates nothing new (idempotent chunks)
        again = p.task_create_batch("work/b", plan, idempotency_key="K")
        self.assertEqual([t["id"] for t in again["tasks"]], [t["id"] for t in out["tasks"]])
        with self.assertRaises(VBoardError) as cm:
            p.task_create_batch("work/b", [{"text": "x", "needs": [{"key": "late"}]}] +
                                [{"text": "y"}] * 20 + [{"key": "late", "text": "late"}], idempotency_key="K2")
        self.assertEqual(cm.exception.code, "invalid_needs")

    def test_batch_halves_on_batch_too_large(self):
        log = self.tmp / "halve.jsonl"
        p = self.vercel(extra={"FAKE_VERCEL_LOG": str(log), "FAKE_VERCEL_BATCH_MAX": "4"})
        out = p.task_create_batch("work/h", [{"text": f"t{i}"} for i in range(10)], idempotency_key="H")
        self.assertEqual([t["text"] for t in out["tasks"]], [f"t{i}" for i in range(10)])
        argvs = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
        keys_sent = [a[a.index("--idempotency-key") + 1] for a in argvs if "--file" in a]
        self.assertEqual(keys_sent[:2], ["H:0", "H:0"])               # 10 -> too large, 5 -> too large
        self.assertEqual(keys_sent[2:], ["H:0", "H:2", "H:4", "H:6", "H:8"])   # chunks of 2


class BatchChunkingTests(Base):
    def test_local_chunks_at_100_and_is_idempotent(self):
        p = self.local()
        sent: list = []
        real = p._batch_once

        def spy(channel, chunk, key):
            sent.append((key, len(chunk)))
            return real(channel, chunk, key)

        p._batch_once = spy
        plan = [{"text": f"t{i}"} for i in range(250)]
        out = p.task_create_batch("work/big", plan, idempotency_key="B")
        self.assertEqual(sent, [("B:0", 100), ("B:100", 100), ("B:200", 50)])
        self.assertEqual(len(out["tasks"]), 250)
        again = p.task_create_batch("work/big", plan, idempotency_key="B")
        self.assertEqual([t["id"] for t in again["tasks"]], [t["id"] for t in out["tasks"]])
        self.assertEqual(len(p.tasks("work/big", state="all", limit=500)["tasks"]), 250)
        with self.assertRaises(VBoardError):
            p.task_create_batch("work/big", [], idempotency_key="B")
        with self.assertRaises(VBoardError):
            p.task_create_batch("work/big", plan[:1], idempotency_key="")

    def test_halving_stops_at_one_and_other_errors_propagate(self):
        p = self.local()

        def always_big(channel, chunk, key):
            raise VBoardError("batch_too_large", "no")

        p._batch_once = always_big
        with self.assertRaises(VBoardError) as cm:
            p.task_create_batch("work/x", [{"text": "a"}, {"text": "b"}], idempotency_key="Z")
        self.assertEqual(cm.exception.code, "batch_too_large")


# ===================================================================================== http adapter
class DiscoveryOnlyServer:
    """Stdlib server whose discovery document has no `fusion` block (what a stock `vb serve` answers)."""

    def __init__(self, doc_extra: dict | None = None):
        outer = self
        self.hits: list = []
        self.doc_extra = doc_extra or {}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def do_GET(self):
                outer.hits.append((self.path, self.headers.get("x-board-profile"), self.headers.get("Authorization")))
                body = json.dumps({"version": "0.54.0", "url": outer.url, "audience": outer.url, "team_id": None,
                                   "project_id": None, "sync": [], **outer.doc_extra}).encode()
                self.send_response(200 if self.path.rstrip("/") == "/api/board" else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/board"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)


class HttpAdapterTests(Base):
    def server(self, **kw):
        srv = serve(BoardStore(self.tmp / "h.db"), **kw)
        self._closers += [srv.stop, srv.store.close]
        return srv

    def test_http_adapter_roundtrip_with_token_file(self):
        srv = self.server()
        # no token anywhere: the server refuses
        bare = HttpBoardAdapter(srv.url, as_="w1", env={"XDG_CONFIG_HOME": str(self.tmp / "nowhere")})
        self._closers.append(bare.close)
        with self.assertRaises(VBoardError) as cm:
            bare.whoami()
        self.assertEqual(cm.exception.code, "unauthorized")
        path = srv.write_token_file(self.xdg)
        self.assertEqual(path.parent, self.xdg / "board" / "local")
        self.assertEqual(vport.token_file_path(srv.url, {"XDG_CONFIG_HOME": str(self.xdg)}), path)
        p = HttpBoardAdapter(srv.url, as_="w1", env=dict(os.environ))
        self._closers.append(p.close)
        self.assertEqual(p.whoami()["profile"], "w1")
        self.assertEqual(p.actor_name, "w1")
        caps = p.capabilities()
        self.assertEqual((caps.backend, caps.conditional_needs, caps.priority_native, caps.batch_max),
                         ("http", True, True, 100))
        mid = p.post("work/rt", "hello")["message"]["id"]
        self.assertEqual(p.read(mid)["message"]["text"], "hello")
        self.assertEqual(p.read("work/rt")["threads"][0]["message"]["id"], mid)
        t = p.task_create("work/rt", "job", labels={"fusion.priority": "3"})
        got = p.task_claim_next("work/rt")
        self.assertEqual(got["task"]["id"], t["task"]["id"])
        self.assertNotIn("token", json.dumps(got))
        cache = ClaimCache()
        doc = cache.get(board_key(url=srv.url), t["task"]["id"], "w1")
        self.assertEqual(doc["epoch"], 1)
        self.assertEqual(stat.S_IMODE(cache.path(board_key(url=srv.url), t["task"]["id"], "w1").stat().st_mode), 0o600)
        self.assertEqual(p.task_renew(t["task"]["id"])["claim"]["epoch"], 1)
        self.assertEqual(p.task_complete(t["task"]["id"], "done")["outcome"], "completed")
        self.assertIsNone(cache.get(board_key(url=srv.url), t["task"]["id"], "w1"))
        self.assertIsNone(p.task_claim_next("work/rt"))
        cur = p.head()
        p.post("work/rt", "again")
        ev = p.wait("work/rt", after=cur, timeout=1)
        self.assertEqual(ev["type"], "message.posted")
        # a different worker cannot settle someone else's claim
        t2 = p.task_create("work/rt", "second", claim=True)
        other = HttpBoardAdapter(srv.url, as_="w2", env=dict(os.environ))
        self._closers.append(other.close)
        with self.assertRaises(VBoardError) as cm:
            other.task_complete(t2["task"]["id"])
        self.assertEqual(cm.exception.code, "claim_not_held")
        with self.assertRaises(VBoardError) as cm:
            other.task_claim(t2["task"]["id"])
        self.assertEqual(cm.exception.code, "task_claimed")

    def test_http_token_reread_after_engine_restart(self):
        srv = self.server()
        srv.write_token_file(self.xdg)
        p = HttpBoardAdapter(srv.url, as_="w1", env=dict(os.environ))
        self._closers.append(p.close)
        p.whoami()
        srv.token = "rotated-" + srv.token                       # engine restarted: new token, file rewritten
        srv.write_token_file(self.xdg)
        self.assertEqual(p.whoami()["profile"], "w1")

    def test_http_adapter_reconnect_after_idle_close(self):
        srv = self.server(idle_timeout_s=0.05)
        srv.write_token_file(self.xdg)
        p = HttpBoardAdapter(srv.url, as_="w1", env=dict(os.environ))
        self._closers.append(p.close)
        first = p.post("dev", "one", idempotency_key="idle-1")
        time.sleep(0.15)                                          # the server drops the idle keep-alive connection
        again = p.post("dev", "one", idempotency_key="idle-1")    # reconnects and retries; same key -> same message
        self.assertEqual(first["message"]["id"], again["message"]["id"])
        t = p.task_create("work/idle", "j", claim=True)
        time.sleep(0.15)
        self.assertEqual(p.task_renew(t["task"]["id"])["claim"]["epoch"], 1)
        time.sleep(0.15)
        self.assertEqual(p.task_complete(t["task"]["id"])["outcome"], "completed")
        # a socket killed under us (stale connection) is also survived once
        p.whoami()
        p._tl.conn.sock.shutdown(2)
        self.assertEqual(p.whoami()["profile"], "w1")

    def test_http_capabilities_without_fusion_extension(self):
        stock = DiscoveryOnlyServer()
        self._closers.append(stock.stop)
        p = HttpBoardAdapter(stock.url, as_="w1", token="t0k", env={"XDG_CONFIG_HOME": str(self.xdg)})
        caps = p.capabilities()
        self.assertEqual((caps.version, caps.priority_native, caps.conditional_needs, caps.batch_max,
                          caps.lease_cap_s), ("0.54.0", False, False, 15, 300))
        self.assertEqual(stock.hits[0][2], None)                  # discovery is unauthenticated
        with self.assertRaises(VBoardError) as cm:
            p.task_create("work/x", "t", needs=[{"id": "1", "if": "failed"}])
        self.assertEqual(cm.exception.code, "not_supported")
        advertised = DiscoveryOnlyServer({"fusion": {"conditional_needs": True, "priority": True, "defer": True,
                                                     "batch_max": 40}})
        self._closers.append(advertised.stop)
        caps = HttpBoardAdapter(advertised.url, as_="w1").capabilities()
        self.assertEqual((caps.conditional_needs, caps.priority_native, caps.batch_max), (True, True, 40))

    def test_http_claim_retry_same_token_is_success(self):
        srv = self.server()
        srv.write_token_file(self.xdg)
        p = HttpBoardAdapter(srv.url, as_="w1", env=dict(os.environ))
        self._closers.append(p.close)
        tid = p.task_create("work/tok", "job")["task"]["id"]
        with mock.patch.object(vport, "_new_token", return_value="T" * 43):
            first = p.task_claim(tid)
            retry = p.task_claim(tid)                              # the retry of a claim whose answer was lost
        self.assertEqual(first["claim"], retry["claim"])
        self.assertEqual(p.task_complete(tid)["outcome"], "completed")
        # another actor with another token is refused
        tid2 = p.task_create("work/tok", "job2")["task"]["id"]
        p.task_claim(tid2)
        rival = HttpBoardAdapter(srv.url, as_="w2", env=dict(os.environ))
        self._closers.append(rival.close)
        with self.assertRaises(VBoardError) as cm:
            rival.task_claim(tid2)
        self.assertEqual(cm.exception.code, "task_claimed")

    def test_http_batch_uses_discovered_batch_max(self):
        srv = self.server()
        srv.write_token_file(self.xdg)
        p = HttpBoardAdapter(srv.url, as_="w1", env=dict(os.environ))
        self._closers.append(p.close)
        sent: list = []
        real = p._batch_once
        p._batch_once = lambda ch, chunk, key: (sent.append((key, len(chunk))), real(ch, chunk, key))[1]
        out = p.task_create_batch("work/hb", [{"text": f"t{i}"} for i in range(130)], idempotency_key="HB")
        self.assertEqual((len(out["tasks"]), sent), (130, [("HB:0", 100), ("HB:100", 30)]))


# ===================================================================================== claim proofs and open_port
class ProofAndOpenTests(Base):
    def run_cli(self, *argv: str) -> tuple[int, dict | None, str]:
        out, err = io.StringIO(), io.StringIO()
        rc = vcli.main(list(argv), stdout=out, stderr=err, env={"XDG_CONFIG_HOME": str(self.xdg),
                                                                "FUSION_HOME": str(self.home), "HOME": str(self.tmp)})
        text = out.getvalue()
        return rc, (json.loads(text) if text.strip() else None), err.getvalue()

    def test_claim_proof_kept_and_cache_shared_with_cli(self):
        db = self.tmp / "shared.db"
        p = LocalEngineAdapter(db, as_="w1")
        self._closers.append(p.close)
        t1 = p.task_create("work/s", "one")["task"]["id"]
        t2 = p.task_create("work/s", "two")["task"]["id"]
        got = p.task_claim(t1)
        self.assertNotIn("token", json.dumps(got))
        cache = ClaimCache()
        board = board_key(db_path=db)
        path = cache.path(board, t1, "w1")
        self.assertTrue(path.exists())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(json.loads(path.read_text())["epoch"], got["claim"]["epoch"])
        # claimed via the port, completed via the CLI
        rc, doc, err = self.run_cli("--db", str(db), "--as", "w1", "--json", "tasks", "complete", t1, "via-cli")
        self.assertEqual((rc, doc["outcome"]), (0, "completed"), err)
        self.assertNotIn(json.loads(path.read_text() or "{}").get("token", "!") if path.exists() else "!", "")
        self.assertFalse(path.exists())
        # claimed via the CLI, completed via a fresh port (proof comes from the shared cache)
        rc, doc, err = self.run_cli("--db", str(db), "--as", "w1", "--json", "tasks", "claim", t2)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("token", doc["claim"])
        fresh = LocalEngineAdapter(db, as_="w1")
        self._closers.append(fresh.close)
        self.assertEqual(fresh.task_complete(t2, "via-port")["outcome"], "completed")
        # a different profile holds no claim
        t3 = p.task_create("work/s", "three")["task"]["id"]
        p.task_claim(t3)
        other = LocalEngineAdapter(db, as_="w2")
        self._closers.append(other.close)
        with self.assertRaises(VBoardError) as cm:
            other.task_complete(t3)
        self.assertEqual(cm.exception.code, "claim_not_held")
        self.assertEqual(p.task_complete(t3)["outcome"], "completed")
        # without the claim cache, proofs live only in memory
        mem = LocalEngineAdapter(self.tmp / "mem.db", as_="w1", claim_cache=False)
        self._closers.append(mem.close)
        tm = mem.task_create("work/m", "x", claim=True)["task"]["id"]
        self.assertFalse(any(cache.dir.glob("*.json")) and cache.get(board_key(db_path=self.tmp / "mem.db"), tm, "w1"))
        self.assertEqual(mem.task_complete(tm)["outcome"], "completed")

    def test_open_port_specs(self):
        p = open_port("local", as_="w1")
        self._closers.append(p.close)
        self.assertIsInstance(p, LocalEngineAdapter)
        self.assertTrue((self.home / "vboard" / "default.db").exists())
        explicit = open_port(f"local:{self.tmp / 'sub' / 'x.db'}", as_="w1")
        self._closers.append(explicit.close)
        self.assertEqual(explicit.store.path, str(self.tmp / "sub" / "x.db"))
        h = open_port("http://127.0.0.1:9/api/board", as_="w1")
        self.assertIsInstance(h, HttpBoardAdapter)
        self.assertEqual(h.url, "http://127.0.0.1:9/api/board")
        v = open_port("vercel", as_="w1")
        self.assertIsInstance(v, VercelCliAdapter)
        self.assertEqual((v.as_, v.name, v.url), ("w1", None, None))
        self.assertEqual(open_port("vercel:team-x", as_="w1").name, "team-x")
        with self.assertRaises(VBoardError) as cm:
            open_port("vercel")                                   # the vercel backend needs an identity
        self.assertEqual(cm.exception.code, "invalid_profile")
        for bad in ("ftp://x", "s3:bucket", "local:", "nonsense"):
            with self.assertRaises(ValueError):
                open_port(bad, as_="w1")
        # env: FUSION_VBOARD picks the spec; identity order is as_ > BOARD_PROFILE > FUSION_AGENT_ID
        os.environ["FUSION_VBOARD"] = "vercel:team-y"
        os.environ["FUSION_AGENT_ID"] = "agent-a"
        self.assertEqual(open_port().as_, "agent-a")
        os.environ["BOARD_PROFILE"] = "prof-b"
        self.assertEqual(open_port().as_, "prof-b")
        self.assertEqual(open_port(as_="explicit").as_, "explicit")


if __name__ == "__main__":
    unittest.main()
