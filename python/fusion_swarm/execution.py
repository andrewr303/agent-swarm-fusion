"""Durable native dispatch outbox and observed execution controller.

Host clients perform their real exposed operations, then record the observation.
A lost dispatch reply is uncertain, never a reason to launch a duplicate worker.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from pathlib import Path

from .mailbox import BoardError, envelope_fingerprint, json_object, positive_int, positive_number
from .host_runtime import CapabilityUnavailable
from .scope import inspect_changes, snapshot
from .sidekick import TaskBrief, WorkerReport, scoped_path, worker_prompt

EXECUTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS swarm_runs (
    id TEXT PRIMARY KEY, lead_id TEXT NOT NULL REFERENCES agents(id), host TEXT NOT NULL,
    capabilities TEXT NOT NULL, bounds TEXT NOT NULL, state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
    dispatches INTEGER NOT NULL DEFAULT 0, infrastructure_failures INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_jobs (
    ticket_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES swarm_runs(id),
    task_id TEXT NOT NULL REFERENCES work_items(id), attempt INTEGER NOT NULL,
    worker_id TEXT NOT NULL REFERENCES agents(id), requested_model TEXT NOT NULL,
    observed_model TEXT, host TEXT NOT NULL, state TEXT NOT NULL,
    native_handle TEXT NOT NULL DEFAULT '', claim_token TEXT, owner TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 0, baseline TEXT NOT NULL, scope_result TEXT,
    packet TEXT NOT NULL, result TEXT, failure_kind TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(task_id,attempt)
);
CREATE TABLE IF NOT EXISTS runtime_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL REFERENCES runtime_jobs(ticket_id),
    event_key TEXT NOT NULL, fingerprint TEXT NOT NULL, observation TEXT NOT NULL,
    created_at REAL NOT NULL, UNIQUE(ticket_id,event_key)
);
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES swarm_runs(id),
    actor TEXT NOT NULL, action TEXT NOT NULL, version INTEGER NOT NULL,
    details TEXT NOT NULL, created_at REAL NOT NULL
);
"""
ACTIVE_RUNTIME_STATES = ("requested", "claimed", "dispatched", "started", "working", "waiting-input", "uncertain")
OBSERVED_STATES = ("dispatched", "started", "working", "waiting-input", "stopped", "failed", "cancelled")
FAILURE_KINDS = ("", "infrastructure", "quota", "authentication", "worker", "review_revision", "needs_input", "capability")


def initialize_execution(connection):
    columns = {row[1] for row in connection.execute("PRAGMA table_info(swarm_runs)")}
    if "deadline_at" not in columns:
        connection.execute("ALTER TABLE swarm_runs ADD COLUMN deadline_at REAL NOT NULL DEFAULT 0")
    if "usage" not in {row[1] for row in connection.execute("PRAGMA table_info(runtime_jobs)")}:
        connection.execute("ALTER TABLE runtime_jobs ADD COLUMN usage TEXT")
    work_columns = {row[1] for row in connection.execute("PRAGMA table_info(work_items)")}
    if "lease_until" not in work_columns:
        connection.execute("ALTER TABLE work_items ADD COLUMN lease_until REAL")
        # Tasks already running before the migration inherit their reservation expiry (or 15 minutes).
        connection.execute("""UPDATE work_items SET lease_until=COALESCE(
            (SELECT MAX(expires_at) FROM reservations r WHERE r.task_id=work_items.id), updated_at+900)
            WHERE state='running'""")
    if "lease_seconds" not in work_columns:
        connection.execute("ALTER TABLE work_items ADD COLUMN lease_seconds REAL")


def usage_from(raw):
    if not isinstance(raw, dict) or raw.get("scope") != "attempt-total" or set(raw) - {"scope", "input_tokens", "output_tokens", "cost_usd"}:
        raise BoardError("reported usage needs attempt-total scope and explicit observed units")
    for key in ("input_tokens", "output_tokens", "cost_usd"):
        value = raw.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or (key != "cost_usd" and not isinstance(value, int))):
            raise BoardError("invalid reported usage")
    json_object(raw, "usage")
    if not any(raw.get(key) is not None for key in ("input_tokens", "output_tokens", "cost_usd")):
        raise BoardError("usage contains no observed units")
    return {key: value for key, value in raw.items() if value is not None}


