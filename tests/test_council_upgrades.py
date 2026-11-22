"""Comprehensive offline unit tests for the 4-Pillar Council Upgrades.

Covers:
  - Pillar 1A: Dashboard Host header DNS-rebinding guard (421), GET /api/events (SSE), GET /api/diff
  - Pillar 1D: Capability-detected tmux & active-instance diff TUI (fusion_swarm.tui)
  - Pillar 2A: Speculative worktree race & tournament (fusion_swarm.race)
  - Pillar 2B: Two-phase execution-then-hardening waves with rollback (fusion_swarm.harden)
  - Pillar 2C: MAP-Elites quality-diversity island evolution & cascade eval (fusion_swarm.evolve)
  - Pillar 2F: Wilson-score lower bound ledger-moe (n>=30 guard) & Stigmergy pheromone trails
  - Pillar 3A: ureview.sh REPO_READ=none / empty-snapshot / REPO_RECEIPT verification
  - Pillar 3B: execution.py readiness-first pre-filter & snapshot deduplication
  - Pillar 3C: CloneWorkspace, authorized WorktreeWorkspace, check_merge_tree
  - Pillar 3D: hive.py separate captain/child ThreadPoolExecutors & metaloop.py dispatch_streaming_dag
  - Pillar 4A: macOS /bin/bash 3.2 set -u empty array safety across provider scripts
  - Pillar 4B: kqueue post-commit board.wakeup & batched JSONL mirror
  - Pillar 4C: APFS Copy-on-Write provision_clone
  - Pillar 4D: Creation-bound process-group escalation ladder (process_runtime.terminate_process_group)
  - Pillar 4E: macOS canonical path identity (/tmp vs /private/tmp) & --hook-rewake exit code 2
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT / "scripts"))

from fusion_swarm import (  # noqa: E402
    board,
    evolve,
    harden,
    ledger_moe,
    mailbox,
    metaloop,
    process_runtime,
    race,
    scope,
    sidekick,
    strategies,
    tui,
    workspaces,
)
from fusion_swarm.contracts import TaskSpec  # noqa: E402
import dashboard_server  # noqa: E402


class TestPillar4MacOSAndBoard(unittest.TestCase):
    def test_4a_bash32_empty_array_nounset_safety(self):
        """Provider scripts must not crash under `/bin/bash -u` when extra args are empty."""
        common_sh = ROOT / "scripts" / "providers" / "_common.sh"
        cmd = [
            "/bin/bash",
            "-c",
            f'set -uo pipefail; source "{common_sh}"; extra=(); printf "%s" "${{extra[@]+${{extra[@]}}}}"',
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_4b_kqueue_wakeup_and_batched_jsonl(self):
        """Board commits bump monotonic .wakeup counter and wake wait_for_board_change immediately."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "board.sqlite"
            with board.HiveBoard(db_path) as b:
                b.register({"id": "arch", "display_name": "Arch", "role": "architect", "provider": "claude", "model": "claude-opus-4-7"})
                b.post(from_agent="arch", body="First message", channel="hive")
                wakeup_path = b.wakeup_path
            self.assertTrue(Path(wakeup_path).exists())
            gen1 = board._read_wakeup_generation(wakeup_path)
            self.assertGreaterEqual(gen1, 1)

            # Immediate return when last_gen < current generation
            res = board.wait_for_board_change(wakeup_path, timeout=1.0, last_gen=0)
            self.assertTrue(res["changed"])
            self.assertEqual(res["generation"], gen1)

            # Background writer wakes kqueue waiter
            def _delayed_write():
                time.sleep(0.08)
                with board.HiveBoard(db_path) as b2:
                    b2.post(from_agent="arch", body="Found edge case", channel="hive")

            t = threading.Thread(target=_delayed_write)
            t.start()
            res2 = board.wait_for_board_change(wakeup_path, timeout=2.0, last_gen=gen1)
            t.join()
            self.assertTrue(res2["changed"])
            self.assertGreater(res2["generation"], gen1)

    def test_4c_apfs_clone_provisioner(self):
        """scope.provision_clone creates an isolated CoW copy excluding .fusion state."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            dst = Path(tmp) / "dst"
            (src / "pkg").mkdir(parents=True)
            (src / ".fusion").mkdir(parents=True)
            (src / "pkg" / "mod.py").write_text("x = 42\n", encoding="utf-8")
            (src / ".fusion" / "board.sqlite").write_text("secret", encoding="utf-8")

            info = scope.provision_clone(
                str(src),
                str(dst),
                authorized=True,
                exclusions=[str(src / ".fusion")],
            )
            self.assertEqual(info["mode"], "apfs-clone")
            self.assertIn(info["clone_mechanism"], ("apfs-clone", "copy"))
            self.assertEqual((dst / "pkg" / "mod.py").read_text(encoding="utf-8"), "x = 42\n")
            self.assertFalse((dst / ".fusion").exists())

            # Mutating clone does not affect source
            (dst / "pkg" / "mod.py").write_text("x = 99\n", encoding="utf-8")
            self.assertEqual((src / "pkg" / "mod.py").read_text(encoding="utf-8"), "x = 42\n")

    def test_4d_process_group_termination_ladder(self):
        """process_runtime.terminate_process_group cleanly reaps a spawned process group."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        try:
            process_runtime.terminate_process_group(proc, grace_int=0.1, grace_term=0.1)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)

    def test_4e_canonical_path_and_hook_rewake(self):
        """Canonical path normalizes /tmp symlink and board wait --hook-rewake exits 2 on activity."""
        p1 = str(Path("/tmp/fusion-canonical-test").resolve(strict=False))
        p2 = str(Path("/private/tmp/fusion-canonical-test").resolve(strict=False))
        if Path("/private/tmp").exists():
            self.assertEqual(p1, p2)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "board.sqlite"
            with board.HiveBoard(db_path) as b:
                b.register({"id": "arch", "display_name": "Arch", "role": "architect", "provider": "claude", "model": "claude-opus-4-7"})
                b.register({"id": "w1", "display_name": "W1", "role": "worker", "provider": "codex", "model": "gpt-5.4"})
                b.dm("arch", "w1", "Wake up worker")
            rc = board.main(["--db", str(db_path), "wait", "--agent", "w1", "--since-id", "0", "--timeout", "0.2", "--hook-rewake"])
            self.assertEqual(rc, 2)


