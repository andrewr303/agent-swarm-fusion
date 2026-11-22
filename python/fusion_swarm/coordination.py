"""Scoped context, file ownership and reviewed task handoffs on one board."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path, PurePath
from typing import Any

from .mailbox import BoardError, envelope_fingerprint, json_object, positive_int, positive_number
from .sidekick import TaskBrief, WorkerReport, scoped_path, strings, text

COORDINATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    path TEXT PRIMARY KEY, workspace TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    task_id TEXT NOT NULL DEFAULT '', expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shared_context (
    namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    version INTEGER NOT NULL, author TEXT NOT NULL REFERENCES agents(id),
    updated_at REAL NOT NULL, PRIMARY KEY(namespace,key)
);
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY, created_by TEXT NOT NULL REFERENCES agents(id),
    assigned_to TEXT NOT NULL REFERENCES agents(id),
    brief TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
    attempt INTEGER NOT NULL DEFAULT 0, report TEXT,
    review_note TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES work_items(id),
    actor TEXT NOT NULL REFERENCES agents(id),
    state TEXT NOT NULL, attempt INTEGER NOT NULL,
    detail TEXT NOT NULL, created_at REAL NOT NULL
);
"""


TEAM_PLAN_NAMESPACE = "fusion"
TEAM_PLAN_LEGACY_KEY = "team-plan"
TEAM_PLAN_PREFIX = "team-plan/"
_CASE_CACHE: dict[str, bool] = {}


def team_plan_key(run_id: str) -> str:
    return TEAM_PLAN_PREFIX + run_id


def fs_case_insensitive(directory: str) -> bool:
    """Detect a case-insensitive filesystem without writing into the workspace.

    A case-swapped spelling of an existing path component resolves to the same
    file only on a case-insensitive volume. Result is cached per directory.
    """
    cached = _CASE_CACHE.get(directory)
    if cached is not None:
        return cached
    result = None
    path = Path(directory)
    probe = path
    while True:
        name = probe.name
        if name and name.swapcase() != name and probe.exists():
            alt = probe.with_name(name.swapcase())
            try:
                result = bool(alt.exists() and os.path.samefile(probe, alt))
            except OSError:
                result = False
            break
        if probe.parent == probe:
            break
        probe = probe.parent
    if result is None:
        result = sys.platform in ("darwin", "win32")
    if len(_CASE_CACHE) > 256:
        _CASE_CACHE.clear()
    _CASE_CACHE[directory] = result
    return result


def _fold(workspace_root: str, key: str) -> str:
    key = os.path.normcase(key)
    return key.casefold() if fs_case_insensitive(workspace_root) else key


def path_key(workspace: str, path: str) -> str:
    """Persisted reservation path: the resolved absolute path in its original case."""
    return os.path.normcase(str(scoped_path(workspace, path)))


def collision_key(workspace: str, path: str) -> str:
    """Comparison key for overlap tests only; case-folded on case-insensitive filesystems."""
    return _fold(str(scoped_path(workspace, ".")), str(scoped_path(workspace, path)))


def overlaps(first: str, second: str) -> bool:
    """Component-wise ancestry test: `src` and `src2` do not overlap."""
    a, b = PurePath(first).parts, PurePath(second).parts
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def is_under(key: str, parent: str) -> bool:
    a, b = PurePath(key).parts, PurePath(parent).parts
    return len(a) >= len(b) and a[:len(b)] == b