def bounds_from(raw: dict) -> dict:
    fields = {"max_concurrency", "max_retries", "max_dispatches", "timeout_seconds", "auto_retry"}
    if not isinstance(raw, dict) or set(raw) - fields:
        raise BoardError("unsupported execution bounds")
    result = {"max_concurrency": raw.get("max_concurrency", 4), "max_retries": raw.get("max_retries", 1),
              "max_dispatches": raw.get("max_dispatches", 100), "timeout_seconds": raw.get("timeout_seconds", 900),
              "auto_retry": raw.get("auto_retry", False)}
    for name in ("max_concurrency", "max_dispatches"):
        positive_int(result[name], name)
    if not isinstance(result["max_retries"], int) or isinstance(result["max_retries"], bool) or not 0 <= result["max_retries"] <= 20:
        raise BoardError("max_retries must be between 0 and 20")
    positive_number(result["timeout_seconds"], "timeout_seconds")
    if not isinstance(result["auto_retry"], bool) or result["max_concurrency"] > 64:
        raise BoardError("auto_retry must be boolean and concurrency cannot exceed 64")
    return result


class ExecutionMixin:
    def _plan_unlocked(self, run_id: str) -> dict:
        return self._team_plan_unlocked(run_id)

    def _run_unlocked(self, run_id: str, agent_id: str | None = None) -> dict:
        row = self._conn.execute("SELECT * FROM swarm_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise BoardError("unknown execution run")
        run = dict(row)
        run["capabilities"], run["bounds"] = json.loads(run["capabilities"]), json.loads(run["bounds"])
        if agent_id is not None and agent_id != run["lead_id"]:
            raise BoardError("only the selected lead may control this run")
        return run

    def _runtime_unlocked(self, ticket_id: str) -> dict:
        row = self._conn.execute("SELECT * FROM runtime_jobs WHERE ticket_id=?", (ticket_id,)).fetchone()
        if row is None:
            raise BoardError("unknown runtime ticket")
        record = dict(row)
        for key in ("baseline", "scope_result", "packet", "result", "usage"):
            record[key] = json.loads(record[key]) if record[key] else None
        return record

    def create_run(self, agent_id: str, run_id: str, host: str, capabilities: dict, bounds: dict | None = None) -> dict:
        limits = bounds_from(bounds or {})
        if host not in ("codex", "andrewcode", "claude", "local"):
            raise BoardError("unsupported execution host")
        if not isinstance(capabilities, dict) or capabilities.get("host") != host:
            raise BoardError("capabilities must identify the selected host")
        if capabilities.get("source") not in (("local-process",) if host == "local" else ("live-runtime", "explicit-cli")):
            raise CapabilityUnavailable("native capabilities and models must come from the live runtime")
        if capabilities.get("source") == "explicit-cli" and not capabilities.get("explicitly_requested"):
            raise CapabilityUnavailable("CLI execution must be selected explicitly")
        models, operations = capabilities.get("models"), capabilities.get("operations")
        if not isinstance(models, list) or not isinstance(operations, list) or any(not isinstance(x, str) for x in models + operations):
            raise BoardError("capabilities need explicit models and operations")
        with self._transaction():
            plan = self._plan_unlocked(run_id)
            if plan["lead"] != agent_id:
                raise BoardError("run lead differs from the selected team lead")
            if host == "claude" and not capabilities.get("explicitly_requested"):
                raise CapabilityUnavailable("Claude execution needs explicit invocation")
            missing = sorted({seat["model"] for seat in plan["roster"]} - set(models))
            reason = "selected models unavailable: " + ", ".join(missing) if missing else ""
            if "dispatch" not in operations:
                reason = "native dispatch capability unavailable"
            existing = self._conn.execute("SELECT 1 FROM swarm_runs WHERE id=?", (run_id,)).fetchone()
            if existing:
                old = self._run_unlocked(run_id, agent_id)
                if old["host"] != host or old["bounds"] != limits or old["capabilities"] != capabilities:
                    raise BoardError("an existing run cannot silently change host, roster capabilities or bounds")
                return self.run_status(agent_id, run_id)
            now = time.time()
            self._conn.execute("INSERT INTO swarm_runs(id,lead_id,host,capabilities,bounds,state,reason,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, agent_id, host, json_object(capabilities, "capabilities"), json_object(limits, "bounds"),
                 "blocked" if reason else "active", reason, now, now))
            self._conn.execute("UPDATE swarm_runs SET deadline_at=? WHERE id=?", (now + limits["timeout_seconds"], run_id))
            return self.run_status(agent_id, run_id)

    def dispatch_actions(self, agent_id: str, run_id: str) -> list[dict]:
        """Persist actions before host calls. Re-reading returns the same ticket."""
        self.expire_tasks()
        # Hashing a workspace can be slow. Keep it outside the mailbox write lock.
        with self._lock:
            run_pre = self._run_unlocked(run_id, agent_id)
            if run_pre["state"] != "active":
                return []
            plan = self._plan_unlocked(run_id)
            active_pre = self._conn.execute(
                "SELECT worker_id FROM runtime_jobs WHERE run_id=? AND state IN (" + ",".join("?" for _ in ACTIVE_RUNTIME_STATES) + ")",
                (run_id, *ACTIVE_RUNTIME_STATES),
            ).fetchall()
            occupied_pre = {row[0] for row in active_pre}
            capacity_pre = max(0, run_pre["bounds"]["max_concurrency"] - len(active_pre))
            candidates = []
            for job in plan["jobs"]:
                if not capacity_pre:
                    break
                item = self._task_unlocked(job["id"])
                brief_pre = item["brief"]
                if (
                    item["state"] != "queued"
                    or brief_pre["assigned_to"] in occupied_pre
                    or not self._dependencies_ready_unlocked(brief_pre)
                ):
                    continue
                candidates.append(brief_pre)
                occupied_pre.add(brief_pre["assigned_to"])
                capacity_pre -= 1
        exclusions = [
            str(self.path),
            str(self.path) + "-wal",
            str(self.path) + "-shm",
            str(self.jsonl_path),
            str(getattr(self, "wakeup_path", str(self.path) + ".wakeup")),
            str(Path(self.path).parent / "runtime"),
        ]
        snapshots_by_workspace: dict[str, dict] = {}
        baselines: dict[str, dict] = {}
        for job in candidates:
            ws_key = str(scoped_path(job["workspace"], "."))
            if ws_key not in snapshots_by_workspace:
                snapshots_by_workspace[ws_key] = snapshot(ws_key, exclusions)
            baselines[job["id"]] = copy.deepcopy(snapshots_by_workspace[ws_key])
        with self._transaction():
            run, plan = self._run_unlocked(run_id, agent_id), self._plan_unlocked(run_id)
            if run["state"] != "active":
                return []
            if time.time() >= (run["deadline_at"] or run["created_at"] + run["bounds"]["timeout_seconds"]):
                self._conn.execute("UPDATE swarm_runs SET state='needs_input',reason='run deadline reached',version=version+1,updated_at=? WHERE id=?", (time.time(), run_id))
                self._run_event_unlocked(run_id, agent_id, "deadline", run["version"] + 1, {})
                return []
            active = self._conn.execute("SELECT worker_id FROM runtime_jobs WHERE run_id=? AND state IN (" + ",".join("?" for _ in ACTIVE_RUNTIME_STATES) + ")",
                (run_id, *ACTIVE_RUNTIME_STATES)).fetchall()
            occupied = {row[0] for row in active}
            capacity = max(0, run["bounds"]["max_concurrency"] - len(active))
            by_seat = {seat["id"]: seat for seat in plan["roster"]}
            for brief_raw in plan["jobs"]:
                item = self._task_unlocked(brief_raw["id"])
                brief = TaskBrief.from_dict(item["brief"])
                if (not capacity or item["state"] != "queued" or brief.assigned_to in occupied or
                    not self._dependencies_ready_unlocked(item["brief"])):
                    continue
                if run["dispatches"] >= run["bounds"]["max_dispatches"]:
                    self._conn.execute("UPDATE swarm_runs SET state='needs_input',reason='dispatch budget reached',version=version+1 WHERE id=?", (run_id,))
                    self._run_event_unlocked(run_id, agent_id, "budget", run["version"] + 1, {})
                    break
                if brief.id not in baselines:
                    continue
                baseline = baselines[brief.id]
                started = self.start_task(brief.assigned_to, brief.id,
                                          lease_seconds=run["bounds"]["timeout_seconds"] + 120)
                ticket = hashlib.sha256(f"{run_id}:{brief.id}:{started['attempt']}".encode()).hexdigest()[:32]
                packet = {"ticket_id": ticket, "run_id": run_id, "task_id": brief.id, "attempt": started["attempt"],
                    "worker_id": brief.assigned_to, "model": by_seat[brief.assigned_to]["model"], "host": run["host"],
                    "workspace": brief.workspace, "writes": brief.writes,
                    "checks": brief.checks, "timeout_seconds": run["bounds"]["timeout_seconds"],
                    "candidate_binding": started.get("candidate_binding"),
                    "dependency_bindings": started.get("dependency_bindings", []),
                    "report_path": str(Path(self.path).parent / "runtime" / run_id / (ticket + ".report.json")),
                    "prompt": worker_prompt(brief, lead_id=agent_id, peers=plan["coordination_peers"][brief.assigned_to], board_path=str(Path(self.path).resolve()))}
                now = time.time()
                self._conn.execute("INSERT INTO runtime_jobs(ticket_id,run_id,task_id,attempt,worker_id,requested_model,host,state,baseline,packet,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ticket, run_id, brief.id, started["attempt"], brief.assigned_to, packet["model"], run["host"], "requested",
                     json_object(baseline, "baseline"), json_object(packet, "packet"), now, now))
                self._conn.execute("UPDATE swarm_runs SET dispatches=dispatches+1,updated_at=? WHERE id=?", (now, run_id))
                run["dispatches"] += 1
                occupied.add(brief.assigned_to)
                capacity -= 1
            return [json.loads(row[0]) for row in self._conn.execute("SELECT packet FROM runtime_jobs WHERE run_id=? AND state='requested' ORDER BY created_at", (run_id,))]

    def claim_dispatch(self, agent_id: str, ticket_id: str, owner: str) -> dict:
        if not isinstance(owner, str) or not owner.strip():
            raise BoardError("a dispatch owner is required")
        with self._transaction():
            ticket = self._runtime_unlocked(ticket_id)
            run = self._run_unlocked(ticket["run_id"], agent_id)
            if run["state"] != "active":
                raise BoardError("run dispatch is paused or blocked")
            if ticket["state"] == "claimed" and ticket["owner"] == owner:
                return {**ticket["packet"], "claim_token": ticket["claim_token"], "version": ticket["version"], "new_claim": False}
            if ticket["state"] != "requested":
                raise BoardError("dispatch is already claimed or observed; reconcile instead of duplicating it")
            token = uuid.uuid4().hex
            self._conn.execute("UPDATE runtime_jobs SET state='claimed',claim_token=?,owner=?,version=version+1,updated_at=? WHERE ticket_id=?",
                               (token, owner, time.time(), ticket_id))
            return {**ticket["packet"], "claim_token": token, "version": ticket["version"] + 1, "new_claim": True}

    def observe_runtime(self, agent_id: str, ticket_id: str, claim_token: str, expected_version: int,
                        native_handle: str, model: str, state: str, event_key: str,
                        evidence: dict | None = None, failure_kind: str = "") -> dict:
        if state not in OBSERVED_STATES or failure_kind not in FAILURE_KINDS or not event_key:
            raise BoardError("invalid runtime state, failure kind or event key")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise BoardError("expected_version must be nonnegative")
        observation = {"state": state, "native_handle": native_handle, "model": model,
                       "failure_kind": failure_kind, "evidence": evidence or {}}
        usage = usage_from(observation["evidence"]["usage"]) if "usage" in observation["evidence"] else None
        fingerprint = envelope_fingerprint(**observation)
        with self._transaction():
            ticket = self._runtime_unlocked(ticket_id)
            run = self._run_unlocked(ticket["run_id"], agent_id)
            if claim_token != ticket["claim_token"] or not claim_token:
                raise BoardError("stale dispatch ownership token")
            prior = self._conn.execute("SELECT fingerprint FROM runtime_events WHERE ticket_id=? AND event_key=?", (ticket_id, event_key)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise BoardError("runtime event key reused with different facts")
                return self.runtime_status(agent_id, ticket_id)
            if ticket["version"] != expected_version:
                raise BoardError("stale runtime version")
            if self._task_unlocked(ticket["task_id"])["attempt"] != ticket["attempt"]:
                raise BoardError("observation belongs to an old task attempt")
            if ticket["state"] not in ACTIVE_RUNTIME_STATES:
                raise BoardError("a stopped runtime cannot become active without explicit recovery")
            if not native_handle or (ticket["native_handle"] and native_handle != ticket["native_handle"]):
                raise BoardError("native handle changed; reconcile the original worker")
            unknown_failure = not model and state in ("failed", "cancelled")
            cli_unobserved = not model and run["capabilities"].get("source") == "explicit-cli"
            if not unknown_failure and not cli_unobserved and model != ticket["requested_model"]:
                raise BoardError("observed model differs from the selected worker model; no substitution is allowed")
            self._conn.execute("INSERT INTO runtime_events(ticket_id,event_key,fingerprint,observation,created_at) VALUES (?,?,?,?,?)",
                               (ticket_id, event_key, fingerprint, json_object(observation, "observation"), time.time()))
            self._conn.execute("UPDATE runtime_jobs SET state=?,native_handle=?,observed_model=?,result=?,failure_kind=?,version=version+1,updated_at=? WHERE ticket_id=?",
                (state, native_handle, model or None, json_object(observation["evidence"], "evidence"), failure_kind, time.time(), ticket_id))
            if usage is not None:
                previous = ticket.get("usage") or {}
                if any(usage.get(key) is not None and previous.get(key) is not None and usage[key] < previous[key] for key in ("input_tokens", "output_tokens", "cost_usd")):
                    raise BoardError("cumulative attempt usage cannot decrease")
                self._conn.execute("UPDATE runtime_jobs SET usage=? WHERE ticket_id=?", (json_object({**previous, **usage}, "usage"), ticket_id))
            ready = self._conn.execute("SELECT version FROM agent_readiness WHERE agent_id=? AND run_id=?", (ticket["worker_id"], ticket["run_id"])).fetchone()
            ready_state = {"dispatched": "ready", "started": "started", "working": "observed", "waiting-input": "observed",
                           "stopped": "stopped", "failed": "error", "cancelled": "stopped"}[state]
            if unknown_failure or cli_unobserved:
                self._conn.execute("UPDATE agent_readiness SET state='error',version=version+1,observed_at=? WHERE agent_id=? AND run_id=?",
                                   (time.time(), ticket["worker_id"], ticket["run_id"]))
            else:
                self.readiness(ticket["worker_id"], ticket["run_id"], ready_state, native_handle, model, ready[0] if ready else 0)
            if state in ("failed", "cancelled"):
                item = self._task_unlocked(ticket["task_id"])
                if item["state"] == "running":
                    self.submit_task(ticket["worker_id"], {"task_id": ticket["task_id"], "attempt": ticket["attempt"],
                        "status": "blocked", "summary": f"Runtime {state}: {failure_kind or 'worker'}",
                        "gaps": [str(observation["evidence"].get("error", "runtime did not return an accepted result"))]})
                if failure_kind == "infrastructure":
                    self._conn.execute("UPDATE swarm_runs SET infrastructure_failures=infrastructure_failures+1 WHERE id=?", (run["id"],))
                failures = self._run_unlocked(run["id"])["infrastructure_failures"]
                if failures >= 3:
                    self._conn.execute("UPDATE swarm_runs SET state='needs_input',reason='repeated infrastructure failure',version=version+1 WHERE id=?", (run["id"],))
                    self._run_event_unlocked(run["id"], agent_id, "infrastructure-circuit", run["version"] + 1, {"failures": failures})
                elif (state == "failed" and failure_kind == "infrastructure" and run["bounds"]["auto_retry"]
                      and ticket["attempt"] <= run["bounds"]["max_retries"]):
                    self.review_task(agent_id, ticket["task_id"], ticket["attempt"], "revise", "Bounded retry of the same selected model after observed infrastructure failure")
            return self.runtime_status(agent_id, ticket_id)

    def submit_runtime_result(self, agent_id: str, ticket_id: str, report: dict) -> dict:
        with self._lock:
            captured = self._runtime_unlocked(ticket_id)
            self._run_unlocked(captured["run_id"], agent_id)
            brief = self._task_unlocked(captured["task_id"])["brief"]
            plan = self._plan_unlocked(captured["run_id"])
            others = [path for job in plan["jobs"] if job["id"] != brief["id"] for path in job["writes"]]
        scope = inspect_changes(captured["baseline"], brief["writes"], other_owned_scopes=others)
        with self._transaction():
            ticket = self._runtime_unlocked(ticket_id)
            self._run_unlocked(ticket["run_id"], agent_id)
            if ticket["state"] != "stopped" or ticket["observed_model"] != ticket["requested_model"]:
                raise BoardError("result requires the stopped original worker and its observed selected model")
            item = self._task_unlocked(ticket["task_id"])
            if item["attempt"] != ticket["attempt"]:
                raise BoardError("result belongs to a stale runtime attempt")
            parsed = WorkerReport.from_dict(report, TaskBrief.from_dict(item["brief"]))
            if ticket["version"] != captured["version"]:
                raise BoardError("runtime changed during scope inspection")
            if scope["violations"]:
                raise BoardError("observed out-of-scope changes: " + ", ".join(scope["violations"]))
            actual_files = {path for path in scope["changed_paths"] if scope["current"]["files"].get(path, ticket["baseline"]["files"].get(path, {})).get("kind") != "directory"}
            root = Path(item["brief"]["workspace"])
            reported = {scoped_path(str(root), path).relative_to(root).as_posix() for path in parsed.changed_paths}
            if reported != actual_files or len(reported) != len(parsed.changed_paths):
                raise BoardError("reported changed_paths differ from observed baseline changes")
            self._conn.execute("UPDATE runtime_jobs SET scope_result=?,updated_at=? WHERE ticket_id=?",
                               (json_object({key: value for key, value in scope.items() if key != "current"}, "scope"), time.time(), ticket_id))
            self.submit_task(ticket["worker_id"], report)
            return self.runtime_status(agent_id, ticket_id)

    def runtime_status(self, agent_id: str, ticket_id: str) -> dict:
        with self._lock:
            ticket = self._runtime_unlocked(ticket_id)
            self._run_unlocked(ticket["run_id"], agent_id)
            return {key: value for key, value in ticket.items() if key not in ("baseline", "claim_token", "packet")}

    def dispatch_uncertain(self, agent_id: str, ticket_id: str, claim_token: str, note: str, native_handle: str = "") -> dict:
        if not isinstance(note, str) or not note.strip():
            raise BoardError("uncertain dispatch needs a diagnostic")
        with self._transaction():
            ticket = self._runtime_unlocked(ticket_id)
            self._run_unlocked(ticket["run_id"], agent_id)
            if ticket["claim_token"] != claim_token or ticket["state"] not in ACTIVE_RUNTIME_STATES:
                raise BoardError("uncertain dispatch needs its current ownership token")
            if ticket["native_handle"] and native_handle and ticket["native_handle"] != native_handle:
                raise BoardError("uncertain dispatch must retain the original handle")
            self._conn.execute("UPDATE runtime_jobs SET state='uncertain',native_handle=?,result=?,version=version+1,updated_at=? WHERE ticket_id=?",
                (native_handle or ticket["native_handle"], json_object({"error": note}, "diagnostic"), time.time(), ticket_id))
            return self.runtime_status(agent_id, ticket_id)

    def run_status(self, agent_id: str, run_id: str) -> dict:
        self.expire_tasks()
        with self._lock:
            run = self._run_unlocked(run_id, agent_id)
            ids = {job["id"] for job in self._plan_unlocked(run_id)["jobs"]}
            tasks = self._tasks_by_ids_unlocked(ids)
            tickets = [self.runtime_status(agent_id, row[0]) for row in self._conn.execute("SELECT ticket_id FROM runtime_jobs WHERE run_id=? ORDER BY created_at", (run_id,))]
            if tasks and all(item["state"] == "accepted" for item in tasks):
                derived = "completed"
            elif run["state"] != "active":
                derived = run["state"]
            elif any(item["state"] == "escalated" for item in tasks) or any(t["state"] in ("waiting-input", "uncertain") for t in tickets):
                derived = "needs_input"
            elif any(item["state"] == "review" for item in tasks):
                derived = "awaiting_review"
            elif any(item["state"] == "blocked" for item in tasks):
                derived = "blocked"
            else:
                derived = "working" if any(t["state"] in ACTIVE_RUNTIME_STATES for t in tickets) else "ready"
            reported = [ticket["usage"] for ticket in tickets if ticket.get("usage")]
            usage = None if not reported else {"scope": "reported-attempt-totals", "reported_attempts": len(reported), "total_attempts": len(tickets),
                "known_totals": {key: sum(value[key] for value in reported if value.get(key) is not None) if any(value.get(key) is not None for value in reported) else None for key in ("input_tokens", "output_tokens", "cost_usd")}}
            return {**run, "observed_state": derived, "tasks": [{"id": t["id"], "state": t["state"], "attempt": t["attempt"]} for t in tasks],
                    "runtimes": tickets, "provider_usage": usage, "verification": "native observations and baseline changes; acceptance remains with the lead"}

    def control_run(self, agent_id: str, run_id: str, state: str, note: str, expected_version: int) -> dict:
        positive_int(expected_version, "expected_version")
        if state not in ("paused", "active") or not isinstance(note, str) or not note.strip():
            raise BoardError("run control requires active/paused and a reason")
        with self._transaction():
            run = self._run_unlocked(run_id, agent_id)
            if run["version"] != expected_version:
                if run["version"] == expected_version + 1 and run["state"] == state and run["reason"] == note:
                    return self.run_status(agent_id, run_id)
                raise BoardError("stale run control version")
            if state == "active":
                plan = self._plan_unlocked(run_id)
                if "dispatch" not in run["capabilities"]["operations"] or {seat["model"] for seat in plan["roster"]} - set(run["capabilities"]["models"]):
                    raise BoardError("resolve unavailable capabilities before activating this run")
            self._conn.execute("UPDATE swarm_runs SET state=?,reason=?,version=version+1,infrastructure_failures=CASE WHEN ?='active' THEN 0 ELSE infrastructure_failures END,updated_at=? WHERE id=?",
                               (state, note, state, time.time(), run_id))
            self._run_event_unlocked(run_id, agent_id, state, expected_version + 1, {"note": note})
            return self.run_status(agent_id, run_id)

    def _run_event_unlocked(self, run_id: str, actor: str, action: str, version: int, details: dict) -> None:
        self._conn.execute("INSERT INTO run_events(run_id,actor,action,version,details,created_at) VALUES (?,?,?,?,?,?)",
                           (run_id, actor, action, version, json_object(details, "event"), time.time()))

    def amend_run_bounds(self, agent_id: str, run_id: str, bounds: dict, expected_version: int, note: str, renew_deadline: bool = False) -> dict:
        positive_int(expected_version, "expected_version")
        limits = bounds_from(bounds)
        if not isinstance(note, str) or not note.strip() or not isinstance(renew_deadline, bool):
            raise BoardError("budget amendment needs a reason and a boolean deadline renewal")
        with self._transaction():
            run = self._run_unlocked(run_id, agent_id)
            if run["version"] != expected_version:
                raise BoardError("stale run budget version")
            deadline = time.time() + limits["timeout_seconds"] if renew_deadline else run["deadline_at"]
            self._conn.execute("UPDATE swarm_runs SET bounds=?,deadline_at=?,version=version+1,updated_at=? WHERE id=?",
                (json_object(limits, "bounds"), deadline, time.time(), run_id))
            self._run_event_unlocked(run_id, agent_id, "bounds", expected_version + 1, {"bounds": limits, "renew_deadline": renew_deadline, "note": note})
            return self.run_status(agent_id, run_id)

    def run_history(self, agent_id: str, run_id: str) -> list[dict]:
        with self._lock:
            self._run_unlocked(run_id, agent_id)
            return [{**dict(row), "details": json.loads(row["details"])} for row in self._conn.execute("SELECT * FROM run_events WHERE run_id=? ORDER BY id", (run_id,))]

    def supervision(self, agent_id: str, run_id: str) -> dict:
        from .supervision import status
        return status(self, agent_id, run_id)

    def export_run(self, agent_id: str, run_id: str) -> dict:
        from .supervision import export
        return export(self, agent_id, run_id)

    def reconcile_runtime(self, agent_id: str, ticket_id: str, native_handle: str, stopped: bool, note: str) -> dict:
        if not isinstance(stopped, bool) or not isinstance(note, str) or not note.strip():
            raise BoardError("reconciliation needs an observed stopped flag and scope note")
        with self._transaction():
            ticket = self._runtime_unlocked(ticket_id)
            self._run_unlocked(ticket["run_id"], agent_id)
            if not stopped:
                raise BoardError("the original worker may still be alive; re-dispatch is refused")
            if not isinstance(native_handle, str) or not native_handle.strip():
                raise BoardError("reconciliation needs the observed original handle or a verified not-dispatched ticket identity")
            if ticket["native_handle"] and ticket["native_handle"] != native_handle:
                raise BoardError("reconcile the recorded native handle, not another worker")
            item = self._task_unlocked(ticket["task_id"])
            if item["attempt"] != ticket["attempt"]:
                raise BoardError("reconciliation belongs to a stale task attempt")
            scope = inspect_changes(ticket["baseline"], item["brief"]["writes"])
            if scope["violations"]:
                raise BoardError("reconcile out-of-scope changes before requeuing: " + ", ".join(scope["violations"]))
            self._conn.execute("UPDATE runtime_jobs SET scope_result=? WHERE ticket_id=?",
                (json_object({key: value for key, value in scope.items() if key != "current"}, "scope"), ticket_id))
            self._conn.execute("UPDATE runtime_jobs SET state='cancelled',native_handle=?,version=version+1,updated_at=? WHERE ticket_id=?",
                               (native_handle, time.time(), ticket_id))
            self._conn.execute("UPDATE agent_readiness SET state='stopped',version=version+1,observed_at=? WHERE agent_id=? AND run_id=?",
                               (time.time(), ticket["worker_id"], ticket["run_id"]))
            if item["state"] == "running":
                self.recover_task(agent_id, item["id"], item["attempt"], note)
            return self.runtime_status(agent_id, ticket_id)

    def refresh_capabilities(self, agent_id: str, run_id: str, capabilities: dict, expected_version: int) -> dict:
        positive_int(expected_version, "expected_version")
        with self._transaction():
            run, plan = self._run_unlocked(run_id, agent_id), self._plan_unlocked(run_id)
            if run["version"] != expected_version or capabilities.get("host") != run["host"]:
                raise BoardError("stale capability version or a different host")
            source = run["capabilities"]["source"]
            if capabilities.get("source") != source or not isinstance(capabilities.get("models"), list) or not isinstance(capabilities.get("operations"), list):
                raise CapabilityUnavailable("capability refresh needs the same host's live inventory")
            if run["host"] == "claude" and not capabilities.get("explicitly_requested"):
                raise CapabilityUnavailable("Claude execution still requires explicit invocation")
            missing = {seat["model"] for seat in plan["roster"]} - set(capabilities["models"])
            reason = "selected models unavailable: " + ", ".join(sorted(missing)) if missing else ""
            if "dispatch" not in capabilities["operations"]:
                reason = "native dispatch capability unavailable"
            state = "blocked" if reason else ("active" if run["state"] == "blocked" else run["state"])
            self._conn.execute("UPDATE swarm_runs SET capabilities=?,state=?,reason=?,version=version+1,updated_at=? WHERE id=?",
                               (json_object(capabilities, "capabilities"), state, reason, time.time(), run_id))
            self._run_event_unlocked(run_id, agent_id, "capabilities", expected_version + 1, {"state": state, "reason": reason})
            return self.run_status(agent_id, run_id)