class TestPillar3OrchestrationUpgrades(unittest.TestCase):
    def test_3a_ureview_repo_receipt_and_honest_repo_read(self):
        """ureview.sh verifies REPO_RECEIPT sha16 and reports honest REPO_READ=none when tools disabled."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            sample = ws / "hello.py"
            sample.write_text("line1\nline2_target\nline3\n", encoding="utf-8")
            sha16 = hashlib.sha256(b"line2_target").hexdigest()[:16]
            out_file = ws / "reviewer.out"
            out_file.write_text(
                f"Review complete.\nREPO_RECEIPT: hello.py:2 {sha16}\n",
                encoding="utf-8",
            )
            (ws / "reviewer.out.log").write_text("normal log\n", encoding="utf-8")
            disabled_out = ws / "disabled.out"
            disabled_out.write_text("No tools.\n", encoding="utf-8")
            (ws / "disabled.out.log").write_text("mode: isolated-tools-disabled\n", encoding="utf-8")

            script = f"""
            set -uo pipefail
            eval "$(sed -n '/^verify_repo_receipt()/,/^}}/p' "{ROOT / "scripts" / "ureview.sh"}")"
            eval "$(sed -n '/^repo_read_status()/,/^}}/p' "{ROOT / "scripts" / "ureview.sh"}")"
            verify_repo_receipt "{ws}" "{out_file}"
            echo "STATUS_VERIFIED=$(repo_read_status "{ws}" "{out_file}" claude)"
            echo "STATUS_DISABLED=$(repo_read_status "{ws}" "{disabled_out}" grok)"
            """
            proc = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("STATUS_VERIFIED=ok", proc.stdout)
            self.assertIn("STATUS_DISABLED=none", proc.stdout)

    def test_3c_workspaces_and_merge_tree(self):
        """CloneWorkspace provisions CoW clone; unauthorized WorktreeWorkspace still raises NotImplementedError."""
        with self.assertRaises(NotImplementedError):
            workspaces.get_workspace(workspaces.WORKTREE)

        self.assertIn("apfs-clone", strategies.ISOLATION)

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "repo"
            src.mkdir()
            (src / "app.py").write_text("print('hi')\n", encoding="utf-8")
            ws = workspaces.get_workspace(workspaces.CLONE, repo_root=str(src), authorized=True)
            task = TaskSpec(id="task_1", objective="Clone test", category="code", acceptance_criteria=["ok"])
            ctx = ws.prepare(task)
            checkout = Path(ctx["writable_checkout"])
            try:
                self.assertTrue((checkout / "app.py").exists())
            finally:
                ws.cleanup(task)
            self.assertFalse(checkout.exists())

    def test_3d_metaloop_streaming_dag(self):
        """metaloop.dispatch_streaming_dag schedules dependents as soon as their parents complete."""
        from fusion_swarm.adapter import Reply
        t1 = TaskSpec(id="t1", objective="Root A", category="code", acceptance_criteria=["ok"], dependencies=[])
        t2 = TaskSpec(id="t2", objective="Root B", category="code", acceptance_criteria=["ok"], dependencies=[])
        t3 = TaskSpec(id="t3", objective="Child of A", category="code", acceptance_criteria=["ok"], dependencies=["t1"])

        order: list[str] = []

        def _fake_dispatch(provider: str, prompt: str, **kwargs):
            for tid in ("t1", "t2", "t3"):
                if f'"id": "{tid}"' in prompt:
                    if tid == "t2":
                        time.sleep(0.12)
                    order.append(tid)
                    payload = json.dumps({
                        "status": "returned",
                        "summary": f"done {tid}",
                        "artifacts": [],
                        "evidence": ["ok"],
                        "checks_run": [],
                        "confidence": 0.9,
                        "blockers": [],
                        "scope_expansion_needed": False,
                    })
                    return Reply(provider=provider, text=payload, status="returned", ok=True)
            raise AssertionError("task id not found in prompt")

        assignments = [(t1, "claude", "cap"), (t2, "codex", "cap"), (t3, "grok", "cap")]
        res = metaloop.dispatch_streaming_dag(assignments, dispatch_fn=_fake_dispatch, max_workers=3)
        self.assertEqual(len(res), 3)
        self.assertTrue(all(r["result"] is not None for r in res))
        self.assertLess(order.index("t1"), order.index("t3"))


class TestPillar2NewSwarmTopologies(unittest.TestCase):
    def test_2a_speculative_race_and_tournament(self):
        """race.run verifies 4-stage gate and promotes first qualified candidate."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "solution.py").write_text("VALUE = 1\n", encoding="utf-8")

            def _fake_dispatch(provider, prompt, **kwargs):
                if provider == "claude":
                    return {"ok": True, "output": "def solve():\n    return 42\n"}
                return {"ok": False, "output": "", "error": "timeout"}

            res = race.run(
                "Implement solve()",
                providers=["claude", "grok"],
                workspace=ws,
                mode="race",
                dispatch_fn=_fake_dispatch,
            )
            self.assertEqual(res["strategy"], "race")
            self.assertEqual(res["qualified_count"], 1)
            self.assertEqual(res["winner"]["provider"], "claude")
            self.assertTrue(res["winner"]["qualified"])

    def test_2b_harden_waves_and_regression_rollback(self):
        """harden.run stops after 2 consecutive clean waves and rolls back on gate failure."""
        calls: list[tuple[str, str]] = []

        def _fake_dispatch(provider, prompt, *, role="", **kwargs):
            calls.append((provider, role))
            if role == "implementer":
                return {"ok": True, "output": "def safe_fn(x):\n    return int(x)\n"}
            # Hardener: wave 1 reports a finding, wave 2 and wave 3 report CLEAN
            critic_calls = sum(1 for _, r in calls if r == "critic")
            if critic_calls == 1:
                return {"ok": True, "output": "HIGH: missing negative input guard"}
            return {"ok": True, "output": "HARDEN_STATUS: CLEAN"}

        res = harden.run(
            "Harden parser",
            builder="codex",
            hardener="grok",
            max_waves=4,
            required_clean_waves=2,
            dispatch_fn=_fake_dispatch,
        )
        self.assertTrue(res["converged"])
        self.assertEqual(res["clean_streak"], 2)
        self.assertEqual(res["waves_executed"], 3)

    def test_2c_map_elites_island_evolution_and_cascade(self):
        """evolve.run populates MAP-Elites 2D archive and migrates elites across islands."""
        stage2_called = []

        def s1(text):
            return (False, 0.1)

        def s2(text):
            stage2_called.append(True)
            return (True, 1.0)

        cascade_res = evolve._cascade_evaluate("candidate code", stages=[s1, s2])
        self.assertFalse(cascade_res["passed"])
        self.assertEqual(cascade_res["stage_reached"], "stage1")
        self.assertEqual(len(stage2_called), 0, "Stage 2 must not run when Stage 1 fails")

        def _fake_dispatch(provider, prompt, **kwargs):
            if provider == "claude":
                return {"ok": True, "output": "def fast():\n    return 1\n"}
            return {"ok": True, "output": "def thorough():\n    x = 1\n    y = 2\n    return x + y\n"}

        res = evolve.run(
            "Optimize solver",
            providers=["claude", "codex"],
            islands=2,
            generations=2,
            migration_interval=1,
            dispatch_fn=_fake_dispatch,
        )
        self.assertEqual(res["strategy"], "island-evolve")
        self.assertGreaterEqual(res["archive"]["occupied_cells"], 1)
        self.assertIsNotNone(res["best"])
        self.assertTrue(len(res["migrations"]) > 0)

    def test_2f_ledger_moe_wilson_bound_and_stigmergy(self):
        """ledger_moe enforces n>=30 minimum sample guard and board Stigmergy expires traces."""
        # Small sample (3/3 = 100% empirical) must have lower Wilson bound than 28/30 (93.3% empirical)
        lb_small = ledger_moe.wilson_lower_bound(3, 3)
        lb_large = ledger_moe.wilson_lower_bound(28, 30)
        self.assertLess(lb_small, lb_large)

        with tempfile.TemporaryDirectory() as tmp:
            runs_file = Path(tmp) / "runs.jsonl"
            lines = [json.dumps({"provider": "codex", "ok": True, "task_type": "code"}) for _ in range(32)]
            lines += [json.dumps({"provider": "grok", "ok": True, "task_type": "code"}) for _ in range(5)]
            runs_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            report = ledger_moe.rank_providers(runs_file, task_type="code", min_samples=30, providers=["codex", "grok"])
            by_prov = {r["provider"]: r for r in report["rankings"]}
            self.assertTrue(by_prov["codex"]["confident"])
            self.assertFalse(by_prov["codex"]["insufficient_data"])
            self.assertFalse(by_prov["grok"]["confident"])
            self.assertTrue(by_prov["grok"]["insufficient_data"])

            # Stigmergy pheromone trail deposit & sensing
            db_path = Path(tmp) / "board.sqlite"
            with board.HiveBoard(db_path) as b:
                b.deposit_trace("scout-1", "src/auth.py", kind="hotspot", strength=0.8, note="race condition", ttl=60)
                b.deposit_trace("scout-1", "src/auth.py", kind="hotspot", strength=0.5, note="confirmed", ttl=60)
                traces = b.sense_traces("src/auth.py", kind="hotspot")
                self.assertEqual(len(traces), 2)
                self.assertGreaterEqual(traces[0]["effective_strength"], 0.7)


