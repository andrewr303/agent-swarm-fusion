"""Versioned communication contracts and bounded durable delivery.

An effect receipt deduplicates a database record, not arbitrary external side
effects. Native observations are cooperative facts, not authentication.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .mailbox import (BoardError, default_mail_max_attempts, envelope_fingerprint, json_object,
                      positive_int, positive_number)

PURPOSES = ("assignment", "handoff", "question", "answer", "decision", "blocker", "finding", "progress", "stop")
READINESS = ("registered", "ready", "started", "observed", "stopped", "error")
PROTOCOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_protocol (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    run_id TEXT NOT NULL, trace_id TEXT NOT NULL, envelope TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS protocol_trace ON message_protocol(run_id, trace_id, message_id);
CREATE TABLE IF NOT EXISTS delivery_policies (
    message_id INTEGER NOT NULL REFERENCES messages(id), agent_id TEXT NOT NULL REFERENCES agents(id),
    max_attempts INTEGER NOT NULL, deadline REAL, PRIMARY KEY(message_id,agent_id)
);
CREATE INDEX IF NOT EXISTS delivery_policies_msg ON delivery_policies(message_id, agent_id);
CREATE INDEX IF NOT EXISTS delivery_policies_deadline ON delivery_policies(deadline) WHERE deadline IS NOT NULL;
CREATE TABLE IF NOT EXISTS delivery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL REFERENCES messages(id),
    agent_id TEXT NOT NULL REFERENCES agents(id), state TEXT NOT NULL, attempts INTEGER NOT NULL,
    reason TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS message_effects (
    agent_id TEXT NOT NULL REFERENCES agents(id), run_id TEXT NOT NULL, effect_key TEXT NOT NULL,
    message_id INTEGER NOT NULL REFERENCES messages(id), fingerprint TEXT NOT NULL,
    result TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(agent_id,run_id,effect_key)
);
CREATE TABLE IF NOT EXISTS agent_readiness (
    agent_id TEXT NOT NULL REFERENCES agents(id), run_id TEXT NOT NULL, state TEXT NOT NULL,
    native_handle TEXT NOT NULL, model TEXT NOT NULL, version INTEGER NOT NULL,
    observed_at REAL NOT NULL, PRIMARY KEY(agent_id,run_id)
);
"""

# Only rows that can expire are selected (B12): expired leases via deliveries_lease, deadlines via the
# partial delivery_policies_deadline index. Healthy leased rows are never loaded. Parameters:
# (default_max, now, now, now, now, default_max).
EXPIRY_SQL = """
SELECT d.*, COALESCE(p.max_attempts, ?) AS max_attempts, p.deadline AS deadline
FROM deliveries d LEFT JOIN delivery_policies p ON p.message_id=d.message_id AND p.agent_id=d.agent_id
WHERE d.state IN ('pending','leased') AND d.rowid IN (
    SELECT rowid FROM deliveries WHERE state='leased' AND lease_until<=?
    UNION
    SELECT d2.rowid FROM delivery_policies p2
    JOIN deliveries d2 ON d2.message_id=p2.message_id AND d2.agent_id=p2.agent_id
    WHERE p2.deadline IS NOT NULL AND p2.deadline<=? AND d2.state IN ('pending','leased'))
AND ((p.deadline IS NOT NULL AND p.deadline<=?)
     OR (d.state='leased' AND d.lease_until<=? AND d.attempts>=COALESCE(p.max_attempts, ?)))
"""
_MAIL_INDEXES = (
    "CREATE INDEX IF NOT EXISTS deliveries_lease ON deliveries(state, lease_until)",
    "CREATE INDEX IF NOT EXISTS delivery_policies_msg ON delivery_policies(message_id, agent_id)",
    "CREATE INDEX IF NOT EXISTS delivery_policies_deadline ON delivery_policies(deadline) WHERE deadline IS NOT NULL",
)


def nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise BoardError(f"{field} must be a non-empty string")
    return value


