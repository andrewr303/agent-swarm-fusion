"""Tests for the engine manager (python/fusion_swarm/vboard/engine.py).

stdlib unittest. Never installs, never starts real Node, never touches the network beyond 127.0.0.1: the `node`, `vb`
and `npx` programs are fake scripts written into a temp PATH (a tiny Python server answers the discovery document).
FUSION_HOME and XDG_CONFIG_HOME point into a temp dir and every call passes an explicit `env` mapping.
Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_vboard_engine -v   (from the plugin root)
"""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from fusion_swarm.vboard import engine  # noqa: E402
from fusion_swarm.vboard.model import VBoardError  # noqa: E402
from fusion_swarm.vboard.port import HttpBoardAdapter, LocalEngineAdapter  # noqa: E402

SERVER = r"""
import http.server, json, os, sys, threading

def log(line):
    path = os.environ.get("FAKE_ENGINE_LOG")
    if path:
        with open(path, "a") as fh:
            fh.write(line + "\n")

args = sys.argv[1:]
prog = os.path.basename(sys.argv[0])
log(json.dumps({"prog": prog, "argv": args}))
if args == ["--version"]:
    print(os.environ.get("FAKE_%s_VERSION" % prog.upper(), "%s" % ("v22.20.0" if prog == "node" else "0.54.0")))
    sys.exit(0)
if prog == "npx":
    sys.exit(0)
if "serve" not in args:
    sys.exit(2)
if os.environ.get("FAKE_ENGINE_MODE") == "die":
    print("boom", file=sys.stderr)
    sys.exit(3)
port = int(args[args.index("--port") + 1])
if os.environ.get("FAKE_ENGINE_MODE") == "hang":
    threading.Event().wait()

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        url = "http://127.0.0.1:%d/api/board" % port
        if self.path.rstrip("/") != "/api/board":
            self.send_response(404); self.end_headers(); return
        body = json.dumps({"version": "0.54.0", "url": url, "audience": url, "team_id": None,
                           "project_id": None, "sync": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
"""


@unittest.skipUnless(os.name == "posix", "fake engine scripts need a POSIX shell")
class EngineBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.home = self.tmp / "fusion-home"
        self.log = self.tmp / "engine.log"
        self.env = {"PATH": str(self.bin), "FUSION_HOME": str(self.home), "HOME": str(self.tmp),
                    "XDG_CONFIG_HOME": str(self.tmp / "xdg"), "FAKE_ENGINE_LOG": str(self.log)}
        self.scopes: list[str] = []
        self.addCleanup(self._stop_all)

    def _stop_all(self) -> None:
        for scope in self.scopes:
            try:
                engine.stop(scope, env=self.env, grace=2.0)
            except Exception:  # noqa: BLE001
                pass

    def script(self, name: str, directory: Path | None = None) -> Path:
        path = (directory or self.bin) / name
        path.write_text(f"#!{sys.executable}\n{SERVER}")
        path.chmod(0o755)
        return path

    def install_cli(self) -> Path:
        cli = engine.installed_cli(self.env)
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text("// fake cli.mjs\n")
        return cli

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def wait_gone(self, pid: int) -> bool:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not engine.pid_alive(pid):
                return True
            time.sleep(0.02)
        return False


class ResolveTests(EngineBase):
    def test_resolve_order_env_then_path_then_installed(self):
        self.assertIsNone(engine.resolve(self.env))                  # empty PATH, nothing installed
        self.install_cli()
        self.assertIsNone(engine.resolve(self.env))                  # installed cli but no node
        self.script("node")
        spec = engine.resolve(self.env)
        self.assertEqual((spec.source, spec.argv[0], spec.argv[1]), ("installed", str(self.bin / "node"),
                                                                     str(engine.installed_cli(self.env))))
        self.script("vb")
        spec = engine.resolve(self.env)                              # `vb` on PATH wins over the installed cli
        self.assertEqual((spec.source, spec.argv, spec.version), ("path", (str(self.bin / "vb"),), "0.54.0"))
        override = self.script("my-engine", self.tmp)
        spec = engine.resolve({**self.env, "FUSION_VBOARD_ENGINE": str(override)})
        self.assertEqual((spec.source, spec.argv), ("env", (str(override),)))
        mjs = self.tmp / "custom.mjs"
        mjs.write_text("//")
        spec = engine.resolve({**self.env, "FUSION_VBOARD_ENGINE": str(mjs)})
        self.assertEqual((spec.source, spec.argv), ("env", (str(self.bin / "node"), str(mjs))))
        spec = engine.resolve({**self.env, "FUSION_VBOARD_ENGINE": str(self.tmp / "missing")})
        self.assertEqual(spec.source, "path")                        # a bad override falls through

    def test_old_node_is_rejected(self):
        self.install_cli()
        self.script("node")
        self.assertIsNotNone(engine.resolve(self.env))
        self.assertTrue(engine.node_ok(str(self.bin / "node"), env=self.env))
        old = {**self.env, "FAKE_NODE_VERSION": "v22.17.9"}
        self.assertIsNone(engine.resolve(old))
        self.assertIsNone(engine.resolve({**self.env, "FAKE_NODE_VERSION": "v20.11.0"}))
        self.assertIsNotNone(engine.resolve({**self.env, "FAKE_NODE_VERSION": "v22.18.0"}))
        self.assertIsNotNone(engine.resolve({**self.env, "FAKE_NODE_VERSION": "v26.13.0"}))

    def test_npx_only_with_explicit_opt_in(self):
        self.script("npx")
        self.assertIsNone(engine.resolve(self.env))
        spec = engine.resolve({**self.env, "FUSION_VBOARD_ALLOW_NPX": "1"})
        self.assertEqual((spec.source, spec.argv[1:]), ("npx", ("-y", engine.ENGINE_PACKAGE)))
        self.assertEqual(engine.ENGINE_PACKAGE, "@vercel/board@0.54.0")


