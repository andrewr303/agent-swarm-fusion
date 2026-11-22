"""peers-acp: ACP v1 client, durable board handoff and the peers CLI.

Fake agent only (tests/fixtures/fake_acp_agent.py, tests/fixtures/fake_peer_cli.py): no real CLIs, no network, temp dirs.
Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_peers_acp -v   (from the plugin root)
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from fusion_swarm.peers import acp, handoff  # noqa: E402
from fusion_swarm.peers import __main__ as peers_cli  # noqa: E402
from fusion_swarm.peers.acp import AcpClient, AcpError  # noqa: E402
from fusion_swarm.peers.policy import PeerError  # noqa: E402
from fusion_swarm.vboard.model import VBoardError  # noqa: E402
from fusion_swarm.vboard.port import LocalEngineAdapter  # noqa: E402

FAKE_ACP = ROOT / "tests" / "fixtures" / "fake_acp_agent.py"
FAKE_PEER = ROOT / "tests" / "fixtures" / "fake_peer_cli.py"
POSIX = os.name == "posix"


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="peers-acp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = Path(self.tmp, "bin")
        self.bin.mkdir()
        self.log = Path(self.tmp, "acp.jsonl")
        self.calls = Path(self.tmp, "calls.txt")
        self.work = Path(self.tmp, "work")
        self.work.mkdir()
        self._clients: list = []
        self.addCleanup(self._close_clients)

    def _close_clients(self):
        for c in self._clients:
            c.close()

    def make_bins(self):
        """andrewcode -> fake ACP agent; codex / codex-acp / claude-agent-acp record any invocation."""
        ac = self.bin / "andrewcode"
        ac.write_text(f"#!{sys.executable}\nimport os, sys\n"
                      f"os.execv(sys.executable, [sys.executable, {str(FAKE_ACP)!r}, *sys.argv[1:]])\n", "utf-8")
        ac.chmod(0o755)
        for name in ("codex", "codex-acp", "claude-agent-acp"):
            p = self.bin / name
            p.write_text(f"#!/bin/sh\necho {name} >> {self.calls}\nexit 9\n", "utf-8")
            p.chmod(0o755)

    def env(self, **extra):
        e = {"PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin", "FUSION_HOME": str(Path(self.tmp, "home")),
             "XDG_CONFIG_HOME": str(Path(self.tmp, "xdg")), "FAKE_ACP_LOG": str(self.log)}
        e.update(extra)
        return e

    def client(self, mode="", **kw):
        env = self.env(FAKE_ACP_MODE=mode, **kw.pop("env_extra", {}))
        c = AcpClient([sys.executable, str(FAKE_ACP)], env=env, cwd=str(self.work), **kw)
        self._clients.append(c)
        return c

    def sent(self):
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text("utf-8").splitlines() if x.strip()]

    def received(self, method):
        return [r["recv"] for r in self.sent() if r.get("recv", {}).get("method") == method]

    def session(self, c):
        c.start()
        return c.new_session(str(self.work))["sessionId"]


# ---------------------------------------------------------------------------------------------- ACP client
class AcpClientTests(Sandbox):
    def test_acp_negotiates_v1_and_streams_text(self):
        seen = []
        c = self.client(on_update=seen.append)
        res = c.start()
        self.assertEqual(res["protocolVersion"], 1)
        init = self.received("initialize")[0]["params"]
        self.assertEqual(init["protocolVersion"], 1)
        self.assertEqual(init["clientCapabilities"], {"fs": {"readTextFile": False, "writeTextFile": False},
                                                      "terminal": False})
        self.assertEqual(init["clientInfo"]["name"], "fusion")
        sess = c.new_session(str(self.work))
        self.assertEqual(sess["sessionId"], "sess-1")
        self.assertEqual(self.received("session/new")[0]["params"]["cwd"], str(self.work))
        c.set_config("sess-1", "model", "gpt-6.1-sol")
        self.assertEqual(self.received("session/set_config_option")[0]["params"],
                         {"sessionId": "sess-1", "configId": "model", "value": "gpt-6.1-sol"})
        turn = c.prompt("sess-1", "say hi")
        self.assertEqual(self.received("session/prompt")[0]["params"]["prompt"], [{"type": "text", "text": "say hi"}])
        self.assertEqual(turn.stop_reason, "end_turn")
        self.assertEqual(turn.text, "Hello, world")
        self.assertEqual(len(turn.tool_calls), 1)
        self.assertEqual(turn.tool_calls[0]["toolCallId"], "t1")
        self.assertEqual(turn.tool_calls[0]["status"], "completed")  # tool_call_update merged
        self.assertEqual(turn.tool_calls[0]["kind"], "read")
        self.assertEqual(turn.updates, 6)  # 3 chunks + tool_call + tool_call_update + thought
        self.assertEqual(len(seen), 6)
        self.assertGreaterEqual(c.ignored_notifications, 1)  # available_commands_update outside a turn
        c.close()
        c.close()  # idempotent
        self.assertIsNotNone(c.proc.poll())

    def test_acp_negotiation_down_and_v2_refused(self):
        c = self.client(protocol_version=2)  # we offer 2, the agent answers 1: accepted
        self.assertEqual(c.start()["protocolVersion"], 1)
        self.assertEqual(self.received("initialize")[0]["params"]["protocolVersion"], 2)
        c.close()
        c2 = self.client("v2")  # an agent insisting on v2 is refused (only v1 is implemented)
        with self.assertRaises(AcpError) as cm:
            c2.start()
        self.assertEqual(cm.exception.code, "peer_failed")
        self.assertIn("v1", str(cm.exception))

    def test_acp_ignores_non_json_stdout_lines(self):
        c = self.client("badline")
        c.start()
        self.assertEqual(c.bad_lines, 1)

    def test_acp_permission_policy_deny_and_allow_read(self):
        rows = [("deny", {}, "perm_read=cancelled perm_edit=cancelled"),
                ("allow_read", {}, "perm_read=allow-1 perm_edit=cancelled"),
                ("allow_all", {"allow_all": True}, "perm_read=allow-1 perm_edit=allow-1")]
        for permission, kw, expected in rows:
            with self.subTest(permission=permission):
                c = self.client("perm_read,perm_edit", permission=permission, **kw)
                turn = c.prompt(self.session(c), "go")
                self.assertTrue(turn.text.endswith(expected), turn.text)
                self.assertEqual([d["tool_kind"] for d in c.permission_log], ["read", "edit"])
                c.close()
        with self.assertRaises(AcpError) as cm:  # allow_all needs the explicit flag
            AcpClient([sys.executable, str(FAKE_ACP)], permission="allow_all")
        self.assertEqual(cm.exception.code, "invalid_request")
        with self.assertRaises(AcpError):
            AcpClient([sys.executable, str(FAKE_ACP)], permission="yolo")

    def test_acp_fs_requests_rejected(self):
        c = self.client("fs")
        turn = c.prompt(self.session(c), "go")
        self.assertIn("fs_err=-32601 term_err=-32601", turn.text)
        self.assertEqual(turn.stop_reason, "end_turn")

    def test_acp_cancel_turn_and_close_kills_group(self):
        pidfile = Path(self.tmp, "child.pid")
        started = threading.Event()
        c = self.client("cancel,spawn_child", env_extra={"FAKE_ACP_PIDFILE": str(pidfile)},
                        on_update=lambda u: started.set())
        sid = self.session(c)

        def canceller():
            if started.wait(5):
                c.cancel_turn(sid)

        t = threading.Thread(target=canceller, daemon=True)
        t.start()
        turn = c.prompt(sid, "long job")
        t.join(5)
        self.assertEqual(turn.stop_reason, "cancelled")
        self.assertEqual(turn.text, "working")
        self.assertEqual(len(self.received("session/cancel")), 1)
        pid = int(pidfile.read_text())
        os.kill(pid, 0)  # grandchild alive while the session is open
        c.close()
        if POSIX:
            deadline = time.monotonic() + 3
            alive = True
            while alive and time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                    time.sleep(0.05)
                except ProcessLookupError:
                    alive = False
            self.assertFalse(alive, "grandchild survived close()")
        self.assertIsNotNone(c.proc.poll())

    def test_acp_external_cancel_object_sends_session_cancel(self):
        ev = threading.Event()
        c = self.client("cancel", cancel=ev, on_update=lambda u: ev.set())
        turn = c.prompt(self.session(c), "go")
        self.assertEqual(turn.stop_reason, "cancelled")
        self.assertEqual(len(self.received("session/cancel")), 1)

    def test_acp_timeout(self):
        c = self.client("hang", timeout=0.4)
        sid = self.session(c)
        t0 = time.monotonic()
        with self.assertRaises(AcpError) as cm:
            c.prompt(sid, "go")
        self.assertEqual(cm.exception.code, "timeout")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIsNotNone(c.proc.poll())  # closed, process group killed
        with self.assertRaises(AcpError):
            c.prompt(sid, "again")  # closed clients refuse

    def test_acp_agent_exit_error_is_redacted(self):
        c = self.client("exit_early")
        with self.assertRaises(AcpError) as cm:
            c.start()
        self.assertEqual(cm.exception.code, "peer_failed")
        msg = str(cm.exception)
        self.assertNotIn("hunter2value", msg)
        self.assertNotIn("FAKESECRET", msg)
        self.assertIn("[redacted]", msg)

    def test_thinking_mapping(self):
        for given, want in (("xhigh", "max"), ("ultra", "max"), ("max", "max"), ("high", "high"), ("low", "low"),
                            ("off", "off"), (None, None), ("", None)):
            self.assertEqual(acp.thinking_value(given), want, given)
        with self.assertRaises(AcpError):
            acp.thinking_value("galaxy")


# ---------------------------------------------------------------------------------------------- policy gate
class ConnectPolicyTests(Sandbox):
    def calls_made(self):
        return self.calls.read_text("utf-8").split() if self.calls.exists() else []

    def test_connect_respects_policy_codex(self):
        self.make_bins()
        claude_host = self.env(CLAUDECODE="1", FUSION_HOST="codex", FAKE_ACP_MODE="")  # legacy codex default
        cl = acp.connect("codex", env=claude_host)
        self._clients.append(cl)
        self.assertEqual([os.path.basename(cl.argv[0]), *cl.argv[1:]], ["andrewcode", "acp"])
        self.assertEqual(cl.transport, "andrewcode-acp")
        self.assertEqual(cl.default_model, "gpt-6.1-sol")
        # full flow through the codex-model seat: model set, ultra -> max, real codex binaries never touched
        res = acp.ask("codex", "hi there", env=claude_host, thinking="ultra", name="fusion-test")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["transport"], "andrewcode-acp")
        self.assertEqual(res["text"], "Hello, world")
        self.assertEqual(res["model"], "gpt-6.1-sol")
        sets = [r["params"] for r in self.received("session/set_config_option")]
        self.assertEqual(sets, [{"sessionId": "sess-1", "configId": "model", "value": "gpt-6.1-sol"},
                                {"sessionId": "sess-1", "configId": "thinking", "value": "max"}])
        startup = [r for r in self.sent() if "argv" in r][-1]
        self.assertEqual(startup["argv"], ["acp"])
        self.assertEqual(startup["env"]["FUSION_AGENT_ID"], "fusion-test")
        self.assertEqual(startup["env"]["FUSION_HOST"], "claude")  # not "codex": stripped markers stay a claude host
        self.assertIsNone(startup["env"]["CLAUDECODE"])
        self.assertEqual(self.calls_made(), [])
        # permission advisory is surfaced for andrewcode
        res = acp.ask("andrewcode", "hi", env=claude_host, permission="allow_read")
        self.assertTrue(any("advisory" in w for w in res["warnings"]))
        # the codex seat is the only route; claude on a Claude host is host-native (not spawnable)
        with self.assertRaises(PeerError) as cm:
            acp.connect("claude", env=claude_host)
        self.assertEqual(cm.exception.code, "peer_not_allowed")
        with self.assertRaises(PeerError) as cm:
            acp.connect("opencode", env=claude_host)
        self.assertEqual(cm.exception.code, "invalid_request")
        self.assertEqual(self.calls_made(), [])

    def test_real_codex_spawn_refused_without_gate(self):
        self.make_bins()
        env = self.env(CLAUDECODE="1")
        for argv in ([str(self.bin / "codex-acp")], [str(self.bin / "codex"), "acp"], ["codex-acp"],
                     ["/usr/bin/env", "codex-acp"], [sys.executable, "-c", "pass", "codex"]):
            with self.subTest(argv=argv):
                c = AcpClient(argv, env=env)
                with self.assertRaises(PeerError) as cm:
                    c.start()
                self.assertEqual(cm.exception.code, "peer_not_allowed")
        self.assertEqual(self.calls_made(), [])
        for launcher in ("npx", "/usr/local/bin/npx"):
            with self.assertRaises(PeerError) as cm:
                AcpClient([launcher, "-y", "@zed-industries/codex-acp"], env=env).start()
            self.assertEqual(cm.exception.code, "peer_not_allowed")
        # connect() refuses an absent andrewcode instead of falling back to anything else
        with self.assertRaises(PeerError) as cm:
            acp.connect("andrewcode", env={"PATH": str(Path(self.tmp, "empty")), "CLAUDECODE": "1"})
        self.assertEqual(cm.exception.code, "peer_absent")

    def test_codex_acp_only_when_gate_open_and_on_path(self):
        self.make_bins()
        gated = self.env(FUSION_ALLOW_CODEX_EXEC="1", CLAUDECODE="1")
        cl = acp.connect("codex", env=gated)  # unstarted: nothing is executed here
        self.assertEqual(os.path.basename(cl.argv[0]), "codex-acp")
        self.assertEqual(cl.transport, "codex-acp")
        host = self.env(FUSION_HOST="codex")
        self.assertEqual(os.path.basename(acp.connect("codex", env=host).argv[0]), "codex-acp")
        claude = self.env(FUSION_ALLOW_CLAUDE="1", CLAUDECODE="1")
        self.assertEqual(os.path.basename(acp.connect("claude", env=claude).argv[0]), "claude-agent-acp")
        self.assertEqual(self.calls_made(), [])


# ---------------------------------------------------------------------------------------------- handoff
def make_boards(tmp, *names):
    db = str(Path(tmp, "board.db"))
    return [LocalEngineAdapter(db, as_=n, claim_cache=False) for n in names]


class HandoffTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.claude, self.worker = make_boards(self.tmp, "claude", "andrewcode")
        self.addCleanup(self.claude.close)
        self.addCleanup(self.worker.close)

    def serve_in_thread(self, handler, port=None, **kw):
        box = {}

        def run():
            box["job"] = handoff.serve_one(port or self.worker, "work/peers", handler, **kw)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, box

    def test_handoff_submit_wait_serve_roundtrip_local_port(self):
        job = handoff.submit(self.claude, "work/peers", "Review the diff", to="andrewcode",
                             payload={"files": ["a.py"]}, idempotency_key="k-1", labels={"run": "r1"})
        self.assertIn(job.state, ("ready", "waiting"))
        again = handoff.submit(self.claude, "work/peers", "Review the diff", to="andrewcode",
                               payload={"files": ["a.py"]}, idempotency_key="k-1", labels={"run": "r1"})
        self.assertEqual(again.job_id, job.job_id)  # idempotent
        view = self.claude.read(job.job_id)["message"]
        self.assertEqual(view["labels"]["fusion.handoff"], "1")
        self.assertEqual(view["labels"]["run"], "r1")
        self.assertIn('"files"', view["text"])
        self.assertIn("```json", view["text"])
        self.assertEqual(handoff.get_result(self.claude, job.job_id), None)  # still open
        seen = {}

        def handler(task):
            seen["text"] = task["text"]
            return "LGTM: " + task["text"].splitlines()[0]

        t, box = self.serve_in_thread(handler)
        done = handoff.wait(self.claude, job.job_id, timeout=10)
        t.join(10)
        self.assertEqual(done.state, "completed")
        self.assertEqual(done.result, "LGTM: Review the diff")
        self.assertEqual(box["job"].state, "completed")
        self.assertEqual(box["job"].job_id, job.job_id)
        self.assertEqual(seen["text"].splitlines()[0], "Review the diff")
        self.assertEqual(handoff.get_result(self.claude, job.job_id), "LGTM: Review the diff")
        self.assertEqual(handoff.status(self.claude, job.job_id).state, "completed")
        # nothing ready -> None; non-handoff tasks are ignored
        self.claude.task_create("work/peers", "plain task", idempotency_key="plain")
        self.assertIsNone(handoff.serve_one(self.worker, "work/peers", lambda t: "x"))

    def test_handoff_input_validation(self):
        with self.assertRaises(PeerError):
            handoff.submit(self.claude, "work/peers", "x", idempotency_key="k", labels={"board.task": "done"})
        with self.assertRaises(PeerError):
            handoff.submit(self.claude, "work/peers", "x" * 10_001, idempotency_key="k")
        with self.assertRaises(PeerError):
            handoff.submit(self.claude, "work/peers", " ", idempotency_key="k")
        with self.assertRaises(PeerError) as cm:
            handoff.status(self.claude, "99999")
        self.assertEqual(cm.exception.code, "invalid_request")

    def test_handoff_fail_closed_on_claim_lost(self):
        # (a) the task is canceled under the worker: no success is reported, nothing is posted
        job = handoff.submit(self.claude, "work/peers", "job a", idempotency_key="a")

        def handler(task):
            self.claude.task_cancel(task["id"], "no longer needed")
            return "result that must not land"

        t, box = self.serve_in_thread(handler)
        t.join(10)
        self.assertEqual(box["job"].state, "lost")
        after = self.claude.read(job.job_id)
        self.assertEqual(after["message"]["task"]["state"], "canceled")
        self.assertEqual(after["replies"], [])
        self.assertIsNone(after["message"]["task"]["result"])
        with self.assertRaises(PeerError):
            handoff.get_result(self.claude, job.job_id)

        # (b) a renew reports claim_lost while the handler runs: the handler result is dropped
        job_b = handoff.submit(self.claude, "work/peers", "job b", idempotency_key="b")
        renewed = threading.Event()

        class LosingPort:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def task_renew(self, task_id, **kw):
                renewed.set()
                raise VBoardError("claim_lost", "lease expired")

        def slow_handler(task):
            renewed.wait(5)
            time.sleep(0.05)
            return "also dropped"

        t, box = self.serve_in_thread(slow_handler, port=LosingPort(self.worker), renew_every=0.02)
        t.join(10)
        self.assertEqual(box["job"].state, "lost")
        view = self.claude.read(job_b.job_id)
        self.assertEqual(view["message"]["task"]["state"], "claimed")  # never completed
        self.assertEqual(view["replies"], [])

        # (c) delivery cannot be confirmed: reported as undelivered, never completed
        self.claude.task_cancel(job_b.job_id, "cleanup")
        job_c = handoff.submit(self.claude, "work/peers", "job c", idempotency_key="c")

        class BrokenDelivery(LosingPort):
            def task_renew(self, task_id, **kw):
                return self._inner.task_renew(task_id, **kw)

            def task_complete(self, task_id, result=None):
                raise VBoardError("unavailable", "board down")

        t, box = self.serve_in_thread(lambda task: "done", port=BrokenDelivery(self.worker))
        t.join(10)
        self.assertEqual(box["job"].state, "undelivered")
        self.assertEqual(self.claude.read(job_c.job_id)["message"]["task"]["state"], "claimed")

    def test_handoff_handler_exception_fails_the_task(self):
        job = handoff.submit(self.claude, "work/peers", "job d", idempotency_key="d")

        def handler(task):
            raise ValueError("boom " + "x" * 2000)

        t, box = self.serve_in_thread(handler)
        t.join(10)
        self.assertEqual(box["job"].state, "failed")
        done = handoff.wait(self.claude, job.job_id, timeout=5)
        self.assertEqual(done.state, "failed")
        self.assertTrue(done.reason.startswith("ValueError: boom"))
        self.assertLessEqual(len(done.reason), 1000)
        with self.assertRaises(PeerError) as cm:
            handoff.get_result(self.claude, job.job_id)
        self.assertEqual(cm.exception.code, "peer_failed")

    def test_handoff_long_result_posted_as_reply(self):
        job = handoff.submit(self.claude, "work/peers", "big job", idempotency_key="big")
        long_text = "line of result\n" * 200  # 3000 chars
        t, box = self.serve_in_thread(lambda task: long_text)
        t.join(10)
        self.assertEqual(box["job"].state, "completed")
        self.assertEqual(box["job"].result, long_text)
        view = self.claude.read(job.job_id)
        recorded = view["message"]["task"]["result"]
        self.assertRegex(recorded, r"^see reply #\S+$")
        self.assertEqual(len(view["replies"]), 1)
        self.assertEqual(recorded, f"see reply #{view['replies'][0]['id']}")
        self.assertIn(long_text, view["replies"][0]["text"])
        self.assertLessEqual(len(recorded), 1000)
        self.assertEqual(handoff.get_result(self.claude, job.job_id), long_text)
        self.assertEqual(handoff.wait(self.claude, job.job_id, timeout=1).result, long_text)
        # beyond the board's 10,000 character cap the reply is truncated, never the task result
        job2 = handoff.submit(self.claude, "work/peers", "huge job", idempotency_key="huge")
        t, box = self.serve_in_thread(lambda task: "y" * 20_000)
        t.join(10)
        self.assertEqual(box["job"].state, "completed")
        reply = self.claude.read(job2.job_id)["replies"][0]["text"]
        self.assertLessEqual(len(reply), 10_000)
        self.assertTrue(reply.endswith("[truncated]"))

    def test_handoff_wait_timeout_returns_open_job(self):
        job = handoff.submit(self.claude, "work/peers", "never served", idempotency_key="idle")
        t0 = time.monotonic()
        res = handoff.wait(self.claude, job.job_id, timeout=0.3)
        self.assertIn(res.state, ("ready", "waiting"))
        self.assertLess(time.monotonic() - t0, 5)


# ---------------------------------------------------------------------------------------------- CLI
class CliTests(Sandbox):
    def run_cli(self, argv, env):
        out, err = io.StringIO(), io.StringIO()
        rc = peers_cli.main(argv, env=env, out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def peer_bins(self):
        body = FAKE_PEER.read_text("utf-8").split("\n", 1)[1]
        for name in ("claude", "andrewcode", "codex", "opencode"):
            p = self.bin / name
            p.write_text(f"#!{sys.executable}\n{body}", "utf-8")
            p.chmod(0o755)

    def test_cli_policy_and_ask_with_fake_cli(self):
        self.peer_bins()
        env = self.env(CLAUDECODE="1", FUSION_HOST="codex", FAKE_PEER_LOG=str(self.calls))
        rc, out, _ = self.run_cli(["policy"], env)
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual(doc["host"], "claude")
        self.assertFalse(doc["codex_exec_allowed"])
        self.assertEqual(doc["routes"]["codex"]["oneshot"]["transport"], "andrewcode-oneshot")
        self.assertEqual(doc["routes"]["codex"]["session"]["transport"], "andrewcode-acp")
        self.assertEqual(doc["routes"]["opencode"]["session"]["error"], "invalid_request")
        rc, out, _ = self.run_cli(["route", "codex", "--session"], env)
        self.assertEqual((rc, json.loads(out)["transport"]), (0, "andrewcode-acp"))
        rc, out, _ = self.run_cli(["route", "claude"], env)
        self.assertEqual(rc, 2)  # host-native: not spawnable
        self.assertFalse(json.loads(out)["allowed"])
        rc, out, err = self.run_cli(["ask", "codex", "--prompt", "hello", "--cwd", str(self.work), "--as", "cli-test"],
                                    env)
        self.assertEqual(rc, 0, err)
        res = json.loads(out)
        self.assertEqual((res["ok"], res["transport"], res["text"]), (True, "andrewcode-oneshot", "andrewcode says hi"))
        self.assertEqual(res["model"], "gpt-6.1-sol")
        calls = [json.loads(x)["name"] for x in self.calls.read_text("utf-8").splitlines() if x.strip()]
        self.assertNotIn("codex", calls)
        # usage / policy errors exit 2 with a JSON error on stderr
        rc, _, err = self.run_cli(["ask", "codex"], env)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(err)["error"]["code"], "invalid_request")
        rc, _, err = self.run_cli(["ask", "claude", "--prompt", "x"], env)
        self.assertEqual((rc, json.loads(err)["error"]["code"]), (2, "peer_not_allowed"))
        rc, _, _ = self.run_cli(["bogus"], env)
        self.assertEqual(rc, 2)
        rc, out, _ = self.run_cli(["whoami"], self.env(BOARD_PROFILE="zed"))
        self.assertEqual((rc, json.loads(out)["name"]), (0, "zed"))

    def test_cli_acp_command_through_andrewcode_seat(self):
        self.make_bins()
        env = self.env(CLAUDECODE="1", FAKE_ACP_MODE="perm_edit")
        rc, out, err = self.run_cli(["acp", "codex", "--prompt", "hi", "--thinking", "xhigh"], env)
        self.assertEqual(rc, 0, err)
        res = json.loads(out)
        self.assertEqual((res["ok"], res["transport"]), (True, "andrewcode-acp"))
        self.assertTrue(res["text"].endswith("perm_edit=cancelled"))  # deny is the default
        self.assertEqual(res["permission_decisions"][0]["decision"], "deny")
        rc, out, _ = self.run_cli(["acp", "andrewcode", "--prompt", "hi", "--permission", "allow_all",
                                   "--yes-allow-all"], env)
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["text"].endswith("perm_edit=allow-1"))
        rc, _, err = self.run_cli(["acp", "andrewcode", "--prompt", "hi", "--permission", "allow_all"], env)
        self.assertEqual((rc, json.loads(err)["error"]["code"]), (2, "invalid_request"))
        self.assertEqual(self.calls.exists(), False)  # no real codex / claude binary was run

    def test_cli_handoff_submit_wait_serve(self):
        board = f"local:{Path(self.tmp, 'cli.db')}"
        env = self.env()
        brief = Path(self.tmp, "brief.md")
        brief.write_text("shout this please", "utf-8")
        rc, out, err = self.run_cli(["handoff", "--board", board, "--as", "claude", "submit", "--channel", "work/cli",
                                     "--brief-file", str(brief), "--key", "cli-1"], env)
        self.assertEqual(rc, 0, err)
        job = json.loads(out)
        cmd = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"]
        rc, out, err = self.run_cli(["handoff", "--board", board, "--as", "andrewcode", "serve", "--channel",
                                     "work/cli", "--lease", "60", "--", *cmd], env)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["state"], "completed")
        rc, out, _ = self.run_cli(["handoff", "--board", board, "--as", "claude", "wait", job["job_id"],
                                   "--timeout", "5"], env)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["result"], "SHOUT THIS PLEASE")
        rc, out, _ = self.run_cli(["handoff", "--board", board, "--as", "claude", "get", job["job_id"]], env)
        self.assertEqual((rc, out.strip()), (0, "SHOUT THIS PLEASE"))
        # a failing handler fails the task and exits 1; a codex handler is refused up front without the gate
        rc, out, _ = self.run_cli(["handoff", "--board", board, "--as", "claude", "submit", "--channel", "work/cli",
                                   "--brief", "second", "--key", "cli-2"], env)
        rc, out, _ = self.run_cli(["handoff", "--board", board, "--as", "andrewcode", "serve", "--channel", "work/cli",
                                   "--", sys.executable, "-c", "import sys; sys.exit(3)"], env)
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["state"], "failed")
        rc, _, err = self.run_cli(["handoff", "--board", board, "--as", "andrewcode", "serve", "--channel",
                                   "work/cli", "--", "codex", "exec"], self.env(CLAUDECODE="1"))
        self.assertEqual((rc, json.loads(err)["error"]["code"]), (2, "peer_not_allowed"))

    def test_module_entrypoint_policy(self):
        env = dict(self.env(CLAUDECODE="1"), PYTHONPATH=str(ROOT / "python"), PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-m", "fusion_swarm.peers", "policy"], env=env, capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["routes"]["andrewcode"]["session"]["transport"], "andrewcode-acp")


if __name__ == "__main__":
    unittest.main()
