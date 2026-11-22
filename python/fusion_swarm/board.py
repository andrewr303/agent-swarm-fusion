"""HiveBoard — file + SQLite WAL message bus for nested Fusion swarms.

Independent AndrewCode / CLI processes talk through one board. There is no
daemon: every process opens the same SQLite file (WAL so readers never block
writers) and a sibling JSONL file for greppability.

This is the Slack-channel + forum hybrid the architect/host uses internally.
It is not human-facing except via the architect. Default rooms on init:

    hive        everyone
    architect   architect seat
    captains    captains
    workers     workers / children
    system      system events

Stdlib only. Fail-closed: unknown channel kinds, missing agents, empty bodies,
and unknown CLI verbs are errors, never silently coerced.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import select
import sqlite3
import struct
import sys
import threading
import time
from datetime import datetime, timezone
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

try:  # POSIX only; the wakeup counter lock is a no-op on Windows
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None

from .mailbox import (BoardError, MAILBOX_SCHEMA, MailboxMixin, PRIORITIES,
                      envelope_fingerprint, json_object, positive_int, positive_number)
from .coordination import COORDINATION_SCHEMA, CoordinationMixin
from .protocol import PROTOCOL_SCHEMA, ProtocolMixin
from .execution import EXECUTION_SCHEMA, ExecutionMixin, initialize_execution
from .candidates import CANDIDATE_SCHEMA, CandidateMixin
from .knowledge import KNOWLEDGE_SCHEMA, KnowledgeMixin, initialize_fts

# Channel kinds the contract names. Fail closed on anything else.
CHANNEL_KINDS = ("room", "dm", "thread", "lineage", "task")
MESSAGE_KINDS = ("message", "system", "ack", "presence", "task", "handoff",
                 "finding", "blocker", "decision", "review")
DEFAULT_CHANNELS = ("hive", "architect", "captains", "workers", "system")
AGENT_ROLES = ("architect", "operator", "captain", "worker", "child")
AGENT_STATUSES = ("online", "offline", "busy", "idle", "error", "blocked", "done")
BOARD_SCHEMA_VERSION = 2
_BASE_TABLES = ("agents", "channels", "channel_members", "messages", "acks", "presence")

# Role -> default rooms an agent is auto-joined to on register.
_ROLE_ROOMS = {
    "architect": ("hive", "architect", "system"),
    "operator": ("hive", "architect", "captains", "system"),
    "captain": ("hive", "captains"),
    "worker": ("hive", "workers"),
    "child": ("hive", "workers"),
}

STIGMERGY_SCHEMA = """
CREATE TABLE IF NOT EXISTS stigmergy_traces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    locus       TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'hotspot',
    strength    REAL NOT NULL DEFAULT 1.0,
    half_life   REAL NOT NULL DEFAULT 600.0,
    note        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stigmergy_locus ON stigmergy_traces(locus, expires_at);
