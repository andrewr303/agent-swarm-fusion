"""Compile coding swarm forms into validated, executable native-worker handoffs.

Planning is offline. Preparing persists tasks on the Hive Board. Execution stays
with the current host's native workers, so model selections and permissions do
not silently turn into a different CLI roster.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .board import HiveBoard
from .mailbox import BoardError
from .sidekick import LEADERSHIP_MODES, TaskBrief, scoped_path, strings, text, worker_prompt

from .strategies import FORM_DETAILS, REGISTRY, options as strategy_options



@dataclass(frozen=True)
class TeamSeat:
    id: str
    provider: str
    model: str
    role: str = "worker"

    @classmethod
    def from_dict(cls, raw: Any, allow_claude: bool = False) -> "TeamSeat":
        if not isinstance(raw, dict) or set(raw) - {"id", "provider", "model", "role"}:
            raise BoardError("roster seats need id, provider, model and optional role")
        for name in ("id", "provider", "model"):
            text(raw.get(name), f"roster.{name}")
        if raw.get("role", "worker") not in ("lead", "worker", "reviewer", "researcher", "integrator"):
            raise BoardError("unknown team role")
        # Fable is routed through Claude Code by this plugin's legacy adapter.
        if raw["provider"].lower() in ("fable", "claude", "claude-code") and not allow_claude:
            raise BoardError("Claude-backed seats require an explicit --allow-claude user request")
        return cls(**raw)


def forms() -> list[dict]:
    return [{"name": name, "when": detail[0], "mechanic": detail[1],
             "communication": detail[2], "execution": "host-native",
             "model_selection": "explicit roster; no automatic substitution"}
            for name, detail in FORM_DETAILS.items()]


def _ordered(briefs: list[TaskBrief]) -> list[list[str]]:
    dependencies = {b.id: set(b.dependencies) for b in briefs}
    known = set(dependencies)
    if any(deps - known for deps in dependencies.values()):
        raise BoardError("task dependency names an unknown job")
    waves = []
    remaining = dict(dependencies)
    while remaining:
        ready = sorted(task_id for task_id, deps in remaining.items() if not deps)
        if not ready:
            raise BoardError("team dependency cycle: " + ", ".join(sorted(remaining)))
        waves.append(ready)
        remaining = {task_id: deps - set(ready) for task_id, deps in remaining.items() if task_id not in ready}
    return waves


def compile_spec(raw: Any, *, form: str | None = None, leadership: str | None = None,
                 allow_claude: bool = False) -> dict:
    if not isinstance(raw, dict) or set(raw) - {"run_id", "task", "workspace", "form", "leadership", "roster", "jobs", "rounds", "allowed_models", "strategy"}:
        raise BoardError("team spec has unknown fields or is not an object")
    run_id = text(raw.get("run_id"), "run_id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", run_id):
        raise BoardError("run_id must be a simple identifier of at most 80 characters")
    task = text(raw.get("task"), "task")
    workspace = str(scoped_path(raw.get("workspace"), "."))
    selected_form = form or raw.get("form", "sidekick")
    if not isinstance(selected_form, str) or selected_form not in FORM_DETAILS:
        raise BoardError(f"form must be one of {tuple(FORM_DETAILS)}")
    mode = leadership or raw.get("leadership") or (selected_form if selected_form in LEADERSHIP_MODES else "sidekick")
    if mode not in LEADERSHIP_MODES:
        raise BoardError(f"leadership must be one of {LEADERSHIP_MODES}")
    raw_seats = raw.get("roster")
    if not isinstance(raw_seats, list) or not raw_seats:
        raise BoardError("team needs an explicit selected roster")
    seats = [TeamSeat.from_dict(s, allow_claude) for s in raw_seats]
    if len({s.id for s in seats}) != len(seats):
        raise BoardError("duplicate roster id")
    leads = [s for s in seats if s.role == "lead"]
    if len(leads) != 1:
        raise BoardError("team needs exactly one lead representing the user's selected host")
    lead = leads[0]
    strategy = strategy_options(raw.get("strategy"), form=selected_form, leadership=mode, seats=[s.id for s in seats])
    allowed_models = strings(raw.get("allowed_models", list(dict.fromkeys(s.model for s in seats))), "allowed_models")
    if any(s.model not in allowed_models for s in seats):
        raise BoardError("roster uses a model outside allowed_models")
    jobs = raw.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise BoardError("team needs one or more fully specified jobs")
    job_ids = [text(j.get("id"), "job.id") if isinstance(j, dict) else text(None, "job") for j in jobs]
    if len(set(job_ids)) != len(job_ids):
        raise BoardError("duplicate job id")
    stages = strategy["stages"]
    if stages and (len([job for stage in stages for job in stage]) != len(job_ids) or set(job for stage in stages for job in stage) != set(job_ids)):
        raise BoardError("composed stages must include every job exactly once")
    if selected_form in ("map-reduce", "review-pair", "builder-breaker", "council") and len(jobs) < 2:
        raise BoardError(f"{selected_form} requires at least two jobs")
    if selected_form == "diamond" and len(jobs) < 3:
        raise BoardError("diamond requires framing, specialist and integration jobs")
    rounds = raw.get("rounds", 1)
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1:
        raise BoardError("rounds must be a positive integer")
    if rounds != 1 and selected_form != "ring":
        raise BoardError("only ring supports unrolled rounds; use an explicit DAG for other loops")
    briefs: list[TaskBrief] = []
    previous_tail = None
    for round_index in range(rounds):
        round_ids = [job_id if round_index == 0 else f"{job_id}.r{round_index + 1}" for job_id in job_ids]
        for index, job in enumerate(jobs):
            if set(job) - (set(TaskBrief.__dataclass_fields__) - {"workspace", "mode"}):
                raise BoardError("job has unknown fields; workspace and leadership belong to the team")
            deps = strings(job.get("dependencies", []), "dependencies")
            if any(dep not in job_ids for dep in deps):
                raise BoardError("task dependency names an unknown job")
            deps = [round_ids[job_ids.index(dep)] for dep in deps]
            rule = REGISTRY[selected_form]["dependency_rule"]
            if rule == "sequence":
                if index:
                    deps.append(round_ids[index - 1])
                elif previous_tail:
                    deps.append(previous_tail)
            elif rule == "join" and index == len(jobs) - 1:
                deps.extend(round_ids[:-1])
            elif rule == "diamond":
                if index == len(jobs) - 1:
                    deps.extend(round_ids[1:-1])
                elif index:
                    deps.append(round_ids[0])
            for position, stage in enumerate(stages):
                if job["id"] in stage and position:
                    deps.extend(round_ids[job_ids.index(parent)] for parent in stages[position - 1])
            data = {**job, "id": round_ids[index], "workspace": workspace,
                    "dependencies": list(dict.fromkeys(deps)), "mode": mode}
            if job.get("candidate_of"):
                if job["candidate_of"] not in job_ids:
                    raise BoardError("candidate producer must be a known job")
                data["candidate_of"] = round_ids[job_ids.index(job["candidate_of"])]
            brief = TaskBrief.from_dict(data)
            binding = strategy["roles"].get(brief.role)
            if binding and brief.assigned_to not in ([binding] if isinstance(binding, str) else binding):
                raise BoardError("job role differs from its selected strategy seat binding")
            if brief.assigned_to not in {s.id for s in seats} or brief.assigned_to == lead.id:
                raise BoardError("job must be assigned to a selected worker, not an invented seat or the lead")
            if selected_form == "council" and index < len(jobs) - 1 and brief.writes:
                raise BoardError("council proposers must be read-only")
            briefs.append(brief)
        previous_tail = round_ids[-1]
    if len({b.id for b in briefs}) != len(briefs):
        raise BoardError("unrolled ring identifiers collide with existing job ids")
    waves = _ordered(briefs)
    by_id = {b.id: b for b in briefs}
    for brief in briefs:
        if brief.candidate_of and by_id[brief.candidate_of].assigned_to == brief.assigned_to:
            raise BoardError("candidate review needs a separate selected worker")
    for stage in strategy["review"]["required_stages"]:
        if not any(brief.candidate_of and brief.review_stage == stage for brief in briefs):
            raise BoardError("required review stage has no selected candidate reviewer: " + stage)
    # Ancestors, rather than waves, decide whether two writers can overlap.
    ancestors: dict[str, set[str]] = {}
    for wave in waves:
        for job_id in wave:
            ancestors[job_id] = set(by_id[job_id].dependencies)
            for parent in by_id[job_id].dependencies:
                ancestors[job_id].update(ancestors[parent])
    from .coordination import collision_key, overlaps
    for index, first in enumerate(briefs):
        for second in briefs[index + 1:]:
            if first.id in ancestors[second.id] or second.id in ancestors[first.id]:
                continue
            if any(overlaps(collision_key(workspace, a), collision_key(workspace, b)) for a in first.writes for b in second.writes):
                raise BoardError(f"unordered writers overlap: {first.id}, {second.id}")
    if selected_form in ("review-pair", "builder-breaker") and any(
            briefs[i].assigned_to == briefs[i - 1].assigned_to for i in range(1, len(briefs))):
        raise BoardError(f"{selected_form} needs separate workers for adjacent roles")
    peers = {}
    worker_ids = list(dict.fromkeys(b.assigned_to for b in briefs))
    for worker_id in worker_ids:
        peers[worker_id] = [lead.id]
        if strategy["communication"] in ("peer-consultation", "board-mediated"):
            peers[worker_id] += [w for w in worker_ids if w != worker_id]
        elif strategy["communication"] == "ring-neighbors" and len(worker_ids) > 1:
            position = worker_ids.index(worker_id)
            peers[worker_id] += list(dict.fromkeys((worker_ids[(position - 1) % len(worker_ids)], worker_ids[(position + 1) % len(worker_ids)])))
    return {"pattern": "team", "run_id": run_id, "task": task, "workspace": workspace,
            "form": selected_form, "leadership": mode, "lead": lead.id, "strategy": strategy,
            "roster": [asdict(s) for s in seats], "allowed_models": allowed_models,
            "jobs": [b.to_dict() for b in briefs], "waves": waves,
            "ready": waves[0], "coordination_peers": peers,
            "context_namespace": run_id, "execution": "host-native",
            "verification": "worker-reported evidence plus lead review of actual artifacts",
            "unused_seats": [s.id for s in seats if s.id != lead.id and s.id not in worker_ids]}


def prepare(plan: dict, board_path: str, *, allow_multi_run: bool = False) -> dict:
    """Create the entire plan atomically; retries do not reset work or duplicate mail.

    A board belongs to one run unless `allow_multi_run` is set; plans are stored per run under
    `fusion/team-plan/<run_id>` and mirrored to the legacy `fusion/team-plan` key.
    """
    with HiveBoard(board_path) as board:
        run_id = plan["run_id"]
        isolation = plan.get("strategy", {}).get("isolation", {"mode": "scoped-shared"})
        if isolation["mode"] != "scoped-shared":
            from .mailbox import envelope_fingerprint
            from .scope import provision_clone, provision_directory, provision_worktree, snapshot
            request_digest = envelope_fingerprint(plan=plan)
            with board._lock:
                if not allow_multi_run and board._other_run_ids_unlocked(run_id):
                    raise BoardError("board already belongs to a different team plan")
                marker = board._team_plan_marker_unlocked(run_id)
            if marker["found"]:
                if marker["value"].get("request_digest") != request_digest:
                    raise BoardError("board already belongs to a different isolation request")
                plan = marker["value"]
            else:
                exclusions = [str(board.path), str(board.path) + "-wal", str(board.path) + "-shm", str(board.jsonl_path), str(board.wakeup_path)]
                if isolation["mode"] == "directory":
                    provision = provision_directory(plan["workspace"], isolation["workspace"], authorized=isolation.get("authorized", False),
                        exclusions=exclusions)
                elif isolation["mode"] == "apfs-clone":
                    provision = provision_clone(plan["workspace"], isolation["workspace"], authorized=isolation.get("authorized", False),
                        exclusions=exclusions)
                elif isolation["mode"] == "git-worktree":
                    provision = provision_worktree(plan["workspace"], isolation["workspace"], isolation["ref"],
                        authorized=isolation.get("authorized", False), provision_paths=isolation.get("provision_paths", []))
                else:
                    destination = str(scoped_path(isolation["workspace"], "."))
                    if destination == plan["workspace"]:
                        raise BoardError("host-managed isolation must identify a separate existing workspace")
                    provision = {"mode": "host-managed", "source": plan["workspace"], "workspace": destination,
                                 "provisioned_baseline": snapshot(destination), "source_baseline": snapshot(plan["workspace"]), "git_publication": False}
                plan = {**plan, "workspace": provision["workspace"], "request_digest": request_digest}
                plan["jobs"] = [{**job, "workspace": provision["workspace"]} for job in plan["jobs"]]
                plan["isolation_provision"] = provision
        with board._transaction():
            if not allow_multi_run and board._other_run_ids_unlocked(run_id):
                raise BoardError("board already belongs to a different team plan")
            marker = board._team_plan_marker_unlocked(run_id)
            if marker["found"] and marker["value"] != plan:
                # Additive metadata migrates old compiled forms without changing their jobs.
                legacy = marker["value"]
                equivalent = all(legacy.get(key) == plan.get(key) for key in ("run_id", "task", "workspace", "form", "leadership", "roster", "allowed_models", "waves", "coordination_peers"))
                old_jobs = [TaskBrief.from_dict(job).to_dict() for job in legacy.get("jobs", [])]
                if not equivalent or old_jobs != plan["jobs"] or "strategy" in legacy:
                    raise BoardError("board already belongs to a different team plan")
            if marker["found"]:
                board._publish_team_plan_unlocked(plan["lead"], plan)
            if not marker["found"]:
                for seat in sorted(plan["roster"], key=lambda s: s["id"] != plan["lead"]):
                    board.register({"id": seat["id"], "provider": seat["provider"], "model": seat["model"],
                                    "role": "architect" if seat["id"] == plan["lead"] else "worker"}, replace=True)
                board._publish_team_plan_unlocked(plan["lead"], plan)
                by_id = {b["id"]: b for b in plan["jobs"]}
                for wave in plan["waves"]:
                    for task_id in wave:
                        board.create_task(plan["lead"], by_id[task_id])
        current = {item["id"]: item for item in board.tasks()}
        ready = [job["id"] for job in plan["jobs"] if current[job["id"]]["state"] == "queued"
                 and board._dependencies_ready_unlocked(job)]
    packet = {**plan, "board_path": str(Path(board_path).resolve()), "prepared": True, "ready": ready,
              "jobs_state": {task_id: {"state": item["state"], "attempt": item["attempt"]}
                             for task_id, item in current.items()},
              "worker_packets": {}}
    for job in plan["jobs"]:
        brief = TaskBrief.from_dict(job)
        packet["worker_packets"][brief.id] = worker_prompt(brief, lead_id=plan["lead"],
            peers=plan["coordination_peers"][brief.assigned_to], board_path=packet["board_path"])
    return packet


def run(task: str = "", *, spec_file: str | None = None, form: str | None = None,
        leadership: str | None = None, prepare_run: bool = False, board_path: str | None = None,
        allow_claude: bool = False, allow_multi_run: bool = False) -> dict:
    if not spec_file:
        if prepare_run:
            raise BoardError("--prepare requires a fully specified --spec")
        return {"pattern": "team", "task": task, "form": form, "forms": forms(),
                "execution": "not dispatched", "next_step": "Fill a TeamSpec with the selected native roster, scoped jobs and success criteria; pass --spec to plan or --prepare to queue it."}
    with open(spec_file, encoding="utf-8") as stream:
        raw = json.load(stream)
    if not isinstance(raw, dict):
        raise BoardError("TeamSpec must be an object")
    if task:
        raw["task"] = task
    plan = compile_spec(raw, form=form, leadership=leadership, allow_claude=allow_claude)
    if not prepare_run:
        return {**plan, "prepared": False}
    path = board_path or str(Path.home() / ".fusion" / "teams" / plan["run_id"] / "board.sqlite")
    return prepare(plan, path, allow_multi_run=allow_multi_run)
