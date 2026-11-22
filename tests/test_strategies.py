import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from fusion_swarm import team, strategies
from fusion_swarm.board import HiveBoard, BoardError


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fusion strategy ")
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.spec = {"run_id": "compose", "task": "build with reviewed contract", "workspace": str(self.workspace),
            "roster": [{"id": "lead", "model": "selected-lead", "provider": "native", "role": "lead"},
                       *[{"id": name, "model": "selected-worker", "provider": "native"} for name in ("builder", "spec", "code")]],
            "jobs": [{"id": "build", "goal": "build artifact", "assigned_to": "builder", "writes": ["a.txt"], "success_criteria": ["file matches intent"]},
                     {"id": "review", "goal": "review contract", "assigned_to": "spec", "reads": ["a.txt"], "success_criteria": ["no unresolved contract defects"]},
                     {"id": "verify", "goal": "verify artifact", "assigned_to": "code", "reads": ["a.txt"], "success_criteria": ["artifact verified"]}]}

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, board, job, paths=None):
        item = board.tasks(job)[0]
        return board.submit_task(item["assigned_to"], {"task_id": job, "attempt": item["attempt"], "status": "complete", "summary": "Inspected actual fixture artifact", "changed_paths": paths or []})

    def prepared_review(self):
        plan = team.compile_spec(strategies.workflow("review-and-revise", self.spec))
        path = self.root / "board.sqlite"
        team.prepare(plan, str(path))
        return HiveBoard(path)

    def test_candidate_review_can_start_before_acceptance_but_write_dependencies_stay_blocked(self):
        with self.prepared_review() as board:
            board.start_task("builder", "build")
            (self.workspace / "a.txt").write_text("candidate")
            self.report(board, "build", ["a.txt"])
            with self.assertRaises(BoardError):
                board.review_task("lead", "build", 1, "accept", "premature")
            with self.assertRaises(BoardError):
                board.start_task("code", "verify")
            started = board.start_task("spec", "review")
            digest = started["candidate_binding"]["digest"]
            board.record_candidate_review("spec", "review", 1, digest, "approve", [])
            self.report(board, "review")
            board.review_task("lead", "review", 1, "accept", "Reviewed fixture findings")
            board.review_task("lead", "build", 1, "accept", "Reviewed exact candidate")
            board.start_task("code", "verify")

    def test_changed_artifact_invalidates_review_and_critical_objections_require_evidence(self):
        with self.prepared_review() as board:
            board.start_task("builder", "build")
            file = self.workspace / "a.txt"
            file.write_text("candidate")
            self.report(board, "build", ["a.txt"])
            started = board.start_task("spec", "review")
            digest = started["candidate_binding"]["digest"]
            finding = {"id": "contract", "severity": "critical", "summary": "Fixture contract gap", "evidence": "a.txt:1"}
            with self.assertRaisesRegex(BoardError, "unresolved"):
                board.record_candidate_review("spec", "review", 1, digest, "approve", [finding])
            resolved = {**finding, "resolution": {"summary": "Verified required contract", "evidence": "Inspected a.txt:1"}}
            board.record_candidate_review("spec", "review", 1, digest, "approve", [resolved])
            self.report(board, "review")
            file.write_text("different candidate")
            with self.assertRaisesRegex(BoardError, "stale"):
                board.review_task("lead", "review", 1, "accept", "obsolete approval")
            with self.assertRaises(BoardError):
                board.candidate_status("unselected", "build")

    def test_revision_requeues_digest_bound_reviews_and_retains_history(self):
        with self.prepared_review() as board:
            board.start_task("builder", "build")
            file = self.workspace / "a.txt"
            file.write_text("first candidate")
            self.report(board, "build", ["a.txt"])
            original = board.start_task("spec", "review")["candidate_binding"]["digest"]
            board.record_candidate_review("spec", "review", 1, original, "approve", [])
            self.report(board, "review")
            board.review_task("lead", "review", 1, "accept", "First review checked")
            board.review_task("lead", "build", 1, "revise", "Refine actual artifact")
            self.assertEqual(board.tasks("review")[0]["state"], "queued")
            board.start_task("builder", "build")
            file.write_text("second candidate")
            self.report(board, "build", ["a.txt"])
            started = board.start_task("spec", "review")
            self.assertEqual(started["attempt"], 2)
            self.assertNotEqual(started["candidate_binding"]["digest"], original)
            board.record_candidate_review("spec", "review", 2, started["candidate_binding"]["digest"], "approve", [])
            self.report(board, "review")
            board.review_task("lead", "review", 2, "accept", "Second review checked")
            board.review_task("lead", "build", 2, "accept", "Second exact artifact checked")
            self.assertEqual(board._conn.execute("SELECT COUNT(*) FROM candidate_reviews").fetchone()[0], 2)

    def test_axes_compose_real_edges_peers_and_decision_ownership(self):
        spec = {**self.spec, "form": "fan-out", "leadership": "genius-boss", "strategy": {
            "communication": "peer-consultation", "roles": {"implementation-owner": "builder"}, "stages": [["build"], ["review", "verify"]]}}
        plan = team.compile_spec(spec)
        self.assertEqual(plan["waves"], [["build"], ["review", "verify"]])
        self.assertEqual(plan["coordination_peers"]["builder"], ["lead", "spec", "code"])
        self.assertEqual(plan["strategy"]["decision_owner"], "selected-lead")
        experts = team.compile_spec({**spec, "leadership": "idiot-boss"})
        self.assertEqual(experts["strategy"]["decision_owner"], "expert-workers")
        self.assertEqual(experts["roster"], plan["roster"])
        with self.assertRaises(BoardError):
            team.compile_spec({**self.spec, "strategy": {"version": 2}})

    def test_all_six_templates_create_distinct_executable_dependencies(self):
        for name, stages in strategies.WORKFLOWS.items():
            with self.subTest(name=name):
                spec = copy.deepcopy(self.spec)
                spec["jobs"] = [{"id": stage, "assigned_to": ("builder" if stage in ("implement", "build", "fix", "repair", "migrate", "contract") else "spec"),
                    "goal": "Explicit stage " + stage, "writes": [stage + ".txt"], "success_criteria": ["Specified stage output verified"]} for stage in stages]
                if name == "ci-repair":
                    spec["jobs"][0]["writes"] = []
                # Reviewer independence is explicit, including the contract-first producer.
                raw = strategies.workflow(name, spec)
                plan = team.compile_spec(raw)
                self.assertEqual(plan["strategy"]["workflow"], name)
                self.assertGreater(len(plan["waves"]), 1)
                self.assertEqual(len(plan["jobs"]), len(stages))

    def test_migration_waves_fan_out_disjoint_owners_then_join_verification(self):
        spec = copy.deepcopy(self.spec)
        spec["jobs"] = [
            {"id": name, "goal": "Explicit migration stage", "assigned_to": seat, "writes": writes, "success_criteria": ["Schema behavior verified"]}
            for name, seat, writes in (("inventory", "spec", []), ("migrate", "builder", ["schema-a.sql"]),
                                      ("migrate.b", "code", ["schema-b.sql"]), ("verify", "spec", []))]
        plan = team.compile_spec(strategies.workflow("migration-waves", spec))
        self.assertEqual(plan["waves"], [["inventory"], ["migrate", "migrate.b"], ["verify"]])

    def test_accepted_readonly_dependency_changes_fence_success_reports(self):
        plan = team.compile_spec(self.spec, form="pipeline")
        db = self.root / "board.sqlite"
        team.prepare(plan, str(db))
        with HiveBoard(db) as board:
            board.start_task("builder", "build")
            file = self.workspace / "a.txt"
            file.write_text("accepted artifact")
            self.report(board, "build", ["a.txt"])
            board.review_task("lead", "build", 1, "accept", "Checked accepted file")
            board.start_task("spec", "review")
            file.write_text("changed after worker read")
            with self.assertRaisesRegex(BoardError, "dependency input changed"):
                self.report(board, "review")

    def test_authorized_directory_isolation_materializes_once_and_preserves_source(self):
        spec = {**self.spec, "strategy": {"isolation": {"mode": "directory", "workspace": str(self.root / "isolated"), "authorized": True}}}
        (self.workspace / "preexisting.txt").write_text("dependency")
        plan = team.compile_spec(spec)
        first = team.prepare(plan, str(self.root / "board.sqlite"))
        second = team.prepare(plan, str(self.root / "board.sqlite"))
        self.assertEqual(first["workspace"], second["workspace"])
        self.assertEqual(Path(first["workspace"]).joinpath("preexisting.txt").read_text(), "dependency")
        self.assertTrue(all(job["workspace"] == first["workspace"] for job in first["jobs"]))


if __name__ == "__main__":
    unittest.main()
