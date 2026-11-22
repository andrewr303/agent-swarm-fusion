"""Versioned offline behavior benchmarks; actual timing, no model/cost estimates."""
import argparse
import io
import json
import platform
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "python")]
SCENARIOS = {
    "coding-restart": "test_execution.ExecutionTests.test_dependent_coding_workflow_restarts_and_requires_lead_acceptance",
    "delivery-interruption": "test_protocol.ProtocolTests.test_interrupted_delivery_redelivers_and_old_consumer_cannot_commit",
    "lost-dispatch": "test_execution.ExecutionTests.test_dispatch_claim_and_lost_reply_never_launch_duplicate",
    "scope-violation": "test_execution.ExecutionTests.test_out_of_scope_untracked_and_ignored_changes_fail_observed_check",
    "infrastructure-circuit": "test_execution.ExecutionTests.test_bounded_infrastructure_failures_stop_on_same_model",
    "stale-review": "test_strategies.StrategyTests.test_changed_artifact_invalidates_review_and_critical_objections_require_evidence",
    "stale-knowledge": "test_supervision.SupervisionTests.test_knowledge_requires_acceptance_citations_and_invalidates_source_changes",
    "owned-cancellation": "test_process_runtime.ProcessTests.test_cancel_owned_windows_job_and_timeout_are_observed",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, help="Optional prior measured result of this same scenario version")
    args = parser.parse_args()
    results = []
    for name, case in SCENARIOS.items():
        stream = io.StringIO()
        started = time.perf_counter()
        result = unittest.TextTestRunner(stream=stream, verbosity=1).run(unittest.defaultTestLoader.loadTestsFromName(case))
        results.append({"scenario": name, "case": case, "seconds": round(time.perf_counter() - started, 6),
                        "checks_run": result.testsRun, "passed": result.wasSuccessful(), "skipped": len(result.skipped),
                        "failures": len(result.failures), "errors": len(result.errors), "raw_result": stream.getvalue(),
                        "provider_usage": None, "provider_cost": None})
    report = {"schema_version": 1, "scenario_version": "fusion-offline-v2", "captured_at": datetime.now(timezone.utc).isoformat(),
              "environment": {"python": platform.python_version(), "system": platform.system()}, "results": results,
              "provider_calls": 0, "baseline_comparison": None,
              "boundary": "deterministic fixture behavior and local latency; no coding model quality or historical performance claim"}
    if args.baseline:
        previous = json.loads(args.baseline.read_text(encoding="utf-8"))
        if previous.get("scenario_version") != report["scenario_version"] or previous.get("environment") != report["environment"]:
            parser.error("baseline scenario version/environment differs; comparison is refused")
        old = {item["scenario"]: item for item in previous["results"]}
        if set(old) != set(SCENARIOS):
            parser.error("baseline must contain the same measured scenarios")
        report["baseline_comparison"] = {"source": str(args.baseline.resolve()), "captured_at": previous["captured_at"],
            "results": [{"scenario": item["scenario"], "prior_passed": old[item["scenario"]]["passed"], "current_passed": item["passed"],
                         "seconds_delta": round(item["seconds"] - old[item["scenario"]]["seconds"], 6)} for item in results]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        parser.error("output already exists; preserve prior measurements and choose a new file")
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "scenarios": len(results), "passed": sum(item["passed"] for item in results), "provider_calls": 0}))
    return 0 if all(item["passed"] and not item["skipped"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
