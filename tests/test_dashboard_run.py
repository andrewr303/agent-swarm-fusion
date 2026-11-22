"""Bound run HTTP behavior and control security on an actual server."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "scripts"), str(ROOT / "tests")]
import dashboard_server as srv
from test_dashboard_api import _req
from fusion_swarm import team
from fusion_swarm.board import HiveBoard


class DashboardRunTests(unittest.TestCase):
    def test_bound_run_status_pause_resume_and_stale_control(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = {"run_id": "http-run", "task": "Bound run", "workspace": directory, "roster": [
                {"id": "lead", "model": "fixture", "provider": "native", "role": "lead"}, {"id": "worker", "model": "fixture", "provider": "native"}],
                "jobs": [{"id": "a", "goal": "Verify bound run", "assigned_to": "worker", "success_criteria": ["State matches persisted facts"]}]}
            db = root / "board.sqlite"
            team.prepare(team.compile_spec(spec), str(db))
            with HiveBoard(db) as board:
                board.create_run("lead", "http-run", "local", {"host": "local", "source": "local-process", "models": ["fixture"], "operations": ["dispatch"]})
            server = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = "http://127.0.0.1:" + str(server.server_port)
            headers = {"Origin": url, "X-Fusion-Token": srv.SESSION_TOKEN, "Content-Type": "application/json"}
            try:
                with patch.dict(os.environ, {"FUSION_BOARD": str(db), "FUSION_LEAD": "lead", "FUSION_RUN": "http-run"}):
                    code, view = _req("GET", url + "/api/run")
                    self.assertEqual(code, 200)
                    self.assertEqual(view["run"]["observed_state"], "ready")
                    control = {"state": "paused", "note": "Inspect before dispatch", "expected_version": 1}
                    self.assertEqual(_req("POST", url + "/api/run/control", control, {"X-Fusion-Token": srv.SESSION_TOKEN})[0], 403)
                    self.assertEqual(_req("POST", url + "/api/run/control", control, {**headers, "Origin": "https://foreign.example"})[0], 403)
                    self.assertEqual(_req("POST", url + "/api/run/control", control, headers)[0], 200)
                    self.assertEqual(_req("GET", url + "/api/run")[1]["run"]["state"], "paused")
                    stale = {"state": "active", "note": "stale resume", "expected_version": 1}
                    self.assertEqual(_req("POST", url + "/api/run/control", stale, headers)[0], 400)
                    self.assertEqual(_req("POST", url + "/api/run/control", {**stale, "expected_version": 2}, headers)[0], 200)
                    self.assertEqual(_req("GET", url + "/api/run/export")[1]["run"]["version"], 3)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
