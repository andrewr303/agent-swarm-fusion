"""fusion-board MCP server (python/fusion_swarm/vboard/mcp.py, mcps/fusion-board/server.py) and its installer wiring.

stdlib unittest; no network, no Node, no real provider CLIs. Every board database, FUSION_HOME and XDG_CONFIG_HOME
lives in a temp dir. Subprocess tests speak the real newline-delimited stdio transport.
Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_vboard_mcp -v   (from the plugin root)
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "scripts"))

import fusion_install as installer  # noqa: E402
from fusion_swarm.vboard import VERSION  # noqa: E402
from fusion_swarm.vboard import mcp as vmcp  # noqa: E402
from fusion_swarm.vboard.mcp import BoardMcpServer  # noqa: E402
from fusion_swarm.vboard.model import VBoardError  # noqa: E402

SERVER = ROOT / "mcps" / "fusion-board" / "server.py"

FROZEN_TOOLS = {
    "board_whoami", "board_read", "board_post", "board_reply", "board_label", "board_react", "board_wait",
    "board_head", "board_tasks", "board_task_create", "board_task_create_batch", "board_task_claim",
    "board_task_claim_next", "board_task_renew", "board_task_release", "board_task_unblock", "board_task_reopen",
    "board_task_complete", "board_task_fail", "board_task_cancel", "board_task_block", "board_task_assign",
    "board_task_needs", "board_capabilities",
}
READ_ONLY = {"board_whoami", "board_read", "board_wait", "board_head", "board_tasks", "board_capabilities"}
IDENTITY_VARS = ("BOARD_PROFILE", "FUSION_AGENT_ID", "FUSION_VBOARD", "BOARD_URL", "BOARD_TOKEN", "BOARD_NAME",
                 "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "FUSION_HOST", "FUSION_HOST_EXPLICIT")


def rpc(i, method, **params):
    return {"jsonrpc": "2.0", "id": i, "method": method, "params": params}


def call(i, name, **arguments):
    return rpc(i, "tools/call", name=name, arguments=arguments)


def doc(response):
    """Structured document of a tools/call response."""
    return response["result"]["structuredContent"]


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="fusion board mcp ")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = str(self.tmp / "board.db")
        self.env = {k: v for k, v in os.environ.items() if k not in IDENTITY_VARS}
        self.env.update({"PYTHONUTF8": "1", "FUSION_HOME": str(self.tmp / "home"), "XDG_CONFIG_HOME": str(self.tmp / "xdg")})

    def exchange(self, requests, *args, env=None):
        """One process, a batch of requests on stdin, EOF; returns the parsed responses keyed by id (+ list)."""
        lines = [json.dumps(r, ensure_ascii=False) if not isinstance(r, str) else r for r in requests]
        result = subprocess.run([sys.executable, "-B", str(SERVER), "--backend", "local", "--db", self.db, *args],
                                input="\n".join(lines) + "\n", text=True, encoding="utf-8", capture_output=True,
                                cwd=self.tmp, timeout=30, env=env or self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return [json.loads(line) for line in result.stdout.splitlines()]


class Session:
    """A long-lived server process answering one request at a time (for steps that depend on each other)."""

    def __init__(self, test: Base, *args):
        self.proc = subprocess.Popen([sys.executable, "-B", str(SERVER), "--backend", "local", "--db", test.db, *args],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     encoding="utf-8", cwd=test.tmp, env=test.env)
        test.addCleanup(self.close)
        self.n = 0

    def ask(self, method, **params):
        self.n += 1
        self.proc.stdin.write(json.dumps(rpc(self.n, method, **params)) + "\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        assert reply["id"] == self.n, reply
        return reply

    def tool(self, name, **arguments):
        return self.ask("tools/call", name=name, arguments=arguments)

    def close(self):
        if self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.proc.stdin.close()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        for stream in (self.proc.stdout, self.proc.stderr):
            with contextlib.suppress(Exception):
                stream.close()


class FakePort:
    """Minimal BoardPort double for in-process server tests."""

    def __init__(self, identity, log, *, wait_gate=None, wait_started=None):
        self.identity, self.log = identity, log
        self.wait_gate, self.wait_started = wait_gate, wait_started
        self.closed = False

    def whoami(self):
        return {"profile": self.identity}

    def capabilities(self):
        from fusion_swarm.vboard.port import Capabilities
        return Capabilities(backend="local", version="fake", help_hash=None, lease_cap_s=3600)

    def head(self):
        return "7"

    def wait(self, target=None, **kw):
        self.log.append(("wait", self.identity, kw))
        if self.wait_started is not None:
            self.wait_started.set()
        if self.wait_gate is not None:
            self.wait_gate.wait(10)
        return None

    def close(self):
        self.closed = True


class FeedReader:
    """Iterable of stdin lines fed from a queue (None ends the stream)."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()

    def send(self, request):
        self.q.put(json.dumps(request) + "\n")

    def end(self):
        self.q.put(None)

    def __iter__(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            yield item


class Sink:
    """Thread-safe stdout double that records parsed responses."""

    def __init__(self):
        self.responses: queue.Queue = queue.Queue()
        self._buf = ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.responses.put(json.loads(line))

    def flush(self):
        pass

    def get(self, timeout=5):
        return self.responses.get(timeout=timeout)


class StdioProtocolTests(Base):
    def test_initialize_and_tools_list_names_frozen(self):
        out = self.exchange([
            rpc(1, "initialize", protocolVersion="2025-11-25"),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            rpc(2, "tools/list"), "not json", rpc(3, "ping"), rpc(4, "nope"),
            {"jsonrpc": "2.0", "id": 5, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
        ], "--as", "alice")
        self.assertEqual(len(out), 6)                                  # the notification answers nothing
        by_id = {r.get("id"): r for r in out if r.get("id") is not None}
        self.assertEqual(by_id[1]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(by_id[1]["result"]["serverInfo"], {"name": "fusion-board", "version": VERSION})
        self.assertEqual(by_id[5]["result"]["protocolVersion"], "2025-11-25")      # unknown version negotiates down
        tools = by_id[2]["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, FROZEN_TOOLS)
        self.assertEqual(len(tools), len(FROZEN_TOOLS))
        for tool in tools:
            schema = tool["inputSchema"]
            self.assertIs(schema["additionalProperties"], False, tool["name"])
            self.assertIn("as", schema["properties"], tool["name"])
            self.assertNotIn("as", schema["required"], tool["name"])
            self.assertEqual(tool["annotations"]["readOnlyHint"], tool["name"] in READ_ONLY, tool["name"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
            self.assertFalse(tool["annotations"]["openWorldHint"])           # local backend
        required = {t["name"]: t["inputSchema"]["required"] for t in tools}
        self.assertEqual(required["board_post"], ["target", "text"])
        self.assertEqual(required["board_task_create_batch"], ["channel", "plan", "idempotency_key"])
        self.assertEqual(required["board_task_claim_next"], ["channel"])
        errors = [r for r in out if "error" in r]
        self.assertEqual(sorted(e["error"]["code"] for e in errors), [-32700, -32601])
        self.assertIsNone(next(e for e in errors if e["error"]["code"] == -32700)["id"])
        self.assertEqual(next(e for e in errors if e["error"]["code"] == -32601)["id"], 4)

    def test_post_claim_complete_roundtrip_with_as(self):
        s = Session(self)
        s.ask("initialize", protocolVersion="2025-11-25")
        who = s.tool("board_whoami", **{"as": "alice"})
        self.assertFalse(who["result"]["isError"], who)
        self.assertEqual(doc(who)["profile"], "alice")
        self.assertEqual(doc(who)["capabilities"]["backend"], "local")
        posted = s.tool("board_post", **{"as": "alice", "target": "dev", "text": "héllo wörld", "idempotency_key": "k1"})
        self.assertFalse(posted["result"]["isError"], posted)
        again = s.tool("board_post", **{"as": "alice", "target": "dev", "text": "héllo wörld", "idempotency_key": "k1"})
        self.assertEqual(doc(again)["message"]["id"], doc(posted)["message"]["id"])        # idempotent retry
        thread = s.tool("board_read", **{"as": "bob", "target": "dev"})
        self.assertIn("héllo wörld", json.dumps(doc(thread), ensure_ascii=False))
        created = s.tool("board_task_create", **{"as": "alice", "channel": "work/mcp", "text": "do the thing",
                                                 "labels": {"fusion.role": "worker"}})
        self.assertFalse(created["result"]["isError"], created)
        task_id = doc(created)["task"]["id"]
        listing = s.tool("board_tasks", **{"as": "bob", "channel": "work/mcp"})
        self.assertEqual([t["message"]["id"] for t in doc(listing)["tasks"]], [task_id])
        claimed = s.tool("board_task_claim_next", **{"as": "bob", "channel": "work/mcp", "lease_s": 60})
        self.assertFalse(claimed["result"]["isError"], claimed)
        self.assertEqual(doc(claimed)["task"]["id"], task_id)
        self.assertIn("epoch", doc(claimed)["claim"])
        stranger = s.tool("board_task_complete", **{"as": "carol", "task_id": task_id, "result": "stolen"})
        self.assertTrue(stranger["result"]["isError"])                                       # no claim proof for carol
        self.assertEqual(doc(stranger)["reason"], "claim_not_held")
        done = s.tool("board_task_complete", **{"as": "bob", "task_id": task_id, "result": "ok: tested"})
        self.assertFalse(done["result"]["isError"], done)
        self.assertEqual(doc(done)["task"]["task"]["state"], "completed")
        empty = s.tool("board_task_claim_next", **{"as": "bob", "channel": "work/mcp"})
        self.assertEqual(doc(empty), {"task": None, "reason": "nothing_ready"})
        head = s.tool("board_head", **{"as": "bob"})
        self.assertIsInstance(doc(head)["cursor"], str)
        caps = s.tool("board_capabilities", **{"as": "bob"})
        self.assertTrue(doc(caps)["tasks"])
        s.close()
        self.assertEqual(s.proc.returncode, 0, s.proc.stderr.read() if not s.proc.stderr.closed else "")

    def test_same_identity_calls_keep_arrival_order_in_a_batch(self):
        out = self.exchange([
            call(1, "board_post", **{"as": "alice", "target": "dev", "text": "first"}),
            call(2, "board_task_create", **{"as": "alice", "channel": "work/ord", "text": "t1", "claim": True}),
            call(3, "board_tasks", **{"as": "alice", "channel": "work/ord", "state": "claimed"}),
            call(4, "board_read", **{"as": "alice", "target": "dev"}),
        ])
        by_id = {r["id"]: r for r in out}
        self.assertFalse(any(r["result"]["isError"] for r in out), out)
        self.assertEqual(len(doc(by_id[3])["tasks"]), 1)
        self.assertIn("first", json.dumps(doc(by_id[4])))

    def test_missing_identity_is_error_not_crash(self):
        out = self.exchange([call(1, "board_whoami"), call(2, "board_post", target="dev", text="x"),
                             call(3, "board_whoami", **{"as": "dave"})])
        by_id = {r["id"]: r for r in out}
        for rid in (1, 2):
            self.assertTrue(by_id[rid]["result"]["isError"])
            self.assertEqual(doc(by_id[rid])["reason"], "invalid_profile")
        self.assertFalse(by_id[3]["result"]["isError"])             # the server survived and still answers
        self.assertEqual(doc(by_id[3])["profile"], "dave")

    def test_identity_resolution_order(self):
        env = dict(self.env, FUSION_AGENT_ID="from-agent-id")
        out = {r["id"]: r for r in self.exchange([call(1, "board_whoami"),
                                                  call(2, "board_whoami", **{"as": "explicit"})], env=env)}
        self.assertEqual(doc(out[1])["profile"], "from-agent-id")
        self.assertEqual(doc(out[2])["profile"], "explicit")
        env["BOARD_PROFILE"] = "from-profile"                        # BOARD_PROFILE beats FUSION_AGENT_ID
        self.assertEqual(doc(self.exchange([call(1, "board_whoami")], env=env)[0])["profile"], "from-profile")
        flag = self.exchange([call(1, "board_whoami")], "--as", "flagged", env=env)    # --as beats the environment
        self.assertEqual(doc(flag[0])["profile"], "flagged")
        host = dict(self.env, CLAUDECODE="1")                        # host default when nothing else is set
        self.assertEqual(doc(self.exchange([call(1, "board_whoami")], env=host)[0])["profile"], "claude")

    def test_invalid_arguments_are_error_results_with_the_request_id(self):
        out = self.exchange([
            call(1, "board_post", **{"as": "alice", "target": "dev"}),                              # missing text
            call(2, "board_post", **{"as": "alice", "target": "dev", "text": "x", "bogus": 1}),     # unknown field
            call(3, "board_post", **{"as": "alice", "target": "dev", "text": "x" * 10_001}),        # too long
            call(4, "board_whoami", **{"as": "9bad name"}),                                         # bad profile
            call(5, "board_nope", **{"as": "alice"}),                                               # unknown tool
            call(6, "board_task_complete", **{"as": "alice", "task_id": "../etc", "result": "x"}),  # bad id
            call(7, "board_wait", **{"as": "alice", "timeout_s": 0}),                               # out of range
            rpc(8, "tools/call", name="board_whoami", arguments=[1]),                               # non-object args
            rpc(9, "tools/call", name=7),                                                           # non-string name
        ])
        by_id = {r["id"]: r for r in out}
        self.assertEqual(sorted(by_id), list(range(1, 10)))
        for rid in range(1, 10):
            self.assertTrue(by_id[rid]["result"]["isError"], rid)
            self.assertEqual(doc(by_id[rid])["reason"] in ("invalid_request", "invalid_profile"), True, rid)
        self.assertNotIn("bogus", by_id[2]["result"]["content"][0]["text"].replace("'bogus'", ""))

    def test_wait_timeout_shape_and_cap(self):
        s = Session(self)
        head = doc(s.tool("board_head", **{"as": "alice"}))["cursor"]
        # an event is delivered when one is waiting ...
        s.tool("board_post", **{"as": "bob", "target": "dev", "text": "ping"})
        got = s.tool("board_wait", **{"as": "alice", "target": "dev", "after": head, "timeout_s": 5})
        self.assertFalse(got["result"]["isError"])
        self.assertIsNotNone(doc(got)["event"])
        self.assertIn("cursor", doc(got)["event"])
        s.close()

    def test_wait_timeouts_are_capped_and_return_the_resume_cursor(self):
        log: list = []
        server = BoardMcpServer(port_factory=lambda who: FakePort(who, log), wait_cap_s=0.05)
        res = server.handle(call(1, "board_wait", **{"as": "alice", "timeout_s": 300}))
        self.assertFalse(res["result"]["isError"])
        out = doc(res)
        self.assertIsNone(out["event"])
        self.assertEqual(out["reason"], "timeout")
        self.assertEqual(out["cursor"], "7")                       # head captured when no `after` was given
        self.assertTrue(out["capped"])
        self.assertEqual(log[0][2]["timeout"], 0.05)
        self.assertEqual(log[0][2]["after"], "7")
        res = server.handle(call(2, "board_wait", **{"as": "alice", "timeout_s": 1, "after": "3"}))
        self.assertEqual(doc(res)["cursor"], "3")                  # a caller's `after` is preserved
        server.close()

    def test_claim_next_wait_is_capped_with_timeout_reason(self):
        s = Session(self)
        s.tool("board_task_create", **{"as": "alice", "channel": "work/empty", "text": "claim me", "claim": True})
        res = s.tool("board_task_claim_next", **{"as": "bob", "channel": "work/empty", "wait_s": 0.1})
        self.assertEqual(doc(res), {"task": None, "reason": "timeout"})
        s.close()


class InProcessServerTests(Base):
    def test_unexpected_exception_keeps_request_id(self):
        def boom(identity):
            raise KeyError("secret-token-in-message")
        server = BoardMcpServer(port_factory=boom)
        res = server.handle(call(41, "board_whoami", **{"as": "alice"}))
        self.assertEqual(res["id"], 41)
        self.assertTrue(res["result"]["isError"])
        self.assertEqual(doc(res)["reason"], "unexpected_error")
        self.assertNotIn("secret-token", json.dumps(res))          # never echo exception text or arguments
        self.assertIn("KeyError", doc(res)["message"])

        class Broken(FakePort):
            def head(self):
                raise RuntimeError("password=hunter2")
        server = BoardMcpServer(port_factory=lambda who: Broken(who, []))
        sink, feed = Sink(), FeedReader()
        thread = threading.Thread(target=server.serve, args=(feed, sink))
        thread.start()
        feed.send(call("req-7", "board_head", **{"as": "alice"}))
        feed.send(call(8, "board_whoami", **{"as": "alice"}))
        reply = {r["id"]: r for r in (sink.get(), sink.get())}
        feed.end()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(reply["req-7"]["result"]["isError"])
        self.assertNotIn("hunter2", json.dumps(reply["req-7"]))
        self.assertFalse(reply[8]["result"]["isError"])            # the next call still works

    def test_one_port_per_identity_cached(self):
        opened: list[str] = []

        def factory(identity):
            opened.append(identity)
            return FakePort(identity, [])
        server = BoardMcpServer(port_factory=factory, default_as="alice")
        for _ in range(3):
            self.assertEqual(doc(server.handle(call(1, "board_whoami")))["profile"], "alice")
        self.assertEqual(doc(server.handle(call(2, "board_whoami", **{"as": "Alice"})))["profile"], "alice")  # case folds
        self.assertEqual(doc(server.handle(call(3, "board_whoami", **{"as": "bob"})))["profile"], "bob")
        server.handle(call(4, "board_head", **{"as": "bob"}))
        self.assertEqual(sorted(opened), ["alice", "bob"])
        ports = server.ports_open()
        self.assertEqual(sorted(ports), ["alice", "bob"])
        server.close()
        self.assertTrue(all(p.closed for p in ports.values()))
        self.assertEqual(server.ports_open(), {})

    def test_failed_port_open_is_not_cached_as_success(self):
        attempts: list[str] = []

        def factory(identity):
            attempts.append(identity)
            if len(attempts) == 1:
                raise VBoardError("unavailable", "board down")
            return FakePort(identity, [])
        server = BoardMcpServer(port_factory=factory)
        first = server.handle(call(1, "board_head", **{"as": "alice"}))
        self.assertTrue(first["result"]["isError"])
        self.assertEqual(doc(first)["reason"], "unavailable")
        self.assertFalse(server.handle(call(2, "board_head", **{"as": "alice"}))["result"]["isError"])
        self.assertEqual(attempts, ["alice", "alice"])

    def test_blocking_wait_does_not_stall_other_identity(self):
        gate, started = threading.Event(), threading.Event()
        log: list = []

        def factory(identity):
            return FakePort(identity, log, wait_gate=gate if identity == "alice" else None,
                            wait_started=started if identity == "alice" else None)
        server = BoardMcpServer(port_factory=factory)
        feed, sink = FeedReader(), Sink()
        thread = threading.Thread(target=server.serve, args=(feed, sink))
        thread.start()
        try:
            feed.send(call("alice-wait", "board_wait", **{"as": "alice", "timeout_s": 5}))
            self.assertTrue(started.wait(5), "alice's wait never started")
            feed.send(call("bob-head", "board_head", **{"as": "bob"}))
            first = sink.get(timeout=5)                            # bob answers while alice is still blocked
            self.assertEqual(first["id"], "bob-head")
            self.assertEqual(doc(first)["cursor"], "7")
            self.assertTrue(sink.responses.empty())
            gate.set()
            second = sink.get(timeout=5)
            self.assertEqual(second["id"], "alice-wait")
            self.assertEqual(doc(second)["reason"], "timeout")
        finally:
            gate.set()
            feed.end()
            thread.join(10)
        self.assertFalse(thread.is_alive())

    def test_concurrent_identities_are_serialised_per_identity_only(self):
        active: dict[str, int] = {}
        overlap: list[str] = []
        lock = threading.Lock()

        class Counting(FakePort):
            def head(self):
                with lock:
                    active[self.identity] = active.get(self.identity, 0) + 1
                    if active[self.identity] > 1:
                        overlap.append(self.identity)
                threading.Event().wait(0.02)
                with lock:
                    active[self.identity] -= 1
                return "1"
        server = BoardMcpServer(port_factory=lambda who: Counting(who, []))
        feed, sink = FeedReader(), Sink()
        thread = threading.Thread(target=server.serve, args=(feed, sink))
        thread.start()
        ids = []
        for n in range(6):
            for who in ("alice", "bob", "carol"):
                ids.append(f"{who}-{n}")
                feed.send(call(ids[-1], "board_head", **{"as": who}))
        got = [sink.get(timeout=10)["id"] for _ in ids]
        feed.end()
        thread.join(10)
        self.assertEqual(sorted(got), sorted(ids))
        self.assertEqual(overlap, [])                              # one identity never runs two calls at once
        for who in ("alice", "bob", "carol"):                      # and its answers keep arrival order
            self.assertEqual([i for i in got if i.startswith(who)], [f"{who}-{n}" for n in range(6)])

    def test_open_world_hint_only_for_vercel(self):
        server = BoardMcpServer(backend="vercel")
        self.assertTrue(all(t["annotations"]["openWorldHint"] for t in server.tools_list()))
        self.assertFalse(any(t["annotations"]["openWorldHint"] for t in BoardMcpServer(backend="local").tools_list()))
        with self.assertRaises(ValueError):
            BoardMcpServer(backend="carrier-pigeon")

    def test_validator_rejects_without_echoing_values(self):
        with self.assertRaises(VBoardError) as ctx:
            vmcp.validate({"text": "TOPSECRET", "x": 1}, {"type": "object", "properties": {"text": {"type": "string"}},
                                                           "additionalProperties": False})
        self.assertNotIn("TOPSECRET", str(ctx.exception))
        for bad in (True, "3", 2.5, float("inf")):
            with self.assertRaises(VBoardError):
                vmcp.validate(bad, {"type": "integer", "minimum": 1})
        vmcp.validate("#12", {"type": "string", "pattern": vmcp.ID_PATTERN})
        with self.assertRaises(VBoardError):
            vmcp.validate("12\n", {"type": "string", "pattern": vmcp.ID_PATTERN})
        vmcp.validate(["1", {"id": "2", "if": "failed"}], vmcp.NEEDS)
        with self.assertRaises(VBoardError):
            vmcp.validate([{"id": "2", "extra": 1}], vmcp.NEEDS)


class ManifestAndInstallerTests(Base):
    def test_manifests_declare_both_servers(self):
        legacy = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        native = json.loads((ROOT / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        for servers in (legacy, native):
            self.assertEqual(set(servers), {"fusion-coordination", "fusion-board"})
        self.assertEqual(legacy["fusion-board"], {"command": "python", "args": ["-B", "mcps/fusion-board/server.py"],
                                                  "cwd": ".", "env": {"PYTHONUTF8": "1"}})
        self.assertEqual(native["fusion-board"], {"type": "stdio", "command": "python",
                                                  "args": ["-B", "${PLUGIN_ROOT}/mcps/fusion-board/server.py"],
                                                  "env": {"PYTHONUTF8": "1"}})
        self.assertNotIn("--require-board", legacy["fusion-board"]["args"])
        self.assertIn("--require-board", legacy["fusion-coordination"]["args"])
        for name, server in legacy.items():
            self.assertTrue((ROOT / server["args"][1]).is_file(), name)
        for name, server in native.items():
            self.assertTrue((ROOT / server["args"][1].replace("${PLUGIN_ROOT}/", "")).is_file(), name)
        self.assertIn("mcps/fusion-board", installer.INCLUDE_DIRS)
        self.assertIn("mcps/fusion-board/server.py", installer.CHECK_FILES)

    def test_installer_rewrites_fusion_board_absolute(self):
        home = self.tmp / "andrewcode"
        with contextlib.redirect_stdout(io.StringIO()):
            managed = installer.install_kimi(ROOT, home, python=sys.executable)
        legacy = installer.read_object(managed / ".mcp.json")["mcpServers"]
        board = legacy["fusion-board"]
        self.assertEqual(board["command"], sys.executable)
        self.assertEqual(board["args"], ["-B", str(managed / "mcps/fusion-board/server.py")])   # absolute, no --require-board
        self.assertTrue(Path(board["args"][1]).is_file())
        self.assertEqual(legacy["fusion-coordination"]["args"],
                         ["-B", str(managed / "mcps/fusion-coordination/server.py"), "--require-board"])
        self.assertEqual(board["env"]["FUSION_PYTHON"], sys.executable)
        native = installer.read_object(managed / "mcp.json")["mcpServers"]
        self.assertEqual(native["fusion-board"]["args"], ["-B", "${PLUGIN_ROOT}/mcps/fusion-board/server.py"])
        self.assertEqual(native["fusion-board"]["env"], board["env"])
        self.assertEqual(native["fusion-board"]["command"], native["fusion-coordination"]["command"])
        for relative in ("kimi.plugin.json", ".kimi-plugin/plugin.json"):                  # kimi keeps RELATIVE args (J-12)
            servers = installer.read_object(managed / relative)["mcpServers"]
            self.assertEqual(servers["fusion-board"]["args"], ["-B", "mcps/fusion-board/server.py"])
            self.assertEqual(servers["fusion-board"]["cwd"], "./")
            self.assertEqual(servers["fusion-board"]["transport"], "stdio")
            self.assertIn("--require-board", servers["fusion-coordination"]["args"])
        # the installed copy starts from its own root with the pinned config, like the kimi host would
        config = installer.read_object(managed / "kimi.plugin.json")["mcpServers"]["fusion-board"]
        result = subprocess.run([sys.executable, *config["args"]], cwd=managed / config["cwd"],
                                input=json.dumps(rpc(1, "tools/list")) + "\n", capture_output=True, text=True,
                                encoding="utf-8", timeout=30, env={**self.env, **config["env"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        names = {t["name"] for t in json.loads(result.stdout)["result"]["tools"]}
        self.assertEqual(names, FROZEN_TOOLS)

    def test_verify_host_compares_both_servers(self):
        source = self.tmp / "prepared"
        source.mkdir()
        installer.copy_surface(ROOT, source)
        metadata = installer.validate_source(source)
        expected = installer.read_object(source / ".mcp.json")["mcpServers"]
        listing = self.tmp / "plugins.json"
        markets = self.tmp / "markets.json"
        markets.write_text(json.dumps([{"name": metadata["marketplace"], "source": "directory", "path": str(source)}]))

        def write(servers):
            listing.write_text(json.dumps([{"id": f"fusion@{metadata['marketplace']}", "scope": "user", "enabled": True,
                                            "version": metadata["version"], "mcpServers": servers}]))
        write(expected)
        verified = installer.verify_host("claude", listing, source, None, "user", markets)
        self.assertEqual(verified["loadMode"], "local-source")
        write({**expected, "fusion-board": {**expected["fusion-board"], "args": ["wrong-board.py"]}})
        with self.assertRaisesRegex(RuntimeError, "board MCP declaration"):
            installer.verify_host("claude", listing, source, None, "user", markets)
        write({k: v for k, v in expected.items() if k != "fusion-board"})
        with self.assertRaisesRegex(RuntimeError, "board MCP declaration"):
            installer.verify_host("claude", listing, source, None, "user", markets)


if __name__ == "__main__":
    unittest.main()