def message_envelope(raw: Any) -> dict:
    fields = {"schema_version", "run_id", "task_id", "attempt", "trace_id", "reply_to", "purpose",
              "summary", "artifacts", "requested_action", "deadline", "max_attempts", "progress_key"}
    if not isinstance(raw, dict) or set(raw) - fields:
        raise BoardError("envelope must contain only supported versioned fields")
    if raw.get("schema_version", 1) != 1 or isinstance(raw.get("schema_version"), bool):
        raise BoardError("unsupported envelope schema_version")
    result = {"schema_version": 1}
    for key in ("run_id", "trace_id", "summary"):
        result[key] = nonempty(raw.get(key), key)
    if len(result["summary"]) > 4000:
        raise BoardError("summary must be concise (at most 4000 characters)")
    result["purpose"] = raw.get("purpose")
    if result["purpose"] not in PURPOSES:
        raise BoardError(f"purpose must be one of {PURPOSES}")
    result["task_id"] = raw.get("task_id", "")
    if not isinstance(result["task_id"], str):
        raise BoardError("task_id must be a string")
    result["attempt"] = raw.get("attempt", 0)
    if isinstance(result["attempt"], bool) or not isinstance(result["attempt"], int) or result["attempt"] < 0:
        raise BoardError("attempt must be a nonnegative integer")
    result["reply_to"] = raw.get("reply_to")
    if result["reply_to"] is not None:
        positive_int(result["reply_to"], "reply_to")
    if result["purpose"] == "answer" and result["reply_to"] is None:
        raise BoardError("an answer needs reply_to")
    action = raw.get("requested_action", "")
    if not isinstance(action, str) or (result["purpose"] != "progress" and not action.strip()):
        raise BoardError("requested_action is required except for progress")
    result["requested_action"] = action
    artifacts = raw.get("artifacts", [])
    if not isinstance(artifacts, list) or any(not isinstance(item, dict) for item in artifacts):
        raise BoardError("artifacts must be a list of reference objects")
    json_object({"artifacts": artifacts}, "artifacts")
    result["artifacts"] = artifacts
    result["max_attempts"] = positive_int(raw.get("max_attempts", 3), "max_attempts")
    if result["max_attempts"] > 20:
        raise BoardError("max_attempts cannot exceed 20")
    result["deadline"] = raw.get("deadline")
    if result["deadline"] is not None:
        result["deadline"] = positive_number(result["deadline"], "deadline")
    result["progress_key"] = raw.get("progress_key", result["task_id"])
    if not isinstance(result["progress_key"], str):
        raise BoardError("progress_key must be a string")
    return result


