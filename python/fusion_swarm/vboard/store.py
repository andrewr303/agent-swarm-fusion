"""BoardStore: vercel-board-compatible local board over SQLite (design/v4-spec.md section 1, judge J-03/J-15/J-17).

Design in one paragraph: an append-only `events` table is the authority for the feed and cursors; materialised
tables (messages, labels, tasks, ...) are updated in the SAME transaction that appends the event. Every write is
`BEGIN IMMEDIATE`, re-checks its preconditions inside the transaction (conditional append), and either commits all
of its events and rows or none. Task state transitions go through the pure `fold_task` function so a stale event can
never move a task. Lease expiry is lazy (a lapsed claim reads as `ready` before any event) and swept (`sweep()`).

Concurrency: one sqlite connection per thread, WAL, `busy_timeout=5000`; an in-process RLock serialises writers of one
BoardStore, sqlite's write lock serialises BoardStores/processes on one file. Waiters poll with jitter.

Stdlib only.
"""
from __future__ import annotations

import builtins
import difflib
import hashlib
import hmac
import json
import os
import random
import re as _re
import secrets
import sqlite3
import threading
import time
import weakref
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping, Sequence

from . import model as M
from .model import Caller, VBoardError

SCHEMA_VERSION = 1

_SET = builtins.set
_ALL = builtins.all

_DDL: tuple[str, ...] = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE events (idx INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, created_ms INTEGER NOT NULL,"
    " channel TEXT, message_id TEXT, thread_id TEXT, actor TEXT, payload TEXT NOT NULL,"
    " hidden INTEGER NOT NULL DEFAULT 0, lbl_before TEXT, lbl_after TEXT, tsnap TEXT)",
    "CREATE INDEX events_channel ON events(channel, idx)",
    "CREATE INDEX events_thread ON events(thread_id, idx)",
    "CREATE TABLE channels (path TEXT PRIMARY KEY, created_idx INTEGER NOT NULL, last_idx INTEGER NOT NULL,"
    " messages INTEGER NOT NULL DEFAULT 0, threads INTEGER NOT NULL DEFAULT 0, private INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE actors (id TEXT PRIMARY KEY, principal TEXT NOT NULL, profile TEXT, kind TEXT NOT NULL,"
    " handle TEXT NOT NULL, name TEXT NOT NULL, avatar TEXT, first_idx INTEGER NOT NULL,"
    " last_seen_ms INTEGER NOT NULL)",
    "CREATE TABLE messages (id TEXT PRIMARY KEY, idx INTEGER NOT NULL UNIQUE, channel TEXT NOT NULL,"
    " root_id TEXT NOT NULL, reply_to TEXT, actor TEXT NOT NULL, principal TEXT NOT NULL, profile TEXT,"
    " text TEXT NOT NULL, created_ms INTEGER NOT NULL, edited_ms INTEGER, deleted INTEGER NOT NULL DEFAULT 0,"
    " version INTEGER NOT NULL, revision INTEGER NOT NULL, no_sync INTEGER NOT NULL DEFAULT 0,"
    " last_activity_idx INTEGER NOT NULL, replies INTEGER NOT NULL DEFAULT 0, last_reply_id TEXT)",
    "CREATE INDEX messages_root ON messages(root_id, idx)",
    "CREATE INDEX messages_channel_activity ON messages(channel, last_activity_idx) WHERE id = root_id",
    "CREATE TABLE labels (message_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,"
    " PRIMARY KEY (message_id, key))",
    "CREATE INDEX labels_kv ON labels(key, value)",
    "CREATE TABLE reactions (message_id TEXT NOT NULL, emoji TEXT NOT NULL, actor TEXT NOT NULL,"
    " idx INTEGER NOT NULL, PRIMARY KEY (message_id, emoji, actor))",
    "CREATE TABLE mentions (message_id TEXT NOT NULL, actor TEXT NOT NULL, PRIMARY KEY (message_id, actor))",
    "CREATE TABLE tasks (id TEXT PRIMARY KEY, channel TEXT NOT NULL, state TEXT NOT NULL, owner TEXT NOT NULL,"
    " by_actor TEXT NOT NULL, for_actor TEXT, epoch INTEGER NOT NULL DEFAULT 0, claim_actor TEXT,"
    " claim_until_ms INTEGER, claim_token_hash TEXT, blocked_kind TEXT, blocked_reason TEXT, result TEXT,"
    " reason TEXT, created_idx INTEGER NOT NULL, updated_idx INTEGER NOT NULL, settled_kind TEXT)",
    "CREATE INDEX tasks_queue ON tasks(channel, state, id)",
    "CREATE INDEX tasks_claim_until ON tasks(claim_until_ms) WHERE state = 'claimed'",
    "CREATE TABLE task_needs (task_id TEXT NOT NULL, need_id TEXT NOT NULL, ord INTEGER NOT NULL,"
    " cond TEXT NOT NULL DEFAULT 'completed', PRIMARY KEY (task_id, need_id))",
    "CREATE INDEX task_needs_need ON task_needs(need_id)",
    "CREATE TABLE idempotency (principal TEXT NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL,"
    " result TEXT NOT NULL, created_ms INTEGER NOT NULL, PRIMARY KEY (principal, scope, key))",
    "CREATE INDEX idempotency_key ON idempotency(key)",
)


def _create_schema(conn: sqlite3.Connection, name: str | None, created_ms: int, lease_cap_s: int) -> None:
    """Run the one-time DDL. Called inside BEGIN IMMEDIATE, only when user_version == 0."""
    for stmt in _DDL:
        conn.execute(stmt)
    conn.execute("INSERT INTO meta(key,value) VALUES('board_name', ?)", (name or "",))
    conn.execute("INSERT INTO meta(key,value) VALUES('created_ms', ?)", (str(created_ms),))
    conn.execute("INSERT INTO meta(key,value) VALUES('lease_cap_s', ?)", (str(lease_cap_s),))
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


# ------------------------------------------------------------------------------------------------ pure helpers
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(obj: Any) -> str:
    return hashlib.sha256(_canon(obj).encode("utf-8")).hexdigest()


_SELF = object()   # sentinel: "the idx of this very event"


def fold_task(task: dict | None, etype: str, payload: Mapping[str, Any], *, is_root: bool = True,
              deleted: bool = False) -> dict | None:
    """Pure task state machine (b5 item 73). Returns a NEW dict, or the SAME object when the event does not
    apply to the current state (stale, duplicate, wrong epoch) so callers can detect a no-op with `is`.

    `task` keys: state, owner, by, for, needs, needs_if, epoch, claim {actor, until_ms, token_hash}|None,
    blocked {kind, reason}|None, result, reason, settled.
    """
    if etype == "task.created":
        if task is not None or not is_root or deleted:
            return task
        needs = list(payload.get("needs") or [])
        return {
            "state": "waiting" if needs else "ready", "owner": payload["owner"], "by": payload["by"],
            "for": payload.get("for"), "needs": needs, "needs_if": dict(payload.get("needs_if") or {}),
            "epoch": 0, "claim": None, "blocked": None, "result": None, "reason": None, "settled": None,
        }
    if task is None:
        return None
    st = task["state"]
    epoch = payload.get("epoch")

    def cp(**changes: Any) -> dict:
        new = dict(task)
        new["needs"] = list(task["needs"])
        new["needs_if"] = dict(task.get("needs_if") or {})
        new.update(changes)
        return new

    if etype == "task.assigned":
        if task.get("for") == payload.get("for"):
            return task
        return cp(**{"for": payload.get("for")})
    if etype == "task.needs_changed":
        if st not in ("waiting", "ready", "blocked"):
            return task
        needs = list(payload.get("needs") or [])
        if st == "blocked":
            state = "blocked"
        else:
            state = payload.get("state") or ("waiting" if needs else "ready")
            if state not in ("waiting", "ready"):
                return task
        return cp(needs=needs, needs_if=dict(payload.get("needs_if") or {}), state=state)
    if etype == "task.ready":
        return cp(state="ready") if st == "waiting" else task
    if etype == "task.claimed":
        if st != "ready" or epoch != task["epoch"] + 1:
            return task
        return cp(state="claimed", epoch=epoch, settled=None, blocked=None,
                  claim={"actor": payload["actor"], "until_ms": payload["until_ms"],
                         "token_hash": payload["token_hash"]})
    if etype in ("task.renewed", "task.released", "task.expired", "task.completed", "task.failed"):
        if st != "claimed" or epoch != task["epoch"]:
            return task
        if etype == "task.renewed":
            claim = dict(task["claim"] or {})
            claim["until_ms"] = payload["until_ms"]
            return cp(claim=claim)
        if etype == "task.released":
            return cp(state="ready", claim=None, settled="released")
        if etype == "task.expired":
            return cp(state="ready", claim=None, settled="expired")
        if etype == "task.completed":
            return cp(state="completed", claim=None, settled="completed", result=payload.get("result"),
                      reason=None)
        return cp(state="failed", claim=None, settled="failed", result=None, reason=payload.get("reason"))
    if etype == "task.blocked":
        if st not in ("waiting", "ready", "claimed"):
            return task
        return cp(state="blocked", claim=None, settled="blocked",
                  blocked={"kind": payload.get("kind"), "reason": payload.get("reason")})
    if etype == "task.unblocked":
        if st != "blocked":
            return task
        state = payload.get("state") or ("waiting" if task["needs"] else "ready")
        return cp(state=state, blocked=None)
    if etype == "task.canceled":
        if st in ("completed", "canceled"):
            return task
        return cp(state="canceled", claim=None, blocked=None, settled="canceled", reason=payload.get("reason"),
                  result=None)
    if etype == "task.reopened":
        if st != "failed":
            return task
        return cp(state="ready", reason=None, settled=None)
    return task


class _Tx:
    __slots__ = ("conn", "now_ms", "seen", "readied", "canceled", "expired")

    def __init__(self, conn: sqlite3.Connection, now_ms: int):
        self.conn = conn
        self.now_ms = now_ms
        self.seen: set[str] = set()
        self.readied: list[str] = []
        self.canceled: list[str] = []
        self.expired: list[str] = []


_CODE_SPAN = _re.compile(r"```.*?```|`[^`\n]*`", _re.S)
_MENTION = _re.compile(r"(?<![A-Za-z0-9_@])@([A-Za-z0-9][A-Za-z0-9_-]{0,63}(?:/[A-Za-z0-9][A-Za-z0-9_-]{0,31})?)")
_NUM = _re.compile(r"^\d{1,18}$")
_TOKEN = _re.compile(M.TOKEN_RE)
_ASSIGN = _re.compile(M.ASSIGN_PROFILE_RE)
_BLOCK_KIND = _re.compile(M.BLOCK_KIND_RE)