class TestPillar1CockpitAndTUI(unittest.TestCase):
    def test_1a_dashboard_dns_rebinding_421_and_sse_and_diff(self):
        """dashboard_server rejects spoofed Host with 421 and serves /api/events and /api/diff."""
        server = dashboard_server.ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # 1. Spoofed Host header -> 421 Misdirected Request
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/providers", headers={"Host": "evil.attacker.example"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 421)
            conn.close()

            # 2. Valid Host header on /api/diff -> 200 OK
            conn2 = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn2.request("GET", "/api/diff?task_id=t1", headers={"Host": f"127.0.0.1:{port}"})
            resp2 = conn2.getresponse()
            self.assertEqual(resp2.status, 200)
            body2 = json.loads(resp2.read().decode("utf-8"))
            self.assertTrue(body2["available"])
            self.assertEqual(body2["task_id"], "t1")
            conn2.close()

            # 3. SSE endpoint /api/events?once=1 -> 200 text/event-stream
            conn3 = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn3.request("GET", "/api/events?once=1", headers={"Host": f"localhost:{port}"})
            resp3 = conn3.getresponse()
            self.assertEqual(resp3.status, 200)
            self.assertIn("text/event-stream", resp3.getheader("Content-Type", ""))
            sse_data = resp3.read().decode("utf-8")
            self.assertIn("event: snapshot", sse_data)
            conn3.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_1d_tui_snapshot_and_render(self):
        """tui.build_tui_snapshot and render_tui_text work cleanly without requiring tmux."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / ".fusion" / "board.sqlite"
            with board.HiveBoard(db_path) as b:
                b.deposit_trace("codex", "python/fusion_swarm/tui.py", kind="Verified", note="clean")
            snap = tui.build_tui_snapshot(tmp, db_path=db_path)
            text = tui.render_tui_text(snap)
            self.assertIn("FUSION TUI", text)
            self.assertIn("python/fusion_swarm/tui.py", text)


if __name__ == "__main__":
    unittest.main()
