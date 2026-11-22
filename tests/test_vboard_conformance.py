"""Offline BoardPort conformance, plus the explicitly opt-in hosted parity run."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from fusion_swarm.vboard import cli, conformance
from fusion_swarm.vboard.claims import ClaimCache
from fusion_swarm.vboard.http import serve
from fusion_swarm.vboard.model import Caller
from fusion_swarm.vboard.port import HttpBoardAdapter, LocalEngineAdapter, VercelCliAdapter
from fusion_swarm.vboard.store import BoardStore

OPT = {7, 17, 29, 30, 31, 32, 60, 75, 76, 77, 78}


class ConformanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(self.root / "config"),
                        FUSION_HOME=str(self.root / "fusion"))
        for var in ("BOARD_URL", "BOARD_NAME", "BOARD_TOKEN", "FUSION_VBOARD_DB",
                    "FUSION_VBOARD_CONFORMANCE_URL", "FUSION_VBOARD_CONFORMANCE_TARGET",
                    "FUSION_VBOARD_CONFORMANCE_CHANNEL"):
            self.env.pop(var, None)

    def check_report(self, report):
        self.assertTrue(report.ok, [c.to_dict() for c in report.failed])

    def test_local_target_all_core_checks_pass(self):
        now = time.time()

        def clock():
            return now

        def advance(seconds):
            nonlocal now
            now += seconds

        store = BoardStore(self.root / "local.db", clock=clock)
        self.addCleanup(store.close)
        cache = ClaimCache(env=self.env, clock=clock)
        report = conformance.run(
            lambda profile: LocalEngineAdapter(store, as_=profile, claim_cache=cache, clock=clock),
            clock_control=advance,
        )
        self.check_report(report)
        self.assertFalse(report.skipped)
        core_ids = {f"c{i:02}" for i in range(1, 79) if i not in OPT}
        fusion_ids = {f"f{i:02}" for i in range(1, 6)}
        self.assertEqual({check.name.split("_", 1)[0] for check in report.passed}, core_ids | fusion_ids)
        self.assertEqual(len(report.passed), len(core_ids | fusion_ids))

    def test_report_shape_matches_upstream(self):
        report = conformance.Report(
            url="local",
            checks=[conformance.Check("core", "passed"), conformance.Check("tasks", "skipped", "skipped", "needs clock_control")],
            ok=True,
        )
        self.assertEqual(report.to_dict(), {
            "url": "local",
            "checks": [
                {"group": "core", "name": "passed", "status": "passed"},
                {"group": "tasks", "name": "skipped", "status": "skipped", "detail": "needs clock_control"},
            ],
            "ok": True,
        })

    def test_conformance_cli_local_target(self):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["conformance", "--group", "paging", "--json"], env=self.env, stdout=out, stderr=err)
        self.assertEqual(code, 0, err.getvalue())
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["url"], "local")
        self.assertTrue(doc["ok"], doc)
        self.assertEqual({c["group"] for c in doc["checks"]}, {"paging"})
        self.assertFalse((self.root / "fusion").exists())

        db_path = self.root / "explicit.db"
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["--db", str(db_path), "conformance", "--channel", "test/cli",
                         "--group", "paging", "--json"], env=self.env, stdout=out, stderr=err)
        self.assertEqual(code, 0, err.getvalue())
        store = BoardStore(db_path)
        self.addCleanup(store.close)
        posted = store.messages(Caller.local("conf-a"), "test/cli/c20")["messages"]
        self.assertEqual(len(posted), 4)
        self.assertTrue(all(message["deleted"] for message in posted))

    def test_loopback_target(self):
        store = BoardStore(self.root / "http.db")
        self.addCleanup(store.close)
        server = serve(store)
        self.addCleanup(server.stop)
        report = conformance.run(
            lambda profile: HttpBoardAdapter(server.url, as_=profile, token=server.token, env=self.env),
        )
        self.check_report(report)
        self.assertEqual(report.url, server.url)
        self.assertGreater(len(report.passed), 55)
        out, err = io.StringIO(), io.StringIO()
        env = dict(self.env, BOARD_TOKEN=server.token, FUSION_VBOARD_CONFORMANCE_URL=server.url)
        code = cli.main(["conformance", "--group", "paging", "--json"], env=env, stdout=out, stderr=err)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["url"], server.url)

    def test_fake_vercel_target_runs_vercel_subset(self):
        fixture = ROOT / "tests" / "fixtures" / "fake_vercel.py"

        def fake_runner(argv, **kw):
            return subprocess.run([sys.executable, str(fixture), *argv[1:]], **kw)

        env = dict(self.env, FAKE_VERCEL_DB=str(self.root / "fake.db"), PYTHONDONTWRITEBYTECODE="1")
        report = conformance.run(lambda profile: VercelCliAdapter(as_=profile, runner=fake_runner, env=env))
        self.check_report(report)
        expected = set(conformance.vercel_subset())
        self.assertTrue(expected)
        self.assertEqual({c.name for c in report.passed}, expected)
        self.assertEqual({c.name for c in report.skipped}, set(conformance.check_names()) - expected)

    def test_hosted_target_requires_disposable_channel(self):
        with mock.patch.object(VercelCliAdapter, "__init__", side_effect=AssertionError("must not start hosted CLI")):
            out, err = io.StringIO(), io.StringIO()
            env = dict(self.env, FUSION_VBOARD_CONFORMANCE_TARGET="vercel")
            code = cli.main(["conformance", "--json"], env=env, stdout=out, stderr=err)
        self.assertEqual(code, 2)
        self.assertIn("FUSION_VBOARD_CONFORMANCE_CHANNEL", json.loads(err.getvalue())["message"])

    @unittest.skipUnless(os.environ.get("FUSION_VBOARD_CONFORMANCE_TARGET") == "vercel"
                         and os.environ.get("FUSION_VBOARD_CONFORMANCE_CHANNEL", "").strip(),
                         "requires an explicitly opted-in disposable hosted channel")
    def test_vercel_target_optin(self):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["conformance", "--json"], stdout=out, stderr=err)
        self.assertEqual(code, 0, out.getvalue() or err.getvalue())
        self.assertTrue(json.loads(out.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
