"""Offline tests for `python -m fusion_swarm.vboard` (design/v4-spec.md section 2, judge J-06/J-07).

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_vboard_cli -v   (from the plugin root)
Every board lives in a temp dir; XDG_CONFIG_HOME and FUSION_HOME point there too. No network except one loopback
fake server, no sleeps over 0.2 s.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "python"))

from fusion_swarm.vboard import BoardStore, VBoardError  # noqa: E402
from fusion_swarm.vboard.cli import main  # noqa: E402
from fusion_swarm.vboard.model import Caller  # noqa: E402


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.env = {"FUSION_HOME": self.tmp, "XDG_CONFIG_HOME": os.path.join(self.tmp, "xdg"), "HOME": self.tmp}
        self.db = os.path.join(self.tmp, "vboard", "default.db")

    def run_cli(self, *argv, env=None, **hooks):
        out, err = io.StringIO(), io.StringIO()
        rc = main(list(argv), stdout=out, stderr=err, env={**self.env, **(env or {})}, **hooks)
        return rc, out.getvalue(), err.getvalue()

    def ok(self, *argv, **hooks) -> dict:
        rc, out, err = self.run_cli(*argv, "--json", **hooks)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def fail(self, *argv, code=1, **hooks) -> dict:
        rc, out, err = self.run_cli(*argv, "--json", **hooks)
        self.assertEqual(rc, code, (out, err))
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        return json.loads(err)


class ShapesAndFailures(CliCase):
    def test_post_read_reply_json_shapes(self):
        posted = self.ok("--as", "alice", "post", "work/demo", "hello @alice", "--label", "k=v")
        self.assertEqual(set(posted), {"message", "outcome", "mentions"})
        self.assertEqual(posted["outcome"], "posted")
        mid = posted["message"]["id"]
        reply = self.ok("reply", mid, "second", "--as", "bob")  # global flag after the verb
        self.assertEqual(set(reply), {"message", "outcome", "mentions"})
        self.assertEqual(reply["message"]["by"]["profile"], "bob")
        page = self.ok("read", "work/demo")
        self.assertEqual(set(page), {"threads", "cursor", "older"})
        self.assertEqual(page["threads"][0]["message"]["id"], mid)
        view = self.ok("read", f"#{mid}")
        self.assertEqual(set(view), {"message", "replies", "cursor", "schedules"})
        self.assertEqual(len(view["replies"]), 1)
        self.assertEqual(set(self.ok("whoami", "--as", "alice")),
                         {"actor", "principal", "kind", "handle", "name", "avatar", "profile", "can", "board",
                          "source"})
        self.assertEqual(set(self.ok("channels")), {"channels", "next"})
        self.assertEqual(set(self.ok("actors")), {"actors"})
        self.assertEqual(self.ok("channels", "create", "work/fresh")["outcome"], "created")
        self.assertEqual(self.ok("channels", "create", "work/fresh")["outcome"], "unchanged")
        for argv, outcome in ((("edit", mid, "changed"), "edited"), (("react", mid, "eyes"), "reacted"),
                              (("react", mid, "eyes", "--remove"), "unreacted"),
                              (("label", mid, "a=b", "--unset", "k"), "labeled")):
            doc = self.ok("--as", "alice", *argv)
            self.assertEqual(set(doc), {"message", "outcome"}, argv)
            self.assertEqual((doc["outcome"], list(doc["message"])), (outcome, ["id"]))
        self.assertEqual(self.ok("--as", "alice", "delete", mid)["outcome"], "deleted")
        text_in = self.ok("post", "work/demo", "--as", "alice", "--idempotency-key", "k1", "once")
        again = self.ok("post", "work/demo", "--as", "alice", "--idempotency-key", "k1", "once")
        self.assertEqual(text_in["message"]["id"], again["message"]["id"])
        self.assertEqual(self.ok("--version"), {"version": __import__("fusion_swarm.vboard", fromlist=["x"]).VERSION})

    def test_failure_envelope_and_exit_codes(self):
        doc = self.fail("read", "999")
        self.assertEqual(doc["status"], "error")
        self.assertEqual(doc["reason"], "message_not_found")
        self.assertEqual(doc["data"]["error"]["status"], 404)
        self.assertEqual(doc["data"]["error"]["code"], "message_not_found")
        self.assertIs(doc["data"]["retryable"], False)
        self.assertIsInstance(doc["next"], list)
        doc = self.fail("post", "--bogus", "work/x", "hi", code=2)
        self.assertEqual(doc["reason"], "unknown_option")
        self.assertEqual(self.fail("frobnicate", code=2)["reason"], "unknown_command")
        self.assertEqual(self.fail("post", "work/x", code=2)["reason"], "missing_arguments")
        self.assertEqual(self.fail("--as", "9bad", "whoami", code=2)["reason"], "invalid_profile")
        self.assertEqual(self.fail("--db", "a.db", "--url", "http://127.0.0.1:1/x", "whoami", code=2)["reason"],
                         "conflicting_options")
        self.assertEqual(self.fail("post", "work/x", "t", "--label", "board.task=x", code=2)["reason"],
                         "reserved_label")
        rc, out, err = self.run_cli("read", "999")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: "))

    def test_unsupported_verbs_not_supported(self):
        for verb in ("grants", "schemas", "schedule", "schedules", "typing", "sync", "inbox", "sources", "download",
                     "hooks", "inspect"):
            self.assertEqual(self.fail(verb)["reason"], "not_supported", verb)
        import importlib.util
        for verb, mod in (("serve", "http"), ("conformance", "conformance")):
            if importlib.util.find_spec(f"fusion_swarm.vboard.{mod}") is None:
                doc = self.fail(verb, code=2)
                self.assertEqual(doc["reason"], "not_supported")
                self.assertTrue(doc.get("hint"))

    def test_sweep_and_tasks_verbs_shapes(self):
        self.assertEqual(set(self.ok("sweep")), {"expired", "readied", "canceled"})
        created = self.ok("tasks", "create", "work/q", "first", "--label", "a=b")
        self.assertEqual(set(created), {"task", "outcome"})
        tid = created["task"]["id"]
        dep = self.ok("tasks", "create", "work/q", "second", "--needs", tid)
        self.assertEqual(dep["task"]["task"]["state"], "waiting")
        listed = self.ok("tasks", "work/q", "--state", "all")
        self.assertEqual(set(listed), {"tasks", "cursor"})
        self.assertEqual(len(listed["tasks"]), 2)
        self.assertEqual(self.ok("tasks", "needs", dep["task"]["id"], "--none")["outcome"], "set")
        self.assertEqual(self.ok("tasks", "assign", tid, "--pool")["outcome"], "unchanged")
        self.assertEqual(self.ok("tasks", "cancel", tid, "no longer needed")["outcome"], "canceled")
        self.assertEqual(self.ok("tasks", "reopen", dep["task"]["id"])["outcome"], "unchanged")


class ClaimCache(CliCase):
    def claim_file(self, board_db, task, profile):
        payload = json.dumps(["file://" + os.path.abspath(board_db), task, profile], ensure_ascii=False,
                             separators=(",", ":"))
        name = hashlib.sha256(payload.encode()).hexdigest()[:32] + ".json"
        return os.path.join(self.env["XDG_CONFIG_HOME"], "board", "claims", name)

    def test_claim_cache_layout_and_permissions(self):
        tid = self.ok("tasks", "create", "work/c", "job")["task"]["id"]
        rc, out, err = self.run_cli("--as", "Worker-1", "tasks", "claim", tid, "--json", "--lease", "5m")
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertEqual(set(doc), {"task", "claim"})
        self.assertEqual(set(doc["claim"]), {"epoch", "until"})
        path = self.claim_file(self.db, tid, "worker-1")
        self.assertTrue(os.path.exists(path), os.listdir(os.path.dirname(path)))
        with open(path) as fh:
            saved = json.load(fh)
        self.assertEqual(set(saved), {"board", "task", "profile", "epoch", "token", "until"})
        self.assertEqual((saved["task"], saved["profile"], saved["epoch"]), (tid, "worker-1", 1))
        self.assertEqual(saved["board"], "file://" + os.path.abspath(self.db))
        self.assertNotIn(saved["token"], out + err)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")], [])
        renewed = self.ok("--as", "worker-1", "tasks", "renew", tid, "--lease", "300")  # bare seconds accepted
        self.assertEqual(set(renewed), {"task", "claim"})
        with open(path) as fh:
            self.assertEqual(json.load(fh)["until"], renewed["claim"]["until"])
        done = self.ok("--as", "worker-1", "tasks", "complete", tid, "all good")
        self.assertEqual(done["outcome"], "completed")
        self.assertEqual(done["task"]["task"]["result"], "all good")
        self.assertFalse(os.path.exists(path), "settle drops the cache file")
        self.assertEqual(self.fail("tasks", "create", "work/c", "x", "--lease", "5m", code=2)["reason"],
                         "invalid_request")

    def test_claim_not_held_for_other_profile(self):
        tid = self.ok("tasks", "create", "work/c", "job")["task"]["id"]
        self.ok("--as", "alice", "tasks", "claim", tid)
        for who in (["--as", "bob"], []):
            doc = self.fail(*who, "tasks", "complete", tid, "x")
            self.assertEqual(doc["reason"], "claim_not_held")
            self.assertEqual(doc["data"]["error"]["status"], 409)
            self.assertIn(f"tasks claim {tid}", doc["next"][0]["command"])
            self.assertEqual(doc["next"][0]["when"], "Claim it")
        self.assertEqual(self.ok("--as", "ALICE", "tasks", "complete", tid)["outcome"], "completed")

    def test_claim_ready_and_nothing_ready(self):
        self.assertEqual(self.fail("--as", "w", "tasks", "claim", "--ready", "work/z")["reason"], "nothing_ready")
        self.ok("tasks", "create", "work/z", "j1", "--label", "kind=a")
        doc = self.ok("--as", "w", "tasks", "claim", "--ready", "work/", "--label", "kind=a")
        self.assertEqual(doc["task"]["text"], "j1")
        doc = self.fail("--as", "w", "tasks", "claim", "--ready", "work/z", "--wait", "0.1s")
        self.assertEqual(doc["reason"], "timeout")


class FailingStore(BoardStore):
    """Refuses batches over `limit` tasks and can fail the Nth batch call once (shared state in class attrs)."""
    limit = 40
    calls = 0
    fail_call = 0
    client_ids: list = []

    def create_tasks(self, caller, channel, tasks, *, client_id=None):
        type(self).calls += 1
        if len(tasks) > type(self).limit:
            raise VBoardError("batch_too_large", "too many")
        if type(self).calls == type(self).fail_call:
            raise VBoardError("unavailable", "try again")
        type(self).client_ids.append((client_id, len(tasks)))
        return super().create_tasks(caller, channel, tasks, client_id=client_id)


class PlanFile(CliCase):
    def write_plan(self, n):
        plan = [{"key": f"k{i}", "text": f"task {i}", **({"needs": [{"key": "k0"}]} if i >= 120 else {})}
                for i in range(n)]
        path = os.path.join(self.tmp, "plan.json")
        with open(path, "w") as fh:
            json.dump({"tasks": plan}, fh)
        return path

    def factory(self, path, name):
        return FailingStore(path, name=name)

    def test_tasks_create_file_plan_chunking_and_resume(self):
        plan = self.write_plan(150)
        FailingStore.calls, FailingStore.fail_call, FailingStore.client_ids = 0, 0, []
        doc = self.ok("--as", "lead", "tasks", "create", "work/plan", "--file", plan, "--idempotency-key", "run1",
                      store_factory=self.factory)
        self.assertEqual((doc["outcome"], len(doc["tasks"])), ("created", 150))
        sizes = {n for _, n in FailingStore.client_ids}
        self.assertLessEqual(max(sizes), 40, "halved until the store accepted the batch")
        self.assertEqual(FailingStore.client_ids[0][0], "run1:0")
        self.assertIn("run1:25", [c for c, _ in FailingStore.client_ids])
        # a key from chunk 0 is rewritten to the created id for later chunks
        dependent = doc["tasks"][130]
        self.assertEqual(dependent["task"]["needs"], [doc["tasks"][0]["id"]])
        # resume after a mid-run failure: same key, no duplicates
        shutil_db = os.path.join(self.tmp, "vboard", "default.db")
        os.unlink(shutil_db)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(shutil_db + suffix):
                os.unlink(shutil_db + suffix)
        FailingStore.calls, FailingStore.fail_call, FailingStore.client_ids = 0, 6, []
        bad = self.fail("--as", "lead", "tasks", "create", "work/plan", "--file", plan, "--idempotency-key", "run2",
                        store_factory=self.factory)
        self.assertEqual(bad["reason"], "unavailable")
        self.assertTrue(bad["data"]["retryable"])
        FailingStore.fail_call = 0
        doc = self.ok("--as", "lead", "tasks", "create", "work/plan", "--file", plan, "--idempotency-key", "run2",
                      store_factory=self.factory)
        self.assertEqual(len(doc["tasks"]), 150)
        listed = self.ok("tasks", "work/plan", "--state", "all", "--limit", "500", store_factory=self.factory)
        self.assertEqual(len(listed["tasks"]), 150, "resume must not duplicate chunks that already landed")

    def test_plan_small_uses_bare_key(self):
        plan = self.write_plan(3)
        FailingStore.calls, FailingStore.fail_call, FailingStore.client_ids = 0, 0, []
        doc = self.ok("tasks", "create", "work/p", "--file", plan, "--idempotency-key", "solo",
                      store_factory=self.factory)
        self.assertEqual(len(doc["tasks"]), 3)
        self.assertEqual(FailingStore.client_ids, [("solo", 3)])


class WaitAndRun(CliCase):
    def test_wait_timeout_reason_timeout_exit1(self):
        self.ok("post", "work/w", "seed")
        doc = self.fail("wait", "work/w", "--timeout", "0.1s")
        self.assertEqual(doc["reason"], "timeout")
        self.assertIn("Nothing matched within 0.1s", doc["message"])
        self.assertEqual(doc["data"]["error"]["status"], 408)
        got = self.ok("wait", "work/w", "--after", "0", "--timeout", "1s")
        self.assertEqual(set(got), {"event"})
        self.assertIn("type", got["event"])

    def test_tasks_run_completes_and_fails_by_exit_code(self):
        marker = os.path.join(self.tmp, "seen.txt")
        tid = self.ok("tasks", "create", "work/r", "ok job")["task"]["id"]
        code = ("import os,sys; open(sys.argv[1],'w').write(os.environ['BOARD_TASK']); print('child-says-hi')")
        rc, out, err = self.run_cli("--as", "runner", "tasks", "run", tid, "--result", "fine", "--json", "--",
                                    sys.executable, "-c", code, marker, renew_floor_s=0.05)
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertEqual(set(doc), {"task", "outcome", "code"})
        self.assertEqual((doc["outcome"], doc["code"]), ("completed", 0))
        self.assertEqual(doc["task"]["task"]["result"], "fine")
        with open(marker) as fh:
            self.assertEqual(fh.read(), tid)
        self.assertIn("child-says-hi", err, "child stdout goes to stderr under --json")
        self.assertNotIn("child-says-hi", out)
        tid2 = self.ok("tasks", "create", "work/r", "bad job")["task"]["id"]
        rc, out, err = self.run_cli("--as", "runner", "tasks", "run", tid2, "--json", "--", sys.executable, "-c",
                                    "import sys; sys.exit(3)", renew_floor_s=0.05)
        self.assertEqual(rc, 3, err)
        doc = json.loads(out)
        self.assertEqual((doc["outcome"], doc["code"]), ("failed", 3))
        self.assertEqual(doc["task"]["task"]["state"], "failed")
        self.assertEqual(doc["task"]["task"]["reason"], "exited 3")
        # --ready picks the oldest ready task
        tid3 = self.ok("tasks", "create", "work/r2", "ready job")["task"]["id"]
        rc, out, err = self.run_cli("--as", "runner", "tasks", "run", "--ready", "work/r2", "--json", "--",
                                    sys.executable, "-c", "pass", renew_floor_s=0.05)
        self.assertEqual((rc, json.loads(out)["task"]["id"]), (0, tid3), err)
        # a missing command releases the task
        tid4 = self.ok("tasks", "create", "work/r", "no cmd")["task"]["id"]
        doc = self.fail("--json", "--as", "runner", "tasks", "run", tid4, "--", "definitely-not-a-command-xyz")
        self.assertEqual(doc["reason"], "command_not_found")
        self.assertEqual(self.ok("tasks", "work/r", "--state", "ready")["tasks"][0]["message"]["id"], tid4)

    def test_tasks_run_claim_lost_terminates_child(self):
        clock = Clock()
        path = self.db
        store_for_test = BoardStore(path, clock=clock)
        self.addCleanup(store_for_test.close)
        tid = self.ok("tasks", "create", "work/lost", "long job", store_factory=lambda p, n: BoardStore(p, clock=clock),
                      clock=clock)["task"]["id"]
        ticks = []

        def sleeper(seconds):
            ticks.append(seconds)
            if len(ticks) == 1:
                clock.t += 100  # the 30 s lease lapses ...
                store_for_test.claim(Caller.local("thief"), tid)  # ... and someone else takes the task

        rc, out, err = self.run_cli("--as", "slow", "tasks", "run", tid, "--lease", "30s", "--json", "--",
                                    sys.executable, "-c", "import time; time.sleep(30)",
                                    store_factory=lambda p, n: BoardStore(p, clock=clock), clock=clock, sleep=sleeper,
                                    renew_floor_s=0.01)
        self.assertEqual(rc, 1, err)
        doc = json.loads(err.strip().splitlines()[-1])
        self.assertEqual(doc["reason"], "claim_lost")
        self.assertIs(doc["data"]["retryable"], False)
        self.assertEqual(out, "")
        self.assertEqual(ticks[0], 10.0, "interval is max(floor, lease/3)")
        cache_dir = os.path.join(self.env["XDG_CONFIG_HOME"], "board", "claims")
        self.assertEqual([n for n in os.listdir(cache_dir) if n.endswith(".json")], [], "claim_lost drops the cache")
        view = store_for_test.thread(Caller.local("thief"), tid)["message"]["task"]
        self.assertEqual(view["state"], "claimed")
        self.assertIn("thief", view["claim"]["actor"])


class SubprocessSmoke(CliCase):
    def test_subprocess_smoke_whoami_as_profile(self):
        env = {**os.environ, **self.env, "PYTHONPATH": os.path.join(_ROOT, "python"), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("BOARD_PROFILE", None)
        env.pop("FUSION_AGENT_ID", None)
        proc = subprocess.run([sys.executable, "-m", "fusion_swarm.vboard", "whoami", "--as", "Smoke-Test", "--json"],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["profile"], "smoke-test")
        self.assertTrue(doc["actor"].endswith("+smoke-test"))
        env["FUSION_AGENT_ID"] = "claude-fv4-vbcli"
        proc = subprocess.run([sys.executable, "-m", "fusion_swarm.vboard", "whoami", "--json"],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(json.loads(proc.stdout)["profile"], "claude-fv4-vbcli", "FUSION_AGENT_ID is the fallback")
        bad = subprocess.run([sys.executable, "-m", "fusion_swarm.vboard", "--nope", "--json"],
                             capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(json.loads(bad.stderr)["reason"], "unknown_option")


class UrlClient(CliCase):
    def test_url_mode_sends_profile_and_token_headers(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def _send(self, status, doc):
                body = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                seen.append((self.path, self.headers.get("x-board-profile"), self.headers.get("authorization")))
                if self.path.endswith("/me"):
                    self._send(200, {"actor": "a+b", "principal": "a", "kind": "agent", "handle": "a/b", "name": "a/b",
                                     "avatar": None, "profile": "b", "can": ["read"]})
                elif self.path.endswith("/messages/7"):
                    self._send(404, {"error": {"code": "message_not_found", "message": "no message 7"}})
                else:
                    self._send(200, {})

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length))
                seen.append((self.path, body))
                self._send(201, {"message": {"id": "1", "text": body["text"]}, "mentions": []})

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(2)))
        url = f"http://127.0.0.1:{server.server_port}/api/board"
        doc = self.ok("--url", url, "--as", "Bee", "whoami", env={"BOARD_TOKEN": "tok-123"})
        self.assertEqual(doc["profile"], "b")
        self.assertEqual(seen[0][1:], ("bee", "Bearer tok-123"))
        doc = self.ok("--url", url, "post", "work/u", "over http", env={"BOARD_TOKEN": "tok-123"})
        self.assertEqual(doc["outcome"], "posted")
        self.assertEqual(seen[-1], ("/api/board/messages", {"text": "over http", "channel": "work/u"}))
        err = self.fail("--url", url, "read", "7")
        self.assertEqual((err["reason"], err["data"]["error"]["status"]), ("message_not_found", 404))
        dead = self.fail("--url", "http://127.0.0.1:9/api/board", "whoami")
        self.assertEqual(dead["reason"], "network_error")
        self.assertIs(dead["data"]["retryable"], True)


if __name__ == "__main__":
    unittest.main()
