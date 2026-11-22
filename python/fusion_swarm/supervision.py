"""Persisted operator views and evidence exports, with no hidden model probes."""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from .host_runtime import CapabilityUnavailable, executable_argv
from .mailbox import BoardError


_SECRET_KEY = re.compile(r"(?i)(^|[_-])(token|secret|password|passwd|api[_-]?key|authorization|credential|cookie|lease_token|claim_token)$")
_USAGE_KEY = re.compile(r"(?i)(usage|totals?)")


def _secret_key(key) -> bool:
    key = str(key)
    if re.search(r"(?i)tokens$", key):
        return False  # usage counts such as input_tokens / total_tokens are not secrets (B8)
    return bool(_SECRET_KEY.search(key))


def redact(value, _usage: bool = False):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            in_usage = _usage or bool(_USAGE_KEY.search(str(key)))
            if isinstance(item, (int, float)) and not isinstance(item, bool) and in_usage:
                out[key] = item
            else:
                out[key] = "[redacted]" if _secret_key(key) else redact(item, in_usage)
        return out
    if isinstance(value, list):
        return [redact(item, _usage) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(bearer\s+)[\w.\-]+", r"\1[redacted]", value)
        return re.sub(r"\b(?:sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{20,})", "[redacted]", value)
    return value


def status(board, agent_id: str, run_id: str) -> dict:
    run = board.run_status(agent_id, run_id)
    with board._lock:
        plan = board._plan_unlocked(run_id)
        readiness = [dict(row) for row in board._conn.execute("SELECT * FROM agent_readiness WHERE run_id=? ORDER BY agent_id", (run_id,))]
        job_ids = sorted({str(job["id"]) for job in plan["jobs"]})
        marks = ",".join("?" for _ in job_ids) or "NULL"
        # Scoped to this run's plan jobs (B16): other runs on a shared board are not mixed in.
        mail = [dict(row) for row in board._conn.execute(
            "SELECT r.agent_id,r.state,COUNT(*) AS count FROM deliveries r JOIN message_envelopes e ON e.message_id=r.message_id "
            f"WHERE r.state NOT IN ('acked','superseded') AND e.task_id IN ({marks}) GROUP BY r.agent_id,r.state ORDER BY r.agent_id,r.state", job_ids)]
        candidate_reviews = [dict(row) for row in board._conn.execute(
            f"SELECT task_id,attempt,reviewer,candidate_of,digest,stage,verdict,created_at FROM candidate_reviews WHERE task_id IN ({marks}) ORDER BY created_at", job_ids)]
    next_actions = []
    if run["state"] == "paused":
        next_actions.append({"operation": "control_run", "reason": "Resume dispatch when ready", "expected_version": run["version"]})
    if run["state"] in ("blocked", "needs_input"):
        next_actions.append({"operation": "refresh_capabilities" if run["state"] == "blocked" else "control_run", "reason": run["reason"]})
    for ticket in run["runtimes"]:
        if ticket["state"] in ("claimed", "uncertain"):
            next_actions.append({"operation": "reconcile_runtime", "ticket_id": ticket["ticket_id"], "reason": "Inspect the original worker identity and changes before recovery"})
        elif ticket["state"] == "stopped" and not ticket["observed_model"]:
            next_actions.append({"operation": "reconcile_runtime", "ticket_id": ticket["ticket_id"], "reason": "Selected model identity was not observed; result cannot be accepted"})
    for item in run["tasks"]:
        if item["state"] == "review":
            next_actions.append({"operation": "review_task", "task_id": item["id"], "attempt": item["attempt"], "reason": "Inspect the current artifact, review findings and reported checks"})
    if not next_actions and run["observed_state"] == "ready":
        next_actions.append({"operation": "dispatch_actions", "reason": "Persist the next eligible native dispatch"})
    return redact({"schema_version": 1, "run": run, "readiness": readiness, "mail_backlog": mail,
        "dependencies": [{"id": job["id"], "depends_on": job["dependencies"], "candidate_of": job.get("candidate_of", ""), "owner": job["assigned_to"]} for job in plan["jobs"]],
        "ownership": board.reservations(), "candidate_reviews": candidate_reviews, "next_actions": next_actions,
        "history": board.run_history(agent_id, run_id), "observed_at": time.time()})


def render(view: dict) -> str:
    run = view["run"]
    lines = [f"Fusion {run['id']} | {run['observed_state']} | host={run['host']} | version={run['version']}",
             f"dispatches={run['dispatches']}/{run['bounds']['max_dispatches']} | provider usage: " + ("unavailable" if run["provider_usage"] is None else json.dumps(run["provider_usage"]))]
    for job in run["tasks"]:
        lines.append(f"  {job['id']}: {job['state']} (attempt {job['attempt']})")
    for ticket in run["runtimes"]:
        lines.append(f"  runtime {ticket['ticket_id'][:8]}: {ticket['state']} | {ticket['native_handle'] or 'handle uncertain'} | model={ticket['observed_model'] or 'unobserved'}")
    for action in view["next_actions"]:
        lines.append(f"  next: {action['operation']} — {action['reason']}")
    return "\n".join(lines)


def export(board, agent_id: str, run_id: str) -> dict:
    view = status(board, agent_id, run_id)
    return redact({**view, "tasks": [item for item in board.tasks() if item["id"] in {job["id"] for job in view["dependencies"]}], "communication": board.trace(agent_id, run_id),
                   "verification_boundary": "worker-reported checks plus persisted host/scope observations; reviewed acceptance is explicit"})


def doctor(host: str, executable: str | None = None, capabilities: dict | None = None) -> dict:
    facts = {name: {"state": "unverified", "evidence": None} for name in ("installation", "discovery", "mcp_loading", "dispatch", "messaging", "wait", "cancellation", "recovery")}
    try:
        if not executable and host in ("codex", "andrewcode"):
            from .andrewcode_cli import ENTRY
            if not ENTRY.is_file():
                raise CapabilityUnavailable("AndrewCode Node entry is missing")
            command = executable_argv("node") + [str(ENTRY)]
        else:
            command = executable_argv(executable or {"claude": "claude"}.get(host, host))
        result = subprocess.run(command + ["--version"], capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if __import__("os").name == "nt" else 0)
        facts["installation"] = {"state": "observed" if result.returncode == 0 else "failed", "evidence": (result.stdout or result.stderr).strip()[:500], "exit_code": result.returncode}
    except (CapabilityUnavailable, OSError, subprocess.TimeoutExpired) as exc:
        facts["installation"] = {"state": "unavailable", "evidence": str(exc)}
    if capabilities and capabilities.get("host") == host:
        facts["discovery"] = {"state": "declared-live" if capabilities.get("source") == "live-runtime" else "declared", "evidence": capabilities.get("source"), "models": capabilities.get("models", [])}
        mapping = {"dispatch": "dispatch", "messaging": "message", "wait": "wait", "cancellation": "cancel", "recovery": "recover"}
        for fact, operation in mapping.items():
            facts[fact] = {"state": "supported-unexercised" if operation in capabilities.get("operations", []) else "unavailable", "evidence": "supplied runtime contract"}
    return {"host": host, "capabilities": facts, "provider_calls": 0, "boundary": "installation and declared capabilities do not prove authenticated execution"}