class BoardStore:
    """Local vercel-board-compatible store. All public methods take a `Caller` first (except head/sweep/close)."""

    def __init__(self, path: str | os.PathLike, *, name: str | None = None,
                 clock: Callable[[], float] = time.time, lease_default_s: int = 900, lease_cap_s: int = 3600,
                 monotonic: Callable[[], float] = time.monotonic):
        self.path = os.fspath(path)
        self.name = name
        self._clock = clock
        self._mono = monotonic
        self.lease_default_s = int(lease_default_s)
        self.lease_cap_s = int(lease_cap_s)
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        self._tls = threading.local()
        self._conns: list[tuple[weakref.ref, sqlite3.Connection]] = []
        self._conns_lock = threading.Lock()
        self._wlock = threading.RLock()
        self._cond = threading.Condition(threading.Lock())
        self._gen = 0
        self._closed = False
        try:
            self._init_schema()
        except BaseException:
            self.close()
            raise

    # ------------------------------------------------------------------------------------------ connections
    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _conn(self) -> sqlite3.Connection:
        if self._closed:
            raise VBoardError("unavailable", "the board store is closed")
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = self._open()
            self._tls.conn = conn
            with self._conns_lock:
                self._prune_dead_threads()
                self._conns.append((weakref.ref(threading.current_thread()), conn))
        return conn

    def _prune_dead_threads(self) -> None:
        """Close the connections of threads that have ended (one-thread-per-request servers would leak them)."""
        keep = []
        for ref, conn in self._conns:
            thread = ref()
            if thread is None or not thread.is_alive():
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            else:
                keep.append((ref, conn))
        self._conns = keep

    def _init_schema(self) -> None:
        conn = self._conn()
        for attempt in range(200):
            try:
                mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                break
            except sqlite3.OperationalError:
                time.sleep(0.01)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self._begin_immediate(conn)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    _create_schema(conn, self.name, int(self._clock() * 1000), self.lease_cap_s)
                    version = SCHEMA_VERSION
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        if version != SCHEMA_VERSION:
            raise VBoardError("internal", f"board file has schema version {version}; this build reads "
                                          f"version {SCHEMA_VERSION}")
        if self.name is None:
            row = conn.execute("SELECT value FROM meta WHERE key='board_name'").fetchone()
            if row and row[0]:
                self.name = row[0]

    @staticmethod
    def _begin_immediate(conn: sqlite3.Connection) -> None:
        deadline = time.monotonic() + 30.0
        delay = 0.002
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise
                if time.monotonic() > deadline:
                    raise VBoardError("unavailable", "the board is busy; try again") from exc
                time.sleep(delay)
                delay = min(delay * 2, 0.05)

    def close(self) -> None:
        self._closed = True
        with self._conns_lock:
            conns, self._conns = [c for _, c in self._conns], []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._tls = threading.local()
        self._notify()

    def __enter__(self) -> "BoardStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------------------------------ transactions
    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    @contextmanager
    def _write(self, now_ms: int | None = None) -> Iterator[_Tx]:
        tls = self._tls
        if getattr(tls, "wdepth", 0):
            tls.wdepth += 1
            try:
                yield tls.tx
            finally:
                tls.wdepth -= 1
            return
        with self._wlock:
            conn = self._conn()
            self._begin_immediate(conn)
            tx = _Tx(conn, self._now_ms() if now_ms is None else now_ms)
            tls.tx, tls.wdepth = tx, 1
            try:
                yield tx
            except BaseException:
                tls.wdepth = 0
                tls.tx = None
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            tls.wdepth = 0
            tls.tx = None
            try:
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        self._notify()

    @contextmanager
    def _read(self) -> Iterator[tuple[sqlite3.Connection, int]]:
        tls = self._tls
        if getattr(tls, "wdepth", 0):
            yield tls.tx.conn, tls.tx.now_ms
            return
        if getattr(tls, "rdepth", 0):
            tls.rdepth += 1
            try:
                yield tls.rconn, tls.rnow
            finally:
                tls.rdepth -= 1
            return
        conn = self._conn()
        conn.execute("BEGIN")
        tls.rdepth, tls.rconn, tls.rnow = 1, conn, self._now_ms()
        try:
            yield conn, tls.rnow
        finally:
            tls.rdepth = 0
            tls.rconn = None
            try:
                conn.execute("COMMIT")
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass

    def _notify(self) -> None:
        with self._cond:
            self._gen += 1
            self._cond.notify_all()

    def _park(self, deadline: float, gen0: int, started: float) -> None:
        """Block until a commit in this process, or one poll interval (cross-process wakeups)."""
        now = self._mono()
        interval = 0.05 if (now - started) < 1.0 else 0.25 * (0.8 + 0.4 * random.random())
        timeout = max(0.0, min(deadline - now, interval))
        with self._cond:
            if self._gen == gen0 and timeout > 0:
                self._cond.wait(timeout)

    # ------------------------------------------------------------------------------------------ small helpers
    @staticmethod
    def _perm(caller: Caller, perm: str) -> None:
        if caller.can is not None and perm not in caller.can:
            raise VBoardError("forbidden", f"you don't have {perm} permission here")

    @staticmethod
    def _nid(value: Any, code: str = "message_not_found") -> str:
        text = str(value if value is not None else "").strip()
        if text.startswith("#"):
            text = text[1:]
        if not _NUM.match(text):
            raise VBoardError(code, f"no message {str(value)[:40]!r}" if code == "message_not_found"
                              else f"{str(value)[:40]!r} isn't a message id or cursor")
        return str(int(text))

    @staticmethod
    def _cursor(value: Any) -> int:
        text = str(value if value is not None else "").strip()
        if text.startswith("#"):
            text = text[1:]
        if not _NUM.match(text):
            raise VBoardError("invalid_cursor", f"{str(value)[:40]!r} isn't a cursor or message id")
        return int(text)

    @staticmethod
    def _text(text: Any, *, field: str = "text") -> str:
        if not isinstance(text, str) or not text.strip():
            raise VBoardError("invalid_request", f"{field}: say something (text can't be empty)")
        if len(text) > M.TEXT_MAX:
            raise VBoardError("text_too_long", f"{field}: a message is at most {M.TEXT_MAX} characters")
        return text

    @staticmethod
    def _labels_in(labels: Mapping[str, Any] | None) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, value in (labels or {}).items():
            if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise VBoardError("invalid_request", f"label {str(key)[:40]}: a label value is text")
            value = str(value)
            M.label_ok(key, value)
            out[key] = value
        return out

    @staticmethod
    def _client_id(cid: Any) -> str | None:
        if cid is None:
            return None
        if not isinstance(cid, str) or not cid or len(cid) > M.CLIENT_ID_MAX:
            raise VBoardError("invalid_request", f"id: 1 to {M.CLIENT_ID_MAX} characters")
        return cid

    def _lease(self, lease: Any) -> int:
        return M.lease_ok(lease, default_s=self.lease_default_s, cap_s=self.lease_cap_s)

    @staticmethod
    def _token_in(token: Any) -> str | None:
        if token is None:
            return None
        if not isinstance(token, str) or not _TOKEN.match(token):
            raise VBoardError("invalid_request", "token: 16 to 128 characters of letters, digits, - and _")
        return token

    @staticmethod
    def _note(value: Any, field: str, *, required: bool) -> str | None:
        if value is None:
            if required:
                raise VBoardError("invalid_request", f"{field}: say why (it can't be empty)")
            return None
        if not isinstance(value, str) or (required and not value.strip()):
            raise VBoardError("invalid_request", f"{field}: say why (it can't be empty)" if required
                              else f"{field} is text")
        if len(value) > M.TASK_NOTE_MAX:
            raise VBoardError("invalid_request", f"{field}: at most {M.TASK_NOTE_MAX} characters")
        return value

    @staticmethod
    def _limit(limit: Any, default: int) -> int:
        if limit is None:
            return default
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > M.READ_LIMIT_MAX:
            raise VBoardError("invalid_request", f"limit: 1 to {M.READ_LIMIT_MAX}")
        return limit

    # ------------------------------------------------------------------------------------------ row access
    @staticmethod
    def _msg(conn: sqlite3.Connection, mid: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()

    def _msg_or_404(self, conn: sqlite3.Connection, value: Any) -> sqlite3.Row:
        mid = self._nid(value)
        row = self._msg(conn, mid)
        if row is None:
            raise VBoardError("message_not_found", f"no message {mid}")
        return row

    @staticmethod
    def _task(conn: sqlite3.Connection, tid: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()

    @staticmethod
    def _head(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _eff(row: sqlite3.Row, now_ms: int) -> str:
        if row["state"] == "claimed" and row["claim_until_ms"] is not None and row["claim_until_ms"] <= now_ms:
            return "ready"
        return row["state"]

    def _author(self, conn: sqlite3.Connection, actor_id: str, cache: dict | None = None) -> dict:
        if cache is not None and actor_id in cache:
            return cache[actor_id]
        row = conn.execute("SELECT * FROM actors WHERE id=?", (actor_id,)).fetchone()
        if row is None:
            principal, _, profile = actor_id.partition("+")
            user = principal.rsplit(":", 1)[-1]
            handle = user if not profile else f"{user}/{profile}"
            view = M.actor_view({"id": actor_id, "principal": principal, "profile": profile or None,
                                 "kind": "agent" if profile else "user", "handle": handle, "name": handle,
                                 "avatar": None})
        else:
            view = M.actor_view(row)
        if cache is not None:
            cache[actor_id] = view
        return view

    # ------------------------------------------------------------------------------------------ views
    def _task_view(self, conn: sqlite3.Connection, row: sqlite3.Row, now_ms: int, *, effective: bool = True) -> dict:
        state = self._eff(row, now_ms) if effective else row["state"]
        needs_rows = conn.execute("SELECT need_id, cond FROM task_needs WHERE task_id=? ORDER BY ord",
                                  (row["id"],)).fetchall()
        needs = [r["need_id"] for r in needs_rows]
        needs_if = {r["need_id"]: r["cond"] for r in needs_rows if r["cond"] != "completed"}
        claim = None
        if state == "claimed":
            claim = {"actor": row["claim_actor"], "until": M.iso(row["claim_until_ms"])}
        blocked = None
        if state == "blocked":
            blocked = {"kind": row["blocked_kind"], "reason": row["blocked_reason"]}
        return M.task_view(
            state=state, owner=row["owner"], by=row["by_actor"], for_=row["for_actor"], needs=needs,
            epoch=row["epoch"], claim=claim, blocked=blocked,
            result=row["result"] if state == "completed" else None,
            reason=row["reason"] if state in ("failed", "canceled") else None, needs_if=needs_if or None)

    def _msg_view(self, conn: sqlite3.Connection, row: sqlite3.Row, now_ms: int,
                  cache: dict | None = None) -> dict:
        mid = row["id"]
        labels = {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM labels WHERE message_id=?",
                                                              (mid,))}
        reactions: dict[str, list[str]] = {}
        for r in conn.execute("SELECT emoji, actor FROM reactions WHERE message_id=? ORDER BY idx", (mid,)):
            reactions.setdefault(r["emoji"], []).append(r["actor"])
        task = None
        if row["root_id"] == mid:
            trow = self._task(conn, mid)
            if trow is not None:
                task = self._task_view(conn, trow, now_ms)
                labels["board.task"] = task["state"]
        return M.message_view(row, labels, reactions, task, self._author(conn, row["actor"], cache))

    def _view_id(self, conn: sqlite3.Connection, mid: str, now_ms: int, cache: dict | None = None) -> dict:
        row = self._msg(conn, mid)
        if row is None:  # pragma: no cover - guarded by callers
            raise VBoardError("message_not_found", f"no message {mid}")
        return self._msg_view(conn, row, now_ms, cache)

    # ------------------------------------------------------------------------------------------ events
    def _append(self, tx: _Tx, etype: str, payload: Mapping[str, Any], *, channel: str | None = None,
                message_id: Any = None, thread_id: Any = None, actor: str | None = None,
                hidden: bool | None = None, lbl_before: list[str] | None = None, tsnap: dict | None = None,
                idx: int | None = None) -> int:
        conn = tx.conn
        hid = 1 if (etype in M.HIDDEN_EVENTS if hidden is None else hidden) else 0
        cols = "type, created_ms, channel, message_id, thread_id, actor, payload, hidden, lbl_before, tsnap"
        mid = None if message_id is _SELF else message_id
        tid = None if thread_id is _SELF else thread_id
        vals = [etype, tx.now_ms, channel, mid, tid, actor, json.dumps(dict(payload), ensure_ascii=False), hid,
                json.dumps(lbl_before) if lbl_before is not None else None,
                json.dumps(tsnap, ensure_ascii=False) if tsnap is not None else None]
        if idx is not None:
            cur = conn.execute(f"INSERT INTO events(idx, {cols}) VALUES(?,?,?,?,?,?,?,?,?,?,?)", [idx, *vals])
            new_idx = idx
        else:
            cur = conn.execute(f"INSERT INTO events({cols}) VALUES(?,?,?,?,?,?,?,?,?,?)", vals)
            new_idx = int(cur.lastrowid)
        if message_id is _SELF:
            mid = str(new_idx)
        if thread_id is _SELF:
            tid = str(new_idx)
        if mid is not None or tid is not None:
            conn.execute("UPDATE events SET message_id=?, thread_id=? WHERE idx=?", (mid, tid, new_idx))
        if mid is not None and etype != "message.posted":
            conn.execute("UPDATE messages SET version=? WHERE id=?", (new_idx, mid))
        if tid is not None and not hid:
            conn.execute("UPDATE messages SET last_activity_idx=? WHERE id=?", (new_idx, tid))
        if channel is not None and not hid:
            conn.execute("UPDATE channels SET last_idx=? WHERE path=?", (new_idx, channel))
        if mid is not None:
            conn.execute("UPDATE events SET lbl_after=? WHERE idx=?",
                         (json.dumps(self._flabels(tx, mid)), new_idx))
        return new_idx

    def _flabels(self, tx: _Tx, mid: str) -> list[str]:
        """'k=v' pairs of a message and its thread root (filter semantics), with lazy task-state override."""
        conn = tx.conn
        row = conn.execute("SELECT root_id, deleted FROM messages WHERE id=?", (mid,)).fetchone()
        if row is None or row["deleted"]:
            ids = (mid,)
        else:
            ids = (mid, row["root_id"]) if row["root_id"] != mid else (mid,)
        pairs: list[str] = []
        for i in ids:
            for r in conn.execute("SELECT key, value FROM labels WHERE message_id=?", (i,)):
                pair = f"{r['key']}={r['value']}"
                if r["key"] == "board.task":
                    trow = self._task(conn, i)
                    if trow is not None:
                        pair = f"board.task={self._eff(trow, tx.now_ms)}"
                if pair not in pairs:
                    pairs.append(pair)
        return pairs

    def _ensure_actor(self, tx: _Tx, caller: Caller) -> None:
        if caller.actor in tx.seen:
            return
        tx.seen.add(caller.actor)
        conn = tx.conn
        if conn.execute("SELECT 1 FROM actors WHERE id=?", (caller.actor,)).fetchone():
            return
        idx = self._append(tx, "actor.seen", {"actor": caller.actor}, actor=caller.actor, hidden=True)
        conn.execute("INSERT INTO actors(id, principal, profile, kind, handle, name, avatar, first_idx,"
                     " last_seen_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                     (caller.actor, caller.principal, caller.profile, "agent" if caller.profile else "user",
                      caller.handle, caller.handle, None, idx, tx.now_ms))

    def _ensure_channel(self, tx: _Tx, path: str, *, private: bool = False, actor: str | None = None) -> bool:
        conn = tx.conn
        if conn.execute("SELECT 1 FROM channels WHERE path=?", (path,)).fetchone():
            return False
        next_idx = self._head(conn) + 1
        conn.execute("INSERT INTO channels(path, created_idx, last_idx, private) VALUES(?,?,?,?)",
                     (path, next_idx, next_idx, 1 if private else 0))
        self._append(tx, "channel.created", {"path": path}, channel=path, actor=actor)
        return True

    # ------------------------------------------------------------------------------------------ idempotency
    @staticmethod
    def _idem_lookup(conn: sqlite3.Connection, caller: Caller, scope: str, key: str, digest: str) -> dict | None:
        row = conn.execute("SELECT principal, scope, digest, result FROM idempotency WHERE key=? LIMIT 1",
                           (key,)).fetchone()
        if row is None:
            return None
        if row["principal"] != caller.principal or row["scope"] != scope or row["digest"] != digest:
            raise VBoardError("duplicate_id", "that id was already used for a different write")
        return json.loads(row["result"])

    @staticmethod
    def _idem_store(tx: _Tx, caller: Caller, scope: str, key: str, digest: str, result: Mapping[str, Any]) -> None:
        tx.conn.execute("INSERT INTO idempotency(principal, scope, key, digest, result, created_ms)"
                        " VALUES(?,?,?,?,?,?)",
                        (caller.principal, scope, key, digest, json.dumps(dict(result)), tx.now_ms))

    # ------------------------------------------------------------------------------------------ mentions
    def _resolve_mentions(self, conn: sqlite3.Connection, text: str) -> list[str]:
        stripped = _CODE_SPAN.sub(" ", text)
        found: list[str] = []
        for m in _MENTION.finditer(stripped):
            handle = m.group(1).lower()
            rows = conn.execute("SELECT id FROM actors WHERE lower(handle)=?", (handle,)).fetchall()
            if len(rows) == 1 and rows[0]["id"] not in found:
                found.append(rows[0]["id"])
        return found

    def _store_mentions(self, conn: sqlite3.Connection, mid: str, text: str) -> list[str]:
        conn.execute("DELETE FROM mentions WHERE message_id=?", (mid,))
        ids = self._resolve_mentions(conn, text)
        for actor in ids:
            conn.execute("INSERT OR IGNORE INTO mentions(message_id, actor) VALUES(?,?)", (mid, actor))
        return ids

    # ------------------------------------------------------------------------------------------ identity / board
    def me(self, caller: Caller) -> dict:
        can = [p for p in M.PERMISSIONS if caller.can is None or p in caller.can]
        return {"actor": caller.actor, "principal": caller.principal,
                "kind": "agent" if caller.profile else "user", "handle": caller.handle, "name": caller.handle,
                "avatar": None, "profile": caller.profile, "can": can,
                "board": self.name or os.path.splitext(os.path.basename(self.path))[0], "source": "local"}

    def actors(self, caller: Caller, ids: Sequence[str] = ()) -> dict:
        self._perm(caller, "read")
        with self._read() as (conn, now_ms):
            rows = conn.execute("SELECT * FROM actors ORDER BY first_idx").fetchall()
            out = [self._actor_full(r) for r in rows]
            if _ALL(a["id"] != caller.actor for a in out):
                me = self.me(caller)
                out.append({"id": caller.actor, "kind": me["kind"], "handle": me["handle"], "name": me["name"],
                            "avatar": None, "principal": caller.principal, "profile": caller.profile,
                            "via": caller.principal.rsplit(":", 1)[-1] if caller.profile else None,
                            "verified": False})
            if ids:
                wanted = _SET(ids)
                out = [a for a in out if a["id"] in wanted]
            return {"actors": out, "cursor": str(self._head(conn))}

    @staticmethod
    def _actor_full(row: sqlite3.Row) -> dict:
        view = M.actor_view(row)
        view["principal"] = row["principal"]
        view["profile"] = row["profile"]
        return view

    def channels(self, caller: Caller, *, prefix: str | None = None, after: str | None = None,
                 limit: int = 500) -> dict:
        self._perm(caller, "read")
        limit = self._limit(limit, 500)
        with self._read() as (conn, now_ms):
            rows = conn.execute("SELECT * FROM channels ORDER BY path").fetchall()
            paths = [r for r in rows if (not prefix or r["path"].startswith(prefix))
                     and (after is None or r["path"] > after)]
            page = paths[:limit]
            more = len(paths) > limit
            return {"channels": [{"path": r["path"], "lastIndex": r["last_idx"], "messages": r["messages"],
                                  "threads": r["threads"]} for r in page],
                    "cursor": str(self._head(conn)), "next": page[-1]["path"] if (more and page) else None}

    def create_channel(self, caller: Caller, path: str, *, private: bool = False) -> dict:
        self._perm(caller, "post")
        if not M.channel_ok(path):
            raise VBoardError("invalid_request", f"{str(path)[:60]!r} isn't a channel name: lowercase letters, "
                                                 "digits, - and _, joined by /")
        with self._write() as tx:
            self._ensure_actor(tx, caller)
            created = self._ensure_channel(tx, path, private=private, actor=caller.actor)
            row = tx.conn.execute("SELECT * FROM channels WHERE path=?", (path,)).fetchone()
            return {"channel": {"path": row["path"], "lastIndex": row["last_idx"], "messages": row["messages"],
                                "threads": row["threads"]}, "created": created}

    def head(self) -> str:
        with self._read() as (conn, _now):
            return str(self._head(conn))

    # ------------------------------------------------------------------------------------------ channel checks
    @staticmethod
    def _chan_clause(channel: str | None, column: str = "channel") -> tuple[str, list]:
        """SQL for an exact channel or a `x/` subtree (which also matches `x` itself)."""
        if channel is None:
            return "", []
        if channel.endswith("/"):
            base = channel[:-1]
            return (f"({column} = ? OR substr({column}, 1, ?) = ?)", [base, len(channel), channel])
        return f"{column} = ?", [channel]

    def _require_channel(self, conn: sqlite3.Connection, channel: str) -> None:
        clause, params = self._chan_clause(channel, "path")
        if conn.execute(f"SELECT 1 FROM channels WHERE {clause} LIMIT 1", params).fetchone():
            return
        known = [r["path"] for r in conn.execute("SELECT path FROM channels")]
        guess = difflib.get_close_matches(channel.rstrip("/"), known, n=1, cutoff=0.6)
        raise VBoardError("channel_not_found", f"no channel named {channel!r}",
                          hint=f"did you mean {guess[0]!r}?" if guess else None)

    def _check_channel_name(self, channel: str, *, allow_tree: bool = False) -> None:
        base = channel[:-1] if (allow_tree and channel.endswith("/")) else channel
        if not isinstance(channel, str) or not M.channel_ok(base):
            raise VBoardError("invalid_request", f"{str(channel)[:60]!r} isn't a channel name: lowercase letters, "
                                                 "digits, - and _, joined by /")

    # ------------------------------------------------------------------------------------------ messages
    def post(self, caller: Caller, *, text: str, channel: str | None = None, re: str | None = None,
             labels: dict | None = None, client_id: str | None = None, after: str | None = None,
             no_sync: bool = False) -> dict:
        self._perm(caller, "post")
        text = self._text(text)
        labs = self._labels_in(labels)
        if labs:
            self._perm(caller, "label")
        cid = self._client_id(client_id)
        if re is None and channel is None:
            raise VBoardError("invalid_request", "name a channel to post in, or a message to reply to")
        if channel is not None:
            self._check_channel_name(channel)
        with self._write() as tx:
            conn = tx.conn
            root_id = reply_to = None
            if re is not None:
                target = self._msg_or_404(conn, re)
                root_id = target["root_id"]
                root = self._msg(conn, root_id)
                if channel is not None and channel != root["channel"]:
                    raise VBoardError("invalid_request", "that message is in another channel")
                channel = root["channel"]
                reply_to = target["id"] if target["id"] != root_id else None
            elif after is not None:
                raise VBoardError("invalid_request", "after only applies to a reply")
            digest = _digest({"scope": "post", "actor": caller.actor, "channel": channel, "re": root_id,
                              "replyTo": reply_to, "text": text, "labels": labs, "noSync": bool(no_sync)})
            if cid is not None:
                hit = self._idem_lookup(conn, caller, "post", cid, digest)
                if hit is not None:
                    return self._post_result(conn, hit["id"], tx.now_ms)
            if after is not None:
                self._check_after(conn, root_id, after)
            self._ensure_actor(tx, caller)
            self._ensure_channel(tx, channel, actor=caller.actor)
            idx = self._head(conn) + 1
            mid = str(idx)
            conn.execute(
                "INSERT INTO messages(id, idx, channel, root_id, reply_to, actor, principal, profile, text,"
                " created_ms, version, revision, no_sync, last_activity_idx) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, idx, channel, root_id or mid, reply_to, caller.actor, caller.principal, caller.profile, text,
                 tx.now_ms, idx, idx, 1 if no_sync else 0, idx))
            for k, v in labs.items():
                conn.execute("INSERT INTO labels(message_id, key, value) VALUES(?,?,?)", (mid, k, v))
            conn.execute("UPDATE channels SET messages=messages+1, threads=threads+? WHERE path=?",
                         (0 if root_id else 1, channel))
            if root_id:
                conn.execute("UPDATE messages SET replies=replies+1, last_reply_id=? WHERE id=?", (mid, root_id))
            mentions = self._store_mentions(conn, mid, text)
            self._append(tx, "message.posted", {"id": mid}, channel=channel, message_id=mid,
                         thread_id=root_id or mid, actor=caller.actor, idx=idx)
            if cid is not None:
                self._idem_store(tx, caller, "post", cid, digest, {"id": mid})
            return {"message": self._view_id(conn, mid, tx.now_ms), "mentions": mentions}

    def _post_result(self, conn: sqlite3.Connection, mid: str, now_ms: int) -> dict:
        mentions = [r["actor"] for r in conn.execute("SELECT actor FROM mentions WHERE message_id=?", (mid,))]
        return {"message": self._view_id(conn, mid, now_ms), "mentions": mentions}

    def _check_after(self, conn: sqlite3.Connection, root_id: str, after: Any) -> None:
        try:
            aid = self._nid(after, "invalid_after")
        except VBoardError:
            raise VBoardError("invalid_after", f"{str(after)[:40]!r} isn't a message in this thread") from None
        row = self._msg(conn, aid)
        if row is None or row["root_id"] != root_id:
            raise VBoardError("invalid_after", f"{aid} isn't a message in this thread")
        newest = conn.execute("SELECT MAX(idx) FROM messages WHERE root_id=?", (root_id,)).fetchone()[0]
        if int(aid) != int(newest):
            raise VBoardError("newer_message", f"message {newest} was posted after {aid}")

    def posted(self, caller: Caller, client_id: str) -> dict:
        self._perm(caller, "read")
        cid = self._client_id(client_id)
        with self._read() as (conn, now_ms):
            row = conn.execute("SELECT result FROM idempotency WHERE principal=? AND scope='post' AND key=?",
                               (caller.principal, cid)).fetchone()
            if row is None:
                raise VBoardError("post_not_found", f"no post with id {cid!r}")
            return {"message": self._view_id(conn, json.loads(row["result"])["id"], now_ms)}

    def thread(self, caller: Caller, message_id: str, *, after: str | None = None, limit: int | None = None,
               label: Sequence[str] = ()) -> dict:
        self._perm(caller, "read")
        flt = self._parse_labels(label)
        if limit is not None:
            limit = self._limit(limit, M.READ_LIMIT_DEFAULT)
        with self._read() as (conn, now_ms):
            row = self._msg_or_404(conn, message_id)
            root_id = row["root_id"]
            cache: dict = {}
            root = self._msg_view(conn, self._msg(conn, root_id), now_ms, cache)
            after_idx = self._cursor(after) if after is not None else None
            rows = conn.execute("SELECT * FROM messages WHERE root_id=? AND id<>? AND idx>? ORDER BY idx",
                                (root_id, root_id, after_idx or 0)).fetchall()
            views = [self._msg_view(conn, r, now_ms, cache) for r in rows]
            if flt:
                views = [v for v in views if self._labels_match(flt, self._pairs_of(v, root))]
            if limit is not None:
                views = views[:limit] if after_idx is not None else views[-limit:]
            return {"message": root, "replies": views, "cursor": str(self._head(conn)), "schedules": []}

    @staticmethod
    def _pairs_of(view: dict, root: dict | None = None) -> list[str]:
        pairs = [f"{k}={v}" for k, v in view["labels"].items()]
        if root is not None and root is not view:
            pairs += [f"{k}={v}" for k, v in root["labels"].items()]
        return pairs

    def messages(self, caller: Caller, channel: str, *, after: str | None = None, limit: int = 50) -> dict:
        self._perm(caller, "read")
        self._check_channel_name(channel)
        limit = self._limit(limit, M.READ_LIMIT_DEFAULT)
        with self._read() as (conn, now_ms):
            self._require_channel(conn, channel)
            cache: dict = {}
            head = self._head(conn)
            if after is None:
                rows = conn.execute("SELECT * FROM messages WHERE channel=? ORDER BY idx DESC LIMIT ?",
                                    (channel, limit)).fetchall()[::-1]
                cursor = head
            else:
                a = self._cursor(after)
                rows = conn.execute("SELECT * FROM messages WHERE channel=? AND idx>? ORDER BY idx LIMIT ?",
                                    (channel, a, limit + 1)).fetchall()
                if len(rows) > limit:
                    rows = rows[:limit]
                    cursor = int(rows[-1]["idx"])
                else:
                    cursor = head
            return {"messages": [self._msg_view(conn, r, now_ms, cache) for r in rows], "cursor": str(cursor)}

    def threads(self, caller: Caller, *, channel: str | None = None, re: str | None = None, by: str | None = None,
                mentions: str | None = None, label: Sequence[str] = (), source: str | None = None,
                text: str | None = None, after: str | None = None, before: str | None = None,
                limit: int = 50) -> dict:
        self._perm(caller, "read")
        limit = self._limit(limit, M.READ_LIMIT_DEFAULT)
        if after is not None and before is not None:
            raise VBoardError("invalid_request", "use after or before, not both")
        flt = self._parse_labels(label)
        if channel is not None:
            self._check_channel_name(channel, allow_tree=True)
        before_key = None
        if before is not None:
            m = _re.match(r"^(\d+)\.(\d+)$", str(before))
            if not m:
                raise VBoardError("invalid_cursor", f"{str(before)[:40]!r} isn't a page cursor")
            before_key = (int(m.group(1)), int(m.group(2)))
        after_idx = self._cursor(after) if after is not None else None
        if source is not None and source != "local":
            with self._read() as (conn, _now):
                return {"threads": [], "cursor": str(self._head(conn)), "older": None}
        with self._read() as (conn, now_ms):
            if channel is not None:
                self._require_channel(conn, channel)
            clause, params = self._chan_clause(channel)
            where = ["id = root_id"]
            args: list[Any] = []
            if clause:
                where.append(clause)
                args += params
            if re is not None:
                where.append("id = ?")
                args.append(self._nid(re))
            if after_idx is not None:
                where.append("last_activity_idx > ?")
                args.append(after_idx)
            if before_key is not None:
                where.append("(last_activity_idx < ? OR (last_activity_idx = ? AND CAST(id AS INTEGER) < ?))")
                args += [before_key[0], before_key[0], before_key[1]]
            order = "ASC" if after_idx is not None else "DESC"
            sql = (f"SELECT * FROM messages WHERE {' AND '.join(where)} "
                   f"ORDER BY last_activity_idx {order}, CAST(id AS INTEGER) {order}")
            cache: dict = {}
            picked: list[sqlite3.Row] = []
            more = False
            for row in conn.execute(sql, args):
                if not self._thread_matches(conn, row, by, mentions, flt, text, now_ms):
                    continue
                if len(picked) >= limit:
                    more = True
                    break
                picked.append(row)
            head = self._head(conn)
            out = [M.thread_summary(self._msg_view(conn, r, now_ms, cache), r["replies"],
                                    self._view_id(conn, r["last_reply_id"], now_ms, cache)
                                    if r["last_reply_id"] else None) for r in picked]
            if after_idx is not None:
                out_rows = picked
                cursor = int(out_rows[-1]["last_activity_idx"]) if (more and out_rows) else head
                return {"threads": out, "cursor": str(cursor), "older": None}
            picked.reverse()
            out.reverse()
            older = f"{picked[0]['last_activity_idx']}.{picked[0]['id']}" if (more and picked) else None
            return {"threads": out, "cursor": str(head), "older": older}

    def _thread_matches(self, conn: sqlite3.Connection, root: sqlite3.Row, by: str | None, mentions: str | None,
                        flt: list, text: str | None, now_ms: int) -> bool:
        if root["deleted"] and not conn.execute("SELECT 1 FROM messages WHERE root_id=? AND deleted=0 LIMIT 1",
                                                (root["id"],)).fetchone():
            return False
        if by is not None and not self._actor_is(conn, root["actor"], by):
            return False
        if mentions is not None and not self._mentioned(conn, root["id"], mentions, whole_thread=True):
            return False
        if flt:
            trow = self._task(conn, root["id"])
            pairs = [] if root["deleted"] else [f"{r['key']}={r['value']}" for r in
                                               conn.execute("SELECT key,value FROM labels WHERE message_id=?",
                                                            (root["id"],))]
            if trow is not None and not root["deleted"]:
                pairs = [p for p in pairs if not p.startswith("board.task=")]
                pairs.append(f"board.task={self._eff(trow, now_ms)}")
            if not self._labels_match(flt, pairs):
                return False
        if text:
            needle = text.lower()
            hit = conn.execute("SELECT 1 FROM messages WHERE root_id=? AND deleted=0 AND instr(lower(text), ?)>0"
                               " LIMIT 1", (root["id"], needle)).fetchone()
            if not hit:
                return False
        return True

    @staticmethod
    def _actor_is(conn: sqlite3.Connection, actor_id: str | None, who: str) -> bool:
        if actor_id is None:
            return False
        if actor_id == who:
            return True
        row = conn.execute("SELECT handle FROM actors WHERE id=?", (actor_id,)).fetchone()
        return bool(row and row["handle"].lower() == who.lower())

    def _mentioned(self, conn: sqlite3.Connection, mid: str, who: str, *, whole_thread: bool = False) -> bool:
        if whole_thread:
            rows = conn.execute("SELECT mn.actor FROM mentions mn JOIN messages m ON m.id=mn.message_id"
                                " WHERE m.root_id=?", (mid,)).fetchall()
        else:
            rows = conn.execute("SELECT actor FROM mentions WHERE message_id=?", (mid,)).fetchall()
        return any(self._actor_is(conn, r["actor"], who) for r in rows)

    @staticmethod
    def _parse_labels(label: Sequence[str]) -> list[tuple[str, frozenset | None]]:
        out: list[tuple[str, frozenset | None]] = []
        for item in label or ():
            if not isinstance(item, str) or not item:
                raise VBoardError("invalid_request", "label filters look like key or key=value")
            key, eq, value = item.partition("=")
            M.label_ok(key, None, allow_reserved=True)
            out.append((key, frozenset(value.split("|")) if eq else None))
        return out

    @staticmethod
    def _labels_match(flt: list[tuple[str, frozenset | None]], pairs: Sequence[str]) -> bool:
        for key, values in flt:
            ok = False
            for pair in pairs:
                k, _, v = pair.partition("=")
                if k == key and (values is None or v in values):
                    ok = True
                    break
            if not ok:
                return False
        return True

    def edit(self, caller: Caller, message_id: str, text: str, *, revision: int | None = None) -> dict:
        self._perm(caller, "post")
        text = self._text(text)
        rev = self._revision(revision)
        with self._write() as tx:
            conn = tx.conn
            row = self._msg_or_404(conn, message_id)
            self._author_check(caller, row)
            if rev is not None and rev != row["revision"]:
                raise VBoardError("revision_conflict", "the message changed since you read it")
            if row["deleted"]:
                return {"message": self._msg_view(conn, row, tx.now_ms)}
            mid = row["id"]
            trow = self._lapse_task(tx, mid) if row["root_id"] == mid else None
            self._ensure_actor(tx, caller)
            idx = self._append(tx, "message.edited", {"id": mid}, channel=row["channel"], message_id=mid,
                               thread_id=row["root_id"], actor=caller.actor)
            conn.execute("UPDATE messages SET text=?, edited_ms=?, revision=? WHERE id=?",
                         (text, tx.now_ms, idx, mid))
            self._store_mentions(conn, mid, text)
            if trow is not None and trow["state"] in ("waiting", "ready", "claimed"):
                holds = not (caller.actor == trow["by_actor"] or
                             (caller.profile is None and caller.principal == trow["owner"]))
                if holds:
                    self._task_apply(tx, trow["id"], "task.blocked",
                                     {"id": mid, "thread": mid, "kind": "edited",
                                      "reason": "the task text was edited by someone other than its owner"},
                                     actor=caller.actor)
            return {"message": self._view_id(conn, mid, tx.now_ms)}

    @staticmethod
    def _revision(revision: Any) -> int | None:
        if revision is None:
            return None
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise VBoardError("invalid_request", "revision is a number")
        return revision

    @staticmethod
    def _author_check(caller: Caller, row: sqlite3.Row) -> None:
        if caller.actor == row["actor"] or (caller.profile is None and caller.principal == row["principal"]):
            return
        raise VBoardError("forbidden", "only the author of a message can edit or delete it")

    def delete(self, caller: Caller, message_id: str, *, revision: int | None = None) -> dict:
        self._perm(caller, "post")
        rev = self._revision(revision)
        with self._write() as tx:
            conn = tx.conn
            row = self._msg_or_404(conn, message_id)
            self._author_check(caller, row)
            if rev is not None and rev != row["revision"]:
                raise VBoardError("revision_conflict", "the message changed since you read it")
            if row["deleted"]:
                return {"message": self._msg_view(conn, row, tx.now_ms)}
            mid = row["id"]
            self._ensure_actor(tx, caller)
            if row["root_id"] == mid:
                trow = self._lapse_task(tx, mid)
                if trow is not None and trow["state"] not in ("completed", "canceled"):
                    self._task_apply(tx, mid, "task.canceled", {"id": mid, "thread": mid, "reason": "deleted"},
                                     actor=caller.actor)
                    self._propagate(tx, mid)
            idx = self._append(tx, "message.deleted", {"id": mid}, channel=row["channel"], message_id=mid,
                               thread_id=row["root_id"], actor=caller.actor)
            conn.execute("UPDATE messages SET deleted=1, revision=? WHERE id=?", (idx, mid))
            conn.execute("DELETE FROM labels WHERE message_id=?", (mid,))
            conn.execute("DELETE FROM reactions WHERE message_id=?", (mid,))
            conn.execute("DELETE FROM mentions WHERE message_id=?", (mid,))
            return {"message": self._view_id(conn, mid, tx.now_ms)}

    def react(self, caller: Caller, message_id: str, emoji: str, *, remove: bool = False) -> dict:
        self._perm(caller, "react")
        name = M.emoji_ok(emoji)
        with self._write() as tx:
            conn = tx.conn
            row = self._msg_or_404(conn, message_id)
            if row["deleted"]:
                raise VBoardError("message_deleted", "that message was deleted")
            mid = row["id"]
            have = conn.execute("SELECT 1 FROM reactions WHERE message_id=? AND emoji=? AND actor=?",
                                (mid, name, caller.actor)).fetchone()
            if bool(have) == (not remove):
                return {"message": self._msg_view(conn, row, tx.now_ms)}
            self._ensure_actor(tx, caller)
            idx = self._append(tx, "reaction.removed" if remove else "reaction.added", {"emoji": name},
                               channel=row["channel"], message_id=mid, thread_id=row["root_id"],
                               actor=caller.actor)
            if remove:
                conn.execute("DELETE FROM reactions WHERE message_id=? AND emoji=? AND actor=?",
                             (mid, name, caller.actor))
            else:
                conn.execute("INSERT INTO reactions(message_id, emoji, actor, idx) VALUES(?,?,?,?)",
                             (mid, name, caller.actor, idx))
            return {"message": self._view_id(conn, mid, tx.now_ms)}

    def label(self, caller: Caller, message_id: str, *, set: dict | None = None, unset: Sequence[str] = (),
              if_: dict | None = None) -> dict:
        self._perm(caller, "label")
        to_set = self._labels_in(set)
        to_unset = []
        for key in unset or ():
            M.label_ok(key, None)
            to_unset.append(key)
        conds: dict[str, str | None] = {}
        for key, want in (if_ or {}).items():
            M.label_ok(key, None, allow_reserved=True)
            if want is not None and not isinstance(want, str):
                raise VBoardError("invalid_request", f"if {key}: a label value is text, or null for unset")
            conds[key] = want
        with self._write() as tx:
            conn = tx.conn
            row = self._msg_or_404(conn, message_id)
            if row["deleted"]:
                raise VBoardError("message_deleted", "that message was deleted")
            mid = row["id"]
            if row["root_id"] == mid:
                self._lapse_task(tx, mid)
            current = {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM labels WHERE message_id=?",
                                                                   (mid,))}
            for key, want in conds.items():
                if want == current.get(key) and (want is not None or key not in current):
                    continue
                state = "is set" if key in current else "isn't set"
                raise VBoardError("label_conflict", f"label {key} {state}: not what the write expected")
            changed = {k: v for k, v in to_set.items() if current.get(k) != v}
            removed = [k for k in to_unset if k in current]
            if not changed and not removed:
                return {"message": self._msg_view(conn, row, tx.now_ms)}
            self._ensure_actor(tx, caller)
            before = self._flabels(tx, mid)
            for k, v in changed.items():
                conn.execute("INSERT INTO labels(message_id, key, value) VALUES(?,?,?)"
                             " ON CONFLICT(message_id, key) DO UPDATE SET value=excluded.value", (mid, k, v))
            for k in removed:
                conn.execute("DELETE FROM labels WHERE message_id=? AND key=?", (mid, k))
            self._append(tx, "labels.changed", {"set": changed, "unset": removed}, channel=row["channel"],
                         message_id=mid, thread_id=row["root_id"], actor=caller.actor, lbl_before=before)
            return {"message": self._view_id(conn, mid, tx.now_ms)}

    # ------------------------------------------------------------------------------------------ event feed
    def events(self, caller: Caller, *, after: str | None = None, last: int | None = None, wait: float = 0.0,
               all: bool = False, channel: str | None = None, re: str | None = None, by: str | None = None,
               mentions: str | None = None, label: Sequence[str] = (), source: str | None = None,
               text: str | None = None, should_stop: Callable[[], bool] | None = None) -> dict:
        self._perm(caller, "read")
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait < 0 or wait > M.EVENTS_WAIT_MAX_S:
            raise VBoardError("invalid_request", f"wait: 0 to {M.EVENTS_WAIT_MAX_S} seconds")
        if channel is not None:
            self._check_channel_name(channel, allow_tree=True)
        flt = {"channel": channel, "re": self._nid(re) if re is not None else None, "by": by,
               "mentions": mentions, "labels": self._parse_labels(label), "source": source, "text": text,
               "all": bool(all)}
        if last is not None:
            if after is not None or wait:
                raise VBoardError("invalid_request", "last can't be combined with after or wait")
            if isinstance(last, bool) or not isinstance(last, int) or last < 1 or last > M.EVENTS_LAST_MAX:
                raise VBoardError("invalid_request", f"last: 1 to {M.EVENTS_LAST_MAX}")
            return self._events_last(flt, last)
        started = self._mono()
        deadline = started + float(wait)
        cur = self._cursor(after) if after is not None else None
        if cur is None:
            with self._read() as (conn, _now):
                cur = self._head(conn)
        while True:
            gen0 = self._gen
            if wait > 0:
                self.sweep()
            result, cur, more = self._events_pass(flt, cur)
            if result["events"] or wait <= 0 or self._mono() >= deadline or (should_stop and should_stop()):
                return result
            if more:
                continue
            self._park(deadline, gen0, started)

    def _event_sql(self, flt: dict) -> tuple[str, list]:
        where: list[str] = []
        args: list[Any] = []
        if not flt["all"]:
            where.append("hidden = 0")
        clause, params = self._chan_clause(flt["channel"])
        if clause:
            where.append(clause)
            args += params
        if flt["re"] is not None:
            where.append("thread_id = ?")
            args.append(flt["re"])
        return (" AND " + " AND ".join(where)) if where else "", args

    def _events_pass(self, flt: dict, cur: int) -> tuple[dict, int, bool]:
        sql_extra, sql_args = self._event_sql(flt)
        with self._read() as (conn, now_ms):
            head = self._head(conn)
            rows = conn.execute(f"SELECT * FROM events WHERE idx > ?{sql_extra} ORDER BY idx LIMIT ?",
                                [cur, *sql_args, M.FEED_SCAN]).fetchall()
            truncated = len(rows) >= M.FEED_SCAN
            cache: dict = {}
            out = [self._event_out(conn, r, now_ms, cache) for r in rows if self._event_match(conn, r, flt)]
            new_cur = int(rows[-1]["idx"]) if truncated else max(head, cur)
            return {"events": out, "cursor": str(new_cur)}, new_cur, (truncated and not out)

    def _events_last(self, flt: dict, last: int) -> dict:
        sql_extra, sql_args = self._event_sql(flt)
        with self._read() as (conn, now_ms):
            head = self._head(conn)
            found: list[sqlite3.Row] = []
            before = head + 1
            while len(found) < last:
                rows = conn.execute(f"SELECT * FROM events WHERE idx < ?{sql_extra} ORDER BY idx DESC LIMIT 500",
                                    [before, *sql_args]).fetchall()
                if not rows:
                    break
                for r in rows:
                    if self._event_match(conn, r, flt):
                        found.append(r)
                        if len(found) >= last:
                            break
                before = int(rows[-1]["idx"])
                if len(rows) < 500:
                    break
            found.reverse()
            cache: dict = {}
            return {"events": [self._event_out(conn, r, now_ms, cache) for r in found], "cursor": str(head)}

    def _event_match(self, conn: sqlite3.Connection, row: sqlite3.Row, flt: dict) -> bool:
        if flt["source"] is not None and flt["source"] != "local":
            return False
        needs_message = flt["by"] is not None or flt["mentions"] is not None or flt["labels"] or flt["text"]
        mid = row["message_id"]
        if needs_message and mid is None:
            return False
        if flt["by"] is not None:
            mrow = self._msg(conn, mid)
            if not (self._actor_is(conn, row["actor"], flt["by"]) or
                    (mrow is not None and self._actor_is(conn, mrow["actor"], flt["by"]))):
                return False
        if flt["mentions"] is not None and not self._mentioned(conn, mid, flt["mentions"]):
            return False
        if flt["text"]:
            mrow = self._msg(conn, mid)
            if mrow is None or mrow["deleted"] or flt["text"].lower() not in mrow["text"].lower():
                return False
        if flt["labels"]:
            after = json.loads(row["lbl_after"]) if row["lbl_after"] else []
            before = json.loads(row["lbl_before"]) if row["lbl_before"] else after
            if not (self._labels_match(flt["labels"], after) or self._labels_match(flt["labels"], before)):
                return False
        return True

    def _event_out(self, conn: sqlite3.Connection, row: sqlite3.Row, now_ms: int, cache: dict) -> dict:
        message = None
        if row["message_id"] is not None:
            mrow = self._msg(conn, row["message_id"])
            if mrow is not None:
                message = self._msg_view(conn, mrow, now_ms, cache)
                if row["tsnap"]:
                    snap = json.loads(row["tsnap"])
                    message["task"] = snap
                    if mrow["deleted"]:
                        # the cancel written by a delete shows the labels as they were before the tombstone
                        message["labels"] = dict(p.split("=", 1) for p in json.loads(row["lbl_after"] or "[]"))
                    message["labels"]["board.task"] = snap["state"]
        actor = self._author(conn, row["actor"], cache) if row["actor"] else None
        return M.event_view(cursor=str(row["idx"]), type=row["type"], created_ms=row["created_ms"],
                            payload=json.loads(row["payload"]), message=message, actor=actor)

    def export_events(self, after: str | None = None) -> Iterator[dict]:
        cur = self._cursor(after) if after is not None else 0
        while True:
            with self._read() as (conn, _now):
                rows = conn.execute("SELECT * FROM events WHERE idx > ? ORDER BY idx LIMIT 500", (cur,)).fetchall()
            if not rows:
                return
            for r in rows:
                yield {"cursor": str(r["idx"]), "type": r["type"], "createdAt": M.iso(r["created_ms"]),
                       "channel": r["channel"], "message": r["message_id"], "thread": r["thread_id"],
                       "actor": r["actor"], "hidden": bool(r["hidden"]), "payload": json.loads(r["payload"])}
                cur = int(r["idx"])

    # ------------------------------------------------------------------------------------------ task plumbing
    def _task_state_dict(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        needs_rows = conn.execute("SELECT need_id, cond FROM task_needs WHERE task_id=? ORDER BY ord",
                                  (row["id"],)).fetchall()
        claim = None
        if row["state"] == "claimed":
            claim = {"actor": row["claim_actor"], "until_ms": row["claim_until_ms"],
                     "token_hash": row["claim_token_hash"]}
        blocked = None
        if row["state"] == "blocked":
            blocked = {"kind": row["blocked_kind"], "reason": row["blocked_reason"]}
        return {"state": row["state"], "owner": row["owner"], "by": row["by_actor"], "for": row["for_actor"],
                "needs": [r["need_id"] for r in needs_rows],
                "needs_if": {r["need_id"]: r["cond"] for r in needs_rows if r["cond"] != "completed"},
                "epoch": row["epoch"], "claim": claim, "blocked": blocked, "result": row["result"],
                "reason": row["reason"], "settled": row["settled_kind"]}

    def _task_save(self, tx: _Tx, tid: str, new: dict, idx: int) -> None:
        conn = tx.conn
        claim = new["claim"]
        blocked = new["blocked"]
        sets = ["state=?", "owner=?", "by_actor=?", "for_actor=?", "epoch=?", "claim_actor=?", "claim_until_ms=?",
                "blocked_kind=?", "blocked_reason=?", "result=?", "reason=?", "updated_idx=?", "settled_kind=?"]
        vals: list[Any] = [new["state"], new["owner"], new["by"], new["for"], new["epoch"],
                           claim["actor"] if claim else None, claim["until_ms"] if claim else None,
                           blocked["kind"] if blocked else None, blocked["reason"] if blocked else None,
                           new["result"], new["reason"], idx, new["settled"]]
        if claim:
            sets.append("claim_token_hash=?")
            vals.append(claim["token_hash"])
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", [*vals, tid])
        conn.execute("INSERT INTO labels(message_id, key, value) VALUES(?,?,?)"
                     " ON CONFLICT(message_id, key) DO UPDATE SET value=excluded.value",
                     (tid, "board.task", new["state"]))

    def _task_apply(self, tx: _Tx, tid: str, etype: str, payload: dict, *, actor: str | None,
                    fold_extra: Mapping[str, Any] | None = None) -> bool:
        """Fold one task event; when it applies persist the new state and append the event. Returns applied?"""
        conn = tx.conn
        row = self._task(conn, tid)
        if row is None:
            return False
        cur = self._task_state_dict(conn, row)
        fold_payload = dict(payload)
        if fold_extra:
            fold_payload.update(fold_extra)
        new = fold_task(cur, etype, fold_payload)
        if new is cur or new is None:
            return False
        msg = self._msg(conn, tid)
        before = self._flabels(tx, tid)
        if etype == "task.needs_changed":
            conn.execute("DELETE FROM task_needs WHERE task_id=?", (tid,))
            for n, need in enumerate(new["needs"]):
                conn.execute("INSERT INTO task_needs(task_id, need_id, ord, cond) VALUES(?,?,?,?)",
                             (tid, need, n, new["needs_if"].get(need, "completed")))
        self._task_save(tx, tid, new, 0)
        idx = self._append(tx, etype, payload, channel=msg["channel"], message_id=tid, thread_id=tid,
                           actor=actor, lbl_before=before,
                           tsnap=self._task_view(conn, self._task(conn, tid), tx.now_ms, effective=False))
        conn.execute("UPDATE tasks SET updated_idx=? WHERE id=?", (idx, tid))
        if etype == "task.ready":
            tx.readied.append(tid)
        elif etype == "task.canceled":
            tx.canceled.append(tid)
        elif etype == "task.expired":
            tx.expired.append(tid)
        return True

    def _lapse_task(self, tx: _Tx, tid: str) -> sqlite3.Row | None:
        """Write task.expired for a lapsed claim (b5 item 48) and return the fresh row (None when not a task)."""
        row = self._task(tx.conn, tid)
        if row is None:
            return None
        if row["state"] == "claimed" and row["claim_until_ms"] is not None and row["claim_until_ms"] <= tx.now_ms:
            self._task_apply(tx, tid, "task.expired", {"id": tid, "thread": tid, "epoch": row["epoch"]},
                             actor=None)
            row = self._task(tx.conn, tid)
        return row

    @staticmethod
    def _needs_satisfied(conn: sqlite3.Connection, tid: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM task_needs n LEFT JOIN tasks p ON p.id = n.need_id WHERE n.task_id=? AND NOT COALESCE("
            "(n.cond='completed' AND p.state='completed') OR (n.cond='failed' AND p.state='failed'), 0) LIMIT 1",
            (tid,)).fetchone()
        return row is None

    def _maybe_ready(self, tx: _Tx, tid: str) -> bool:
        row = self._task(tx.conn, tid)
        if row is None or row["state"] != "waiting" or not self._needs_satisfied(tx.conn, tid):
            return False
        return self._task_apply(tx, tid, "task.ready", {"id": tid, "thread": tid}, actor=None)

    def _condition_cancel(self, tx: _Tx, dep_id: str, reason: str) -> None:
        row = self._lapse_task(tx, dep_id)
        if row is None:
            return
        if row["state"] in ("waiting", "ready"):
            if self._task_apply(tx, dep_id, "task.canceled", {"id": dep_id, "thread": dep_id, "reason": reason},
                                actor=None):
                self._propagate(tx, dep_id, cascade=True)
            return
        if row["state"] in ("claimed", "blocked", "completed", "failed", "canceled"):
            msg = self._msg(tx.conn, dep_id)
            self._append(tx, "task.condition_unmet", {"id": dep_id, "thread": dep_id, "reason": reason,
                                                     "state": row["state"]},
                         channel=msg["channel"], message_id=dep_id, thread_id=dep_id, hidden=True)

    def _propagate(self, tx: _Tx, prereq_id: str, *, cascade: bool = False) -> None:
        """React to a task reaching completed/failed/canceled: ready dependents, condition-cancel others."""
        conn = tx.conn
        prow = self._task(conn, prereq_id)
        if prow is None:
            return
        pstate = prow["state"]
        deps = conn.execute("SELECT task_id, cond FROM task_needs WHERE need_id=? ORDER BY CAST(task_id AS INTEGER)",
                            (prereq_id,)).fetchall()
        for dep in deps:
            did, cond = dep["task_id"], dep["cond"]
            if pstate == "completed":
                if cond == "failed":
                    self._condition_cancel(tx, did, f"condition not met: {prereq_id} completed")
                else:
                    self._maybe_ready(tx, did)
            elif pstate == "failed":
                if cond == "failed":
                    self._maybe_ready(tx, did)
            elif pstate == "canceled":
                if cond == "failed":
                    self._condition_cancel(tx, did, f"condition not met: {prereq_id} canceled")
                elif cascade:
                    drow = self._lapse_task(tx, did)
                    if drow is not None and drow["state"] in ("waiting", "ready"):
                        if self._task_apply(tx, did, "task.canceled",
                                            {"id": did, "thread": did, "reason": "prerequisite canceled"},
                                            actor=None):
                            self._propagate(tx, did, cascade=True)

    # ------------------------------------------------------------------------------------------ task access
    def _task_for_write(self, tx: _Tx, task_id: Any) -> sqlite3.Row:
        mid = self._nid(task_id)
        msg = self._msg(tx.conn, mid)
        if msg is None:
            raise VBoardError("message_not_found", f"no message {mid}")
        row = self._lapse_task(tx, mid) if msg["root_id"] == mid else None
        if row is None:
            raise VBoardError("not_a_task", f"{mid} isn't a task")
        return row

    def _owns(self, caller: Caller, trow: sqlite3.Row) -> bool:
        if caller.can is not None:
            if "manage" in caller.can:
                return True
            if "post" not in caller.can and "label" not in caller.can:
                return False
        if caller.principal != trow["owner"]:
            return False
        return caller.profile is None or caller.actor == trow["by_actor"]

    def _require_owner(self, caller: Caller, trow: sqlite3.Row) -> None:
        if not self._owns(caller, trow):
            raise VBoardError("task_not_owned", "only the task's owner can do that")

    def _task_result(self, tx: _Tx, tid: str, outcome: str, **extra: Any) -> dict:
        out = {"task": self._view_id(tx.conn, tid, tx.now_ms), "outcome": outcome}
        out.update(extra)
        return out

    # ------------------------------------------------------------------------------------------ needs
    def _norm_needs(self, needs: Sequence[Any]) -> list[tuple[str, str]]:
        """-> [(raw_id, cond)] deduped in order; >20 distinct -> invalid_request (before any lookup)."""
        out: list[tuple[str, str]] = []
        seen: dict[str, str] = {}
        for entry in needs or ():
            cond = "completed"
            if isinstance(entry, Mapping):
                raw = entry.get("id")
                iff = entry.get("if")
                if iff not in (None, "failed"):
                    raise VBoardError("invalid_request", "needs: if can only be 'failed'")
                if iff == "failed":
                    cond = "failed"
            else:
                raw = entry
            if raw is None or isinstance(raw, bool):
                raise VBoardError("invalid_needs", "a need names a task id")
            nid = str(raw).strip().lstrip("#")
            if nid in seen:
                if seen[nid] != cond:
                    raise VBoardError("invalid_request", f"needs: {nid} is listed twice with different conditions")
                continue
            seen[nid] = cond
            out.append((nid, cond))
        if len(out) > M.TASK_NEEDS_MAX:
            raise VBoardError("invalid_request", f"needs: a task waits for at most {M.TASK_NEEDS_MAX} tasks")
        return out

    def _validate_needs(self, conn: sqlite3.Connection, needs: list[tuple[str, str]], self_id: str | None) -> None:
        for nid, cond in needs:
            if self_id is not None and nid == self_id:
                raise VBoardError("invalid_needs", f"{nid} waits for itself")
            if not _NUM.match(nid):
                raise VBoardError("invalid_needs", f"{nid[:40]} isn't a task")
            row = self._task(conn, nid)
            if row is None:
                raise VBoardError("invalid_needs", f"{nid} isn't a task")
            if row["state"] == "canceled":
                raise VBoardError("invalid_needs", f"{nid} was canceled, so it can never complete")
            if cond == "failed" and row["state"] == "completed":
                raise VBoardError("invalid_needs", f"{nid} is completed, so it can never fail")

    def _cycle_check(self, conn: sqlite3.Connection, tid: str, new_needs: list[tuple[str, str]]) -> None:
        stack = [n for n, _ in new_needs]
        seen: set[str] = set()
        while stack:
            n = stack.pop()
            if n == tid:
                raise VBoardError("invalid_needs", f"{tid} would wait for itself")
            if n in seen:
                continue
            seen.add(n)
            if len(seen) > M.NEEDS_WALK_MAX:
                raise VBoardError("invalid_needs", f"too many tasks behind these needs (more than {M.NEEDS_WALK_MAX})")
            row = self._task(conn, n)
            if row is None or row["state"] == "completed":
                continue
            stack.extend(r["need_id"] for r in conn.execute("SELECT need_id FROM task_needs WHERE task_id=?", (n,)))

    def _norm_for(self, owner_principal: str, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise VBoardError("invalid_request", "for: an assignee is one of your own profiles")
        if "+" in value:
            principal, _, profile = value.partition("+")
        else:
            principal, profile = owner_principal, value
        if principal != owner_principal or not _ASSIGN.match(profile):
            raise VBoardError("invalid_request", "for: a task can only be assigned to one of the owner's own profiles")
        return f"{principal}+{profile}"

    # ------------------------------------------------------------------------------------------ task creation
    def _insert_task(self, tx: _Tx, caller: Caller, tid: str, channel: str, needs: list[tuple[str, str]],
                     for_actor: str | None) -> None:
        conn = tx.conn
        new = fold_task(None, "task.created", {"owner": caller.principal, "by": caller.actor, "for": for_actor,
                                               "needs": [n for n, _ in needs],
                                               "needs_if": {n: c for n, c in needs if c != "completed"}})
        conn.execute("INSERT INTO tasks(id, channel, state, owner, by_actor, for_actor, epoch, created_idx,"
                     " updated_idx) VALUES(?,?,?,?,?,?,0,?,?)",
                     (tid, channel, new["state"], new["owner"], new["by"], new["for"], int(tid), int(tid)))
        for n, (need, cond) in enumerate(needs):
            conn.execute("INSERT INTO task_needs(task_id, need_id, ord, cond) VALUES(?,?,?,?)", (tid, need, n, cond))
        conn.execute("INSERT INTO labels(message_id, key, value) VALUES(?,?,?)"
                     " ON CONFLICT(message_id, key) DO UPDATE SET value=excluded.value",
                     (tid, "board.task", new["state"]))
        needs_ids = [n for n, _ in needs]
        payload: dict[str, Any] = {"id": tid, "thread": tid, "state": new["state"], "needs": needs_ids,
                                   "for": for_actor}
        if new["needs_if"]:
            payload["needsIf"] = dict(new["needs_if"])
        self._append(tx, "task.created", payload, channel=channel, message_id=tid, thread_id=tid,
                     actor=caller.actor, tsnap=self._task_view(conn, self._task(conn, tid), tx.now_ms,
                                                              effective=False))
        if needs and self._needs_satisfied(conn, tid):
            self._task_apply(tx, tid, "task.ready", {"id": tid, "thread": tid}, actor=None)

    def create_task(self, caller: Caller, *, channel: str | None = None, text: str | None = None,
                    re: str | None = None, labels: dict | None = None, needs: Sequence[str | dict] = (),
                    for_: str | None = None, claim: bool = False, lease: float | None = None,
                    token: str | None = None, client_id: str | None = None, no_sync: bool = False) -> dict:
        adopt = re is not None
        if adopt:
            if text is not None or channel is not None:
                raise VBoardError("invalid_request", "name a thread to adopt, or a channel and text, not both")
            self._perm(caller, "label")
        else:
            if channel is None or text is None:
                raise VBoardError("invalid_request", "a task needs a channel and text, or the id of a thread")
            self._perm(caller, "post")
            self._check_channel_name(channel)
            text = self._text(text)
        labs = self._labels_in(labels)
        if labs:
            self._perm(caller, "label")
        cid = self._client_id(client_id)
        need_list = self._norm_needs(needs)
        for_actor = self._norm_for(caller.principal, for_)
        lease_s = self._lease(lease) if (claim or lease is not None) else None
        tok = self._token_in(token)
        with self._write() as tx:
            conn = tx.conn
            root_id = None
            if adopt:
                target = self._msg_or_404(conn, re)
                if target["root_id"] != target["id"]:
                    raise VBoardError("invalid_request", f"{target['id']} is a reply: name the root, "
                                                         f"{target['root_id']}")
                root_id = target["id"]
                channel = target["channel"]
            digest = _digest({"scope": "task", "actor": caller.actor, "channel": channel, "re": root_id,
                              "text": text, "labels": labs, "needs": need_list, "for": for_actor,
                              "claim": bool(claim), "noSync": bool(no_sync)})
            if cid is not None:
                hit = self._idem_lookup(conn, caller, "task", cid, digest)
                if hit is not None:
                    return {"task": self._view_id(conn, hit["id"], tx.now_ms)}
            if adopt:
                if target["deleted"]:
                    raise VBoardError("message_deleted", "that thread was deleted")
                if self._task(conn, root_id) is not None:
                    raise VBoardError("already_a_task", f"{root_id} is already a task")
            self._validate_needs(conn, need_list, root_id)
            if claim:
                waits = bool(need_list) and not self._needs_satisfied_for(conn, need_list)
                if waits:
                    raise VBoardError("task_waiting", "the task would wait for its needs, so it can't be claimed yet")
                if for_actor is not None and for_actor != caller.actor:
                    raise VBoardError("task_assigned", "the task is assigned to someone else")
                self._perm(caller, "label")
            self._ensure_actor(tx, caller)
            if adopt:
                tid = root_id
                if labs:
                    for k, v in labs.items():
                        conn.execute("INSERT INTO labels(message_id, key, value) VALUES(?,?,?)"
                                     " ON CONFLICT(message_id, key) DO UPDATE SET value=excluded.value",
                                     (tid, k, v))
                self._insert_task(tx, caller, tid, channel, need_list, for_actor)
            else:
                self._ensure_channel(tx, channel, actor=caller.actor)
                idx = self._head(conn) + 1
                tid = str(idx)
                self._insert_root(tx, caller, idx, channel, text, labs, no_sync)
                self._insert_task(tx, caller, tid, channel, need_list, for_actor)
            result: dict[str, Any] = {}
            if claim:
                trow = self._task(conn, tid)
                result_claim = self._do_claim(tx, trow, caller, lease_s, tok)
                result["claim"] = result_claim
            if cid is not None:
                self._idem_store(tx, caller, "task", cid, digest, {"id": tid})
            result["task"] = self._view_id(conn, tid, tx.now_ms)
            return result

    def _needs_satisfied_for(self, conn: sqlite3.Connection, needs: list[tuple[str, str]]) -> bool:
        for nid, cond in needs:
            row = self._task(conn, nid)
            if row is None:
                return False
            if not ((cond == "completed" and row["state"] == "completed") or
                    (cond == "failed" and row["state"] == "failed")):
                return False
        return True

    def _insert_root(self, tx: _Tx, caller: Caller, idx: int, channel: str, text: str, labs: dict[str, str],
                     no_sync: bool) -> None:
        conn = tx.conn
        mid = str(idx)
        conn.execute(
            "INSERT INTO messages(id, idx, channel, root_id, reply_to, actor, principal, profile, text, created_ms,"
            " version, revision, no_sync, last_activity_idx) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, idx, channel, mid, None, caller.actor, caller.principal, caller.profile, text, tx.now_ms, idx, idx,
             1 if no_sync else 0, idx))
        for k, v in labs.items():
            conn.execute("INSERT INTO labels(message_id, key, value) VALUES(?,?,?)", (mid, k, v))
        conn.execute("UPDATE channels SET messages=messages+1, threads=threads+1 WHERE path=?", (channel,))
        self._store_mentions(conn, mid, text)
        self._append(tx, "message.posted", {"id": mid}, channel=channel, message_id=mid, thread_id=mid,
                     actor=caller.actor, idx=idx)

    def create_tasks(self, caller: Caller, channel: str, tasks: Sequence[dict], *,
                     client_id: str | None = None) -> dict:
        if not isinstance(tasks, (list, tuple)):
            raise VBoardError("invalid_request", "tasks is a list")
        if len(tasks) > M.TASK_BATCH_MAX:
            raise VBoardError("batch_too_large", f"a batch makes {M.TASK_BATCH_MAX} tasks at most, and this one has "
                                                 f"{len(tasks)}: send it in parts")
        self._perm(caller, "post")
        self._check_channel_name(channel)
        cid = self._client_id(client_id)
        if not tasks:
            raise VBoardError("invalid_request", "tasks: a batch makes at least one task")
        items = [self._plan_item(caller, n, t) for n, t in enumerate(tasks)]
        if any(i["labels"] for i in items):
            self._perm(caller, "label")
        digest = _digest({"scope": "batch", "actor": caller.actor, "channel": channel,
                          "tasks": [i["canon"] for i in items]})
        keys: dict[str, int] = {}
        for n, item in enumerate(items):
            if item["key"] is not None:
                if item["key"] in keys:
                    raise VBoardError("invalid_needs", f"two tasks of the batch have the key \"{item['key']}\"")
                keys[item["key"]] = n
        with self._write() as tx:
            conn = tx.conn
            if cid is not None:
                hit = self._idem_lookup(conn, caller, "batch", cid, digest)
                if hit is not None:
                    return {"tasks": [self._view_id(conn, t, tx.now_ms) for t in hit["ids"]]}
            resolved = self._resolve_batch_needs(conn, items, keys)
            self._batch_cycles(items, resolved)
            self._ensure_actor(tx, caller)
            self._ensure_channel(tx, channel, actor=caller.actor)
            base = self._head(conn) + 1
            ids: list[str] = []
            pos = base
            for n, item in enumerate(items):
                ids.append(str(pos))
                batch_needs = any(kind == "batch" for kind, _, _ in resolved[n])
                ready_event = bool(resolved[n]) and not batch_needs and self._needs_satisfied_for(
                    conn, [(ref, cond) for _, ref, cond in resolved[n]])
                pos += 2 + (1 if ready_event else 0)
            for n, item in enumerate(items):
                tid = ids[n]
                need_list = [((ids[ref] if kind == "batch" else ref), cond) for kind, ref, cond in resolved[n]]
                self._insert_root(tx, caller, int(tid), channel, item["text"], item["labels"], item["no_sync"])
                self._insert_task(tx, caller, tid, channel, need_list, item["for"])
            expect_head = pos - 1
            if self._head(conn) != expect_head:  # pragma: no cover - invariant guard
                raise VBoardError("internal", "batch event numbering drifted")
            if cid is not None:
                self._idem_store(tx, caller, "batch", cid, digest, {"ids": ids})
            return {"tasks": [self._view_id(conn, t, tx.now_ms) for t in ids]}

    def _plan_item(self, caller: Caller, n: int, raw: Any) -> dict:
        if not isinstance(raw, Mapping):
            raise VBoardError("invalid_request", f"tasks.{n}: a task is an object with text")
        unknown = _SET(raw) - {"key", "text", "labels", "needs", "for", "noSync", "attachments"}
        if unknown:
            raise VBoardError("invalid_request", f"tasks.{n}: unknown key {sorted(unknown)[0]!r}")
        text = self._text(raw.get("text"), field=f"tasks.{n}.text")
        key = raw.get("key")
        if key is not None and (not isinstance(key, str) or not key or len(key) > M.TASK_KEY_MAX):
            raise VBoardError("invalid_request", f"tasks.{n}.key: 1 to {M.TASK_KEY_MAX} characters")
        atts = raw.get("attachments") or []
        if len(atts) > 10:
            raise VBoardError("invalid_request", f"tasks.{n}: a message carries at most 10 attachments")
        if atts:
            raise VBoardError("not_supported", "attachments aren't kept by this board")
        labels = self._labels_in(raw.get("labels"))
        for_actor = self._norm_for(caller.principal, raw.get("for"))
        entries = raw.get("needs") or []
        if not isinstance(entries, (list, tuple)):
            raise VBoardError("invalid_request", f"tasks.{n}.needs is a list")
        refs: list[tuple[str, str, str]] = []   # (kind, ref, cond)
        seen: dict[tuple[str, str], str] = {}
        for entry in entries:
            cond = "completed"
            kind = ref = None
            if isinstance(entry, Mapping):
                if entry.get("if") not in (None, "failed"):
                    raise VBoardError("invalid_request", f"tasks.{n}.needs: if can only be 'failed'")
                if entry.get("if") == "failed":
                    cond = "failed"
                if entry.get("key") is not None:
                    kind, ref = "key", str(entry["key"])
                elif entry.get("id") is not None:
                    kind, ref = "id", str(entry["id"]).strip().lstrip("#")
            elif entry is not None and not isinstance(entry, bool):
                kind, ref = "any", str(entry).strip().lstrip("#")
            if kind is None:
                raise VBoardError("invalid_needs", f"tasks.{n}.needs: a need names a task id or a key")
            sig = (kind, ref)
            if sig in seen:
                if seen[sig] != cond:
                    raise VBoardError("invalid_request", f"tasks.{n}.needs: {ref} is listed twice with different "
                                                         "conditions")
                continue
            seen[sig] = cond
            refs.append((kind, ref, cond))
        if len(refs) > M.TASK_NEEDS_MAX:
            raise VBoardError("invalid_request", f"tasks.{n}.needs: a task waits for at most {M.TASK_NEEDS_MAX} tasks")
        return {"key": key, "text": text, "labels": labels, "for": for_actor, "no_sync": bool(raw.get("noSync")),
                "refs": refs,
                "canon": {"key": key, "text": text, "labels": labels, "for": for_actor,
                          "noSync": bool(raw.get("noSync")), "needs": [list(r) for r in refs]}}

    def _resolve_batch_needs(self, conn: sqlite3.Connection, items: list[dict], keys: dict[str, int]
                             ) -> list[list[tuple[str, Any, str]]]:
        """-> per task [(kind 'batch'|'id', ref (batch index | task id), cond)]; validates existing ones."""
        out: list[list[tuple[str, Any, str]]] = []
        for n, item in enumerate(items):
            lst: list[tuple[str, Any, str]] = []
            seen: _SET = _SET()
            for kind, ref, cond in item["refs"]:
                if kind in ("key", "any") and ref in keys:
                    target: tuple[str, Any] = ("batch", keys[ref])
                elif kind == "key":
                    raise VBoardError("invalid_needs", f"no task of the batch has the key \"{ref}\"")
                else:
                    target = ("id", ref)
                if target == ("batch", n):
                    raise VBoardError("invalid_needs", f"task \"{item['key'] or n}\" waits for itself")
                if target in seen:
                    continue
                seen.add(target)
                if target[0] == "id":
                    self._validate_needs(conn, [(target[1], cond)], None)
                lst.append((target[0], target[1], cond))
            out.append(lst)
        return out

    @staticmethod
    def _batch_cycles(items: list[dict], resolved: list[list[tuple[str, Any, str]]]) -> None:
        graph = {n: [ref for kind, ref, _ in lst if kind == "batch"] for n, lst in enumerate(resolved)}
        state: dict[int, int] = {}
        stack: list[int] = []

        def visit(node: int) -> list[int] | None:
            state[node] = 1
            stack.append(node)
            for nxt in graph[node]:
                if state.get(nxt) == 1:
                    return stack[stack.index(nxt):]
                if nxt not in state:
                    found = visit(nxt)
                    if found:
                        return found
            stack.pop()
            state[node] = 2
            return None

        for node in graph:
            if node not in state:
                cycle = visit(node)
                if cycle:
                    names = ", ".join(f"\"{items[i]['key'] or i}\"" for i in cycle)
                    raise VBoardError("invalid_needs", f"{names} wait for each other")

    def batch(self, caller: Caller, client_id: str) -> dict:
        self._perm(caller, "read")
        cid = self._client_id(client_id)
        with self._read() as (conn, now_ms):
            row = conn.execute("SELECT result FROM idempotency WHERE principal=? AND scope='batch' AND key=?",
                               (caller.principal, cid)).fetchone()
            if row is None:
                raise VBoardError("batch_not_found", f"no batch with id {cid!r}")
            return {"tasks": [self._view_id(conn, t, now_ms) for t in json.loads(row["result"])["ids"]]}

    # ------------------------------------------------------------------------------------------ task listing
    def list_tasks(self, caller: Caller, *, channel: str | None = None, state: str = "open",
                   label: Sequence[str] = (), by: str | None = None, owner: str | None = None,
                   for_: str | None = None, needs: str | None = None, after: str | None = None, limit: int = 50,
                   latest: bool = False) -> dict:
        self._perm(caller, "read")
        limit = self._limit(limit, M.READ_LIMIT_DEFAULT)
        if state not in ("open", "all") and state not in M.TASK_STATES:
            raise VBoardError("invalid_request", f"state: open, all, or one of {', '.join(M.TASK_STATES)}")
        if latest and after is not None:
            raise VBoardError("invalid_request", "latest can't be combined with after")
        flt = self._parse_labels(label)
        if channel is not None:
            self._check_channel_name(channel, allow_tree=True)
        after_idx = self._cursor(after) if after is not None else None
        need_id = self._nid(needs, "invalid_request") if needs is not None else None
        with self._read() as (conn, now_ms):
            if channel is not None:
                self._require_channel(conn, channel)
            where = ["m.deleted = 0"]
            args: list[Any] = []
            clause, params = self._chan_clause(channel, "t.channel")
            if clause:
                where.append(clause)
                args += params
            if state == "open":
                where.append("t.state IN ('waiting','ready','claimed','blocked')")
            elif state == "ready":
                where.append("(t.state='ready' OR (t.state='claimed' AND t.claim_until_ms <= ?))")
                args.append(now_ms)
            elif state == "claimed":
                where.append("(t.state='claimed' AND t.claim_until_ms > ?)")
                args.append(now_ms)
            elif state != "all":
                where.append("t.state = ?")
                args.append(state)
            if owner is not None:
                where.append("t.owner = ?")
                args.append(owner)
            if need_id is not None:
                where.append("EXISTS (SELECT 1 FROM task_needs n WHERE n.task_id = t.id AND n.need_id = ?)")
                args.append(need_id)
            if after_idx is not None:
                where.append("m.idx > ?")
                args.append(after_idx)
            order = "m.last_activity_idx DESC, m.idx DESC" if latest else "m.idx ASC"
            sql = f"SELECT t.id FROM tasks t JOIN messages m ON m.id = t.id WHERE {' AND '.join(where)} ORDER BY {order}"
            cache: dict = {}
            picked: list[str] = []
            more = False
            for r in conn.execute(sql, args):
                trow = self._task(conn, r["id"])
                if by is not None and not self._actor_is(conn, trow["by_actor"], by):
                    continue
                if for_ is not None and not (trow["for_actor"] == for_ or
                                             (trow["for_actor"] or "").endswith("+" + for_)):
                    continue
                if flt:
                    pairs = [f"{k['key']}={k['value']}" for k in
                             conn.execute("SELECT key, value FROM labels WHERE message_id=?", (r["id"],))
                             if k["key"] != "board.task"]
                    pairs.append(f"board.task={self._eff(trow, now_ms)}")
                    if not self._labels_match(flt, pairs):
                        continue
                if len(picked) >= limit:
                    more = True
                    break
                picked.append(r["id"])
            head = self._head(conn)
            if latest:
                picked.reverse()
            out = []
            for tid in picked:
                root = self._msg(conn, tid)
                last = self._view_id(conn, root["last_reply_id"], now_ms, cache) if root["last_reply_id"] else None
                out.append(M.thread_summary(self._msg_view(conn, root, now_ms, cache), root["replies"], last))
            cursor = int(picked[-1]) if (more and picked and not latest) else head
            return {"tasks": out, "cursor": str(cursor)}

    # ------------------------------------------------------------------------------------------ claims
    def _do_claim(self, tx: _Tx, trow: sqlite3.Row, caller: Caller, lease_s: int | None, token: str | None) -> dict:
        """Claim a task row (already lapse-processed) inside the caller's transaction."""
        tid = trow["id"]
        lease_s = self.lease_default_s if lease_s is None else lease_s
        if (token is not None and trow["state"] == "claimed" and trow["claim_token_hash"] is not None and
                hmac.compare_digest(_hash_token(token), trow["claim_token_hash"])):
            return {"epoch": trow["epoch"], "token": token, "until": M.iso(trow["claim_until_ms"])}
        state = trow["state"]
        if state == "waiting":
            raise VBoardError("task_waiting", f"{tid} is waiting for tasks it needs")
        if state == "blocked":
            raise VBoardError("task_blocked", f"{tid} is blocked")
        if state in M.FINISHED_STATES:
            raise VBoardError("task_finished", f"{tid} is {state}")
        if trow["for_actor"] is not None and not (trow["for_actor"] == caller.actor and
                                                  trow["owner"] == caller.principal):
            raise VBoardError("task_assigned", f"{tid} is assigned to someone else")
        if state == "claimed":
            raise VBoardError("task_claimed", f"{tid} is already claimed")
        self._ensure_actor(tx, caller)
        tok = token or secrets.token_urlsafe(16)
        until_ms = tx.now_ms + lease_s * 1000
        epoch = trow["epoch"] + 1
        payload = {"id": tid, "thread": tid, "epoch": epoch, "until": M.iso(until_ms)}
        self._task_apply(tx, tid, "task.claimed", payload, actor=caller.actor,
                         fold_extra={"actor": caller.actor, "until_ms": until_ms, "token_hash": _hash_token(tok)})
        return {"epoch": epoch, "token": tok, "until": M.iso(until_ms)}

    def claim(self, caller: Caller, task_id: str, *, lease: float | None = None, token: str | None = None) -> dict:
        self._perm(caller, "label")
        lease_s = self._lease(lease)
        tok = self._token_in(token)
        with self._write() as tx:
            self._ensure_actor(tx, caller)
            trow = self._task_for_write(tx, task_id)
            got = self._do_claim(tx, trow, caller, lease_s, tok)
            return {"task": self._view_id(tx.conn, trow["id"], tx.now_ms), "claim": got}

    def claim_next(self, caller: Caller, channel: str, *, label: dict | None = None, from_: Sequence[str] = (),
                   lease: float | None = None, wait: float = 0.0, token: str | None = None,
                   should_stop: Callable[[], bool] | None = None) -> dict:
        self._perm(caller, "label")
        self._check_channel_name(channel, allow_tree=True)
        lease_s = self._lease(lease)
        tok = self._token_in(token)
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait < 0 or wait > M.CLAIM_WAIT_MAX_S:
            raise VBoardError("invalid_request", f"wait: 0 to {M.CLAIM_WAIT_MAX_S} seconds")
        wanted: dict[str, str] = {}
        for key, value in (label or {}).items():
            M.label_ok(key, str(value), allow_reserved=True)
            wanted[key] = str(value)
        owners = [caller.principal]
        if len(list(from_ or ())) > M.FROM_MAX:
            raise VBoardError("invalid_from", f"from names at most {M.FROM_MAX} owners")
        for value in from_ or ():
            if not isinstance(value, str) or "/" in value or ":" not in value or len(value) > M.ID_MAX:
                raise VBoardError("invalid_from", "from takes principal ids like local:user:name, not handles")
            if value not in owners:
                owners.append(value)
        started = self._mono()
        deadline = started + float(wait)
        while True:
            gen0 = self._gen
            if wait > 0:
                self.sweep()
            got = self._claim_next_once(caller, channel, wanted, owners, lease_s, tok)
            if got is not None:
                return got
            if wait <= 0 or self._mono() >= deadline or (should_stop and should_stop()):
                return {"task": None}
            self._park(deadline, gen0, started)

    def _pick_ready(self, conn: sqlite3.Connection, now_ms: int, caller: Caller, channel: str,
                    wanted: dict[str, str], owners: list[str], token_hash: str | None) -> str | None:
        """Replay a matching live claim, or select assigned-first, highest priority, then oldest."""
        if wanted.get("board.task", "ready") != "ready":
            return None
        clause, params = self._chan_clause(channel, "t.channel")
        rows = conn.execute(
            f"SELECT t.* FROM tasks t JOIN messages m ON m.id = t.id WHERE m.deleted = 0 AND {clause} AND "
            "(t.state='ready' OR (t.state='claimed' AND (t.claim_until_ms <= ? OR "
            "(t.claim_actor = ? AND t.claim_token_hash = ?)))) ORDER BY m.idx",
            [*params, now_ms, caller.actor, token_hash]).fetchall()
        cands: list[tuple[int, int, int, str]] = []
        for trow in rows:
            tid = trow["id"]
            assigned = trow["for_actor"]
            mine = assigned is not None and assigned == caller.actor and trow["owner"] == caller.principal
            if (assigned is not None and not mine) or trow["owner"] not in owners:
                continue
            if trow["state"] == "claimed" and trow["claim_until_ms"] > now_ms:
                # A lost response must recover this claim even if its labels changed after selection.
                event = conn.execute(
                    "SELECT lbl_before FROM events WHERE thread_id = ? AND type = 'task.claimed' ORDER BY idx DESC LIMIT 1",
                    (tid,),
                ).fetchone()
                before = json.loads(event["lbl_before"]) if event and event["lbl_before"] else []
                if _ALL(f"{k}={v}" in before for k, v in wanted.items() if k != "board.task"):
                    return tid
                continue
            labels = {k["key"]: k["value"] for k in conn.execute(
                "SELECT key, value FROM labels WHERE message_id=?", (tid,))}
            if not _ALL(labels.get(k) == v for k, v in wanted.items() if k != "board.task"):
                continue
            defer = labels.get(M.FUSION_LABELS["defer_until"])
            if defer:
                try:
                    if M.parse_iso(defer) > now_ms:
                        continue
                except VBoardError:
                    pass
            try:
                prio = int(labels.get(M.FUSION_LABELS["priority"], "0"))
            except ValueError:
                prio = 0
            cands.append((0 if mine else 1, -prio, int(tid), tid))
        return min(cands)[3] if cands else None

    def _claim_next_once(self, caller: Caller, channel: str, wanted: dict[str, str], owners: list[str],
                         lease_s: int, token: str | None) -> dict | None:
        token_hash = _hash_token(token) if token is not None else None
        with self._read() as (conn, now_ms):          # polling stays read-only until something is claimable
            if self._pick_ready(conn, now_ms, caller, channel, wanted, owners, token_hash) is None:
                return None
        with self._write() as tx:
            conn = tx.conn
            tid = self._pick_ready(conn, tx.now_ms, caller, channel, wanted, owners, token_hash)
            if tid is None:
                return None
            self._ensure_actor(tx, caller)
            trow = self._lapse_task(tx, tid)
            got = self._do_claim(tx, trow, caller, lease_s, token)
            return {"task": self._view_id(conn, tid, tx.now_ms), "claim": got}

    # ------------------------------------------------------------------------------------------ settle verbs
    def _fence(self, tx: _Tx, trow: sqlite3.Row, epoch: Any, token: Any, op: str) -> str:
        """-> 'live' (fenced claim, proceed) | 'replay' (same claim already settled this way)."""
        if isinstance(epoch, bool) or not isinstance(epoch, int) or not isinstance(token, str) or not token:
            raise VBoardError("invalid_request", "epoch and token: the claim you hold")
        match_hash = trow["claim_token_hash"] is not None and hmac.compare_digest(
            _hash_token(token), trow["claim_token_hash"])
        same_epoch = trow["epoch"] == epoch
        if trow["state"] == "claimed" and same_epoch and match_hash and trow["claim_until_ms"] > tx.now_ms:
            return "live"
        if same_epoch and match_hash:
            kind = trow["settled_kind"]
            if kind == op:
                return "replay"
            if trow["state"] in M.FINISHED_STATES and kind in ("completed", "failed"):
                raise VBoardError("task_finished", f"{trow['id']} is already {trow['state']}")
        raise VBoardError("claim_lost", "you no longer hold this claim")

    def renew(self, caller: Caller, task_id: str, *, epoch: int, token: str, lease: float | None = None) -> dict:
        self._perm(caller, "label")
        lease_s = self._lease(lease)
        with self._write() as tx:
            trow = self._task_for_write(tx, task_id)
            if self._fence(tx, trow, epoch, token, "renewed") != "live":
                raise VBoardError("claim_lost", "you no longer hold this claim")
            tid = trow["id"]
            until_ms = tx.now_ms + lease_s * 1000
            self._ensure_actor(tx, caller)
            self._task_apply(tx, tid, "task.renewed", {"id": tid, "thread": tid, "epoch": epoch,
                                                       "until": M.iso(until_ms)},
                             actor=caller.actor, fold_extra={"until_ms": until_ms})
            return self._task_result(tx, tid, "renewed", until=M.iso(until_ms))

    def _settle(self, caller: Caller, task_id: str, etype: str, epoch: int, token: str, op: str, outcome: str,
                payload: dict) -> dict:
        self._perm(caller, "label")
        with self._write() as tx:
            trow = self._task_for_write(tx, task_id)
            mode = self._fence(tx, trow, epoch, token, op)
            tid = trow["id"]
            if mode == "replay":
                return self._task_result(tx, tid, "unchanged")
            self._ensure_actor(tx, caller)
            self._task_apply(tx, tid, etype, {"id": tid, "thread": tid, "epoch": epoch, **payload},
                             actor=caller.actor)
            if etype in ("task.completed", "task.failed"):
                self._propagate(tx, tid)
            return self._task_result(tx, tid, outcome)

    def release(self, caller: Caller, task_id: str, *, epoch: int, token: str) -> dict:
        return self._settle(caller, task_id, "task.released", epoch, token, "released", "released", {})

    def complete(self, caller: Caller, task_id: str, *, epoch: int, token: str, result: str | None = None) -> dict:
        res = self._note(result, "result", required=False)
        return self._settle(caller, task_id, "task.completed", epoch, token, "completed", "completed",
                            {"result": res})

    def fail(self, caller: Caller, task_id: str, *, epoch: int, token: str, reason: str) -> dict:
        why = self._note(reason, "reason", required=True)
        return self._settle(caller, task_id, "task.failed", epoch, token, "failed", "failed", {"reason": why})

    # ------------------------------------------------------------------------------------------ owner verbs
    def block(self, caller: Caller, task_id: str, *, reason: str, kind: str | None = None,
              epoch: int | None = None, token: str | None = None) -> dict:
        why = self._note(reason, "reason", required=True)
        if kind is not None and (not isinstance(kind, str) or not _BLOCK_KIND.match(kind) or kind == "edited"):
            raise VBoardError("invalid_request", "kind: lowercase letters, digits, _ . - (and not 'edited')")
        if (epoch is None) != (token is None):
            raise VBoardError("invalid_request", "send the claim's epoch and token together")
        with self._write() as tx:
            trow = self._task_for_write(tx, task_id)
            tid = trow["id"]
            state = trow["state"]
            holder = epoch is not None
            if holder:
                self._perm(caller, "label")
                if self._fence(tx, trow, epoch, token, "blocked") == "replay":
                    return self._task_result(tx, tid, "unchanged")
            else:
                self._require_owner(caller, trow)
                if state == "blocked":
                    raise VBoardError("task_blocked", f"{tid} is already blocked")
                if state in M.FINISHED_STATES:
                    raise VBoardError("task_finished", f"{tid} is {state}")
                if state == "claimed":
                    raise VBoardError("task_claimed", f"{tid} is claimed: cancel it instead")
            self._ensure_actor(tx, caller)
            self._task_apply(tx, tid, "task.blocked", {"id": tid, "thread": tid, "kind": kind, "reason": why},
                             actor=caller.actor)
            return self._task_result(tx, tid, "blocked")

    def unblock(self, caller: Caller, task_id: str) -> dict:
        with self._write() as tx:
            trow = self._task_for_write(tx, task_id)
            tid = trow["id"]
            self._require_owner(caller, trow)
            if trow["state"] != "blocked":
                return self._task_result(tx, tid, "unchanged")
            self._ensure_actor(tx, caller)
            has_needs = tx.conn.execute("SELECT 1 FROM task_needs WHERE task_id=? LIMIT 1", (tid,)).fetchone()
            self._task_apply(tx, tid, "task.unblocked", {"id": tid, "thread": tid,
                                                         "state": "waiting" if has_needs else "ready"},
                             actor=caller.actor)
            if has_needs:
                self._maybe_ready(tx, tid)
            return self._task_result(tx, tid, "unblocked")

    def reopen(self, caller: Caller, task_id: str) -> dict:
        with self._write() as tx:
            trow = self._task_for_write(tx, task_id)
            tid = trow["id"]
            self._require_owner(caller, trow)
            if trow["state"] in ("completed", "canceled"):
                raise VBoardError("task_finished", f"{tid} is {trow['state']}")
            if trow["state"] != "failed":
                return self._task_result(tx, tid, "unchanged")
            self._ensure_actor(tx, caller)
            self._task_apply(tx, tid, "task.reopened", {"id": tid, "thread": tid}, actor=caller.actor)
            return self._task_result(tx, tid, "reopened")

    def assign(self, caller: Caller, task_id: str, for_: str | None) -> dict:
        with self._write() as tx:
            trow = self._task_for_write(tx, task_id)
            tid = trow["id"]
            self._require_owner(caller, trow)
            target = self._norm_for(trow["owner"], for_)
            if trow["state"] in M.FINISHED_STATES:
                raise VBoardError("task_finished", f"{tid} is {trow['state']}")
            if trow["for_actor"] == target:
                return self._task_result(tx, tid, "unchanged")
            self._ensure_actor(tx, caller)
            self._task_apply(tx, tid, "task.assigned", {"id": tid, "thread": tid, "for": target}, actor=caller.actor)
            return self._task_result(tx, tid, "assigned" if target else "pooled")

    def set_needs(self, caller: Caller, task_id: str, needs: Sequence[str | dict]) -> dict:
        need_list = self._norm_needs(needs)
        with self._write() as tx:
            conn = tx.conn
            trow = self._task_for_write(tx, task_id)
            tid = trow["id"]
            self._require_owner(caller, trow)
            if trow["state"] == "claimed":
                raise VBoardError("task_claimed", f"{tid} is claimed: change its needs after it is released")
            if trow["state"] in M.FINISHED_STATES:
                raise VBoardError("task_finished", f"{tid} is {trow['state']}")
            self._validate_needs(conn, need_list, tid)
            self._cycle_check(conn, tid, need_list)
            current = [(r["need_id"], r["cond"]) for r in conn.execute(
                "SELECT need_id, cond FROM task_needs WHERE task_id=? ORDER BY ord", (tid,))]
            if current == need_list:
                return self._task_result(tx, tid, "unchanged")
            self._ensure_actor(tx, caller)
            if trow["state"] == "blocked":
                state = "blocked"
            else:
                state = "ready" if (not need_list or self._needs_satisfied_for(conn, need_list)) else "waiting"
            payload: dict[str, Any] = {"id": tid, "thread": tid, "needs": [n for n, _ in need_list], "state": state}
            if any(c != "completed" for _, c in need_list):
                payload["needsIf"] = {n: c for n, c in need_list if c != "completed"}
            self._task_apply(tx, tid, "task.needs_changed", payload, actor=caller.actor,
                             fold_extra={"needs_if": {n: c for n, c in need_list if c != "completed"}})
            return self._task_result(tx, tid, "set")

    def cancel(self, caller: Caller, task_id: str, *, reason: str) -> dict:
        why = self._note(reason, "reason", required=True)
        with self._write() as tx:
            trow = self._task_for_write(tx, task_id)
            tid = trow["id"]
            self._require_owner(caller, trow)
            if trow["state"] in ("completed", "canceled"):
                raise VBoardError("task_finished", f"{tid} is {trow['state']}")
            self._ensure_actor(tx, caller)
            self._task_apply(tx, tid, "task.canceled", {"id": tid, "thread": tid, "reason": why},
                             actor=caller.actor)
            self._propagate(tx, tid)
            return self._task_result(tx, tid, "canceled")

    # ------------------------------------------------------------------------------------------ sweep
    def sweep(self, now: float | None = None) -> dict:
        """Write task.expired for lapsed claims and repair readiness. Opens a write only when work exists."""
        now_ms = self._now_ms() if now is None else int(now * 1000)
        with self._read() as (conn, _n):
            if not self._sweep_needed(conn, now_ms):
                return {"expired": [], "readied": [], "canceled": []}
        with self._write(now_ms) as tx:
            conn = tx.conn
            for r in conn.execute("SELECT id FROM tasks WHERE state='claimed' AND claim_until_ms <= ?"
                                  " ORDER BY CAST(id AS INTEGER)", (tx.now_ms,)).fetchall():
                self._lapse_task(tx, r["id"])
            for r in conn.execute(self._SWEEP_COND).fetchall():
                self._condition_cancel(tx, r["task_id"], f"condition not met: {r['need_id']} {r['pstate']}")
            for r in conn.execute(self._SWEEP_READY).fetchall():
                self._maybe_ready(tx, r["id"])
            return {"expired": list(tx.expired), "readied": list(tx.readied), "canceled": list(tx.canceled)}

    _SWEEP_READY = (
        "SELECT t.id FROM tasks t WHERE t.state='waiting' AND NOT EXISTS (SELECT 1 FROM task_needs n LEFT JOIN "
        "tasks p ON p.id = n.need_id WHERE n.task_id = t.id AND NOT COALESCE((n.cond='completed' AND "
        "p.state='completed') OR (n.cond='failed' AND p.state='failed'), 0)) ORDER BY CAST(t.id AS INTEGER)")
    _SWEEP_COND = (
        "SELECT t.id AS task_id, n.need_id AS need_id, p.state AS pstate FROM tasks t JOIN task_needs n ON "
        "n.task_id = t.id JOIN tasks p ON p.id = n.need_id WHERE n.cond='failed' AND p.state IN "
        "('completed','canceled') AND t.state IN ('waiting','ready') ORDER BY CAST(t.id AS INTEGER)")

    def _sweep_needed(self, conn: sqlite3.Connection, now_ms: int) -> bool:
        if conn.execute("SELECT 1 FROM tasks WHERE state='claimed' AND claim_until_ms <= ? LIMIT 1",
                        (now_ms,)).fetchone():
            return True
        if conn.execute(self._SWEEP_COND.replace(" ORDER BY CAST(t.id AS INTEGER)", " LIMIT 1")).fetchone():
            return True
        return conn.execute(self._SWEEP_READY.replace(" ORDER BY CAST(t.id AS INTEGER)", " LIMIT 1")).fetchone() \
            is not None


__all__ = ["BoardStore", "SCHEMA_VERSION", "fold_task"]
