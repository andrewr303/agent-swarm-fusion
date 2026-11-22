"""peers-core: policy, identity and exec_json against fake CLIs only (never real claude/codex/andrewcode)."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from fusion_swarm.peers import exec_json, identity, policy  # noqa: E402
from fusion_swarm.peers.exec_json import PeerRequest  # noqa: E402
from fusion_swarm.peers.policy import PeerError  # noqa: E402

FAKE = ROOT / "tests" / "fixtures" / "fake_peer_cli.py"


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="peers-core-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = Path(self.tmp, "bin")
        self.bin.mkdir()
        body = FAKE.read_text("utf-8").split("\n", 1)[1]
        for name in ("claude", "andrewcode", "codex", "opencode"):
            p = self.bin / name
            p.write_text(f"#!{sys.executable}\n{body}", "utf-8")
            p.chmod(0o755)
        self.log = Path(self.tmp, "calls.jsonl")
        self.home = Path(self.tmp, "home")
        self.work = Path(self.tmp, "work")
        self.work.mkdir()

    def env(self, **extra):
        e = {"PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin", "FUSION_HOME": str(self.home),
             "XDG_CONFIG_HOME": str(Path(self.tmp, "xdg")), "FAKE_PEER_LOG": str(self.log)}
        e.update(extra)
        return e

    def calls(self, include_help=False):
        if not self.log.exists():
            return []
        recs = [json.loads(x) for x in self.log.read_text("utf-8").splitlines() if x.strip()]
        return recs if include_help else [r for r in recs if not r.get("help")]


class PolicyTests(unittest.TestCase):
    def test_detect_host_matrix(self):
        rows = [
            ({}, "unknown"),
            ({"CLAUDECODE": "1"}, "claude"),
            ({"CLAUDE_CODE_ENTRYPOINT": "cli"}, "claude"),
            ({"FUSION_HOST": "codex"}, "codex"),
            ({"CLAUDECODE": "1", "FUSION_HOST": "codex"}, "claude"),
            ({"CLAUDECODE": "1", "FUSION_HOST": "codex", "FUSION_HOST_EXPLICIT": "1"}, "codex"),
            ({"CLAUDE_CODE_ENTRYPOINT": "cli", "FUSION_HOST": "codex"}, "claude"),
            ({"FUSION_HOST": "andrewcode", "CLAUDECODE": "1"}, "andrewcode"),
            ({"FUSION_HOST": "kimi"}, "kimi"),
            ({"FUSION_HOST": "bogus"}, "unknown"),
            ({"FUSION_HOST": "bogus", "CLAUDECODE": "1"}, "claude"),
            ({"CLAUDECODE": "0"}, "unknown"),
        ]
        for env, want in rows:
            with self.subTest(env=env):
                self.assertEqual(policy.detect_host(env), want)

    def test_route_table_policy_d2(self):
        claude_host = {"CLAUDECODE": "1"}
        codex_host = {"FUSION_HOST": "codex"}
        gated = {"CLAUDECODE": "1", "FUSION_ALLOW_CODEX_EXEC": "1"}
        rows = [
            ("codex", False, claude_host, "andrewcode-oneshot", True),
            ("codex", True, claude_host, "andrewcode-acp", True),
            ("codex", False, gated, "codex-exec", True),
            ("codex", False, codex_host, "codex-exec", True),
            ("codex", True, codex_host, "andrewcode-acp", True),  # no codex-acp on PATH
            ("andrewcode", False, claude_host, "andrewcode-oneshot", True),
            ("andrewcode", True, codex_host, "andrewcode-acp", True),
            ("claude", False, claude_host, "claude-print", False),
            ("claude", True, claude_host, "claude-acp", False),
            ("claude", False, codex_host, "claude-print", True),
            ("claude", False, {"FUSION_ALLOW_CLAUDE": "1"}, "claude-print", True),
            ("claude", False, {}, "claude-print", False),
            ("opencode", False, claude_host, "opencode-run", True),
        ]
        for peer, session, env, transport, allowed in rows:
            with self.subTest(peer=peer, session=session, env=env):
                env = dict(env, PATH="/nonexistent")
                r = policy.route(peer, session=session, env=env)
                self.assertEqual((r.transport, r.allowed), (transport, allowed))
                if peer == "codex" and policy.detect_host(env) == "claude" and not env.get("FUSION_ALLOW_CODEX_EXEC"):
                    self.assertNotEqual(r.binary, "codex")
                    self.assertEqual(r.default_model, "gpt-6.1-sol")
                if allowed:
                    self.assertIs(policy.require(r), r)
                else:
                    with self.assertRaises(PeerError) as cm:
                        policy.require(r)
                    self.assertEqual(cm.exception.code, "peer_not_allowed")
        self.assertEqual(policy.route("claude", env={"CLAUDECODE": "1"}).reason, "host-native")
        with self.assertRaises(PeerError) as cm:
            policy.route("opencode", session=True, env={})
        self.assertEqual(cm.exception.code, "invalid_request")
        with self.assertRaises(PeerError):
            policy.route("nobody", env={})

    def test_codex_acp_when_gated_and_on_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "codex-acp")
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)
            r = policy.route("codex", session=True, env={"FUSION_HOST": "codex", "PATH": d})
            self.assertEqual(r.transport, "codex-acp")
            r = policy.route("codex", session=True, env={"CLAUDECODE": "1", "PATH": d})
            self.assertEqual(r.transport, "andrewcode-acp")


class CodexGateTests(Sandbox):
    def test_codex_binary_never_invoked_without_gate(self):
        env = self.env(CLAUDECODE="1", FUSION_HOST="codex")  # legacy default on a Claude host
        res = exec_json.run("codex", PeerRequest(prompt="hi", cwd=str(self.work)), env=env)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.transport, "andrewcode-oneshot")
        self.assertEqual(res.model, "gpt-6.1-sol")
        self.assertNotIn("codex", [c["name"] for c in self.calls(include_help=True)])
        # defence in depth: the default runner and help probe refuse codex without the gate
        with self.assertRaises(PeerError) as cm:
            exec_json.default_runner(["codex", "exec"], cwd=None, env=env, input=None, timeout=1, cancel=None)
        self.assertEqual(cm.exception.code, "peer_not_allowed")
        with self.assertRaises(PeerError):
            exec_json.default_runner([str(self.bin / "codex"), "exec"], cwd=None, env=env, input=None,
                                     timeout=1, cancel=None)
        # an injected runner never sees a codex argv either
        seen = []

        def runner(argv, **kw):
            seen.append(argv)
            return 0, json.dumps({"role": "assistant", "content": "x"}) + "\n", ""
        exec_json.run("codex", PeerRequest(prompt="hi"), env=env, runner=runner)
        self.assertTrue(seen and all(not policy.is_codex_binary(a[0]) for a in seen))
        self.assertNotIn("codex", [c["name"] for c in self.calls(include_help=True)])

    def test_feature_flags_codex_probe_requires_gate(self):
        env = self.env(CLAUDECODE="1")
        with self.assertRaises(PeerError) as cm:
            exec_json.feature_flags("codex", env=env)
        self.assertEqual(cm.exception.code, "peer_not_allowed")
        self.assertEqual(self.calls(include_help=True), [])
        flags = exec_json.feature_flags("codex", env=self.env(FUSION_ALLOW_CODEX_EXEC="1"))
        self.assertIn("--output-schema", flags)

    def test_codex_exec_allowed_with_env_gate(self):
        self.assertFalse(policy.codex_exec_allowed({"CLAUDECODE": "1"}))
        self.assertTrue(policy.codex_exec_allowed({"FUSION_ALLOW_CODEX_EXEC": "1"}))
        self.assertTrue(policy.codex_exec_allowed({"FUSION_HOST": "codex"}))
        self.assertFalse(policy.codex_exec_allowed({"FUSION_HOST": "codex", "CLAUDECODE": "1"}))
        env = self.env(CLAUDECODE="1", FUSION_ALLOW_CODEX_EXEC="1")
        schema = {"type": "object"}
        res = exec_json.run("codex", PeerRequest(prompt="review", cwd=str(self.work), model="gpt-x", effort="high",
                                                 json_schema=schema, name="rev-1"), env=env)
        self.assertTrue(res.ok, res.error)
        self.assertEqual((res.transport, res.text, res.session_id), ("codex-exec", "codex says hi", "thr-codex-1"))
        self.assertEqual(res.usage["output_tokens"], 3)
        rec = [c for c in self.calls() if c["name"] == "codex"][0]
        a = rec["argv"]
        self.assertEqual(a[:3], ["--no-daemon", "exec", "--json"])
        self.assertIn("read-only", a)
        self.assertIn('model_reasoning_effort="high"', a)
        self.assertEqual(a[-1], "-")
        self.assertEqual(rec["stdin"], "review")
        self.assertEqual(rec["env"]["FUSION_AGENT_ID"], "rev-1")
        self.assertEqual(rec["env"]["FUSION_PEER_DEPTH"], "1")
        self.assertIsNone(rec["env"]["CLAUDECODE"])


class IdentityTests(unittest.TestCase):
    def test_identity_env_and_depth_limit(self):
        e = identity.identity_env("Worker-1", board_path="/b", parent="lead", run_id="r1", task_id="t1")
        self.assertEqual(e, {"FUSION_AGENT_ID": "worker-1", "BOARD_PROFILE": "worker-1", "FUSION_BOARD": "/b",
                             "FUSION_PARENT_ID": "lead", "FUSION_RUN_ID": "r1", "BOARD_TASK": "t1"})
        for bad in ("1abc", "", "a" * 33, "has space", "under_score"):
            with self.subTest(bad=bad), self.assertRaises(PeerError) as cm:
                identity.normalize(bad)
            self.assertEqual(cm.exception.code, "invalid_request")
        self.assertEqual(identity.agent_name({"FUSION_AGENT_ID": "B", "BOARD_PROFILE": "A"}), "a")
        self.assertEqual(identity.agent_name({"FUSION_AGENT_ID": "B"}), "b")
        self.assertEqual(identity.agent_name({}, default="dflt"), "dflt")
        self.assertIsNone(identity.agent_name({}))
        self.assertEqual(identity.resolve_name({"CLAUDECODE": "1"}), "claude")
        self.assertEqual(identity.resolve_name({"FUSION_AGENT_ID": "x", "CLAUDECODE": "1"}), "x")
        self.assertEqual(identity.resolve_name({"BOARD_PROFILE": "p", "FUSION_AGENT_ID": "x"}), "p")
        c1 = identity.child_env({}, "a")
        self.assertEqual(c1["FUSION_PEER_DEPTH"], "1")
        c2 = identity.child_env(c1, "b")
        self.assertEqual(c2["FUSION_PEER_DEPTH"], "2")
        with self.assertRaises(PeerError) as cm:
            identity.child_env(c2, "c")
        self.assertEqual(cm.exception.code, "depth_exceeded")
        self.assertEqual(identity.child_env({"FUSION_PEER_MAX_DEPTH": "5", "FUSION_PEER_DEPTH": "4"}, "c")
                         ["FUSION_PEER_DEPTH"], "5")

    def test_child_env_strips_claudecode(self):
        base = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "KEEP": "1"}
        c = identity.child_env(base, "x")
        self.assertNotIn("CLAUDECODE", c)
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", c)
        self.assertEqual(c["KEEP"], "1")
        self.assertIn("CLAUDECODE", base)  # base not mutated
        kept = identity.child_env(base, "x", strip_claude_nesting=False)
        self.assertEqual(kept["CLAUDECODE"], "1")

    def test_child_env_keeps_claude_host_after_markers_are_stripped(self):
        # A Claude host with the legacy FUSION_HOST=codex default must not turn into a codex host in the child
        # once CLAUDECODE is stripped (that would open the D2 codex gate).
        parent = {"CLAUDECODE": "1", "FUSION_HOST": "codex"}
        self.assertEqual(policy.detect_host(parent), "claude")
        child = identity.child_env(parent, "x")
        self.assertNotIn("CLAUDECODE", child)
        self.assertEqual(policy.detect_host(child), "claude")
        self.assertFalse(policy.codex_exec_allowed(child))
        # An explicit codex host stays codex through the spawn.
        codex_parent = {"FUSION_HOST": "codex", "FUSION_HOST_EXPLICIT": "1", "CLAUDECODE": "1"}
        self.assertEqual(policy.detect_host(identity.child_env(codex_parent, "x")), "codex")
        # No host information: nothing is invented.
        self.assertEqual(policy.detect_host(identity.child_env({}, "x")), "unknown")

    def test_worker_name_sanitized_and_truncated(self):
        self.assertEqual(identity.worker_name("r7", 2), "uc-r7-2")
        self.assertEqual(identity.worker_name("Run_ID.9", 3), "uc-run-id-9-3")
        long = identity.worker_name("x" * 60, 1)
        self.assertLessEqual(len(long), 32)
        self.assertRegex(long, r"^[a-z][a-z0-9-]{0,31}$")
        self.assertEqual(len(long), 32)
        self.assertEqual(long, identity.worker_name("x" * 60, 1))
        self.assertNotEqual(long, identity.worker_name("x" * 60, 2))
        self.assertTrue(identity.worker_name("9", 1, prefix="1").startswith("w"))


class ArgvParseTests(Sandbox):
    def test_build_argv_each_transport_exact(self):
        r = PeerRequest(prompt="p", model="m1", effort="high", cwd="/w", tools="none",
                        json_schema={"type": "object"}, max_budget_usd=2)
        self.assertEqual(exec_json.build_argv("claude-print", r, binary="claude"), [
            "claude", "-p", "--output-format", "json", "--no-session-persistence", "--model", "m1",
            "--json-schema", '{"type":"object"}', "--max-budget-usd", "2", "--permission-mode", "dontAsk",
            "--disallowedTools", "Bash Edit Write MultiEdit NotebookEdit WebFetch"])
        rr = PeerRequest(prompt="p", tools="read", session_id="s1", resume=True)
        self.assertEqual(exec_json.build_argv("claude-print", rr, binary="claude"), [
            "claude", "-p", "--output-format", "json", "--resume", "s1", "--permission-mode", "dontAsk",
            "--allowedTools", "Read,Grep,Glob"])
        rw = PeerRequest(prompt="p", tools="write", session_id="s2")
        self.assertEqual(exec_json.build_argv("claude-print", rw, binary="claude"), [
            "claude", "-p", "--output-format", "json", "--session-id", "s2", "--permission-mode", "acceptEdits"])
        self.assertEqual(exec_json.build_argv("andrewcode-oneshot", r, binary="andrewcode", prompt_file="/t/p.md"), [
            "andrewcode", "-m", "m1", "--thinking-effort", "high", "--prompt-file", "/t/p.md",
            "--output-format", "stream-json", "--disable-tools", "--ephemeral"])
        self.assertEqual(exec_json.build_argv(
            "andrewcode-oneshot", PeerRequest(prompt="p", tools="write", session_id="s3"), binary="andrewcode",
            prompt_file="/t/p.md"), ["andrewcode", "--prompt-file", "/t/p.md", "--output-format", "stream-json",
                                     "--auto", "-S", "s3"])
        with self.assertRaises(PeerError) as cm:  # J-09
            exec_json.build_argv("andrewcode-oneshot", rr, binary="andrewcode", prompt_file="/t/p.md")
        self.assertEqual(cm.exception.code, "invalid_request")
        self.assertEqual(exec_json.build_argv("codex-exec", r, binary="codex", schema_file="/t/s.json",
                                              last_file="/t/l.txt"), [
            "codex", "--no-daemon", "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--color", "never",
            "-s", "read-only", "-C", "/w", "-m", "m1", "-c", 'model_reasoning_effort="high"',
            "--output-schema", "/t/s.json", "-o", "/t/l.txt", "-"])
        self.assertIn("workspace-write", exec_json.build_argv(
            "codex-exec", PeerRequest(prompt="p", tools="write", cwd="/w"), binary="codex"))
        self.assertEqual(exec_json.build_argv("opencode-run", PeerRequest(prompt="hello", model="prov/mod#hi",
                                                                          cwd="/w"), binary="opencode"),
                         ["opencode", "run", "-m", "prov/mod#hi", "--format", "json", "--dir", "/w", "hello"])
        big = PeerRequest(prompt="x" * 17000, cwd="/w")
        a = exec_json.build_argv("opencode-run", big, binary="opencode", prompt_file="/w/.fusion-task.md")
        self.assertIn("/w/.fusion-task.md", a[-1])
        self.assertLess(len(a[-1]), 200)
        with self.assertRaises(PeerError):
            exec_json.build_argv("claude-acp", r, binary="x")

    def test_parse_each_transport_fixture(self):
        claude = json.dumps({"type": "result", "is_error": False, "result": "R", "structured_output": {"a": 1},
                             "session_id": "s", "total_cost_usd": 0.5, "usage": {"input_tokens": 1},
                             "model_usage": {"claude-x": {}}})
        p = exec_json.parse("claude-print", claude)
        self.assertEqual((p["ok"], p["text"], p["structured"], p["session_id"], p["cost_usd"], p["model"]),
                         (True, "R", {"a": 1}, "s", 0.5, "claude-x"))
        bad = exec_json.parse("claude-print", json.dumps({"is_error": True, "result": "", "subtype": "x"}))
        self.assertFalse(bad["ok"])
        self.assertFalse(exec_json.parse("claude-print", "not json")["ok"])
        ac = "\n".join(json.dumps(x) for x in [
            {"role": "meta", "type": "system.version", "session_id": "S1"},
            {"role": "assistant", "tool_calls": [{}]}, {"role": "tool", "content": "t"},
            {"role": "assistant", "content": "first"}, {"role": "assistant", "content": "final"}])
        p = exec_json.parse("andrewcode-oneshot", ac)
        self.assertEqual((p["ok"], p["text"], p["session_id"]), (True, "final", "S1"))
        p = exec_json.parse("andrewcode-oneshot", json.dumps({"role": "error", "content": "SECRET"}))
        self.assertFalse(p["ok"])
        self.assertNotIn("SECRET", json.dumps(p))
        cx = "\n".join(json.dumps(x) for x in [
            {"type": "thread.started", "thread_id": "T"},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "no"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "one"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "two"}},
            {"type": "turn.completed", "usage": {"input_tokens": 4}}])
        p = exec_json.parse("codex-exec", cx)
        self.assertEqual((p["ok"], p["text"], p["session_id"], p["usage"]), (True, "two", "T", {"input_tokens": 4}))
        p = exec_json.parse("codex-exec", json.dumps({"type": "thread.started", "thread_id": "T"}),
                            last_file_text="from file\n")
        self.assertEqual(p["text"], "from file")
        p = exec_json.parse("codex-exec", json.dumps({"type": "turn.failed", "error": {"message": "bad"}}))
        self.assertFalse(p["ok"])
        oc = "\n".join(json.dumps(x) for x in [
            {"type": "text", "sessionID": "O", "part": {"messageID": "a", "text": "old"}},
            {"type": "text", "part": {"messageID": "b", "text": "new "}},
            {"type": "text", "part": {"messageID": "b", "text": "msg"}}])
        p = exec_json.parse("opencode-run", oc)
        self.assertEqual((p["ok"], p["text"], p["session_id"]), (True, "new msg", "O"))
        self.assertFalse(exec_json.parse("opencode-run", "")["ok"])

    def test_feature_flags_cached_by_help_hash_and_flag_dropped(self):
        env = self.env(FUSION_ALLOW_CLAUDE="1")
        f1 = exec_json.feature_flags("claude", env=env)
        self.assertIn("--json-schema", f1)
        f2 = exec_json.feature_flags("claude", env=env)
        self.assertEqual(f1, f2)
        helps = [c for c in self.calls(include_help=True) if c.get("help")]
        self.assertEqual(len(helps), 1)  # second call served from cache
        cache = json.loads((self.home / "cache" / "cli-help.json").read_text("utf-8"))
        self.assertEqual(len(cache["by_help"]), 1)
        h = next(iter(cache["by_help"]))
        self.assertEqual(len(h), 64)
        # binary changes (size/mtime) -> re-probed, new hash, flag no longer advertised
        claude = self.bin / "claude"
        claude.write_text(claude.read_text("utf-8") + "\n# changed\n", "utf-8")
        env2 = dict(env, FAKE_PEER_HELP="Usage: claude\n  -p\n  --output-format <f>\n")
        f3 = exec_json.feature_flags("claude", env=env2)
        self.assertNotIn("--json-schema", f3)
        self.assertEqual(len([c for c in self.calls(include_help=True) if c.get("help")]), 2)
        # run() drops the unsupported optional flag and records a note
        res = exec_json.run("claude", PeerRequest(prompt="x", json_schema={"type": "object"}, cwd=str(self.work)),
                            env=env2)
        argv = [c for c in self.calls() if c["name"] == "claude"][-1]["argv"]
        self.assertNotIn("--json-schema", argv)
        self.assertIn("dropped unsupported flag --json-schema", res.error or "")
        self.assertEqual(exec_json.build_argv("claude-print", PeerRequest(prompt="x", json_schema={"a": 1}),
                                              binary="claude", flags={"--output-format"})[-4:],
                         ["--permission-mode", "dontAsk", "--disallowedTools",
                          "Bash Edit Write MultiEdit NotebookEdit WebFetch"])


class RunTests(Sandbox):
    def test_run_each_peer_end_to_end(self):
        env = self.env(FUSION_HOST="codex")  # claude allowed on a codex host
        r = exec_json.run("claude", PeerRequest(prompt="q", json_schema={"type": "object"}, cwd=str(self.work),
                                                session_id="abc"), env=env)
        self.assertTrue(r.ok, r.error)
        self.assertEqual((r.text, r.structured, r.session_id, r.model, r.cost_usd),
                         ("claude says hi", {"verdict": "ok"}, "sess-claude-1", "claude-fake-1", 0.0123))
        rec = [c for c in self.calls() if c["name"] == "claude"][0]
        self.assertNotIn("--no-session-persistence", rec["argv"])
        self.assertEqual(rec["stdin"], "q")
        a = exec_json.run("andrewcode", PeerRequest(prompt="q", json_schema={"type": "object"}), env=env)
        self.assertTrue(a.ok, a.error)
        self.assertEqual((a.structured, a.session_id), ({"verdict": "ok"}, "sess-ac-1"))
        arec = [c for c in self.calls() if c["name"] == "andrewcode"][0]
        self.assertIn("--disable-tools", arec["argv"])
        self.assertNotEqual(os.path.realpath(arec["cwd"]), os.path.realpath(os.getcwd()))
        o = exec_json.run("opencode", PeerRequest(prompt="q", cwd=str(self.work)), env=env)
        self.assertEqual((o.ok, o.text, o.session_id), (True, "opencode says hi", "ses-oc-1"))
        # claude seat is blocked on a Claude host
        with self.assertRaises(PeerError) as cm:
            exec_json.run("claude", PeerRequest(prompt="q"), env=self.env(CLAUDECODE="1"))
        self.assertEqual(cm.exception.code, "peer_not_allowed")

    def test_error_and_absent_status(self):
        env = self.env(FUSION_HOST="codex", FAKE_PEER_MODE="error")
        r = exec_json.run("claude", PeerRequest(prompt="q"), env=env)
        self.assertEqual((r.ok, r.status), (False, "error"))
        a = exec_json.run("andrewcode", PeerRequest(prompt="q"), env=env)
        self.assertEqual(a.status, "error")
        self.assertNotIn("SECRET", a.error or "")
        empty = dict(self.env(FUSION_HOST="codex"), PATH="/nonexistent-dir")
        r = exec_json.run("opencode", PeerRequest(prompt="q"), env=empty, probe_flags=False)
        self.assertEqual((r.status, r.ok), ("absent", False))

    def test_run_timeout_and_cancel_kill_group(self):
        pidfile = Path(self.tmp, "pid")
        env = self.env(FUSION_HOST="codex", FAKE_PEER_MODE="hang", FAKE_PEER_PIDFILE=str(pidfile))
        t0 = time.monotonic()
        r = exec_json.run("claude", PeerRequest(prompt="q", timeout=0.4), env=env, probe_flags=False)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual((r.status, r.ok), ("timeout", False))
        self._assert_dead(int(pidfile.read_text()))

        class Cancel:
            def __init__(self):
                self.ev = threading.Event()
                self.procs = []

            def is_set(self):
                return self.ev.is_set()

            def register(self, proc, identity=None):
                self.procs.append(proc)
                return lambda: None

        c = Cancel()
        timer = threading.Timer(0.3, c.ev.set)
        timer.start()
        try:
            r = exec_json.run("claude", PeerRequest(prompt="q", timeout=30), env=env, cancel=c, probe_flags=False)
        finally:
            timer.cancel()
        self.assertEqual((r.status, r.ok), ("cancelled", False))
        self.assertEqual(len(c.procs), 1)
        self._assert_dead(int(pidfile.read_text()))
        pre = Cancel()
        pre.ev.set()
        r = exec_json.run("claude", PeerRequest(prompt="q", timeout=30), env=env, cancel=pre, probe_flags=False)
        self.assertEqual(r.status, "cancelled")

    def _assert_dead(self, pid):
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                pass
            time.sleep(0.05)
        try:
            os.kill(pid, 9)
        finally:
            self.fail(f"grandchild {pid} survived the group kill")

    def test_as_dispatch_fn_contract(self):
        env = self.env(CLAUDECODE="1")
        fn = exec_json.as_dispatch_fn(env=env)
        d = fn("andrewcode", "hello", role="rev", timeout=30, model="gpt-6-luna", effort="low")
        for key in ("ok", "output", "status", "provider", "role", "rc", "elapsed_s", "model", "usage", "session_id"):
            self.assertIn(key, d)
        self.assertEqual((d["ok"], d["status"], d["provider"], d["role"], d["output"], d["rc"]),
                         (True, "returned", "andrewcode", "rev", "andrewcode says hi", 0))
        self.assertEqual(d["model"], "gpt-6-luna")
        argv = [c for c in self.calls() if c["name"] == "andrewcode"][-1]["argv"]
        self.assertEqual(argv[argv.index("--thinking-effort") + 1], "low")
        # codex on a Claude host is served by andrewcode
        d = fn("codex", "hello")
        self.assertEqual((d["ok"], d["model"]), (True, "gpt-6.1-sol"))
        self.assertNotIn("codex", [c["name"] for c in self.calls(include_help=True)])
        # blocked (claude seat on claude host) never raises
        d = fn("claude", "hello")
        self.assertEqual((d["ok"], d["status"]), (False, "blocked"))
        self.assertEqual(fn("grok", "hello")["status"], "absent")
        with self.assertRaises(TypeError):
            fn(None, "x")
        # usage shape from a fake runner
        def runner(argv, **kw):
            return 0, json.dumps({"type": "result", "result": "ok", "usage": {"input_tokens": 3, "output_tokens": 4},
                                  "total_cost_usd": 0.1}), ""
        d = exec_json.as_dispatch_fn(env=self.env(FUSION_HOST="codex"), runner=runner)("claude", "p")
        self.assertEqual(d["usage"], {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7, "cost_usd": 0.1})


if __name__ == "__main__":
    unittest.main()
