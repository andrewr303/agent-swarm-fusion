"""Versioned execution strategies and behavioral workflow templates."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from .mailbox import BoardError
from .execution import bounds_from
from .sidekick import ROLE_NAMES

VERSION = 1
FORM_DETAILS = {
    "sidekick": ("A lead and a focused execution partner", "Explicit dependencies; lead reviews each handoff", "lineage"),
    "genius-boss": ("Detailed expert planning with narrow workers", "Prescriptive briefs; decisions and final review stay with the lead", "lineage"),
    "idiot-boss": ("Light coordination with autonomous expert workers", "Minimal intent-preserving brief; worker owns technical decisions and checks", "lineage"),
    "dag": ("Jobs with real data dependencies", "Only declared prerequisites block a job", "lineage"),
    "pipeline": ("Ordered transformation stages", "Each stage depends on the previous accepted result", "lineage"),
    "relay": ("Sequential specialist handoffs", "Each specialist passes a compact report to its successor", "lineage"),
    "fan-out": ("Independent work in parallel", "Jobs start independently, with one writer per scope", "lineage"),
    "hub-spoke": ("Independent specialists coordinated by one lead", "Direct communication via the lead, independent job starts", "lineage"),
    "map-reduce": ("Parallel maps followed by one integration job", "The last job consumes every accepted map result", "lineage"),
    "diamond": ("Shared framing, parallel specialists, integration", "First job unlocks the middle; the last joins every middle job", "lineage"),
    "ring": ("Ordered peer refinement", "A bounded unrolled relay; next round begins after the previous tail", "lineage"),
    "mesh": ("Independent peers with direct cross consultation", "Parallel jobs and all-peer communication with exclusive write scopes", "open"),
    "blackboard": ("Peers building scoped shared knowledge", "Parallel jobs use versioned context and durable findings", "board"),
    "review-pair": ("Implementation followed by a separate reviewer", "Reviewer waits for accepted implementation evidence", "lineage"),
    "builder-breaker": ("Implementation followed by adversarial verification", "Breaker receives the built artifact and must return checkable failures", "open"),
    "council": ("Independent proposals followed by explicit adjudication", "Read-only proposers; final job maps disagreement and missing evidence", "blind"),
}
REGISTRY = {name: {"version": VERSION, "topology": name, "when": value[0], "mechanic": value[1], "communication": value[2],
    "dependency_rule": "sequence" if name in ("pipeline", "relay", "ring", "review-pair", "builder-breaker") else
                       "join" if name in ("map-reduce", "council") else "diamond" if name == "diamond" else "declared"}
    for name, value in FORM_DETAILS.items()}
COMMUNICATION = ("parent-child", "peer-consultation", "board-mediated", "blind-proposals", "ring-neighbors")
ISOLATION = ("scoped-shared", "host-managed", "git-worktree", "directory", "apfs-clone")
ROLES = ROLE_NAMES
WORKFLOWS = {
    "feature-delivery": ("plan", "implement", "spec-review", "code-review", "verify"),
    "reproduce-build-verify": ("reproduce", "fix", "verify"),
    "migration-waves": ("inventory", "migrate", "verify"),
    "contract-first-api": ("contract", "implement", "contract-review", "verify"),
    "review-and-revise": ("build", "review", "verify"),
    "ci-repair": ("reproduce", "repair", "verify"),
}


def options(raw: dict | None, *, form: str, leadership: str, seats: list[str]) -> dict:
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - {"version", "communication", "roles", "isolation", "review", "bounds", "workflow", "stages"}:
        raise BoardError("unknown strategy axis")
    if raw.get("version", VERSION) != VERSION:
        raise BoardError("unsupported strategy version")
    default = {"open": "peer-consultation", "board": "board-mediated", "blind": "blind-proposals", "lineage": "ring-neighbors" if form == "ring" else "parent-child"}
    communication = raw.get("communication", default[REGISTRY[form]["communication"]])
    if communication not in COMMUNICATION or (form == "council" and communication != "blind-proposals"):
        raise BoardError("unsupported communication or a council privacy violation")
    roles = raw.get("roles", {})
    if not isinstance(roles, dict) or set(roles) - set(ROLES) or any(
        not isinstance(binding, (str, list)) or (isinstance(binding, list) and (not binding or any(not isinstance(seat, str) or seat not in seats for seat in binding)))
        or (isinstance(binding, str) and binding not in seats) for binding in roles.values()):
        raise BoardError("strategy roles must bind known roles to selected seats")
    isolation = raw.get("isolation", {"mode": "scoped-shared"})
    if not isinstance(isolation, dict) or set(isolation) - {"mode", "workspace", "authorized", "ref", "provision_paths"} or isolation.get("mode") not in ISOLATION:
        raise BoardError("invalid isolation strategy")
    if isolation["mode"] != "scoped-shared" and not isolation.get("workspace"):
        raise BoardError("isolated strategy needs an explicit destination workspace")
    if isolation.get("workspace") and not isinstance(isolation["workspace"], str):
        raise BoardError("isolation workspace must be a path string")
    if isolation["mode"] in ("git-worktree", "directory", "apfs-clone") and isolation.get("authorized") is not True:
        raise BoardError("workspace provisioning must be explicitly authorized")
    if isolation["mode"] == "git-worktree" and not isolation.get("ref"):
        raise BoardError("Git worktree needs a selected ref")
    review = raw.get("review", {"required_stages": []})
    if not isinstance(review, dict) or set(review) - {"required_stages"} or not isinstance(review.get("required_stages", []), list) or any(stage not in ("spec", "artifact", "critical") for stage in review.get("required_stages", [])):
        raise BoardError("review stages must be spec, artifact or critical")
    stages = raw.get("stages", [])
    if not isinstance(stages, list) or any(not isinstance(stage, list) or not stage or any(not isinstance(job, str) for job in stage) for stage in stages):
        raise BoardError("composed stages need ordered nonempty job-id arrays")
    if raw.get("workflow") and raw["workflow"] not in WORKFLOWS:
        raise BoardError("unknown workflow")
    return {"version": VERSION, "topology": form, "leadership": leadership, "communication": communication,
            "roles": roles, "isolation": isolation, "review": {"required_stages": list(dict.fromkeys(review.get("required_stages", [])))},
            "bounds": bounds_from(raw.get("bounds", {})), "workflow": raw.get("workflow"), "stages": stages,
            "decision_owner": "expert-workers" if leadership == "idiot-boss" else "selected-lead"}


def workflow(name: str, spec: dict) -> dict:
    """Build edges from fully specified stage jobs; never invent seats or scopes."""
    if name not in WORKFLOWS:
        raise BoardError("unknown workflow")
    raw = copy.deepcopy(spec)
    by_id = {job["id"]: job for job in raw.get("jobs", [])}
    if set(WORKFLOWS[name]) - set(by_id):
        raise BoardError("workflow needs fully specified stage jobs: " + ", ".join(WORKFLOWS[name]))
    stages = list(WORKFLOWS[name])
    raw["form"] = "dag"
    strategy = raw.setdefault("strategy", {})
    strategy.update(version=VERSION, workflow=name)
    if name == "feature-delivery":
        by_id["implement"].setdefault("dependencies", []).append("plan")
        for stage, kind in (("spec-review", "spec"), ("code-review", "artifact")):
            by_id[stage].update(candidate_of="implement", review_stage=kind)
            by_id[stage]["writes"] = []
            by_id[stage].setdefault("dependencies", []).append("implement")
        by_id["code-review"].setdefault("dependencies", []).append("spec-review")
        by_id["verify"].setdefault("dependencies", []).append("implement")
        strategy["review"] = {"required_stages": ["spec", "artifact"]}
    elif name in ("review-and-revise", "contract-first-api"):
        producer, review = ("build", "review") if name == "review-and-revise" else ("implement", "contract-review")
        if name == "contract-first-api":
            by_id[producer].setdefault("dependencies", []).append("contract")
        by_id[review].update(candidate_of=producer, review_stage="artifact", writes=[])
        by_id[review].setdefault("dependencies", []).append(producer)
        by_id["verify"].setdefault("dependencies", []).append(producer)
        strategy["review"] = {"required_stages": ["artifact"]}
    elif name == "migration-waves":
        migrations = [job for job in by_id if job == "migrate" or job.startswith("migrate.")]
        groups = strategy.get("stages") or [["inventory"], migrations, ["verify"]]
        strategy["stages"] = groups
        for migration in migrations:
            by_id[migration].setdefault("dependencies", []).append("inventory")
        by_id["verify"].setdefault("dependencies", []).extend(migrations)
    elif name == "ci-repair":
        if by_id["reproduce"].get("writes"):
            raise BoardError("CI reproduction must be read-only; repair owns the changes")
        for previous, current in zip(stages, stages[1:]):
            by_id[current].setdefault("dependencies", []).append(previous)
    else:
        for previous, current in zip(stages, stages[1:]):
            by_id[current].setdefault("dependencies", []).append(previous)
    for job in raw["jobs"]:
        job["dependencies"] = list(dict.fromkeys(job.get("dependencies", [])))
        if not job.get("role"):
            job["role"] = ("spec-reviewer" if job["id"] in ("spec-review", "contract-review") else "code-reviewer" if job["id"] in ("code-review", "review") else
                "planner" if job["id"] in ("plan", "inventory", "contract") else "reproducer" if job["id"] == "reproduce" else
                "verifier" if job["id"] == "verify" else "specialist" if job["id"].startswith("migrate") else "implementation-owner")
    return raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("catalog", "preview", "workflow"))
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--name", choices=sorted(WORKFLOWS))
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--board", type=Path)
    parser.add_argument("--output-spec", type=Path, help="Save the generated raw TeamSpec")
    parser.add_argument("--allow-claude", action="store_true")
    args = parser.parse_args(argv)
    from .team import compile_spec, prepare
    if args.operation == "catalog":
        result = {"version": VERSION, "forms": list(REGISTRY.values()), "communication": COMMUNICATION, "isolation": ISOLATION,
                  "roles": ROLES, "workflows": WORKFLOWS, "leadership": ["sidekick", "genius-boss", "idiot-boss"]}
    else:
        if not args.spec:
            parser.error("--spec is required")
        spec = json.loads(args.spec.read_text(encoding="utf-8-sig"))
        if args.operation == "workflow" and not args.name:
            parser.error("workflow requires --name")
        raw = workflow(args.name, spec) if args.operation == "workflow" else spec
        result = compile_spec(raw, allow_claude=args.allow_claude)
        if args.output_spec:
            args.output_spec.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.prepare:
            result = prepare(result, str(args.board or Path(result["workspace"]) / ".fusion" / result["run_id"] / "board.sqlite"))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0