"""


def _read_wakeup_generation(wakeup_path: str) -> int:
    try:
        with open(wakeup_path, "rb") as fh:
            raw = fh.read(8)
        if len(raw) == 8:
            return int(struct.unpack("<Q", raw)[0])
    except OSError:
        pass
    return 0


def wait_for_board_change(
    wakeup_path: str,
    timeout: float = 30.0,
    last_gen: Optional[int] = None,
) -> dict[str, Any]:
    """Wait for a post-commit board wakeup event using kqueue EVFILT_VNODE on macOS."""
    timeout = max(0.0, float(timeout))
    parent = os.path.dirname(os.path.abspath(wakeup_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if not os.path.exists(wakeup_path):
        try:
            fd = os.open(wakeup_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                if os.fstat(fd).st_size < 8:
                    os.write(fd, struct.pack("<Q", 0))
            finally:
                os.close(fd)
        except OSError:
            pass
    baseline = _read_wakeup_generation(wakeup_path) if last_gen is None else int(last_gen)
    current = _read_wakeup_generation(wakeup_path)
    if current != baseline:
        return {"changed": True, "generation": current, "mechanism": "immediate"}
    if timeout <= 0:
        return {"changed": False, "generation": current, "mechanism": "immediate"}

    if hasattr(select, "kqueue") and hasattr(select, "KQ_FILTER_VNODE"):
        fd = None
        kq = None
        try:
            fd = os.open(wakeup_path, os.O_RDONLY)
            kq = select.kqueue()
            fflags = (
                getattr(select, "KQ_NOTE_WRITE", 0x0002)
                | getattr(select, "KQ_NOTE_EXTEND", 0x0004)
                | getattr(select, "KQ_NOTE_ATTRIB", 0x0008)
            )
            ev = select.kevent(
                fd,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                fflags=fflags,
            )
            kq.control([ev], 0, 0)
            # Re-check after arming kqueue to close the pre-registration race window.
            current = _read_wakeup_generation(wakeup_path)
            if current != baseline:
                return {"changed": True, "generation": current, "mechanism": "kqueue"}
            events = kq.control(None, 1, timeout)
            current = _read_wakeup_generation(wakeup_path)
            return {
                "changed": bool(events) or current != baseline,
                "generation": current,
                "mechanism": "kqueue",
            }
        except OSError:
            pass
        finally:
            if kq is not None:
                try:
                    kq.close()
                except OSError:
                    pass
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _read_wakeup_generation(wakeup_path)
        if current != baseline:
            return {"changed": True, "generation": current, "mechanism": "poll"}
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    current = _read_wakeup_generation(wakeup_path)
    return {"changed": current != baseline, "generation": current, "mechanism": "poll"}


def _now() -> str:
    # UTC ISO-8601 with milliseconds and a trailing Z. Rows written by older
    # versions hold local-time strings without a zone; readers treat both as
    # opaque text (SQLite INTEGER ids remain the real order).
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _as_agent_dict(agent: Any) -> dict[str, Any]:
    """Accept AgentIdentity dataclass, a duck with asdict, or a plain dict."""
    if is_dataclass(agent) and not isinstance(agent, type):
        d = asdict(agent)
    elif hasattr(agent, "to_dict") and callable(agent.to_dict):
        d = agent.to_dict()
    elif isinstance(agent, dict):
        d = dict(agent)
    else:
        raise BoardError("agent must be a dict or AgentIdentity")
    return d


def _require_str(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise BoardError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise BoardError(f"{name} must be a non-empty string")
    return value


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # CLI convenience: comma-separated mentions.
        parts = [p.strip() for p in value.split(",") if p.strip()]
        return parts
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise BoardError("mentions must be a list of non-empty strings")
            out.append(item.strip())
        return out
    raise BoardError("mentions must be a list of strings or a comma-separated string")


def _dm_channel_name(a: str, b: str) -> str:
    x, y = sorted((a, b))
    return f"dm:{quote(x, safe='-_.')}:{quote(y, safe='-_.')}"


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _parse_mentions_field(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return list(raw)
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    return []


class HiveBoard(MailboxMixin, CoordinationMixin, ProtocolMixin, ExecutionMixin, CandidateMixin, KnowledgeMixin):
    """SQLite WAL hive board. One file, many independent processes.

    Usage::

        with HiveBoard(path) as board:
            board.register({"id": "arch", "role": "architect", ...})
            board.post(from_agent="arch", body="hello", channel="hive")
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(Path(path).resolve(strict=False))
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.jsonl_path = self.path + ".jsonl"
        self.wakeup_path = self.path + ".wakeup"
        # check_same_thread=False so a board opened on one thread can be used
        # from ThreadPoolExecutor workers (fusion_swarm style). WAL + our
        # write lock keep that safe.
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._lock = threading.RLock()
        self._pending_sidecars: list[dict] = []
        self.init_schema()

    def __enter__(self) -> "HiveBoard":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- schema -------------------------------------------------------------

    def init_schema(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= BOARD_SCHEMA_VERSION and self._schema_tables_present():
                # Fully initialised by a previous open: only the idempotent
                # additive migrations run (no-ops when columns exist).
                initialize_execution(self._conn)
                has_fts = self._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='knowledge_fts'").fetchone()
                self.knowledge_fts = True if has_fts else initialize_fts(self._conn)
                self._conn.commit()
                return
            self._init_schema_full()

    def _schema_tables_present(self) -> bool:
        """One cheap SELECT: guards against a stamped DB that lost tables."""
        have = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        want = set(_BASE_TABLES)
        for chunk in (MAILBOX_SCHEMA, COORDINATION_SCHEMA, PROTOCOL_SCHEMA, EXECUTION_SCHEMA,
                      CANDIDATE_SCHEMA, KNOWLEDGE_SCHEMA, STIGMERGY_SCHEMA):
            want.update(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", chunk))
        return want <= have

    def _init_schema_full(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    id          TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    role        TEXT NOT NULL,
                    provider    TEXT NOT NULL,
                    model       TEXT NOT NULL,
                    parent_id   TEXT,
                    lineage     TEXT NOT NULL DEFAULT '',
                    depth       INTEGER NOT NULL DEFAULT 0,
                    spawn_budget INTEGER NOT NULL DEFAULT 0,
                    max_depth   INTEGER NOT NULL DEFAULT 2,
                    status      TEXT NOT NULL DEFAULT 'online',
                    registered_at TEXT NOT NULL,
                    last_seen   TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channels (
                    name        TEXT PRIMARY KEY,
                    kind        TEXT NOT NULL,
                    created_by  TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channel_members (
                    channel     TEXT NOT NULL,
                    agent_id    TEXT NOT NULL,
                    joined_at   TEXT NOT NULL,
                    PRIMARY KEY (channel, agent_id),
                    FOREIGN KEY (channel) REFERENCES channels(name),
                    FOREIGN KEY (agent_id) REFERENCES agents(id)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel     TEXT NOT NULL,
                    from_agent  TEXT NOT NULL,
                    to_agent    TEXT,
                    body        TEXT NOT NULL,
                    kind        TEXT NOT NULL DEFAULT 'message',
                    thread_id   INTEGER,
                    mentions    TEXT NOT NULL DEFAULT '[]',
                    created_at  TEXT NOT NULL,
                    FOREIGN KEY (channel) REFERENCES channels(name),
                    FOREIGN KEY (from_agent) REFERENCES agents(id)
                );

                CREATE TABLE IF NOT EXISTS acks (
                    agent_id    TEXT NOT NULL,
                    message_id  INTEGER NOT NULL,
                    acked_at    TEXT NOT NULL,
                    PRIMARY KEY (agent_id, message_id),
                    FOREIGN KEY (agent_id) REFERENCES agents(id),
                    FOREIGN KEY (message_id) REFERENCES messages(id)
                );

                CREATE TABLE IF NOT EXISTS presence (
                    agent_id    TEXT PRIMARY KEY,
                    status      TEXT NOT NULL,
                    last_seen   TEXT NOT NULL,
                    FOREIGN KEY (agent_id) REFERENCES agents(id)
                );
                """
            )
            self._conn.executescript(MAILBOX_SCHEMA + COORDINATION_SCHEMA + PROTOCOL_SCHEMA + EXECUTION_SCHEMA + CANDIDATE_SCHEMA + KNOWLEDGE_SCHEMA + STIGMERGY_SCHEMA)
            initialize_execution(self._conn)
            self.knowledge_fts = initialize_fts(self._conn)
            for name in DEFAULT_CHANNELS:
                self._ensure_channel_unlocked(name, kind="room", created_by="system")
            self._conn.commit()
            self._conn.execute(f"PRAGMA user_version = {int(BOARD_SCHEMA_VERSION)}")
            self._conn.commit()

    # --- agents -------------------------------------------------------------

    def register(self, agent: Any, *, replace: bool = False) -> dict[str, Any]:
        d = _as_agent_dict(agent)
        agent_id = _require_str(d.get("id"), "id")
        role = d.get("role") or "worker"
        if role not in AGENT_ROLES:
            raise BoardError(f"role must be one of {AGENT_ROLES}")
        status = d.get("status") or "online"
        if status not in AGENT_STATUSES:
            raise BoardError(f"status must be one of {AGENT_STATUSES}")
        display = d.get("display_name") or agent_id
        provider = d.get("provider") or ""
        model = d.get("model") or ""
        if not isinstance(display, str) or not display.strip():
            raise BoardError("display_name must be a non-empty string")
        if not isinstance(provider, str) or not provider.strip():
            raise BoardError("provider must be a non-empty string")
        if not isinstance(model, str) or not model.strip():
            raise BoardError("model must be a non-empty string")
        parent_id = d.get("parent_id")
        if parent_id is not None and not isinstance(parent_id, str):
            raise BoardError("parent_id must be a string or null")
        if parent_id == "":
            parent_id = None
        lineage = d.get("lineage") if d.get("lineage") is not None else ""
        if not isinstance(lineage, str):
            raise BoardError("lineage must be a string")
        depth = d.get("depth", 0)
        spawn_budget = d.get("spawn_budget", 0)
        max_depth = d.get("max_depth", 2)
        for name, val in (("depth", depth), ("spawn_budget", spawn_budget), ("max_depth", max_depth)):
            if isinstance(val, bool) or not isinstance(val, int):
                raise BoardError(f"{name} must be an integer")
            if val < 0:
                raise BoardError(f"{name} must be >= 0")
        stamp = _now()
        rec = {
            "id": agent_id,
            "display_name": display.strip(),
            "role": role,
            "provider": provider.strip(),
            "model": model.strip(),
            "parent_id": parent_id,
            "lineage": lineage,
            "depth": depth,
            "spawn_budget": spawn_budget,
            "max_depth": max_depth,
            "status": status,
            "registered_at": stamp,
            "last_seen": stamp,
        }
        with self._transaction():
            if parent_id is not None:
                parent = self._agent_unlocked(parent_id)
                if parent is None:
                    raise BoardError(f"unknown parent_id: {parent_id}")
            existing = self._agent_unlocked(agent_id)
            if existing is not None:
                if not replace and (
                    existing["role"] != rec["role"] or existing["provider"] != rec["provider"]
                    or existing["model"] != rec["model"] or existing["parent_id"] != rec["parent_id"]
                ):
                    raise BoardError("agent id already registered with a different identity")
                self._conn.execute(
                    """
                    UPDATE agents SET
                        display_name=?, role=?, provider=?, model=?,
                        parent_id=?, lineage=?, depth=?, spawn_budget=?,
                        max_depth=?, status=?, last_seen=?
                    WHERE id=?
                    """,
                    (
                        rec["display_name"], rec["role"], rec["provider"], rec["model"],
                        rec["parent_id"], rec["lineage"], rec["depth"], rec["spawn_budget"],
                        rec["max_depth"], rec["status"], stamp, agent_id,
                    ),
                )
                rec["registered_at"] = existing["registered_at"]
                rec["last_seen"] = stamp
            else:
                self._conn.execute(
                    """
                    INSERT INTO agents (
                        id, display_name, role, provider, model, parent_id,
                        lineage, depth, spawn_budget, max_depth, status,
                        registered_at, last_seen
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        rec["id"], rec["display_name"], rec["role"], rec["provider"],
                        rec["model"], rec["parent_id"], rec["lineage"], rec["depth"],
                        rec["spawn_budget"], rec["max_depth"], rec["status"],
                        rec["registered_at"], rec["last_seen"],
                    ),
                )
            self._conn.execute(
                """
                INSERT INTO presence (agent_id, status, last_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET status=excluded.status,
                    last_seen=excluded.last_seen
                """,
                (agent_id, status, stamp),
            )
            for room in _ROLE_ROOMS.get(role, ("hive",)):
                self._ensure_channel_unlocked(room, kind="room", created_by="system")
                self._join_unlocked(room, agent_id)
        return rec

    def heartbeat(self, agent_id: str, status: str = "online") -> dict[str, Any]:
        agent_id = _require_str(agent_id, "agent_id")
        if status not in AGENT_STATUSES:
            raise BoardError(f"status must be one of {AGENT_STATUSES}")
        stamp = _now()
        with self._transaction():
            if self._agent_unlocked(agent_id) is None:
                raise BoardError(f"unknown agent: {agent_id}")
            self._conn.execute(
                "UPDATE agents SET status=?, last_seen=? WHERE id=?",
                (status, stamp, agent_id),
            )
            self._conn.execute(
                """
                INSERT INTO presence (agent_id, status, last_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET status=excluded.status,
                    last_seen=excluded.last_seen
                """,
                (agent_id, status, stamp),
            )
        return {"id": agent_id, "status": status, "last_seen": stamp}

    def agents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agents ORDER BY lineage, id"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def tree(self) -> dict[str, Any]:
        """Parent-id grouping of registered agents.

        Returns ``{"roots": [...], "children": {parent_id: [child, ...]}}``.
        """
        people = self.agents()
        children: dict[str, list[dict[str, Any]]] = {}
        roots: list[dict[str, Any]] = []
        for agent in people:
            parent = agent.get("parent_id")
            if parent:
                children.setdefault(parent, []).append(agent)
            else:
                roots.append(agent)
        return {"roots": roots, "children": children, "agents": people}

    # --- channels -----------------------------------------------------------

    def ensure_channel(
        self, name: str, kind: str = "room", created_by: str = "system"
    ) -> str:
        name = _require_str(name, "name")
        if kind not in CHANNEL_KINDS:
            raise BoardError(f"kind must be one of {CHANNEL_KINDS}")
        created_by = _require_str(created_by, "created_by")
        with self._transaction():
            self._ensure_channel_unlocked(name, kind=kind, created_by=created_by)
        return name

    def join(self, channel: str, agent_id: str) -> dict[str, Any]:
        channel = _require_str(channel, "channel")
        agent_id = _require_str(agent_id, "agent_id")
        with self._transaction():
            if self._channel_unlocked(channel) is None:
                raise BoardError(f"unknown channel: {channel}")
            if self._agent_unlocked(agent_id) is None:
                raise BoardError(f"unknown agent: {agent_id}")
            info = self._channel_unlocked(channel)
            if info["kind"] == "dm" and not self._conn.execute(
                "SELECT 1 FROM channel_members WHERE channel=? AND agent_id=?", (channel, agent_id)
            ).fetchone():
                raise BoardError("a DM cannot be joined by a third agent")
            self._join_unlocked(channel, agent_id)
        return {"channel": channel, "agent_id": agent_id}

    def channels(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM channels ORDER BY name"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # --- messages -----------------------------------------------------------

    def post(
        self,
        *,
        from_agent: str,
        body: str,
        channel: str = "hive",
        thread_id: Optional[int] = None,
        mentions: Any = None,
        to_agent: Optional[str] = None,
        kind: str = "message",
        task_id: str = "",
        priority: str = "normal",
        payload: Optional[dict] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        from_agent = _require_str(from_agent, "from_agent")
        body = _require_str(body, "body")
        channel = _require_str(channel, "channel")
        if kind not in MESSAGE_KINDS:
            raise BoardError(f"kind must be one of {MESSAGE_KINDS}")
        mention_list = _json_list(mentions)
        if to_agent is not None:
            to_agent = _require_str(to_agent, "to_agent")
        if thread_id is not None:
            if isinstance(thread_id, bool) or not isinstance(thread_id, int):
                raise BoardError("thread_id must be an integer or null")
        task_id = _require_str(task_id, "task_id", allow_empty=True)
        if priority not in PRIORITIES:
            raise BoardError(f"priority must be one of {tuple(PRIORITIES)}")
        encoded_payload = json_object({} if payload is None else payload, "payload")
        if idempotency_key is not None:
            idempotency_key = _require_str(idempotency_key, "idempotency_key")
        stamp = _now()
        with self._transaction():
            if self._agent_unlocked(from_agent) is None:
                raise BoardError(f"unknown agent: {from_agent}")
            if to_agent is not None and self._agent_unlocked(to_agent) is None:
                raise BoardError(f"unknown to_agent: {to_agent}")
            for mentioned in mention_list:
                if self._agent_unlocked(mentioned) is None:
                    raise BoardError(f"unknown mention: {mentioned}")
            # Retry identity follows the authored request, not today's roster.
            # Automatically discovered recipients remain the original snapshot.
            fingerprint = envelope_fingerprint(from_agent=from_agent, body=body,
                channel=channel, thread_id=thread_id, mentions=sorted(set(mention_list)), to_agent=to_agent,
                kind=kind, task_id=task_id, priority=priority, payload=json.loads(encoded_payload))
            if idempotency_key is not None:
                previous = self._conn.execute(
                    "SELECT * FROM message_envelopes WHERE sender=? AND idempotency_key=?",
                    (from_agent, idempotency_key),
                ).fetchone()
                if previous:
                    if previous["fingerprint"] != fingerprint:
                        raise BoardError("idempotency key reused with different message content")
                    row = self._conn.execute("SELECT * FROM messages WHERE id=?", (previous["message_id"],)).fetchone()
                    return self._envelope_dict(row)
            mention_list = sorted(set(mention_list) | {
                found if self._agent_unlocked(found) is not None else found.rstrip(".")
                for found in re.findall(r"(?<![\w@])@([A-Za-z0-9_.-]+)", body)
                if self._agent_unlocked(found) is not None or self._agent_unlocked(found.rstrip(".")) is not None
            })
            channel_info = self._channel_unlocked(channel)
            if channel_info is None:
                raise BoardError(f"unknown channel: {channel}")
            if thread_id is not None:
                parent = self._message_unlocked(thread_id)
                if parent is None:
                    raise BoardError(f"unknown thread_id: {thread_id}")
                if parent["channel"] != channel:
                    raise BoardError("thread root must be in the same channel")
            if channel_info["kind"] == "dm":
                if to_agent is None or channel != _dm_channel_name(from_agent, to_agent):
                    raise BoardError("DM posts require the two original participants")
                if set(mention_list) - {from_agent, to_agent}:
                    raise BoardError("a DM cannot mention a third agent")
            self._join_unlocked(channel, from_agent)
            if to_agent is not None:
                self._join_unlocked(channel, to_agent)
            cur = self._conn.execute(
                """
                INSERT INTO messages (
                    channel, from_agent, to_agent, body, kind, thread_id,
                    mentions, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel, from_agent, to_agent, body, kind, thread_id,
                    json.dumps(mention_list, ensure_ascii=False), stamp,
                ),
            )
            msg_id = int(cur.lastrowid)
            rec = {
                "id": msg_id,
                "channel": channel,
                "from_agent": from_agent,
                "to_agent": to_agent,
                "body": body,
                "kind": kind,
                "thread_id": thread_id,
                "mentions": mention_list,
                "created_at": stamp,
                "task_id": task_id,
                "priority": priority,
                "payload": json.loads(encoded_payload),
                "idempotency_key": idempotency_key,
            }
            self._conn.execute(
                """INSERT INTO message_envelopes(message_id,sender,idempotency_key,
                    fingerprint,task_id,priority,payload) VALUES (?,?,?,?,?,?,?)""",
                (msg_id, from_agent, idempotency_key, fingerprint, task_id, PRIORITIES[priority], encoded_payload),
            )
            self._route_unlocked(rec)
            self._pending_sidecars.append(rec)
        return rec

    def dm(self, from_agent: str, to_agent: str, body: str, *, kind: str = "message",
           task_id: str = "", priority: str = "normal", payload: Optional[dict] = None,
           idempotency_key: Optional[str] = None) -> dict[str, Any]:
        from_agent = _require_str(from_agent, "from_agent")
        to_agent = _require_str(to_agent, "to_agent")
        if from_agent == to_agent:
            raise BoardError("cannot DM yourself")
        channel = _dm_channel_name(from_agent, to_agent)
        with self._transaction():
            if self._agent_unlocked(from_agent) is None:
                raise BoardError(f"unknown agent: {from_agent}")
            if self._agent_unlocked(to_agent) is None:
                raise BoardError(f"unknown to_agent: {to_agent}")
            self._ensure_channel_unlocked(channel, kind="dm", created_by=from_agent)
            self._join_unlocked(channel, from_agent)
            self._join_unlocked(channel, to_agent)
            return self.post(
                from_agent=from_agent, body=body, channel=channel, to_agent=to_agent,
                kind=kind, task_id=task_id, priority=priority, payload=payload,
                idempotency_key=idempotency_key,
            )

    def reply(self, from_agent: str, message_id: int, body: str, *, kind: str = "message",
              priority: str = "normal", idempotency_key: Optional[str] = None) -> dict[str, Any]:
        from_agent = _require_str(from_agent, "from_agent")
        body = _require_str(body, "body")
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            raise BoardError("message_id must be an integer")
        with self._lock:
            parent = self._message_unlocked(message_id)
            if parent is None:
                raise BoardError(f"unknown message: {message_id}")
            if not self._visible_unlocked(from_agent, parent):
                raise BoardError("cannot reply to a message outside your channels")
            channel = parent["channel"]
            thread_root = parent["thread_id"] if parent["thread_id"] is not None else parent["id"]
            recipient = None
            if self._channel_unlocked(channel)["kind"] == "dm":
                recipient = parent["from_agent"] if parent["from_agent"] != from_agent else parent["to_agent"]
            metadata = self._conn.execute(
                "SELECT task_id FROM message_envelopes WHERE message_id=?", (parent["id"],)
            ).fetchone()
        return self.post(
            from_agent=from_agent,
            body=body,
            channel=channel,
            thread_id=int(thread_root),
            to_agent=recipient,
            task_id=metadata[0] if metadata else "",
            kind=kind,
            priority=priority,
            idempotency_key=idempotency_key,
        )

    def poll(
        self, agent_id: str, since_id: Optional[int] = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Messages in channels the agent joined, plus DMs and @mentions.

        Isolation: an agent does not see rooms it never joined, even if those
        rooms have traffic. DMs and @mentions always surface, which is how
        children of different captains talk across lineage.
        """
        agent_id = _require_str(agent_id, "agent_id")
        if since_id is not None and (isinstance(since_id, bool) or not isinstance(since_id, int)):
            raise BoardError("since_id must be an integer or null")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise BoardError("limit must be a positive integer")
        with self._lock:
            if self._agent_unlocked(agent_id) is None:
                raise BoardError(f"unknown agent: {agent_id}")
            rows = self._conn.execute(
                """SELECT m.* FROM messages m JOIN channels c ON c.name=m.channel
                WHERE m.id>? AND (
                    (c.kind='dm' AND (m.from_agent=? OR m.to_agent=?)) OR
                    (c.kind!='dm' AND (m.to_agent=? OR
                        EXISTS(SELECT 1 FROM channel_members cm WHERE cm.channel=m.channel AND cm.agent_id=?) OR
                        EXISTS(SELECT 1 FROM json_each(m.mentions) WHERE value=?))))
                ORDER BY m.id LIMIT ?""",
                (since_id if since_id is not None else 0, agent_id, agent_id,
                 agent_id, agent_id, agent_id, limit),
            ).fetchall()
            return [self._envelope_dict(row) for row in rows]

    def mentions(
        self, agent_id: str, since_id: Optional[int] = None
    ) -> list[dict[str, Any]]:
        agent_id = _require_str(agent_id, "agent_id")
        if since_id is not None and (isinstance(since_id, bool) or not isinstance(since_id, int)):
            raise BoardError("since_id must be an integer or null")
        with self._lock:
            if self._agent_unlocked(agent_id) is None:
                raise BoardError(f"unknown agent: {agent_id}")
            params: list[Any] = [agent_id, agent_id]
            since_sql = ""
            if since_id is not None:
                since_sql = "AND id > ?"
                params.append(since_id)
            rows = self._conn.execute(
                f"""
                SELECT * FROM messages
                WHERE (EXISTS(SELECT 1 FROM json_each(mentions) WHERE value=?) OR to_agent = ?)
                {since_sql}
                ORDER BY id ASC
                """,
                params,
            ).fetchall()
        out = []
        for row in rows:
            rec = self._message_dict(row)
            if agent_id in (rec.get("mentions") or []) or rec.get("to_agent") == agent_id:
                out.append(rec)
        return out

    def ack(self, agent_id: str, message_id: int, lease_token: Optional[str] = None) -> dict[str, Any]:
        agent_id = _require_str(agent_id, "agent_id")
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            raise BoardError("message_id must be an integer")
        stamp = _now()
        with self._transaction():
            self._check_receipt_unlocked(agent_id, message_id, lease_token)
            self._conn.execute(
                """
                INSERT INTO acks (agent_id, message_id, acked_at)
                VALUES (?, ?, ?)
                ON CONFLICT(agent_id, message_id) DO UPDATE SET acked_at=excluded.acked_at
                """,
                (agent_id, message_id, stamp),
            )
            self._conn.execute(
                "UPDATE deliveries SET state='acked',acked_at=?,lease_until=NULL WHERE agent_id=? AND message_id=?",
                (time.time(), agent_id, message_id),
            )
        return {"agent_id": agent_id, "message_id": message_id, "acked_at": stamp}

    def format_feed(self, messages: Iterable[dict[str, Any]]) -> str:
        lines: list[str] = []
        for msg in messages:
            mid = msg.get("id", "?")
            channel = msg.get("channel", "?")
            author = msg.get("from_agent", "?")
            body = msg.get("body", "")
            thread = msg.get("thread_id")
            mentions = msg.get("mentions") or []
            stamp = msg.get("created_at", "")
            prefix = f"#{channel}"
            if thread is not None:
                prefix += f" ↩{thread}"
            mention_s = ""
            if mentions:
                mention_s = " @" + " @".join(mentions)
            dest = ""
            if msg.get("to_agent"):
                dest = f" → {msg['to_agent']}"
            lines.append(f"[{mid}] {stamp} {prefix} {author}{dest}{mention_s}: {body}")
        return "\n".join(lines)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # --- internals ----------------------------------------------------------

    def _agent_unlocked(self, agent_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def _channel_unlocked(self, name: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM channels WHERE name = ?", (name,)
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def _message_unlocked(self, message_id: int) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return self._message_dict(row) if row is not None else None

    def _ensure_channel_unlocked(
        self, name: str, kind: str = "room", created_by: str = "system"
    ) -> None:
        existing = self._channel_unlocked(name)
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO channels (name, kind, created_by, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (name, kind, created_by, _now()),
            )
        elif existing["kind"] != kind:
            raise BoardError(
                f"channel {name!r} already exists as kind {existing['kind']!r}"
            )

    def _join_unlocked(self, channel: str, agent_id: str) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO channel_members (channel, agent_id, joined_at)
            VALUES (?, ?, ?)
            """,
            (channel, agent_id, _now()),
        )

    def _message_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = _row_to_dict(row)
        d["mentions"] = _parse_mentions_field(d.get("mentions"))
        return d

    def _append_jsonl(self, rec: dict[str, Any]) -> None:
        self._append_jsonl_batch([rec])

    def _append_jsonl_batch(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        payload = "".join(
            json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
            for rec in records
        )
        with open(self.jsonl_path, "a", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

    def _touch_wakeup(self) -> int:
        fd = os.open(self.wakeup_path, os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_EX)
                locked = True
            raw = os.read(fd, 8)
            current = struct.unpack("<Q", raw)[0] if len(raw) == 8 else 0
            next_gen = (current + 1) & 0xFFFFFFFFFFFFFFFF
            if next_gen == 0:
                next_gen = 1
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, struct.pack("<Q", next_gen))
            return next_gen
        finally:
            if locked:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)

    def wait(
        self,
        *,
        timeout: float = 30.0,
        agent_id: Optional[str] = None,
        since_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Block until the board changes (or immediate unread mail exists for ``agent_id``)."""
        if timeout < 0:
            raise BoardError("timeout must be >= 0")
        baseline = _read_wakeup_generation(self.wakeup_path)
        if agent_id is not None:
            existing = self.poll(agent_id, since_id=since_id)
            if existing:
                return {
                    "changed": True,
                    "generation": baseline,
                    "mechanism": "preexisting",
                    "messages": existing,
                }
        outcome = wait_for_board_change(self.wakeup_path, timeout=timeout, last_gen=baseline)
        if agent_id is not None:
            outcome["messages"] = self.poll(agent_id, since_id=since_id)
        return outcome

    # --- stigmergy (expiring pheromone discovery trails) --------------------

    def deposit_trace(
        self,
        agent_id: str,
        locus: str,
        *,
        kind: str = "hotspot",
        strength: float = 1.0,
        half_life: float = 600.0,
        ttl: float = 1800.0,
        note: str = "",
    ) -> dict[str, Any]:
        """Deposit an expiring stigmergic discovery trace on a file/symbol locus."""
        agent_id = _require_str(agent_id, "agent_id")
        locus = _require_str(locus, "locus")
        kind = _require_str(kind, "kind")
        strength_val = positive_number(strength, "strength")
        half_life_val = positive_number(half_life, "half_life")
        ttl_val = positive_number(ttl, "ttl")
        if not isinstance(note, str):
            raise BoardError("note must be a string")
        now = time.time()
        expires_at = now + ttl_val
        with self._transaction():
            self._conn.execute("DELETE FROM stigmergy_traces WHERE expires_at <= ?", (now,))
            cur = self._conn.execute(
                """
                INSERT INTO stigmergy_traces (
                    agent_id, locus, kind, strength, half_life, note, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (agent_id, locus.strip(), kind.strip(), strength_val, half_life_val, note, now, expires_at),
            )
            trace_id = int(cur.lastrowid)
            rec = {
                "id": trace_id,
                "agent_id": agent_id,
                "locus": locus.strip(),
                "kind": kind.strip(),
                "strength": strength_val,
                "effective_strength": strength_val,
                "half_life": half_life_val,
                "note": note,
                "created_at": now,
                "expires_at": expires_at,
            }
            self._pending_sidecars.append({"type": "stigmergy_trace", **rec})
        return rec

    def sense_traces(
        self,
        locus_prefix: str = "",
        *,
        kind: Optional[str] = None,
        min_strength: float = 0.05,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Sense active stigmergic traces with exponential half-life decay."""
        if not isinstance(locus_prefix, str):
            raise BoardError("locus_prefix must be a string")
        positive_int(limit, "limit")
        now = time.time()
        with self._transaction():
            self._conn.execute("DELETE FROM stigmergy_traces WHERE expires_at <= ?", (now,))
            sql = "SELECT * FROM stigmergy_traces WHERE expires_at > ?"
            params: list[Any] = [now]
            if locus_prefix:
                sql += " AND locus LIKE ?"
                params.append(locus_prefix + "%")
            if kind:
                sql += " AND kind = ?"
                params.append(kind)
            rows = self._conn.execute(sql, params).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = _row_to_dict(row)
            age = max(0.0, now - float(item["created_at"]))
            hl = max(1e-6, float(item["half_life"]))
            decayed = float(item["strength"]) * math.pow(2.0, -age / hl)
            if decayed >= float(min_strength):
                item["effective_strength"] = round(decayed, 6)
                results.append(item)
        results.sort(key=lambda r: (r["effective_strength"], r["created_at"]), reverse=True)
        return results[:limit]


# --- CLI --------------------------------------------------------------------

def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, default=str))


def _int_or_none(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as ex:
        raise BoardError(f"not an integer: {value!r}") from ex


def default_board_db() -> str:
    """Honor the board injected into a worker before falling back to Hive state."""
    raw = os.environ.get("FUSION_BOARD", "").strip()
    if raw:
        return raw
    root = Path(os.environ.get("FUSION_STATE_DIR") or Path.home() / ".fusion")
    return str(root / "hive" / "board.sqlite")


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="fusion_swarm.board",
        description="HiveBoard CLI — always prints JSON.",
    )
    ap.add_argument("--db", default=None, help="sqlite board (default: FUSION_BOARD or Hive state)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create schema + default channels")

    p_reg = sub.add_parser("register", help="register or update an agent")
    p_reg.add_argument("--id", required=True)
    p_reg.add_argument("--display-name", dest="display_name", default="")
    p_reg.add_argument("--role", default="worker")
    p_reg.add_argument("--provider", required=True)
    p_reg.add_argument("--model", required=True)
    p_reg.add_argument("--parent-id", dest="parent_id", default=None)
    p_reg.add_argument("--lineage", default="")
    p_reg.add_argument("--depth", type=int, default=0)
    p_reg.add_argument("--spawn-budget", dest="spawn_budget", type=int, default=0)
    p_reg.add_argument("--max-depth", dest="max_depth", type=int, default=2)
    p_reg.add_argument("--status", default="online")
    p_reg.add_argument("--replace", action="store_true", help="allow changing role/provider/model/parent of an existing id")

    p_hb = sub.add_parser("heartbeat")
    p_hb.add_argument("--agent", required=True)
    p_hb.add_argument("--status", default="online")

    p_post = sub.add_parser("post")
    p_post.add_argument("--from", "--from-agent", dest="from_agent", required=True)
    body = p_post.add_mutually_exclusive_group(required=True)
    body.add_argument("--body")
    body.add_argument("--body-file")
    p_post.add_argument("--channel", default="hive")
    p_post.add_argument("--thread-id", dest="thread_id", default=None)
    p_post.add_argument("--mentions", default="")
    p_post.add_argument("--to", "--to-agent", dest="to_agent", default=None)
    p_post.add_argument("--kind", default="message")
    p_post.add_argument("--task-id", default="")
    p_post.add_argument("--priority", choices=tuple(PRIORITIES), default="normal")
    p_post.add_argument("--payload-file")
    p_post.add_argument("--idempotency-key")

    p_dm = sub.add_parser("dm")
    p_dm.add_argument("--from", "--from-agent", dest="from_agent", required=True)
    p_dm.add_argument("--to", "--to-agent", dest="to_agent", required=True)
    dm_body = p_dm.add_mutually_exclusive_group(required=True)
    dm_body.add_argument("--body")
    dm_body.add_argument("--body-file")
    p_dm.add_argument("--kind", default="message")
    p_dm.add_argument("--task-id", default="")
    p_dm.add_argument("--priority", choices=tuple(PRIORITIES), default="normal")
    p_dm.add_argument("--payload-file")
    p_dm.add_argument("--idempotency-key")

    p_reply = sub.add_parser("reply")
    p_reply.add_argument("--from", "--from-agent", dest="from_agent", required=True)
    p_reply.add_argument("--message-id", dest="message_id", type=int, required=True)
    p_reply.add_argument("--body", required=True)
    p_reply.add_argument("--kind", default="message")
    p_reply.add_argument("--priority", choices=tuple(PRIORITIES), default="normal")
    p_reply.add_argument("--idempotency-key")

    p_poll = sub.add_parser("poll")
    p_poll.add_argument("--agent", required=True)
    p_poll.add_argument("--since-id", dest="since_id", default=None)
    p_poll.add_argument("--limit", type=int, default=100)

    p_ment = sub.add_parser("mentions")
    p_ment.add_argument("--agent", required=True)
    p_ment.add_argument("--since-id", dest="since_id", default=None)

    sub.add_parser("agents")
    sub.add_parser("tree")
    sub.add_parser("channels")

    p_join = sub.add_parser("join")
    p_join.add_argument("--channel", required=True)
    p_join.add_argument("--agent", required=True)

    p_ack = sub.add_parser("ack")
    p_ack.add_argument("--agent", required=True)
    p_ack.add_argument("--message-id", dest="message_id", type=int, required=True)
    p_ack.add_argument("--lease-token")

    for name in ("inbox", "claim"):
        parser = sub.add_parser(name)
        parser.add_argument("--agent", required=True)
        parser.add_argument("--limit", type=int, default=1 if name == "claim" else 100)
        if name == "claim":
            parser.add_argument("--lease-seconds", type=float, default=60)
        else:
            parser.add_argument("--task-id", default=None)
    p_nack = sub.add_parser("nack")
    p_nack.add_argument("--agent", required=True)
    p_nack.add_argument("--message-id", type=int, required=True)
    p_nack.add_argument("--lease-token", required=True)
    p_receipts = sub.add_parser("receipts")
    p_receipts.add_argument("--message-id", type=int, required=True)
    sub.add_parser("stats")

    for name in ("reserve", "release"):
        parser = sub.add_parser(name)
        parser.add_argument("--agent", required=True)
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--paths", nargs="+", required=True, help="concrete files or directories")
        parser.add_argument("--task-id", default="")
        if name == "reserve":
            parser.add_argument("--lease-seconds", type=float, default=900)
    sub.add_parser("reservations")
    p_context = sub.add_parser("context-get")
    p_context.add_argument("--namespace", required=True)
    p_context.add_argument("--key", required=True)
    p_context_set = sub.add_parser("context-set")
    p_context_set.add_argument("--agent", required=True)
    p_context_set.add_argument("--namespace", required=True)
    p_context_set.add_argument("--key", required=True)
    p_context_set.add_argument("--value-file", required=True)
    p_context_set.add_argument("--expected-version", type=int, required=True)

    p_create = sub.add_parser("create-task")
    p_create.add_argument("--agent", required=True)
    p_create.add_argument("--brief-file", required=True)
    p_tasks = sub.add_parser("tasks")
    p_tasks.add_argument("--task-id")
    p_start = sub.add_parser("start-task")
    p_start.add_argument("--agent", required=True)
    p_start.add_argument("--task-id", required=True)
    p_start.add_argument("--lease-seconds", type=float, default=900)
    p_submit = sub.add_parser("submit-task")
    p_submit.add_argument("--agent", required=True)
    p_submit.add_argument("--report-file", required=True)
    p_review = sub.add_parser("review-task")
    p_review.add_argument("--agent", required=True)
    p_review.add_argument("--task-id", required=True)
    p_review.add_argument("--attempt", type=int, required=True)
    p_review.add_argument("--decision", choices=("accept", "revise", "escalate", "cancel"), required=True)
    p_review.add_argument("--note", required=True)
    p_recover = sub.add_parser("recover-task")
    p_recover.add_argument("--agent", required=True)
    p_recover.add_argument("--task-id", required=True)
    p_recover.add_argument("--attempt", type=int, required=True)
    p_recover.add_argument("--note", required=True)
    p_history = sub.add_parser("task-history")
    p_history.add_argument("--task-id", required=True)

    p_send = sub.add_parser("send-message")
    p_send.add_argument("--from", dest="from_agent", required=True)
    p_send.add_argument("--to", dest="to_agent", required=True)
    p_send.add_argument("--envelope-file", required=True)
    p_send.add_argument("--idempotency-key", required=True)
    p_send.add_argument("--priority", choices=tuple(PRIORITIES), default="normal")
    p_consume = sub.add_parser("consume-delivery")
    p_consume.add_argument("--agent", required=True)
    p_consume.add_argument("--message-id", type=int, required=True)
    p_consume.add_argument("--lease-token", required=True)
    p_consume.add_argument("--effect-key", required=True)
    p_consume.add_argument("--result-file", required=True)
    p_dead = sub.add_parser("dead-letters")
    p_dead.add_argument("--agent", required=True)
    p_retry = sub.add_parser("retry-delivery")
    p_retry.add_argument("--agent", required=True)
    p_retry.add_argument("--message-id", type=int, required=True)
    p_retry.add_argument("--extra-attempts", type=int, default=1)
    p_retry.add_argument("--deadline", type=float)
    p_trace = sub.add_parser("trace")
    p_trace.add_argument("--agent", required=True)
    p_trace.add_argument("--run-id", required=True)
    p_trace.add_argument("--trace-id")
    p_trace.add_argument("--limit", type=int, default=200)
    p_ready = sub.add_parser("readiness")
    p_ready.add_argument("--agent", required=True)
    p_ready.add_argument("--run-id", required=True)
    p_ready.add_argument("--state", choices=("registered","ready","started","observed","stopped","error"), required=True)
    p_ready.add_argument("--native-handle", required=True)
    p_ready.add_argument("--model", required=True)
    p_ready.add_argument("--expected-version", type=int, required=True)

    p_wait = sub.add_parser("wait", help="wait for post-commit board wakeup via kqueue")
    p_wait.add_argument("--timeout", type=float, default=30.0)
    p_wait.add_argument("--agent", default=None)
    p_wait.add_argument("--since-id", dest="since_id", default=None)
    p_wait.add_argument("--hook-rewake", action="store_true", help="exit 2 with stderr notification when new mail arrives")

    p_tdep = sub.add_parser("trace-deposit", help="deposit an expiring stigmergic discovery trace")
    p_tdep.add_argument("--agent", required=True)
    p_tdep.add_argument("--locus", required=True)
    p_tdep.add_argument("--kind", default="hotspot")
    p_tdep.add_argument("--strength", type=float, default=1.0)
    p_tdep.add_argument("--half-life", dest="half_life", type=float, default=600.0)
    p_tdep.add_argument("--ttl", type=float, default=1800.0)
    p_tdep.add_argument("--note", default="")

    p_tsense = sub.add_parser("trace-sense", help="sense active stigmergic traces with half-life decay")
    p_tsense.add_argument("--locus-prefix", dest="locus_prefix", default="")
    p_tsense.add_argument("--kind", default=None)
    p_tsense.add_argument("--min-strength", dest="min_strength", type=float, default=0.05)
    p_tsense.add_argument("--limit", type=int, default=50)

    return ap


def main(argv: Optional[list[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as ex:
        # argparse already printed the error; still emit JSON so callers that
        # always parse stdout don't hang on a human usage string.
        code = ex.code if isinstance(ex.code, int) else 2
        if code != 0:
            _print_json({"ok": False, "error": "invalid arguments"})
        return code if isinstance(code, int) else 2

    try:
        with HiveBoard(args.db or default_board_db()) as board:
            cmd = args.cmd
            if cmd == "init":
                _print_json({"ok": True, "db": board.path, "channels": DEFAULT_CHANNELS})
            elif cmd == "register":
                rec = board.register({
                    "id": args.id,
                    "display_name": args.display_name or args.id,
                    "role": args.role,
                    "provider": args.provider,
                    "model": args.model,
                    "parent_id": args.parent_id or None,
                    "lineage": args.lineage,
                    "depth": args.depth,
                    "spawn_budget": args.spawn_budget,
                    "max_depth": args.max_depth,
                    "status": args.status,
                }, replace=args.replace)
                _print_json(rec)
            elif cmd == "heartbeat":
                _print_json(board.heartbeat(args.agent, status=args.status))
            elif cmd == "send-message":
                _print_json(board.send_message(args.from_agent, args.to_agent, _load_json(args.envelope_file), args.idempotency_key, args.priority))
            elif cmd == "consume-delivery":
                _print_json(board.consume_delivery(args.agent, args.message_id, args.lease_token, args.effect_key, _load_json(args.result_file)))
            elif cmd == "dead-letters":
                _print_json(board.dead_letters(args.agent))
            elif cmd == "retry-delivery":
                _print_json(board.retry_delivery(args.agent, args.message_id, args.extra_attempts, args.deadline))
            elif cmd == "trace":
                _print_json(board.trace(args.agent, args.run_id, args.trace_id, args.limit))
            elif cmd == "readiness":
                _print_json(board.readiness(args.agent, args.run_id, args.state, args.native_handle, args.model, args.expected_version))
            elif cmd == "post":
                _print_json(board.post(
                    from_agent=args.from_agent,
                    body=Path(args.body_file).read_text(encoding="utf-8") if args.body_file else args.body,
                    channel=args.channel,
                    thread_id=_int_or_none(args.thread_id),
                    mentions=args.mentions or None,
                    to_agent=args.to_agent,
                    kind=args.kind,
                    task_id=args.task_id, priority=args.priority,
                    payload=_load_json(args.payload_file) if args.payload_file else None,
                    idempotency_key=args.idempotency_key,
                ))
            elif cmd == "dm":
                _print_json(board.dm(args.from_agent, args.to_agent,
                    Path(args.body_file).read_text(encoding="utf-8") if args.body_file else args.body,
                    kind=args.kind, task_id=args.task_id, priority=args.priority,
                    payload=_load_json(args.payload_file) if args.payload_file else None,
                    idempotency_key=args.idempotency_key))
            elif cmd == "reply":
                _print_json(board.reply(args.from_agent, args.message_id, args.body,
                                         kind=args.kind, priority=args.priority,
                                         idempotency_key=args.idempotency_key))
            elif cmd == "poll":
                _print_json(board.poll(
                    args.agent,
                    since_id=_int_or_none(args.since_id),
                    limit=args.limit,
                ))
            elif cmd == "mentions":
                _print_json(board.mentions(
                    args.agent, since_id=_int_or_none(args.since_id)
                ))
            elif cmd == "agents":
                _print_json(board.agents())
            elif cmd == "tree":
                _print_json(board.tree())
            elif cmd == "channels":
                _print_json(board.channels())
            elif cmd == "join":
                _print_json(board.join(args.channel, args.agent))
            elif cmd == "ack":
                _print_json(board.ack(args.agent, args.message_id, args.lease_token))
            elif cmd == "inbox":
                _print_json(board.inbox(args.agent, args.limit, args.task_id))
            elif cmd == "claim":
                _print_json(board.claim(args.agent, args.limit, args.lease_seconds))
            elif cmd == "nack":
                _print_json(board.nack(args.agent, args.message_id, args.lease_token))
            elif cmd == "receipts":
                _print_json(board.receipts(args.message_id))
            elif cmd == "stats":
                _print_json(board.stats())
            elif cmd == "reserve":
                _print_json(board.reserve(args.agent, args.workspace, args.paths, args.lease_seconds, args.task_id))
            elif cmd == "release":
                _print_json(board.release(args.agent, args.workspace, args.paths, args.task_id))
            elif cmd == "reservations":
                _print_json(board.reservations())
            elif cmd == "context-get":
                _print_json(board.context_get(args.namespace, args.key))
            elif cmd == "context-set":
                _print_json(board.context_set(args.agent, args.namespace, args.key,
                    _load_json(args.value_file), args.expected_version))
            elif cmd == "create-task":
                _print_json(board.create_task(args.agent, _load_json(args.brief_file)))
            elif cmd == "tasks":
                _print_json(board.tasks(args.task_id))
            elif cmd == "start-task":
                _print_json(board.start_task(args.agent, args.task_id, args.lease_seconds))
            elif cmd == "submit-task":
                _print_json(board.submit_task(args.agent, _load_json(args.report_file)))
            elif cmd == "review-task":
                _print_json(board.review_task(args.agent, args.task_id, args.attempt, args.decision, args.note))
            elif cmd == "recover-task":
                _print_json(board.recover_task(args.agent, args.task_id, args.attempt, args.note))
            elif cmd == "task-history":
                _print_json(board.task_history(args.task_id))
            elif cmd == "wait":
                outcome = board.wait(
                    timeout=args.timeout,
                    agent_id=args.agent,
                    since_id=_int_or_none(args.since_id),
                )
                _print_json(outcome)
                if args.hook_rewake and outcome.get("changed") and outcome.get("messages"):
                    feed = board.format_feed(outcome["messages"])
                    print(f"[fusion:board:rewake] {len(outcome['messages'])} new message(s):\n{feed}", file=sys.stderr)
                    return 2
            elif cmd == "trace-deposit":
                _print_json(board.deposit_trace(
                    args.agent,
                    args.locus,
                    kind=args.kind,
                    strength=args.strength,
                    half_life=args.half_life,
                    ttl=args.ttl,
                    note=args.note,
                ))
            elif cmd == "trace-sense":
                _print_json(board.sense_traces(
                    args.locus_prefix,
                    kind=args.kind,
                    min_strength=args.min_strength,
                    limit=args.limit,
                ))
            else:
                raise BoardError(f"unknown command: {cmd}")
        return 0
    except BoardError as ex:
        _print_json({"ok": False, "error": str(ex)})
        return 1
    except Exception as ex:  # pragma: no cover - last-resort CLI guard
        _print_json({"ok": False, "error": f"{type(ex).__name__}: {ex}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
