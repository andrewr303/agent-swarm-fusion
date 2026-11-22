"""sp-fix: spawn.run_provider, ProcessCancel, race loser cancellation, CLI wiring."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from fusion_swarm import __main__ as cli  # noqa: E402
from fusion_swarm import evolve, harden, race, spawn  # noqa: E402

POSIX = os.name == "posix"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, limit: float = 5.0) -> bool:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {
            "FUSION_TEST_HOOKS": "1",
            "FUSION_HOME": str(self.dir / "home"),
            "XDG_CONFIG_HOME": str(self.dir / "xdg"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def script(self, body: str) -> str:
        path = self.dir / "fake-fusion.sh"
        path.write_text("#!/bin/bash\n# $1=dispatch $2=provider $3=prompt $4=out $5=model $6=effort $7=cwd\n" + body)
        os.environ["FUSION_SH_OVERRIDE"] = str(path)
        return str(path)


class RunProviderTests(Base):
    def test_run_provider_returned_text_and_meta(self):
        self.script(
            'echo "answer from $2" > "$4"\n'
            'echo \'{"model":"m-1","usage":{"total_tokens":7},"session_id":"s-9"}\' > "$4.meta.json"\n'
            'exit 0\n')
        res = spawn.run_provider("codex", "hello", role="r", timeout=10)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "returned")
        self.assertEqual(res["output"], "answer from codex")
        self.assertEqual((res["model"], res["session_id"]), ("m-1", "s-9"))
        self.assertEqual(res["usage"], {"total_tokens": 7})
        self.assertEqual((res["provider"], res["role"], res["rc"]), ("codex", "r", 0))
        self.assertIn("elapsed_s", res)

    def test_run_provider_rc_mapping_absent_timeout_blocked_error(self):
        for rc, status in ((127, "absent"), (124, "timeout"), (3, "blocked"), (9, "error")):
            self.script(f"exit {rc}\n")
            res = spawn.run_provider("codex", "p", timeout=10)
            self.assertEqual(res["status"], status, rc)
            self.assertEqual(res["rc"], rc)
            self.assertFalse(res["ok"])
        self.script('exit 0\n')  # returned with empty output -> error
        self.assertEqual(spawn.run_provider("codex", "p", timeout=10)["status"], "error")

    def test_override_ignored_without_test_hooks(self):
        self.script("exit 0\n")
        with mock.patch.dict(os.environ, {"FUSION_TEST_HOOKS": "0"}):
            self.assertEqual(spawn._fusion_sh_path(os.environ), str(spawn.FUSION_SH))

    def test_codex_seat_not_blocked_on_claude_host(self):
        self.script('echo "host=$FUSION_HOST" > "$4"\n')
        env = {k: v for k, v in os.environ.items() if k not in ("FUSION_HOST", "FUSION_HOST_EXPLICIT")}
        env["CLAUDECODE"] = "1"
        env["FUSION_HOST"] = "codex"
        res = spawn.run_provider("codex", "p", timeout=10, env=env)
        self.assertEqual(res["output"], "host=claude")

    @unittest.skipUnless(POSIX, "posix process groups")
    def test_run_provider_cancel_kills_group(self):
        pidfile = self.dir / "gc.pid"
        self.script(f'sleep 60 &\necho $! > "{pidfile}"\nwait\n')
        cancel = spawn.ProcessCancel()
        out: dict = {}
        import threading
        t = threading.Thread(target=lambda: out.update(spawn.run_provider("codex", "p", timeout=60, cancel=cancel)))
        t.start()
        end = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(pidfile.exists())
        time.sleep(0.05)
        gc = int(pidfile.read_text().strip())
        cancel.set()
        t.join(15)
        self.assertFalse(t.is_alive())
        self.assertEqual((out["status"], out["rc"], out["ok"]), ("cancelled", 130, False))
        self.assertTrue(_wait_dead(gc))

    @unittest.skipUnless(POSIX, "posix process groups")
    def test_run_provider_timeout_kills_group(self):
        pidfile = self.dir / "gc.pid"
        self.script(f'sleep 60 &\necho $! > "{pidfile}"\nwait\n')
        t0 = time.monotonic()
        res = spawn.run_provider("codex", "p", timeout=0.5)
        self.assertEqual((res["status"], res["rc"]), ("timeout", 124))
        self.assertLess(time.monotonic() - t0, 20)
        self.assertTrue(_wait_dead(int(pidfile.read_text().strip())))

    @unittest.skipUnless(POSIX, "posix process groups")
    def test_grandchild_killed_after_leader_exits(self):
        pidfile = self.dir / "gc.pid"
        self.script(f'(sleep 60 >/dev/null 2>&1 &\necho $! > "{pidfile}")\necho ok > "$4"\n')
        res = spawn.run_provider("codex", "p", timeout=10)
        self.assertEqual(res["status"], "returned")
        self.assertTrue(_wait_dead(int(pidfile.read_text().strip())))


class RaceWiringTests(Base):
    def test_race_cli_path_uses_run_provider(self):
        self.script('echo "solution $2" > "$4"\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["race", "do thing", "--providers", "codex,opencode", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["strategy"], "race")
        self.assertTrue(data["winner"]["qualified"])
        self.assertIn("solution", data["winner"]["output"])

    def test_unknown_provider_rejected(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["race", "t", "--providers", "codex,nonesuch"])
        self.assertEqual(rc, 2)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["pattern"], "race")
        self.assertIn("unknown provider", out["error"])

    def test_default_providers(self):
        self.assertEqual(race.DEFAULT_PROVIDERS, ("codex", "opencode", "grok"))
        self.assertEqual(evolve.DEFAULT_ISLAND_PROVIDERS, ("codex", "opencode", "grok"))

    @unittest.skipUnless(POSIX, "posix process groups")
    def test_race_first_passer_cancels_running_losers(self):
        self.script('if [ "$2" = codex ]; then echo fast > "$4"; exit 0; fi\nsleep 30\n')
        t0 = time.monotonic()
        res = race.run("t", providers=["codex", "opencode"], mode="race", timeout=60)
        wall = time.monotonic() - t0
        self.assertLess(wall, 20)
        self.assertEqual(res["winner"]["provider"], "codex")
        loser = [c for c in res["candidates"] if c["provider"] == "opencode"][0]
        self.assertEqual(loser["status"], "cancelled")
        self.assertFalse(loser.get("qualified"))

    def test_dispatcher_without_cancel_param_still_works(self):
        calls = []

        def fake(provider, prompt, *, role="", cwd=None, timeout=300):
            calls.append(provider)
            return {"ok": True, "output": "x"}
        res = race.run("t", providers=["a", "b"], mode="tournament", dispatch_fn=fake)
        self.assertEqual(sorted(calls), ["a", "b"])
        self.assertEqual(res["qualified_count"], 2)

    def test_harden_and_evolve_default_dispatch_resolves(self):
        self.script('echo "out from $2" > "$4"\n')
        ws = self.dir / "ws"
        ws.mkdir()
        h = harden.run("t", workspace=str(ws), max_waves=1, required_clean_waves=1, timeout=10)
        self.assertIsInstance(h, dict)
        e = evolve.run("t", islands=1, generations=1, timeout=10)
        self.assertIsInstance(e, dict)
        self.assertNotIn("AttributeError", json.dumps(e, default=str))


if __name__ == "__main__":
    unittest.main()
