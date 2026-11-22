"""Offline tests for discovery, allocation, blackboard, planning, and the forms CLI."""
from __future__ import annotations

import dataclasses
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from fusion_swarm import forms
from fusion_swarm.forms import auction, blackboard, collab, journal, loop_until_dry, planner
from fusion_swarm.forms.ledger import ReliabilityLedger
from fusion_swarm.forms.runtime import FormContext, Verdict
from fusion_swarm.vboard.port import LocalEngineAdapter


def fake_dispatch(scripts):
    queues = {seat: deque(responses) for seat, responses in scripts.items()}
    lock = threading.Lock()
    prompts = []

    def dispatch(provider, prompt, **kwargs):
        with lock:
            prompts.append((provider, prompt))
            output = queues[provider].popleft()
        return {"ok": True, "output": output}

    return dispatch, prompts


class FormsC(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.env = {
            "FUSION_HOME": str(self.home), "XDG_CONFIG_HOME": str(self.root / "xdg"),
            "FUSION_LEDGER_PATH": str(self.home / "ledger.json"), "PYTHONPATH": str(ROOT / "python"),
            "PYTHONDONTWRITEBYTECODE": "1", "FUSION_TEST_HOOKS": "1",
        }
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _context(self, name, **kwargs):
        return FormContext(name, run_dir=self.root / name, **kwargs)

    def _local(self, actor="lead"):
        port = LocalEngineAdapter(self.root / "board.db", as_=actor)
        self.addCleanup(port.close)
        return port

    def _cli(self, *args, code=0):
        out = subprocess.run([sys.executable, "-B", "-m", "fusion_swarm.forms", *map(str, args)],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, code, out.stdout + out.stderr)
        self.assertEqual(out.stderr, "")
        return json.loads(out.stdout)

    def test_auction_thompson_floor_and_verified_only_updates(self):
        ledger = ReliabilityLedger(rng=random.Random(3))
        for _ in range(8):
            ledger.record("experienced", "triage", "worker", True, verified=True)
        tasks = [{"id": "1", "text": "one", "task_class": "triage"}]
        dispatch, _ = fake_dispatch({"new": ["result", "result"]})
        result = auction.run(tasks, bidders=["experienced", "new"], explore=1,
                             ctx=self._context("unverified", dispatch_fn=dispatch, ledger=ledger),
                             self_bids={"experienced": {"confidence": 100}})
        self.assertEqual(result.extra["allocation"][0]["bidder"], "new")
        self.assertTrue(result.extra["allocation"][0]["explored"])
        self.assertEqual(ledger.cell("new", "triage", "worker").n, 0)

        result = auction.run(tasks, bidders=["experienced", "new"], explore=1,
                             ctx=self._context("verified", dispatch_fn=dispatch, ledger=ledger,
                                               verifier=lambda _: Verdict(True, 1, "exec")))
        self.assertEqual(result.status, "ok")
        self.assertEqual(ledger.cell("new", "triage", "worker").n, 1)

    def test_loop_until_dry_stops_after_dry_rounds_and_dedups(self):
        report = {"file": "SRC.py", "symbol": "Fn", "issue_class": "case", "summary": "summary", "evidence": "source"}
        dispatch, prompts = fake_dispatch({"one": [json.dumps([report]), "[]"],
                                           "two": [json.dumps([{**report, "file": "src.py"}])]})
        result = loop_until_dry.run("Find observations", finders=["one", "two"], per_round=1,
                                    scopes=["one scope", "next scope"],
                                    ctx=self._context("discovery", dispatch_fn=dispatch,
                                                      verifier=lambda _: Verdict(True, 1, "exec")))
        self.assertEqual((result.status, result.stop_reason, result.rounds), ("ok", "dry", 3))
        self.assertEqual(len(result.candidates), 1)
        self.assertNotIn("src.py:fn:case", prompts[0][1])
        self.assertIn("src.py:fn:case", prompts[1][1])
        self.assertIn("next scope", prompts[1][1])
        self.assertEqual(result.extra["coverage_detail"]["f2"], 1)

    def test_loop_until_dry_chao1(self):
        self.assertEqual(loop_until_dry.chao1({"a": 1, "b": 1, "c": 2, "d": 3}), {
            "estimate": 6.0, "observed": 4, "f1": 2, "f2": 1, "completeness": 0.667,
        })
        self.assertEqual(loop_until_dry.chao1({})["completeness"], 1.0)

    @staticmethod
    def _blackboard_scripts():
        entries = [
            ("resolver", "claim", "claim text", ["e1"]),
            ("researcher", "evidence", "evidence text", ["e2"]),
            ("decider", "decision", "decision text", ["e3"]),
            ("judge", "verdict", "verdict text", ["e3"]),
        ]
        return {role: [json.dumps({"kind": kind, "text": text, "cites": cites})]
                for role, kind, text, cites in entries}

    def test_blackboard_rule_table_order_and_stall_hash(self):
        scripts = self._blackboard_scripts()
        dispatch, _ = fake_dispatch(scripts)
        result = blackboard.run("Resolve this test question", roles={role: role for role in scripts},
                                ctx=self._context("blackboard", dispatch_fn=dispatch))
        self.assertEqual(result.status, "ok")
        self.assertEqual([a["rule"] for a in result.extra["activations"]], [1, 2, 3, 4])
        self.assertEqual(result.winner["kind"], "verdict")

        stale, _ = fake_dispatch({"resolver": ["{}", "{}"]})
        result = blackboard.run("Resolve another question", roles={role: role for role in scripts},
                                ctx=self._context("stale-board", dispatch_fn=stale))
        self.assertEqual(result.stop_reason, "stalled")
        self.assertEqual([a["repeats"] for a in result.extra["activations"]], [1, 2])

    def test_blackboard_mirrors_to_port(self):
        port = self._local()
        scripts = self._blackboard_scripts()
        dispatch, _ = fake_dispatch(scripts)
        result = blackboard.run("Resolve a mirrored question", roles={role: role for role in scripts}, port=port,
                                channel="forms/test", ctx=self._context("mirror", dispatch_fn=dispatch))
        thread = port.read(result.extra["mirror"]["root_id"])
        labels = [message["labels"]["fusion.bb_kind"] for message in [thread["message"], *thread["replies"]]]
        self.assertEqual(labels, ["question", "claim", "evidence", "decision", "verdict"])

    def test_planner_table_rows(self):
        cases = [
            ("auction", {"kind": "allocation"}),
            ("mapreduce", {"kind": "decomposable", "read_only": True, "n_items": 3}),
            ("loop_until_dry", {"kind": "discovery"}),
            ("delphi", {"kind": "estimate"}),
            ("refute_panel", {"kind": "review", "stakes": "high"}),
            ("critic_repair", {"kind": "implement", "has_verifier": True, "difficulty": "hard"}),
            ("hedged_race", {"kind": "implement", "has_verifier": True, "isolated": True,
                             "latency_sensitive": True}),
            ("tournament", {"kind": "compare"}),
            ("blackboard", {"kind": "open"}),
            ("single", {"kind": "implement"}),
        ]
        for index, (expected, hints) in enumerate(cases, 1):
            with self.subTest(expected=expected):
                plan = planner.choose(planner.classify("task", hints=hints), providers=["claude", "grok"])
                self.assertEqual(plan.form, expected)
                self.assertIn(f"row {index}", plan.reasons[-1])
        unsafe = planner.classify("task", hints={"kind": "decomposable", "read_only": False, "n_items": 3})
        self.assertEqual(planner.choose(unsafe, providers=["claude", "grok"]).form, "single")

    def test_planner_ladder(self):
        stages = planner.ladder(planner.TaskShape("review"))
        self.assertEqual(stages, ["single", "hedged_race", "tournament", "refute_panel"])
        self.assertIsNone(planner.next_stage(stages, "single"))
        self.assertEqual(planner.next_stage(stages, "single", verifier_failed=True), "hedged_race")
        self.assertEqual(planner.ladder(planner.TaskShape("estimate")), ["single", "delphi"])

    def test_stall_detector_replan_and_resume(self):
        detector = collab.StallDetector(max_stalls=2)
        entry = collab.ProgressEntry(False, True, False, "worker", "repeat work")
        self.assertEqual(detector.update(entry), "continue")
        self.assertEqual(detector.update(entry), "continue")
        detector = collab.StallDetector.from_dict(detector.to_dict())
        self.assertEqual(detector.update(entry), "replan")
        self.assertEqual(detector.update(collab.ProgressEntry(True, False, False)), "done")

    def test_ack_deadline_overdue(self):
        lead = self._local()
        worker = self._local("worker")
        mid = lead.post("forms/ack", "request")["message"]["id"]
        collab.request_ack(lead, mid, due_s=5, clock=lambda: 100, addressee="worker")
        self.assertEqual(collab.overdue_acks(lead, "forms/ack", clock=lambda: 104), [])
        self.assertEqual([a["id"] for a in collab.overdue_acks(lead, "forms/ack", clock=lambda: 106)], [mid])
        lead.react(mid, "eyes")
        self.assertFalse(collab.acked(lead, mid))
        worker.react(mid, "white_check_mark")
        self.assertTrue(collab.acked(lead, mid))
        self.assertEqual(collab.overdue_acks(lead, "forms/ack", clock=lambda: 106), [])

    def test_priority_and_defer_labels(self):
        port = self._local()
        tid = port.task_create("forms/tasks", "task")["task"]["id"]
        collab.set_priority(port, tid, 7)
        collab.defer(port, tid, 100)
        labels = port.read(tid)["message"]["labels"]
        self.assertEqual(labels["fusion.priority"], "7")
        self.assertEqual(collab.from_iso(labels["fusion.defer_until"]), 100)

    def test_failure_branch_native_and_emulated(self):
        port = self._local()
        for native in (True, False):
            with self.subTest(native=native):
                channel = "forms/native" if native else "forms/emulated"
                parent = port.task_create(channel, "first")["task"]["id"]
                caps = dataclasses.replace(port.capabilities(), conditional_needs=native)
                with mock.patch.object(port, "capabilities", return_value=caps):
                    branch = collab.failure_branch(port, channel, "recovery", on_failed_of=parent)
                tid = branch["task"]["id"]
                self.assertEqual(branch["emulated"], not native)
                self.assertEqual(port.read(tid)["message"]["task"]["state"], "waiting" if native else "blocked")
                port.task_claim(parent)
                port.task_fail(parent, "test failure")
                if not native:
                    self.assertEqual(collab.resolve_failure_branches(port, channel)[0]["action"], "unblocked")
                self.assertEqual(port.read(tid)["message"]["task"]["state"], "ready")

    def test_is_stale_activity_aware(self):
        path = self.root / "activity.txt"
        path.write_text("activity", encoding="utf-8")
        os.utime(path, (190, 190))
        self.assertFalse(collab.is_stale(100, [path.name], now=200, idle_s=30, workspace=str(self.root)))
        self.assertTrue(collab.is_stale(100, [path.name], now=230, idle_s=30, workspace=str(self.root)))

    def test_registry_all_forms_available_and_names_match(self):
        rows = forms.list_forms()
        self.assertEqual([row["name"] for row in rows], list(forms.FORMS))
        for row in rows:
            self.assertTrue(row["available"], row)
            self.assertEqual(forms.get_form(row["name"]).FORM.name, row["name"])

    def test_cli_list_plan_run_salvage_capacity(self):
        self.assertEqual(len(self._cli("list")), 10)
        plan = self._cli("plan", "--kind", "discovery", "--task", "find", "--providers", "mock")
        self.assertEqual(plan["form"], "loop_until_dry")
        fixture = self.root / "fixture.json"
        fixture.write_text(json.dumps({"mock": ["[]", "[]"]}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FUSION_FORMS_FAKE_DISPATCH": str(fixture)}):
            result = self._cli("run", "dry", "--task", "find", "--providers", "mock", "--dry-rounds-json", "2",
                               "--run-dir", self.root / "run")
        self.assertEqual((result["status"], result["stop_reason"], result["calls"]), ("partial", "dry", 2))
        self.assertEqual(self._cli("salvage", self.root / "run")["status"], "salvaged")
        self.assertEqual(self._cli("capacity", "--width", "100", "--service-s", "104")["captains"], 5)
        self.assertEqual(self._cli("run", "dry", "--task", "find", "--providers", "mock", "--dry-rounds-json", '"two"',
                                   code=2)["error"], "usage")
        with mock.patch.dict(os.environ, {"FUSION_TEST_HOOKS": "0",
                                        "FUSION_FORMS_FAKE_DISPATCH": str(self.root / "missing.json")}):
            result = self._cli("run", "dry", "--task", "find", "--providers", "mock", "--budget-calls", "0", code=1)
        self.assertEqual(result["status"], "budget_exhausted")

    def test_cli_ledger_requires_verification(self):
        args = ("ledger", "record", "--worker", "one", "--task-class", "search", "--role", "finder", "--success")
        self.assertEqual(self._cli(*args, code=2)["error"], "usage")
        self.assertEqual(self._cli(*args, "--verified")["n"], 1)
        show = self._cli("ledger", "show", "--task-class", "search")
        self.assertEqual([row["worker"] for row in show["cells"]], ["one"])

    @unittest.skipUnless(os.name == "posix", "CLI signal delivery requires POSIX")
    def test_cli_cancel_retains_prior_findings(self):
        fixture = self.root / "fixture.json"
        report = [{"file": "one", "symbol": "two", "issue_class": "three", "summary": "four", "evidence": "five"}]
        fixture.write_text(json.dumps({"mock": [json.dumps(report), {"output": "[]", "delay_s": 60}]}), encoding="utf-8")
        run = self.root / "interrupted"
        argv = [sys.executable, "-B", "-m", "fusion_swarm.forms", "run", "dry", "--task", "find", "--providers", "mock",
                "--run-dir", str(run)]
        with subprocess.Popen(argv, env={**os.environ, "FUSION_FORMS_FAKE_DISPATCH": str(fixture)},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    records = journal.read_records(run / "journal.jsonl")
                    if sum(row["kind"] == "call.start" for row in records) >= 2:
                        break
                    if proc.poll() is not None:
                        self.fail(f"process exited before the second call: {proc.communicate()}")
                    time.sleep(0.02)
                else:
                    self.fail("second dispatch never started")
                proc.send_signal(signal.SIGINT)
                out, err = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 130, out + err)
                self.assertEqual(json.loads(out)["status"], "cancelled")
                self.assertIn("one:two:three", json.dumps(json.loads(out)["winner"]))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
        recovered = self._cli("salvage", run)
        self.assertEqual(recovered["status"], "salvaged")
        self.assertIn("four", json.dumps(recovered["best"]))


if __name__ == "__main__":
    unittest.main()