class CoordinationMixin:
    # --- per-run team plans (B5) -------------------------------------------

    def _team_plan_marker_unlocked(self, run_id: str) -> dict:
        """context_get-style marker for a run: per-run key, else a matching legacy key."""
        marker = self.context_get(TEAM_PLAN_NAMESPACE, team_plan_key(run_id))
        if marker["found"]:
            return marker
        legacy = self.context_get(TEAM_PLAN_NAMESPACE, TEAM_PLAN_LEGACY_KEY)
        if legacy["found"] and isinstance(legacy["value"], dict) and legacy["value"].get("run_id") == run_id:
            return legacy
        return marker

    def _team_plan_unlocked(self, run_id: str) -> dict:
        marker = self._team_plan_marker_unlocked(run_id)
        if not marker["found"]:
            raise BoardError("prepare the matching team plan before starting execution")
        return marker["value"]

    def _all_team_plans_unlocked(self) -> list[dict]:
        """Every plan on this board, newest first (per-run keys, then a distinct legacy key)."""
        rows = self._conn.execute(
            "SELECT key,value FROM shared_context WHERE namespace=? AND (key=? OR key LIKE ?) ORDER BY updated_at DESC, key",
            (TEAM_PLAN_NAMESPACE, TEAM_PLAN_LEGACY_KEY, TEAM_PLAN_PREFIX + "%")).fetchall()
        plans, seen = [], set()
        for row in sorted(rows, key=lambda r: r["key"] == TEAM_PLAN_LEGACY_KEY):
            try:
                value = json.loads(row["value"])
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("run_id"), str) and value["run_id"] not in seen:
                seen.add(value["run_id"])
                plans.append(value)
        return plans

    def _plan_for_task_unlocked(self, task_id: str) -> dict | None:
        plans = self._all_team_plans_unlocked()
        for plan in plans:
            if any(isinstance(job, dict) and job.get("id") == task_id for job in plan.get("jobs", [])):
                return plan
        legacy = self.context_get(TEAM_PLAN_NAMESPACE, TEAM_PLAN_LEGACY_KEY)
        if legacy["found"] and isinstance(legacy["value"], dict) and "run_id" in legacy["value"]:
            return legacy["value"]
        return plans[0] if len(plans) == 1 else None

    def _publish_team_plan_unlocked(self, agent_id: str, plan: dict) -> None:
        """Write the per-run key and keep the legacy key readable for older callers."""
        run_id = plan["run_id"]
        per_run = self.context_get(TEAM_PLAN_NAMESPACE, team_plan_key(run_id))
        if not per_run["found"]:
            self.context_set(agent_id, TEAM_PLAN_NAMESPACE, team_plan_key(run_id), plan, expected_version=0)
        elif per_run["value"] != plan:
            self.context_set(agent_id, TEAM_PLAN_NAMESPACE, team_plan_key(run_id), plan, expected_version=per_run["version"])
        legacy = self.context_get(TEAM_PLAN_NAMESPACE, TEAM_PLAN_LEGACY_KEY)
        if not legacy["found"]:
            self.context_set(agent_id, TEAM_PLAN_NAMESPACE, TEAM_PLAN_LEGACY_KEY, plan, expected_version=0)
        elif isinstance(legacy["value"], dict) and legacy["value"].get("run_id") == run_id and legacy["value"] != plan:
            self.context_set(agent_id, TEAM_PLAN_NAMESPACE, TEAM_PLAN_LEGACY_KEY, plan, expected_version=legacy["version"])

    def _other_run_ids_unlocked(self, run_id: str) -> set[str]:
        return {plan["run_id"] for plan in self._all_team_plans_unlocked()} - {run_id}

    def _tasks_by_ids_unlocked(self, ids) -> list[dict]:
        ids = sorted(set(ids))
        out = []
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            out.extend(self._task_unlocked(row[0]) for row in
                       self._conn.execute(f"SELECT id FROM work_items WHERE id IN ({marks}) ORDER BY id", chunk))
        return out

    def _plan_tasks_unlocked(self, task_id: str) -> list[dict]:
        """Tasks of the plan owning `task_id`; every task on the board when no plan owns it."""
        plan = self._plan_for_task_unlocked(task_id)
        if plan is None:
            return [self._task_unlocked(r[0]) for r in self._conn.execute("SELECT id FROM work_items ORDER BY id")]
        return self._tasks_by_ids_unlocked(job["id"] for job in plan["jobs"])

    def _notify_task(self, sender: str, recipient: str, body: str, kind: str, task_id: str,
                     payload: dict, attempt: int, priority: str = "normal", idempotency_key: str | None = None) -> dict:
        plan_value = self._plan_for_task_unlocked(task_id)
        if plan_value is None:
            return self.dm(sender, recipient, body, kind=kind, task_id=task_id,
                           payload=payload, priority=priority, idempotency_key=idempotency_key)
        return self._send_task_envelope(sender, recipient, body, kind, task_id, payload, attempt, priority,
                                        idempotency_key, plan_value["run_id"])

    def _send_task_envelope(self, sender, recipient, body, kind, task_id, payload, attempt, priority, idempotency_key, run_id):
        return self.send_message(sender, recipient, {"schema_version": 1, "run_id": run_id,
            "task_id": task_id, "attempt": attempt, "trace_id": f"{run_id}:{task_id}:{attempt}",
            "purpose": "assignment" if kind == "task" else kind, "summary": body,
            "requested_action": {"task": "Start the assignment when dependencies are accepted",
                "handoff": "Inspect the candidate artifacts and evidence before deciding",
                "decision": "Apply the lead's decision to this task attempt"}[kind]},
            idempotency_key or f"task:{task_id}:{kind}:{attempt}:{envelope_fingerprint(body=body,payload=payload)[:16]}",
            priority=priority, payload=payload)

    def _reserve_unlocked(self, agent_id: str, workspace: str, paths: list[str],
                          ttl: float, task_id: str = "") -> list[dict]:
        self._require_agent(agent_id)
        keys = list(dict.fromkeys(path_key(workspace, p) for p in strings(paths, "paths")))
        folds = {key: collision_key(workspace, key) for key in keys}
        now = time.time()
        self._conn.execute("DELETE FROM reservations WHERE expires_at<=?", (now,))
        existing = self._conn.execute("SELECT * FROM reservations").fetchall()
        existing_folds = [(owner, _fold(owner["workspace"], owner["path"])) for owner in existing]
        for key in keys:
            for owner, owner_fold in existing_folds:
                if overlaps(folds[key], owner_fold) and (owner["agent_id"] != agent_id
                        or owner["task_id"] != task_id):
                    raise BoardError(f"write collision: {key} reserved by {owner['agent_id']} ({owner['task_id']})")
        for key in keys:
            # Same owner re-reserving with a different spelling of the same path replaces the old row.
            for owner, owner_fold in existing_folds:
                if (owner_fold == folds[key] and owner["path"] != key and owner["agent_id"] == agent_id
                        and owner["task_id"] == task_id):
                    self._conn.execute("DELETE FROM reservations WHERE path=?", (owner["path"],))
            self._conn.execute(
                """INSERT INTO reservations(path,workspace,agent_id,task_id,expires_at)
                VALUES (?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET expires_at=excluded.expires_at""",
                (key, str(scoped_path(workspace, ".")), agent_id, task_id, now + ttl),
            )
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM reservations WHERE agent_id=? AND task_id=? ORDER BY path", (agent_id, task_id)
        )]

    def reserve(self, agent_id: str, workspace: str, paths: list[str],
                lease_seconds: float = 900, task_id: str = "") -> list[dict]:
        ttl = positive_number(lease_seconds, "lease_seconds")
        paths = strings(paths, "paths")
        with self._transaction():
            if task_id:
                item = self._task_unlocked(task_id)
                if item["assigned_to"] != agent_id or item["state"] not in ("running", "review"):
                    raise BoardError("task reservations require its current owner and active task")
                brief = TaskBrief.from_dict(item["brief"])
                permitted = [collision_key(brief.workspace, p) for p in brief.writes]
                if collision_key(workspace, ".") != collision_key(brief.workspace, "."):
                    raise BoardError("reservation workspace does not match task")
                for path in paths:
                    key = collision_key(workspace, path)
                    if not any(is_under(key, p) for p in permitted):
                        raise BoardError("reservation exceeds task write scope")
            return self._reserve_unlocked(agent_id, workspace, paths, ttl, task_id)

    def _reservation_rows_unlocked(self, workspace: str, key: str) -> list:
        """Rows whose collision key equals `key`'s (case-insensitive where the filesystem is)."""
        target = collision_key(workspace, key)
        return [row for row in self._conn.execute("SELECT * FROM reservations").fetchall()
                if _fold(row["workspace"], row["path"]) == target]

    def reservations(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM reservations WHERE expires_at>? ORDER BY path", (time.time(),)
            )]

    def release(self, agent_id: str, workspace: str, paths: list[str], task_id: str = "") -> dict:
        keys = [path_key(workspace, p) for p in strings(paths, "paths")]
        with self._transaction():
            self._require_agent(agent_id)
            for key in keys:
                for owner in self._reservation_rows_unlocked(workspace, key):
                    if owner["agent_id"] != agent_id or owner["task_id"] != task_id:
                        raise BoardError("only the reservation owner may release it")
                    self._conn.execute("DELETE FROM reservations WHERE path=?", (owner["path"],))
        return {"released": keys, "agent_id": agent_id}

    def context_get(self, namespace: str, key: str) -> dict:
        text(namespace, "namespace")
        text(key, "key")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM shared_context WHERE namespace=? AND key=?", (namespace, key)
            ).fetchone()
            if not row:
                return {"namespace": namespace, "key": key, "found": False, "version": 0}
            result = dict(row)
            result.update(value=json.loads(result["value"]), found=True)
            return result

    def context_set(self, agent_id: str, namespace: str, key: str, value: Any,
                    expected_version: int) -> dict:
        text(namespace, "namespace")
        text(key, "key")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise BoardError("expected_version must be an integer >= 0")
        serialized = json_object({"value": value}, "context")
        encoded = json.dumps(json.loads(serialized)["value"], ensure_ascii=False, allow_nan=False)
        with self._transaction():
            self._require_agent(agent_id)
            previous = self.context_get(namespace, key)
            if previous["version"] != expected_version:
                raise BoardError(f"stale context version: expected {expected_version}, actual {previous['version']}")
            self._conn.execute(
                """INSERT INTO shared_context(namespace,key,value,version,author,updated_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET
                value=excluded.value,version=excluded.version,author=excluded.author,updated_at=excluded.updated_at""",
                (namespace, key, encoded, expected_version + 1, agent_id, time.time()),
            )
            return self.context_get(namespace, key)

    def _task_unlocked(self, task_id: str) -> dict:
        text(task_id, "task_id")
        row = self._conn.execute("SELECT * FROM work_items WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise BoardError(f"unknown task: {task_id}")
        item = dict(row)
        item["brief"] = json.loads(item["brief"])
        item["report"] = json.loads(item["report"]) if item["report"] else None
        return item

    def _task_event_unlocked(self, task_id: str, actor: str, state: str, attempt: int, detail: str):
        self._conn.execute(
            "INSERT INTO task_events(task_id,actor,state,attempt,detail,created_at) VALUES (?,?,?,?,?,?)",
            (task_id, actor, state, attempt, detail, time.time()),
        )

    def create_task(self, created_by: str, brief: dict) -> dict:
        parsed = TaskBrief.from_dict(brief)
        encoded = json_object(parsed.to_dict(), "brief")
        with self._transaction():
            self._require_agent(created_by)
            self._require_agent(parsed.assigned_to)
            if created_by == parsed.assigned_to:
                raise BoardError("a delegated task needs a distinct lead and worker")
            old = self._conn.execute("SELECT * FROM work_items WHERE id=?", (parsed.id,)).fetchone()
            if old:
                if TaskBrief.from_dict(json.loads(old["brief"])).to_dict() != parsed.to_dict() or old["created_by"] != created_by:
                    raise BoardError("task id already exists with a different brief or lead")
                return self._task_unlocked(parsed.id)
            for dependency in parsed.dependencies:
                self._task_unlocked(dependency)
            self._conn.execute(
                "INSERT INTO work_items(id,created_by,assigned_to,brief,updated_at) VALUES (?,?,?,?,?)",
                (parsed.id, created_by, parsed.assigned_to, encoded, time.time()),
            )
            self._task_event_unlocked(parsed.id, created_by, "queued", 0, "brief created")
            self._notify_task(sender=created_by, recipient=parsed.assigned_to, body=parsed.goal,
                      kind="task", task_id=parsed.id, payload={"brief": parsed.to_dict()},
                      attempt=1, idempotency_key=f"task:{parsed.id}:brief")
            return self._task_unlocked(parsed.id)

    def tasks(self, task_id: str | None = None) -> list[dict]:
        self._expire_if_due()
        with self._lock:
            if task_id:
                return [self._task_unlocked(task_id)]
            return [self._task_unlocked(row[0]) for row in self._conn.execute("SELECT id FROM work_items ORDER BY id")]

    def start_task(self, agent_id: str, task_id: str, lease_seconds: float = 900) -> dict:
        ttl = positive_number(lease_seconds, "lease_seconds")
        with self._transaction():
            self._require_agent(agent_id)
            item = self._task_unlocked(task_id)
            if item["assigned_to"] != agent_id or item["state"] != "queued":
                raise BoardError("only the assigned worker may start a queued task")
            brief = TaskBrief.from_dict(item["brief"])
            if not self._dependencies_ready_unlocked(item["brief"]):
                raise BoardError("dependency is not accepted or the reviewed candidate is unavailable")
            self._reserve_unlocked(agent_id, brief.workspace, brief.writes, ttl, task_id)
            attempt = item["attempt"] + 1
            binding = self._bind_candidate_unlocked(item, attempt)
            inputs = self._bind_dependencies_unlocked(item, attempt)
            self._conn.execute(
                "UPDATE work_items SET state='running',attempt=?,report=NULL,lease_until=?,lease_seconds=?,updated_at=? WHERE id=?",
                (attempt, time.time() + ttl, ttl, time.time(), task_id),
            )
            self._task_event_unlocked(task_id, agent_id, "running", attempt, "worker started")
            return {**self._task_unlocked(task_id), "dependency_bindings": inputs, **({"candidate_binding": binding} if binding else {})}

    def renew_task(self, agent_id: str, task_id: str, attempt: int, lease_seconds: float = 900) -> dict:
        """Extend a running attempt's lease and its write reservations (assignee only, attempt-fenced)."""
        ttl = positive_number(lease_seconds, "lease_seconds")
        positive_int(attempt, "attempt")
        with self._transaction():
            self._require_agent(agent_id)
            self._expire_tasks_unlocked(time.time())
            item = self._task_unlocked(task_id)
            if item["assigned_to"] != agent_id or item["state"] != "running" or item["attempt"] != attempt:
                raise BoardError("renewal is stale or worker does not own this running attempt")
            now = time.time()
            brief = TaskBrief.from_dict(item["brief"])
            lease = max(item["lease_until"] or 0, now + ttl)
            # Re-asserting is idempotent for the same owner and restores purged rows; a foreign holder still collides.
            self._reserve_unlocked(agent_id, brief.workspace, brief.writes, lease - now, task_id)
            self._conn.execute("UPDATE work_items SET lease_until=?,lease_seconds=?,updated_at=? WHERE id=?",
                               (lease, max(ttl, item["lease_seconds"] or 0), now, task_id))
            self._conn.execute("UPDATE reservations SET expires_at=MAX(expires_at,?) WHERE task_id=? AND agent_id=?",
                               (lease, task_id, agent_id))
            return self._task_unlocked(task_id)

    def _expire_if_due(self) -> None:
        """Cheap read probe so read paths open a write transaction only when a lease lapsed."""
        with self._lock:
            due = self._conn.execute("SELECT 1 FROM work_items WHERE state='running' AND lease_until IS NOT NULL AND lease_until<? LIMIT 1",
                                     (time.time(),)).fetchone()
        if due:
            self.expire_tasks()

    def expire_tasks(self, now: float | None = None) -> list[str]:
        """Requeue running tasks whose lease lapsed with no runtime observation inside the lease."""
        stamp = time.time() if now is None else float(now)
        with self._lock:
            due = self._conn.execute("SELECT 1 FROM work_items WHERE state='running' AND lease_until IS NOT NULL AND lease_until<? LIMIT 1",
                                     (stamp,)).fetchone()
        if not due:
            return []
        with self._transaction():
            return self._expire_tasks_unlocked(stamp)

    def _expire_tasks_unlocked(self, now: float) -> list[str]:
        expired = []
        rows = self._conn.execute("SELECT id FROM work_items WHERE state='running' AND lease_until IS NOT NULL AND lease_until<? ORDER BY id",
                                  (now,)).fetchall()
        for row in rows:
            item = self._task_unlocked(row[0])
            window = item["lease_seconds"] or 900
            job = self._conn.execute("SELECT ticket_id,updated_at FROM runtime_jobs WHERE task_id=? AND attempt=?",
                                     (item["id"], item["attempt"])).fetchone()
            if job:
                last = self._conn.execute("SELECT MAX(created_at) FROM runtime_events WHERE ticket_id=?", (job["ticket_id"],)).fetchone()[0]
                if max(job["updated_at"] or 0, last or 0) >= now - window:
                    continue  # runtime activity observed inside the lease: the worker is not lost
            self._conn.execute("UPDATE work_items SET state='queued',lease_until=NULL,updated_at=? WHERE id=?", (now, item["id"]))
            self._conn.execute("DELETE FROM reservations WHERE task_id=?", (item["id"],))
            self._conn.execute("INSERT INTO task_events(task_id,actor,state,attempt,detail,created_at) VALUES (?,?,?,?,?,?)",
                               (item["id"], item["assigned_to"], "expired", item["attempt"],
                                "lease expired without runtime observation; requeued", now))
            expired.append(item["id"])
        return expired

    def submit_task(self, agent_id: str, report: dict) -> dict:
        with self._transaction():
            self._require_agent(agent_id)
            item = self._task_unlocked(report.get("task_id") if isinstance(report, dict) else "")
            parsed = WorkerReport.from_dict(report, TaskBrief.from_dict(item["brief"]))
            if (item["assigned_to"] == agent_id and item["attempt"] == parsed.attempt
                    and item["state"] in ("review", "blocked", "escalated", "accepted")
                    and item["report"] == parsed.to_dict()):
                return item  # Lost reply: identical report retry must not duplicate the handoff.
            if item["assigned_to"] != agent_id or item["state"] != "running" or item["attempt"] != parsed.attempt:
                raise BoardError("report is stale or worker does not own a running task")
            # A complete write report is valid only while its task still owns its scope.
            if parsed.status == "complete":
                self._check_dependency_inputs_unlocked(item)
                for raw in item["brief"]["writes"]:
                    rows = self._reservation_rows_unlocked(item["brief"]["workspace"], path_key(item["brief"]["workspace"], raw))
                    owner = next((r for r in rows if r["agent_id"] == agent_id and r["task_id"] == item["id"]), rows[0] if rows else None)
                    if not owner or owner["agent_id"] != agent_id or owner["task_id"] != item["id"] or owner["expires_at"] <= time.time():
                        raise BoardError("write reservation expired; reconcile and renew before submitting complete")
            state = {"complete": "review", "partial": "blocked", "blocked": "blocked", "escalate": "escalated"}[parsed.status]
            self._conn.execute("UPDATE work_items SET state=?,report=?,lease_until=NULL,updated_at=? WHERE id=?",
                               (state, json_object(parsed.to_dict(), "report"), time.time(), item["id"]))
            self._task_event_unlocked(item["id"], agent_id, state, parsed.attempt, parsed.summary)
            self._publish_candidate_unlocked(self._task_unlocked(item["id"]))
            self._notify_task(sender=agent_id, recipient=item["created_by"], kind="handoff",
                      body=parsed.summary, task_id=item["id"], payload={"report": parsed.to_dict()},
                      priority="urgent" if state in ("blocked", "escalated") else "normal",
                      attempt=parsed.attempt, idempotency_key=f"task:{item['id']}:report:{parsed.attempt}")
            return self._task_unlocked(item["id"])

    def review_task(self, agent_id: str, task_id: str, attempt: int, decision: str, note: str) -> dict:
        positive_int(attempt, "attempt")
        text(note, "review note")
        if decision not in ("accept", "revise", "escalate", "cancel"):
            raise BoardError("decision must be accept, revise, escalate or cancel")
        with self._transaction():
            self._require_agent(agent_id)
            item = self._task_unlocked(task_id)
            state = {"accept": "accepted", "revise": "queued", "escalate": "escalated", "cancel": "cancelled"}[decision]
            if (item["created_by"] == agent_id and item["attempt"] == attempt
                    and item["state"] == state and item["review_note"] == note):
                return item
            if item["created_by"] != agent_id or item["attempt"] != attempt or item["state"] not in ("review", "blocked", "escalated"):
                raise BoardError("only the lead may review the current submitted attempt")
            if decision == "accept" and item["state"] != "review":
                raise BoardError("unfinished work cannot be accepted")
            if decision == "accept":
                self._check_candidate_acceptance_unlocked(item)
                runtime = self._conn.execute("SELECT * FROM runtime_jobs WHERE task_id=? AND attempt=?", (task_id, attempt)).fetchone()
                if runtime:
                    scope = json.loads(runtime["scope_result"]) if runtime["scope_result"] else {}
                    if runtime["state"] != "stopped" or runtime["observed_model"] != runtime["requested_model"] or scope.get("clean_scope") is not True:
                        raise BoardError("runtime acceptance requires the stopped selected model and observed scope validation")
            if decision in ("accept", "revise", "cancel"):
                active_runtime = self._conn.execute("SELECT 1 FROM runtime_jobs WHERE task_id=? AND state IN ('requested','claimed','dispatched','started','working','waiting-input','uncertain')", (task_id,)).fetchone()
                if active_runtime:
                    raise BoardError("observe the original runtime stopped before accepting or revising its task")
            if decision == "revise":
                self._invalidate_candidate_reviews_unlocked(item)
            self._conn.execute("UPDATE work_items SET state=?,review_note=?,lease_until=NULL,updated_at=? WHERE id=?",
                               (state, note, time.time(), task_id))
            if state != "escalated":
                self._conn.execute("DELETE FROM reservations WHERE task_id=?", (task_id,))
            self._task_event_unlocked(task_id, agent_id, state, attempt, note)
            self._notify_task(sender=agent_id, recipient=item["assigned_to"], kind="decision",
                      body=note, task_id=task_id, payload={"decision": decision, "attempt": attempt}, attempt=attempt)
            return self._task_unlocked(task_id)

    def recover_task(self, agent_id: str, task_id: str, attempt: int, note: str) -> dict:
        """Requeue reconciled work after a stopped worker; never terminates processes."""
        positive_int(attempt, "attempt")
        text(note, "recovery note")
        with self._transaction():
            self._require_agent(agent_id)
            item = self._task_unlocked(task_id)
            if item["created_by"] != agent_id or item["attempt"] != attempt or item["state"] != "running":
                raise BoardError("only the lead may recover the current running attempt")
            active = self._conn.execute("SELECT native_handle FROM agent_readiness WHERE agent_id=? AND state IN ('ready','started','observed')",
                                        (item["assigned_to"],)).fetchone()
            if active:
                raise BoardError("reconcile and stop the observed native worker before requeuing its task")
            if self._conn.execute("SELECT 1 FROM runtime_jobs WHERE task_id=? AND state IN ('requested','claimed','dispatched','started','working','waiting-input','uncertain')", (task_id,)).fetchone():
                raise BoardError("reconcile the runtime dispatch before requeuing, even when its handle is uncertain")
            self._conn.execute("UPDATE work_items SET state='queued',review_note=?,lease_until=NULL,updated_at=? WHERE id=?",
                               (note, time.time(), task_id))
            self._conn.execute("DELETE FROM reservations WHERE task_id=?", (task_id,))
            self._task_event_unlocked(task_id, agent_id, "queued", attempt, "recovery: " + note)
            self._notify_task(sender=agent_id, recipient=item["assigned_to"], kind="decision", body=note,
                    task_id=task_id, priority="urgent", payload={"decision": "recover", "attempt": attempt}, attempt=attempt)
            return self._task_unlocked(task_id)

    def task_history(self, task_id: str) -> list[dict]:
        with self._lock:
            self._task_unlocked(task_id)
            return [dict(row) for row in self._conn.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
            )]

    def amend_task(self, agent_id: str, task_id: str, expected_digest: str, changes: dict, note: str) -> dict:
        """Versioned scope negotiation for queued work; selected ownership stays fixed."""
        text(note, "amendment note")
        allowed = {"goal", "reads", "writes", "checks", "constraints", "success_criteria"}
        if not isinstance(changes, dict) or not changes or set(changes) - allowed:
            raise BoardError("amend only goal, scopes, checks, constraints or success criteria")
        with self._transaction():
            item = self._task_unlocked(task_id)
            if item["created_by"] != agent_id or item["state"] != "queued":
                raise BoardError("only the selected lead may amend queued work")
            if envelope_fingerprint(brief=item["brief"]) != expected_digest:
                raise BoardError("stale task brief digest")
            parsed = TaskBrief.from_dict({**item["brief"], **changes})
            jobs = {job["id"]: job for job in self._plan_tasks_unlocked(task_id)}
            jobs.setdefault(task_id, item)
            def ancestors(identifier):
                result, pending = set(), list(jobs[identifier]["brief"]["dependencies"])
                while pending:
                    parent = pending.pop()
                    if parent not in result:
                        result.add(parent)
                        pending.extend(jobs[parent]["brief"]["dependencies"])
                return result
            for other in jobs.values():
                if other["id"] == task_id or other["id"] in ancestors(task_id) or task_id in ancestors(other["id"]):
                    continue
                if any(overlaps(collision_key(parsed.workspace, a), collision_key(other["brief"]["workspace"], b)) for a in parsed.writes for b in other["brief"]["writes"]):
                    raise BoardError("scope amendment overlaps an unordered writer: " + other["id"])
            self._conn.execute("UPDATE work_items SET brief=?,review_note=?,updated_at=? WHERE id=?",
                               (json_object(parsed.to_dict(), "brief"), note, time.time(), task_id))
            self._task_event_unlocked(task_id, agent_id, "queued", item["attempt"], "amendment: " + note)
            plan = self._plan_for_task_unlocked(task_id)
            if plan is not None:
                plan = {**plan, "jobs": [parsed.to_dict() if job["id"] == task_id else job for job in plan["jobs"]]}
                self._publish_team_plan_unlocked(agent_id, plan)
            self._notify_task(sender=agent_id, recipient=parsed.assigned_to, kind="decision", body=note, task_id=task_id,
                payload={"decision": "amend", "brief": parsed.to_dict()}, attempt=item["attempt"],
                idempotency_key="amend:" + task_id + ":" + envelope_fingerprint(brief=parsed.to_dict()))
            return {**self._task_unlocked(task_id), "brief_digest": envelope_fingerprint(brief=parsed.to_dict())}