class LifecycleTests(EngineBase):
    def test_start_records_state_reuses_and_stops(self):
        self.install_cli()
        self.script("node")
        self.scopes.append("t1")
        info = engine.start("t1", env=self.env)
        self.assertEqual(info["dir"], str(self.home / "board" / "t1"))
        self.assertRegex(info["url"], r"^http://127\.0\.0\.1:\d+/api/board$")
        doc = engine.discovery(info["url"])
        self.assertEqual((doc["version"], doc["audience"]), ("0.54.0", info["url"]))
        state_file = self.home / "board" / "t1" / "engine.json"
        self.assertEqual(stat.S_IMODE(state_file.stat().st_mode), 0o600)
        state = json.loads(state_file.read_text())
        self.assertEqual((state["pid"], state["url"], state["scope"]), (info["pid"], info["url"], "t1"))
        serve_call = [c for c in self.calls() if "serve" in c["argv"]][0]["argv"]
        self.assertEqual(serve_call[:1], [str(engine.installed_cli(self.env))])
        port = info["url"].split(":")[2].split("/")[0]
        self.assertEqual(serve_call[1:], ["serve", "--port", port, "--dir", info["dir"], "--no-inspect",
                                          "--no-open"])
        self.assertEqual(engine.live_state("t1", self.env)["pid"], info["pid"])
        again = engine.start("t1", env=self.env)                     # a live engine is reused, never doubled
        self.assertEqual((again["pid"], again["url"], again["reused"]), (info["pid"], info["url"], True))
        self.assertEqual(len([c for c in self.calls() if "serve" in c["argv"]]), 1)
        res = engine.stop("t1", env=self.env, grace=3.0)
        self.assertEqual((res["stopped"], res["pid"]), (True, info["pid"]))
        self.assertTrue(self.wait_gone(info["pid"]))
        self.assertFalse(state_file.exists())
        self.assertIsNone(engine.live_state("t1", self.env))
        self.assertEqual(engine.stop("t1", env=self.env), {"stopped": False, "reason": "not_running"})

    def test_stale_state_is_removed_without_signalling(self):
        directory = self.home / "board" / "stale"
        directory.mkdir(parents=True)
        (directory / "engine.json").write_text(json.dumps({"pid": os.getpid(), "url": "http://127.0.0.1:9/api/board"}))
        with mock.patch.object(os, "kill") as kill:
            res = engine.stop("stale", env=self.env)
        self.assertEqual((res["stopped"], res["reason"]), (False, "stale"))   # our own pid is alive, nothing answers
        for call in kill.call_args_list:
            self.assertNotEqual(call.args[1], 15)                              # SIGTERM never sent
        self.assertFalse((directory / "engine.json").exists())

    def test_start_failures(self):
        with self.assertRaises(VBoardError) as cm:
            engine.start("none", env=self.env)
        self.assertEqual(cm.exception.code, "not_supported")
        self.assertIn("install", cm.exception.hint)
        self.install_cli()
        self.script("node")
        with self.assertRaises(VBoardError) as cm:
            engine.start("dies", env={**self.env, "FAKE_ENGINE_MODE": "die"})
        self.assertEqual(cm.exception.code, "unavailable")
        self.assertIn("exited with 3", cm.exception.message)
        self.assertIn("boom", (self.home / "board" / "dies" / "engine.log").read_text())
        self.assertIsNone(engine.read_state("dies", self.env))
        with self.assertRaises(VBoardError) as cm:
            engine.start("hangs", env={**self.env, "FAKE_ENGINE_MODE": "hang"}, ready_timeout=0.15)
        self.assertIn("did not answer", cm.exception.message)
        with self.assertRaises(ValueError):
            engine.start("Bad Scope!", env=self.env)

    def test_install_prints_command_and_runs_only_with_yes(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
        out = io.StringIO()
        res = engine.install(env=self.env, run=runner, out=out)
        self.assertEqual((res["ran"], runner.call_count), (False, 0))
        cmd = res["command"]
        self.assertEqual(cmd[1:], ["i", "--prefix", str(self.home / "engine"), "@vercel/board@0.54.0"])
        self.assertIn("@vercel/board@0.54.0", out.getvalue())
        self.assertIn("--yes", out.getvalue())
        res = engine.install(yes=True, env=self.env, run=runner, out=io.StringIO())
        self.assertEqual((res["ran"], res["rc"], runner.call_count), (True, 0, 1))
        self.assertEqual(runner.call_args[0][0], cmd)

    def test_command_line(self):
        out = io.StringIO()
        self.assertEqual(engine.main(["resolve"], out=out, env=self.env), 1)
        self.assertEqual(json.loads(out.getvalue()), {"engine": None})
        out = io.StringIO()
        self.assertEqual(engine.main(["install"], out=out, env=self.env), 0)
        self.assertIn("not run", out.getvalue())
        self.assertFalse((self.home / "engine").exists())            # nothing was installed
        out = io.StringIO()
        self.assertEqual(engine.main(["status", "--scope", "x"], out=out, env=self.env), 1)
        self.assertEqual(engine.main(["bogus"], out=io.StringIO(), env=self.env), 2)
        proc = subprocess.run([sys.executable, "-m", "fusion_swarm.vboard.engine", "resolve"], capture_output=True,
                              text=True, env={**self.env, "PYTHONPATH": str(ROOT / "python"),
                                              "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual((proc.returncode, json.loads(proc.stdout)), (1, {"engine": None}))


class OpenBoardTests(EngineBase):
    def test_selection_order(self):
        self.install_cli()
        self.script("node")
        self.scopes.append("default")
        # 1. an explicit url always wins and starts nothing
        explicit = engine.open_board("http://127.0.0.1:9/api/board", as_="w1", env=self.env)
        self.addCleanup(explicit.close)
        self.assertEqual((type(explicit), explicit.selection), (HttpBoardAdapter, "explicit-url"))
        self.assertEqual(self.calls(), [])
        # 2. nothing running, a resolvable engine: it is started
        started = engine.open_board(as_="w1", env=self.env)
        self.addCleanup(started.close)
        self.assertEqual((type(started), started.selection), (HttpBoardAdapter, "engine-started"))
        self.assertEqual(started.capabilities().version, "0.54.0")   # it really talks to the fake engine
        # 3. a live state file is reused, nothing is started twice
        again = engine.open_board(as_="w1", env=self.env)
        self.addCleanup(again.close)
        self.assertEqual((again.selection, again.url), ("engine-state", started.url))
        self.assertEqual(len([c for c in self.calls() if "serve" in c["argv"]]), 1)
        # 4. no engine at all (stopped, empty PATH): the Python local engine, logged
        engine.stop("default", env=self.env, grace=3.0)
        empty = dict(self.env, PATH=str(self.tmp / "nothing"))
        with self.assertLogs("fusion_swarm.vboard", level="INFO") as logs:
            local = engine.open_board(as_="w1", env=empty)
        self.addCleanup(local.close)
        self.assertEqual((type(local), local.selection), (LocalEngineAdapter, "local-fallback"))
        self.assertTrue((self.home / "vboard" / "default.db").exists())
        self.assertTrue(any("local-fallback" in line for line in logs.output))
        self.assertEqual(local.actor_name, "w1")

    def test_failed_engine_start_falls_back_and_scope_isolates(self):
        self.install_cli()
        self.script("node")
        broken = dict(self.env, FAKE_ENGINE_MODE="die")
        with self.assertLogs("fusion_swarm.vboard", level="WARNING"):
            port = engine.open_board(scope="s2", as_="w1", env=broken)
        self.addCleanup(port.close)
        self.assertEqual((type(port), port.selection), (LocalEngineAdapter, "local-fallback"))
        self.assertTrue((self.home / "vboard" / "s2.db").exists())
        self.assertIsNone(engine.live_state("default", self.env))
        port2 = engine.open_board(start_engine=False, as_="w1", env=self.env)   # never starts when told not to
        self.addCleanup(port2.close)
        self.assertEqual(port2.selection, "local-fallback")

    def test_never_runs_npx_without_opt_in(self):
        self.script("npx")
        port = engine.open_board(as_="w1", env=self.env)
        self.addCleanup(port.close)
        self.assertEqual(port.selection, "local-fallback")
        self.assertEqual([c for c in self.calls() if c["prog"] == "npx"], [])
        self.assertFalse(engine.live_state("default", self.env))


if __name__ == "__main__":
    unittest.main()