class ProtocolMixin:
    def _check_protocol_attempt_unlocked(self, envelope: dict, sender: str | None = None,
                                         *, held: bool = False) -> None:
        """Reject mail for a stale or closed task attempt. `held=True` (consume_delivery, B2) is for a
        delivery the caller already holds under a live lease: the attempt must still be the task's
        current attempt, but an accepted/cancelled attempt is no longer refused (guard is for send)."""
        if envelope["task_id"]:
            task = self._task_unlocked(envelope["task_id"])
            expected = task["attempt"] + (1 if task["state"] == "queued" else 0)
            control = envelope["purpose"] in ("decision", "stop") and sender == task["created_by"]
            if control and envelope["attempt"] == task["attempt"]:
                return
            if held and task["state"] in ("accepted", "cancelled") and envelope["attempt"] == task["attempt"]:
                return
            if task["state"] in ("accepted", "cancelled") or envelope["attempt"] != expected:
                raise BoardError("message belongs to a stale or closed task attempt")

    def _run_plan_unlocked(self, run_id: str, mismatch: str) -> dict | None:
        """The team plan of `run_id` (per-run key, B5), or None when the board has no plans at all.
        A board that holds only other runs' plans rejects this run_id."""
        marker = self._team_plan_marker_unlocked(run_id)
        if marker["found"]:
            return marker["value"] if isinstance(marker["value"], dict) else None
        if self._other_run_ids_unlocked(run_id):
            raise BoardError(mismatch)
        return None

    def _ensure_mail_indexes_unlocked(self) -> None:
        """Databases opened at an older schema version skip executescript: add the mail indexes once."""
        if getattr(self, "_mail_indexes_ready", False):
            return
        present = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        if not {"deliveries_lease", "delivery_policies_msg", "delivery_policies_deadline"} <= present:
            for statement in _MAIL_INDEXES:
                self._conn.execute(statement)
        self._mail_indexes_ready = True

    def send_message(self, from_agent: str, to_agent: str, envelope: dict,
                     idempotency_key: str, priority: str = "normal", payload: dict | None = None) -> dict:
        parsed = message_envelope(envelope)
        nonempty(idempotency_key, "idempotency_key")
        with self._transaction():
            self._require_agent(from_agent)
            self._require_agent(to_agent)
            plan = self._run_plan_unlocked(parsed["run_id"], "envelope run_id differs from this team's run")
            if plan is not None:
                peers = plan.get("coordination_peers") or {}
                if from_agent != plan.get("lead") and to_agent not in peers.get(from_agent, []):
                    raise BoardError("recipient is outside this worker's permitted coordination peers")
            self._check_protocol_attempt_unlocked(parsed, from_agent)
            if parsed["reply_to"] is not None:
                parent = self._message_unlocked(parsed["reply_to"])
                if parent is None or not self._visible_unlocked(from_agent, parent):
                    raise BoardError("reply target is absent or not visible")
                metadata = self._conn.execute("SELECT envelope FROM message_protocol WHERE message_id=?",
                                              (parsed["reply_to"],)).fetchone()
                previous = json.loads(metadata[0]) if metadata else None
                if not previous or any(previous[key] != parsed[key] for key in ("run_id", "trace_id", "task_id", "attempt")):
                    raise BoardError("reply must preserve run, trace and task attempt")
                if parsed["purpose"] == "answer" and previous["purpose"] != "question":
                    raise BoardError("an answer must refer to a question")
            kind = {"assignment": "task", "progress": "message", "question": "message", "answer": "message",
                    "stop": "decision"}.get(parsed["purpose"], parsed["purpose"])
            data = {} if payload is None else json.loads(json_object(payload, "payload"))
            if "envelope" in data:
                raise BoardError("payload cannot override the communication envelope")
            data["envelope"] = parsed
            record = self.dm(from_agent, to_agent, parsed["summary"], kind=kind,
                task_id=parsed["task_id"], priority=priority, payload=data,
                idempotency_key=idempotency_key)
            if self._conn.execute("SELECT 1 FROM message_protocol WHERE message_id=?", (record["id"],)).fetchone():
                return self._envelope_dict(self._conn.execute("SELECT * FROM messages WHERE id=?", (record["id"],)).fetchone())
            self._conn.execute("INSERT INTO message_protocol VALUES (?,?,?,?)",
                               (record["id"], parsed["run_id"], parsed["trace_id"], json_object(parsed, "envelope")))
            self._conn.execute("INSERT INTO delivery_policies VALUES (?,?,?,?)",
                               (record["id"], to_agent, parsed["max_attempts"], parsed["deadline"]))
            if parsed["purpose"] == "progress":
                rows = self._conn.execute("SELECT m.id,p.envelope FROM messages m JOIN message_protocol p ON p.message_id=m.id "
                    "JOIN deliveries d ON d.message_id=m.id WHERE m.from_agent=? AND d.agent_id=? AND d.state='pending' AND m.id<?",
                    (from_agent, to_agent, record["id"])).fetchall()
                for row in rows:
                    older = json.loads(row["envelope"])
                    if older["purpose"] == "progress" and all(older[key] == parsed[key] for key in ("run_id", "task_id", "attempt", "progress_key")):
                        self._conn.execute("UPDATE deliveries SET state='superseded' WHERE message_id=? AND agent_id=?",
                                           (row["id"], to_agent))
            self._expire_protocol_deliveries_unlocked(time.time())
            return self._envelope_dict(self._conn.execute("SELECT * FROM messages WHERE id=?", (record["id"],)).fetchone())

    def _delivery_event_unlocked(self, receipt: dict, state: str, reason: str) -> None:
        self._conn.execute("INSERT INTO delivery_events(message_id,agent_id,state,attempts,reason,created_at) VALUES (?,?,?,?,?,?)",
            (receipt["message_id"], receipt["agent_id"], state, receipt["attempts"], reason, time.time()))

    def _expire_protocol_deliveries_unlocked(self, now: float) -> None:
        self._ensure_mail_indexes_unlocked()
        default = default_mail_max_attempts()
        rows = self._conn.execute(EXPIRY_SQL, (default, now, now, now, now, default)).fetchall()
        for row in rows:
            expired = row["deadline"] is not None and row["deadline"] <= now
            self._conn.execute("UPDATE deliveries SET state='dead',lease_until=NULL WHERE message_id=? AND agent_id=?",
                               (row["message_id"], row["agent_id"]))
            self._delivery_event_unlocked(dict(row), "dead", "deadline expired" if expired else "max_attempts")

    def _retry_state_unlocked(self, receipt: Any, reason: str) -> str:
        limit = self._max_attempts_unlocked(receipt["message_id"], receipt["agent_id"])
        state = "dead" if receipt["attempts"] >= limit else "pending"
        self._delivery_event_unlocked(dict(receipt), state, reason)
        return state

    def dead_letters(self, agent_id: str) -> list[dict]:
        with self._transaction():
            self._require_agent(agent_id)
            self._expire_protocol_deliveries_unlocked(time.time())
            rows = self._conn.execute("SELECT m.*,d.agent_id AS recipient FROM messages m JOIN deliveries d ON d.message_id=m.id "
                                      "WHERE d.agent_id=? AND d.state='dead' ORDER BY m.id", (agent_id,)).fetchall()
            return [self._delivery_dict(row) for row in rows]

    def retry_delivery(self, agent_id: str, message_id: int, extra_attempts: int = 1,
                       deadline: float | None = None) -> dict:
        positive_int(message_id, "message_id")
        positive_int(extra_attempts, "extra_attempts")
        if extra_attempts > 20:
            raise BoardError("extra_attempts cannot exceed 20")
        if deadline is not None:
            positive_number(deadline, "deadline")
            if deadline <= time.time():
                raise BoardError("new deadline must be in the future")
        with self._transaction():
            receipt = self._check_receipt_unlocked(agent_id, message_id, None, allow_dead=True)
            if not receipt or receipt["state"] != "dead":
                raise BoardError("only the recipient may explicitly retry its dead delivery")
            policy = self._conn.execute("SELECT * FROM delivery_policies WHERE message_id=? AND agent_id=?",
                                       (message_id, agent_id)).fetchone()
            effective = deadline if deadline is not None else policy["deadline"]
            if effective is not None and effective <= time.time():
                raise BoardError("an expired delivery requires an explicit future deadline")
            self._conn.execute("UPDATE delivery_policies SET max_attempts=?,deadline=? WHERE message_id=? AND agent_id=?",
                               (receipt["attempts"] + extra_attempts, effective, message_id, agent_id))
            self._conn.execute("UPDATE deliveries SET state='pending',lease_token=NULL,lease_until=NULL WHERE message_id=? AND agent_id=?",
                               (message_id, agent_id))
            self._delivery_event_unlocked(dict(receipt), "pending", "explicit delivery retry")
        return {"message_id": message_id, "state": "pending"}

    def consume_delivery(self, agent_id: str, message_id: int, lease_token: str, effect_key: str, result: dict) -> dict:
        nonempty(effect_key, "effect_key")
        encoded = json_object(result, "effect result")
        with self._transaction():
            receipt = self._check_receipt_unlocked(agent_id, message_id, lease_token)
            if not receipt or receipt["state"] not in ("leased", "acked"):
                raise BoardError("consume requires a current delivery lease")
            row = self._conn.execute("SELECT envelope FROM message_protocol WHERE message_id=?", (message_id,)).fetchone()
            if row is None:
                raise BoardError("consume requires a versioned envelope")
            envelope = json.loads(row[0])
            fingerprint = envelope_fingerprint(envelope=envelope, result=result)
            prior = self._conn.execute("SELECT * FROM message_effects WHERE agent_id=? AND run_id=? AND effect_key=?",
                                      (agent_id, envelope["run_id"], effect_key)).fetchone()
            if prior and prior["fingerprint"] != fingerprint:
                raise BoardError("effect key reused for different content")
            if not prior:
                self._check_protocol_attempt_unlocked(envelope, self._message_unlocked(message_id)["from_agent"], held=True)
                self._conn.execute("INSERT INTO message_effects VALUES (?,?,?,?,?,?,?)",
                    (agent_id, envelope["run_id"], effect_key, message_id, fingerprint, encoded, time.time()))
            self.ack(agent_id, message_id, lease_token)
            return {"message_id": message_id, "effect_key": effect_key, "duplicate": bool(prior), "result": json.loads(encoded)}

    def trace(self, agent_id: str, run_id: str, trace_id: str | None = None, limit: int = 200) -> dict:
        nonempty(run_id, "run_id")
        positive_int(limit, "limit")
        with self._lock:
            self._require_agent(agent_id)
            # Newest first with the bound applied in SQL (B16); visibility is checked per row, so page
            # by id until limit+1 visible messages are found or the trace is exhausted.
            visible: list[dict] = []
            before = None
            while len(visible) <= limit:
                rows = self._conn.execute("SELECT m.* FROM messages m JOIN message_protocol p ON p.message_id=m.id "
                    "WHERE p.run_id=? AND (? IS NULL OR p.trace_id=?) AND (? IS NULL OR m.id<?) ORDER BY m.id DESC LIMIT ?",
                    (run_id, trace_id, trace_id, before, before, limit + 1)).fetchall()
                for row in rows:
                    if self._visible_unlocked(agent_id, self._message_dict(row)):
                        visible.append(self._envelope_dict(row))
                        if len(visible) > limit:
                            break
                if len(rows) < limit + 1:
                    break
                before = rows[-1]["id"]
            truncated = len(visible) > limit
            messages = list(reversed(visible[:limit]))
            for message in messages:
                message["receipts"] = [dict(row) for row in self._conn.execute("SELECT * FROM deliveries WHERE message_id=?", (message["id"],))
                                       if row["agent_id"] == agent_id or message["from_agent"] == agent_id]
                for receipt in message["receipts"]:
                    if receipt["agent_id"] != agent_id:
                        receipt.pop("lease_token", None)  # a sender never sees recipients' lease tokens (B9a)
            answers = {m["envelope"]["reply_to"] for m in messages if m["envelope"]["purpose"] == "answer"}
            unanswered = [m["id"] for m in messages if m["envelope"]["purpose"] == "question" and m["id"] not in answers]
            return {"messages": messages, "unanswered_questions": unanswered, "truncated": truncated}

    def readiness(self, agent_id: str, run_id: str, state: str, native_handle: str,
                  model: str, expected_version: int) -> dict:
        nonempty(run_id, "run_id")
        if state not in READINESS:
            raise BoardError(f"state must be one of {READINESS}")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise BoardError("expected_version must be a nonnegative integer")
        if state in ("ready", "started", "observed"):
            nonempty(native_handle, "native_handle")
        if not isinstance(native_handle, str):
            raise BoardError("native_handle must be a string")
        with self._transaction():
            self._require_agent(agent_id)
            self._run_plan_unlocked(run_id, "readiness run_id differs from this team's run")
            if self._agent_unlocked(agent_id)["model"] != model:
                raise BoardError("observed model differs from the selected roster")
            prior = self._conn.execute("SELECT * FROM agent_readiness WHERE agent_id=? AND run_id=?", (agent_id, run_id)).fetchone()
            if (prior["version"] if prior else 0) != expected_version:
                raise BoardError("stale readiness version")
            if prior and prior["state"] in ("ready", "started", "observed") and prior["native_handle"] != native_handle:
                raise BoardError("reconcile the existing native handle before replacing it")
            self._conn.execute("INSERT INTO agent_readiness VALUES (?,?,?,?,?,?,?) ON CONFLICT(agent_id,run_id) "
                "DO UPDATE SET state=excluded.state,native_handle=excluded.native_handle,model=excluded.model,version=excluded.version,observed_at=excluded.observed_at",
                (agent_id, run_id, state, native_handle, model, expected_version + 1, time.time()))
            return dict(self._conn.execute("SELECT * FROM agent_readiness WHERE agent_id=? AND run_id=?", (agent_id, run_id)).fetchone())
