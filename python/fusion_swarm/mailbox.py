"""Durable mailbox semantics on the Hive Board's existing SQLite connection.

The database is authoritative. Delivery is at least once; claim tokens fence
late consumers, and sender keys make producer retries idempotent. No daemon.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from contextlib import contextmanager
from typing import Any


class BoardError(ValueError):
    """A rejected coordination operation."""


PRIORITIES = {"low": 0, "normal": 1, "urgent": 2}
MAILBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_envelopes (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    sender TEXT NOT NULL, idempotency_key TEXT,
    fingerprint TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL DEFAULT '{}',
    UNIQUE(sender, idempotency_key)
);
CREATE TABLE IF NOT EXISTS deliveries (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT, lease_until REAL,
    read_at REAL, acked_at REAL,
    PRIMARY KEY(message_id, agent_id)
);
CREATE INDEX IF NOT EXISTS deliveries_inbox ON deliveries(agent_id, state, message_id);
CREATE INDEX IF NOT EXISTS deliveries_lease ON deliveries(state, lease_until);
"""

DEFAULT_MAIL_MAX_ATTEMPTS = 8


def default_mail_max_attempts() -> int:
    """Redelivery budget for mail without an explicit policy (FUSION_MAIL_MAX_ATTEMPTS, default 8)."""
    raw = os.environ.get("FUSION_MAIL_MAX_ATTEMPTS", "")
    try:
        value = int(raw) if raw.strip() else DEFAULT_MAIL_MAX_ATTEMPTS
    except ValueError:
        return DEFAULT_MAIL_MAX_ATTEMPTS
    return value if value >= 1 else DEFAULT_MAIL_MAX_ATTEMPTS


def positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BoardError(f"{label} must be a positive finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value <= 0:
        raise BoardError(f"{label} must be a positive finite number")
    return float(value)


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BoardError(f"{label} must be a positive integer")
    return value


def json_object(value: Any, label: str) -> str:
    if not isinstance(value, dict):
        raise BoardError(f"{label} must be an object")
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise BoardError(f"{label} must contain JSON values") from exc


def envelope_fingerprint(**fields: Any) -> str:
    return hashlib.sha256(json_object(fields, "envelope").encode("utf-8")).hexdigest()


class MailboxMixin:
    @contextmanager
    def _transaction(self):
        """Serialize writers across processes, with savepoints for compound operations."""
        with self._lock:
            nested = self._conn.in_transaction
            sidecar_start = len(self._pending_sidecars)
            name = "fusion_" + uuid.uuid4().hex
            self._conn.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
            try:
                yield
                if nested:
                    self._conn.execute(f"RELEASE SAVEPOINT {name}")
                else:
                    self._conn.commit()
            except BaseException:
                del self._pending_sidecars[sidecar_start:]
                if nested:
                    self._conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    self._conn.execute(f"RELEASE SAVEPOINT {name}")
                else:
                    self._conn.rollback()
                raise
            if not nested:
                pending, self._pending_sidecars = self._pending_sidecars, []
                if pending:
                    batch_fn = getattr(self, "_append_jsonl_batch", None)
                    if len(pending) > 1 and callable(batch_fn):
                        try:
                            batch_fn(pending)
                        except OSError as exc:
                            for record in pending:
                                record["sidecar_error"] = str(exc)
                    else:
                        for record in pending:
                            try:
                                self._append_jsonl(record)
                            except OSError as exc:
                                record["sidecar_error"] = str(exc)
                wakeup_fn = getattr(self, "_touch_wakeup", None)
                if callable(wakeup_fn):
                    try:
                        wakeup_fn()
                    except OSError:
                        pass

    def _require_agent(self, agent_id: str) -> None:
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise BoardError("agent_id must be a non-empty string")
        if self._agent_unlocked(agent_id) is None:
            raise BoardError(f"unknown agent: {agent_id}")

    def _visible_unlocked(self, agent_id: str, message: dict) -> bool:
        channel = self._channel_unlocked(message["channel"])
        if channel["kind"] == "dm":
            return agent_id in (message["from_agent"], message.get("to_agent"))
        joined = self._conn.execute(
            "SELECT 1 FROM channel_members WHERE channel=? AND agent_id=?",
            (message["channel"], agent_id),
        ).fetchone()
        return bool(joined or message.get("to_agent") == agent_id
                    or agent_id in message.get("mentions", []))

    def _route_unlocked(self, message: dict) -> None:
        channel = self._channel_unlocked(message["channel"])
        if channel["kind"] == "dm":
            recipients = {message.get("to_agent")}
        else:
            recipients = {r[0] for r in self._conn.execute(
                "SELECT agent_id FROM channel_members WHERE channel=?", (message["channel"],)
            )}
            recipients.update(message.get("mentions", []))
            if message.get("to_agent"):
                recipients.add(message["to_agent"])
        recipients.discard(None)
        recipients.discard(message["from_agent"])
        self._conn.executemany(
            "INSERT OR IGNORE INTO deliveries(message_id, agent_id) VALUES (?, ?)",
            [(message["id"], recipient) for recipient in sorted(recipients)],
        )

    def _envelope_dict(self, row) -> dict:
        message = self._message_dict(row)
        metadata = self._conn.execute(
            "SELECT * FROM message_envelopes WHERE message_id=?", (message["id"],)
        ).fetchone()
        if metadata:
            message.update(task_id=metadata["task_id"],
                           priority=next(k for k, v in PRIORITIES.items() if v == metadata["priority"]),
                           payload=json.loads(metadata["payload"]),
                           idempotency_key=metadata["idempotency_key"])
        protocol = self._conn.execute("SELECT envelope FROM message_protocol WHERE message_id=?", (message["id"],)).fetchone()
        if protocol:
            message["envelope"] = json.loads(protocol[0])
        return message

    def _delivery_dict(self, row) -> dict:
        message = self._envelope_dict(row)
        receipt = self._conn.execute(
            "SELECT * FROM deliveries WHERE message_id=? AND agent_id=?",
            (message["id"], row["recipient"]),
        ).fetchone()
        message["delivery"] = dict(receipt)
        message["delivery"]["redelivery"] = receipt["attempts"] > 1
        return message

    def inbox(self, agent_id: str, limit: int = 100, task_id: str | None = None) -> list[dict]:
        """Inspect outstanding mail and record a read receipt, without acknowledging it."""
        positive_int(limit, "limit")
        with self._transaction():
            self._require_agent(agent_id)
            self._expire_protocol_deliveries_unlocked(time.time())
            rows = self._conn.execute(
                """SELECT m.*, d.agent_id AS recipient FROM deliveries d
                JOIN messages m ON m.id=d.message_id
                LEFT JOIN message_envelopes e ON e.message_id=m.id
                WHERE d.agent_id=? AND d.state IN ('pending','leased') AND (? IS NULL OR e.task_id=?)
                ORDER BY COALESCE(e.priority,1) DESC, m.id LIMIT ?""",
                (agent_id, task_id, task_id, limit),
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE deliveries SET read_at=COALESCE(read_at,?) WHERE message_id=? AND agent_id=?",
                    (time.time(), row["id"], agent_id),
                )
            return [self._delivery_dict(row) for row in rows]

    def claim(self, agent_id: str, limit: int = 1, lease_seconds: float = 60) -> list[dict]:
        """Atomically lease pending or expired deliveries to one consumer."""
        positive_int(limit, "limit")
        ttl = positive_number(lease_seconds, "lease_seconds")
        now = time.time()
        with self._transaction():
            self._require_agent(agent_id)
            self._expire_protocol_deliveries_unlocked(now)
            rows = self._conn.execute(
                """SELECT m.*, d.agent_id AS recipient, d.attempts AS delivery_attempts FROM deliveries d
                JOIN messages m ON m.id=d.message_id
                LEFT JOIN message_envelopes e ON e.message_id=m.id
                WHERE d.agent_id=? AND (d.state='pending' OR
                    (d.state='leased' AND d.lease_until<=?))
                ORDER BY COALESCE(e.priority,1) DESC, m.id LIMIT ?""",
                (agent_id, now, limit),
            ).fetchall()
            leased = []
            for row in rows:
                if row["delivery_attempts"] >= self._max_attempts_unlocked(row["id"], agent_id):
                    # Never re-lease past the retry budget (B6): dead-letter instead.
                    self._conn.execute(
                        "UPDATE deliveries SET state='dead',lease_until=NULL WHERE message_id=? AND agent_id=?",
                        (row["id"], agent_id))
                    self._delivery_event_unlocked(
                        {"message_id": row["id"], "agent_id": agent_id, "attempts": row["delivery_attempts"]},
                        "dead", "max_attempts")
                    continue
                self._conn.execute(
                    """UPDATE deliveries SET state='leased', attempts=attempts+1,
                    lease_token=?, lease_until=?, read_at=COALESCE(read_at,?)
                    WHERE message_id=? AND agent_id=?""",
                    (uuid.uuid4().hex, now + ttl, now, row["id"], agent_id),
                )
                leased.append(row)
            return [self._delivery_dict(row) for row in leased]

    def _max_attempts_unlocked(self, message_id: int, agent_id: str) -> int:
        policy = self._conn.execute(
            "SELECT max_attempts FROM delivery_policies WHERE message_id=? AND agent_id=?",
            (message_id, agent_id)).fetchone()
        return policy[0] if policy else default_mail_max_attempts()

    def _check_receipt_unlocked(self, agent_id: str, message_id: int, lease_token: str | None, *, allow_dead: bool = False):
        self._require_agent(agent_id)
        self._expire_protocol_deliveries_unlocked(time.time())
        positive_int(message_id, "message_id")
        row = self._conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if row is None or not self._visible_unlocked(agent_id, self._message_dict(row)):
            raise BoardError("message is absent or not visible to this agent")
        delivery = self._conn.execute(
            "SELECT * FROM deliveries WHERE agent_id=? AND message_id=?", (agent_id, message_id)
        ).fetchone()
        if delivery is None and row["from_agent"] == agent_id:
            raise BoardError("sender cannot acknowledge its own message")
        if delivery:
            if delivery["state"] in ("dead", "superseded") and not allow_dead:
                raise BoardError("delivery is dead or superseded; an explicit retry is required")
            if lease_token is not None and lease_token != delivery["lease_token"]:
                raise BoardError("stale delivery lease token")
            if delivery["state"] == "leased":
                if lease_token != delivery["lease_token"] or delivery["lease_until"] <= time.time():
                    raise BoardError("an active delivery lease token is required")
        return delivery

    def nack(self, agent_id: str, message_id: int, lease_token: str, reason: str = "consumer requested retry") -> dict:
        """Return owned mail for retry; never steal a newer consumer's lease."""
        with self._transaction():
            receipt = self._check_receipt_unlocked(agent_id, message_id, lease_token)
            if not receipt or receipt["state"] != "leased":
                raise BoardError("delivery is not leased")
            if not isinstance(reason, str) or not reason.strip():
                raise BoardError("retry reason must be a non-empty string")
            state = self._retry_state_unlocked(receipt, reason)
            self._conn.execute(
                "UPDATE deliveries SET state=?,lease_until=NULL WHERE message_id=? AND agent_id=?",
                (state, message_id, agent_id),
            )
        return {"message_id": message_id, "agent_id": agent_id, "state": state}

    def receipts(self, message_id: int, agent_id: str | None = None) -> list[dict]:
        """Delivery rows for a message. A lease token is only ever shown to its own recipient (B9a);
        without an actor no token is shown."""
        positive_int(message_id, "message_id")
        with self._lock:
            if agent_id is not None:
                self._require_agent(agent_id)
            if self._message_unlocked(message_id) is None:
                raise BoardError(f"unknown message: {message_id}")
            rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM deliveries WHERE message_id=? ORDER BY agent_id", (message_id,)
            )]
        for row in rows:
            if agent_id is None or row["agent_id"] != agent_id:
                row.pop("lease_token", None)
        return rows

    def stats(self) -> dict:
        with self._transaction():
            self._expire_protocol_deliveries_unlocked(time.time())
            counts = {r[0]: r[1] for r in self._conn.execute(
                "SELECT state, COUNT(*) FROM deliveries GROUP BY state"
            )}
            return {"messages": self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                    "deliveries": {state: counts.get(state, 0) for state in ("pending", "leased", "acked")},
                    "dead_letters": counts.get("dead", 0), "superseded": counts.get("superseded", 0),
                    "expired_leases": self._conn.execute(
                        "SELECT COUNT(*) FROM deliveries WHERE state='leased' AND lease_until<=?",
                        (time.time(),),
                    ).fetchone()[0]}
