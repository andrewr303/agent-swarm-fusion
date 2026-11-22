"""Executable schedules and native handoffs, independent of prompt phrasing."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from fusion_swarm import adapter, team
from fusion_swarm.board import BoardError, HiveBoard


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spec = {"run_id": "test-team", "task": "build and verify", "workspace": str(self.root),
                     "roster": [{"id": "lead", "provider": "native", "model": "selected-lead", "role": "lead"},
                                {"id": "first", "provider": "native", "model": "selected-worker", "role": "worker"},
                                {"id": "second", "provider": "native", "model": "selected-reviewer", "role": "reviewer"}],
                     "jobs": [{"id": "a", "goal": "produce A", "assigned_to": "first", "writes": ["a.py"], "success_criteria": ["A exists"]},
                              {"id": "b", "goal": "produce B", "assigned_to": "second", "writes": ["b.py"], "success_criteria": ["B exists"]}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_forms_compile_to_valid_schedules(self):
        for form in team.FORM_DETAILS:
            with self.subTest(form=form):
                spec = copy.deepcopy(self.spec)
                if form == "diamond":
                    spec["jobs"].append({**spec["jobs"][0], "id": "c", "writes": ["c.py"]})
                if form == "council":
                    spec["jobs"][0]["writes"] = []
                plan = team.compile_spec(spec, form=form)
                scheduled = [task_id for wave in plan["waves"] for task_id in wave]
                self.assertEqual(set(scheduled), {b["id"] for b in plan["jobs"]})
                self.assertEqual(plan["roster"], spec["roster"])

    def test_pipeline_serializes_jobs_and_map_reduce_joins_all_maps(self):
        self.assertEqual(team.compile_spec(self.spec, form="pipeline")["waves"], [["a"], ["b"]])
        spec = copy.deepcopy(self.spec)
        spec["jobs"].append({**spec["jobs"][0], "id": "merge", "writes": ["merged.py"]})
        plan = team.compile_spec(spec, form="map-reduce")
        self.assertEqual(plan["waves"], [["a", "b"], ["merge"]])
        self.assertEqual(plan["jobs"][-1]["dependencies"], ["a", "b"])

    def test_ring_is_unrolled_without_a_cycle(self):
        spec = {**self.spec, "rounds": 2}
        plan = team.compile_spec(spec, form="ring")
        self.assertEqual(plan["waves"], [["a"], ["b"], ["a.r2"], ["b.r2"]])

    def test_leadership_and_topology_can_be_combined_without_changing_models(self):
        plan = team.compile_spec(self.spec, form="mesh", leadership="idiot-boss")
        self.assertEqual(plan["waves"], [["a", "b"]])
        self.assertEqual(plan["leadership"], "idiot-boss")
        self.assertEqual(plan["coordination_peers"]["first"], ["lead", "second"])
        self.assertEqual(plan["roster"], self.spec["roster"])

    def test_unscheduled_writers_cannot_share_a_scope_even_if_same_model_or_agent(self):
        spec = copy.deepcopy(self.spec)
        spec["jobs"][1]["writes"] = ["a.py"]
        for form in ("fan-out", "mesh", "blackboard"):
            with self.assertRaises(BoardError):
                team.compile_spec(spec, form=form)
        plan = team.compile_spec(spec, form="pipeline")
        self.assertEqual(plan["waves"], [["a"], ["b"]])

    def test_unknown_seats_models_and_claude_adapter_are_not_substituted(self):
        for mutation in (lambda s: s["jobs"][0].update(assigned_to="missing"),
                         lambda s: s.update(allowed_models=["selected-lead"]),
                         lambda s: s["roster"][1].update(provider="fable")):
            spec = copy.deepcopy(self.spec)
            mutation(spec)
            with self.assertRaises(BoardError):
                team.compile_spec(spec)

    def test_cycles_unknown_fields_and_empty_success_criteria_are_errors(self):
        for mutation in (lambda s: s["jobs"][0].update(dependencies=["b"]) or s["jobs"][1].update(dependencies=["a"]),
                         lambda s: s.update(invented=True),
                         lambda s: s.update(form=[]),
                         lambda s: s["jobs"][0].update(success_criteria=[])):
            spec = copy.deepcopy(self.spec)
            mutation(spec)
            with self.assertRaises(BoardError):
                team.compile_spec(spec)

    def test_prepare_is_idempotent_and_preserves_progress_on_restart(self):
        plan = team.compile_spec(self.spec, form="pipeline")
        db = str(self.root / "board.sqlite")
        first = team.prepare(plan, db)
        self.assertEqual(first["ready"], ["a"])
        with HiveBoard(db) as board:
            board.start_task("first", "a")
            board.submit_task("first", {"task_id": "a", "attempt": 1, "status": "complete", "summary": "A built", "changed_paths": ["a.py"]})
            board.review_task("lead", "a", 1, "accept", "inspected A")
            before = board.stats()["messages"]
        second = team.prepare(plan, db)
        self.assertEqual(second["ready"], ["b"])
        with HiveBoard(db) as board:
            self.assertEqual(board.stats()["messages"], before)
            self.assertEqual(board.tasks("a")[0]["state"], "accepted")

    def test_a_different_run_cannot_reuse_a_board(self):
        db = str(self.root / "board.sqlite")
        team.prepare(team.compile_spec(self.spec), db)
        spec = {**self.spec, "run_id": "different"}
        with self.assertRaises(BoardError):
            team.prepare(team.compile_spec(spec), db)

    def test_prepare_is_atomic_when_a_handoff_fails(self):
        db = str(self.root / "board.sqlite")
        with patch.object(HiveBoard, "create_task", side_effect=BoardError("handoff rejected")):
            with self.assertRaises(BoardError):
                team.prepare(team.compile_spec(self.spec), db)
        with HiveBoard(db) as board:
            self.assertEqual(board.agents(), [])
            self.assertEqual(board.tasks(), [])
            self.assertFalse(board.context_get("fusion", "team-plan")["found"])

    def test_cli_plan_has_no_model_calls_or_board_mutation(self):
        path = self.root / "spec.json"
        path.write_text(json.dumps(self.spec), encoding="utf-8")
        db = self.root / "not-created.sqlite"
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "fusion.py"),
                                 "sidekick", "--spec", str(path), "--board-db", str(db), "--json"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)["prepared"])
        self.assertFalse(db.exists())

    def test_failed_provider_detection_does_not_invent_available_workers(self):
        with patch.object(adapter, "_bash", side_effect=OSError("no detector")):
            self.assertEqual(adapter.available(), [])
        with patch.object(adapter, "dispatch") as dispatch:
            self.assertEqual(adapter.panel("task", providers=[]), [])
            dispatch.assert_not_called()

    def test_native_launcher_emits_utf8_even_with_an_ascii_parent_pipe(self):
        path = self.root / "unicode.json"
        path.write_text(json.dumps({**self.spec, "task": "Réparer le café"}), encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "fusion.py"),
            "team", "--spec", str(path), "--json"], capture_output=True, text=True, encoding="utf-8",
            timeout=10, env={**os.environ, "PYTHONIOENCODING": "ascii"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["task"], "Réparer le café")


if __name__ == "__main__":
    unittest.main()
