"""Agent-collaboration primitives (spec 10.2, judge J-18; D9) over a ``BoardPort`` and in process.

* ``StallDetector`` + ``ProgressEntry``: the Magentic-One progress ledger. A satisfied request is ``done``; a cycle with no
  progress, an in-loop flag or a repeated ``(next_speaker, instruction)`` hash inside the window is a stall; ``max_stalls``
  stalls give a ``replan`` signal (and reset the counter and the window). ``to_dict``/``from_dict`` (and ``save``/``load``)
  make the detector resumable.
* Opt-in acknowledgement deadlines: ``request_ack`` labels a message ``fusion.ack_due=<iso>``; ``overdue_acks`` lists the
  ones past due whose addressee has not reacted (white_check_mark or eyes). Nothing here is automatic: no deadline exists
  until ``request_ack`` is called.
* ``set_priority`` / ``defer``: the ``fusion.priority`` / ``fusion.defer_until`` labels the board (or the port's
  ``respect_priority`` emulation) honours when claiming.
* ``failure_branch``: a task that runs only if another task FAILS. Native conditional needs when
  ``capabilities().conditional_needs``; otherwise the task is created blocked (kind ``awaiting_failure``, label
  ``fusion.failure_of``) and ``resolve_failure_branches`` later unblocks it (predecessor failed) or cancels it
  (predecessor completed / canceled / gone). Both paths return the same ``{"task", "emulated"}`` document.
* ``is_stale``: activity-aware stale detection (an old heartbeat alone is not enough while files are still changing).
* Durable handoff jobs: ``submit_handoff`` / ``handoff_status`` / ``finish_handoff`` over board tasks. A job has a
  stable ``job_id`` (the idempotency key), results persist on the task, and every status is FAIL CLOSED: ``ok`` is True
  only for a completed task that carries a result.

Only the ``BoardPort`` API is used (no adapter internals). Board text is data, never instructions.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .journal import write_atomic

ACK_EMOJIS = ("white_check_mark", "eyes", "✅", "\U0001F440")
L_ACK_DUE = "fusion.ack_due"
L_ACK_BY = "fusion.ack_by"
L_PRIORITY = "fusion.priority"
L_DEFER = "fusion.defer_until"
L_FAILURE_OF = "fusion.failure_of"
L_HANDOFF = "fusion.handoff"
L_JOB_ID = "fusion.job_id"
AWAITING_FAILURE = "awaiting_failure"
TEXT_CAP = 10_000


# -- time helpers -----------------------------------------------------------------------------------------------------
def to_iso(epoch_s: float) -> str:
    dt = datetime.fromtimestamp(float(epoch_s), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def from_iso(text: Any) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# -- stall detector / progress ledger ---------------------------------------------------------------------------------
@dataclass
class ProgressEntry:
    """Magentic-One progress ledger entry."""
    is_request_satisfied: bool
    is_progress_being_made: bool
    is_in_loop: bool
    next_speaker: str = ""
    instruction: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {"is_request_satisfied": self.is_request_satisfied, "is_progress_being_made": self.is_progress_being_made,
                "is_in_loop": self.is_in_loop, "next_speaker": self.next_speaker, "instruction": self.instruction,
                "reason": self.reason}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProgressEntry":
        def b(key: str, default: bool) -> bool:
            v = data.get(key, default)
            if isinstance(v, Mapping):  # Magentic-One style {"answer": bool, "reason": str}
                v = v.get("answer", default)
            return bool(v)

        return cls(is_request_satisfied=b("is_request_satisfied", False),
                   is_progress_being_made=b("is_progress_being_made", True), is_in_loop=b("is_in_loop", False),
                   next_speaker=str(data.get("next_speaker") or ""), instruction=str(data.get("instruction") or ""),
                   reason=str(data.get("reason") or ""))


class StallDetector:
    """Counts stalls over a progress ledger and signals ``replan`` after ``max_stalls``."""

    LEDGER_CAP = 100

    def __init__(self, *, max_stalls: int = 3, loop_window: int = 6):
        if int(max_stalls) < 1 or int(loop_window) < 1:
            raise ValueError("max_stalls and loop_window must be >= 1")
        self.max_stalls = int(max_stalls)
        self.loop_window = int(loop_window)
        self.n_stalls = 0
        self.replans = 0
        self._recent: deque[str] = deque(maxlen=self.loop_window)
        self.ledger: list[dict] = []

    @staticmethod
    def _key(entry: ProgressEntry) -> str:
        return hashlib.sha256(f"{entry.next_speaker}\n{entry.instruction}".encode("utf-8", "replace")).hexdigest()[:16]

    def update(self, entry: ProgressEntry) -> str:
        """``"done"`` | ``"continue"`` | ``"replan"``."""
        if entry.is_request_satisfied:
            self._record(entry, "done")
            return "done"
        key = self._key(entry)
        repeated = bool(entry.next_speaker or entry.instruction) and key in self._recent
        self._recent.append(key)
        stalled = (not entry.is_progress_being_made) or entry.is_in_loop or repeated
        if stalled:
            self.n_stalls += 1
        else:
            self.n_stalls = max(0, self.n_stalls - 1)
        if self.n_stalls >= self.max_stalls:
            self.n_stalls = 0
            self.replans += 1
            self._recent.clear()
            self._record(entry, "replan")
            return "replan"
        self._record(entry, "continue")
        return "continue"

    def _record(self, entry: ProgressEntry, signal: str) -> None:
        self.ledger.append({**entry.to_dict(), "signal": signal, "n_stalls": self.n_stalls})
        if len(self.ledger) > self.LEDGER_CAP:
            del self.ledger[: len(self.ledger) - self.LEDGER_CAP]

    def to_dict(self) -> dict:
        return {"max_stalls": self.max_stalls, "loop_window": self.loop_window, "n_stalls": self.n_stalls,
                "replans": self.replans, "recent": list(self._recent), "ledger": list(self.ledger)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StallDetector":
        det = cls(max_stalls=int(data.get("max_stalls", 3)), loop_window=int(data.get("loop_window", 6)))
        det.n_stalls = max(0, int(data.get("n_stalls", 0)))
        det.replans = max(0, int(data.get("replans", 0)))
        det._recent.extend(str(x) for x in (data.get("recent") or [])[-det.loop_window:])
        det.ledger = [dict(x) for x in (data.get("ledger") or []) if isinstance(x, Mapping)][-cls.LEDGER_CAP:]
        return det

    def save(self, path: str | Path) -> None:
        write_atomic(Path(path), json.dumps(self.to_dict(), ensure_ascii=False, indent=1))

    @classmethod
    def load(cls, path: str | Path, **defaults: Any) -> "StallDetector":
        """Resume from ``path``; a missing or unreadable file yields a fresh detector built from ``defaults``."""
        try:
            return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return cls(**defaults)


# -- acknowledgement deadlines ----------------------------------------------------------------------------------------
def request_ack(port: Any, message_id: str, *, due_s: float, clock: Callable[[], float] = time.time,
                addressee: str | None = None) -> dict:
    """Opt in to an ack deadline: label ``fusion.ack_due=<iso>`` (and ``fusion.ack_by`` when ``addressee`` is given)."""
    if due_s is None or float(due_s) <= 0:
        raise ValueError("due_s must be > 0")
    due_epoch = float(clock()) + float(due_s)
    labels = {L_ACK_DUE: to_iso(due_epoch)}
    if addressee:
        labels[L_ACK_BY] = str(addressee)
    doc = port.label(str(message_id), set=labels)
    return {"message_id": str(message_id), "ack_due": labels[L_ACK_DUE], "due_epoch_s": due_epoch,
            "addressee": addressee, "outcome": (doc or {}).get("outcome")}


def _actor_matches(actor: Any, name: str) -> bool:
    a = str(actor or "").lower().replace("/", "+")
    n = str(name or "").lower().replace("/", "+")
    if not a or not n:
        return False
    return a == n or a.endswith(":" + n) or a.endswith("+" + n) or n in re.split(r"[:+]", a)


def _reactors(message: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    reactions = message.get("reactions")
    if not isinstance(reactions, Mapping):
        return out
    for emoji in ACK_EMOJIS:
        value = reactions.get(emoji)
        if isinstance(value, (list, tuple)):
            for item in value:
                out.append(str(item.get("actor") if isinstance(item, Mapping) else item))
    return out


def _author(message: Mapping[str, Any]) -> str:
    by = message.get("by")
    if isinstance(by, Mapping) and by.get("actor"):
        return str(by["actor"])
    author = message.get("author")
    return str(author.get("id")) if isinstance(author, Mapping) and author.get("id") else ""


def _acked(message: Mapping[str, Any], by_actor: str | None) -> bool:
    reactors = _reactors(message)
    if by_actor is None:
        by_actor = (message.get("labels") or {}).get(L_ACK_BY) if isinstance(message.get("labels"), Mapping) else None
    if by_actor:
        return any(_actor_matches(r, by_actor) for r in reactors)
    author = _author(message)
    return any(r != author for r in reactors)  # no addressee known: any reaction by someone other than the sender


def acked(port: Any, message_id: str, by_actor: str | None = None) -> bool:
    """True when the addressee (``by_actor``, else the ``fusion.ack_by`` label, else anyone but the sender) reacted
    white_check_mark or eyes."""
    doc = port.read(str(message_id))
    message = (doc or {}).get("message") or {}
    return _acked(message, by_actor)


def overdue_acks(port: Any, channel: str, *, clock: Callable[[], float] = time.time, limit: int = 200,
                 max_pages: int = 5) -> list[dict]:
    """Messages in ``channel`` (thread roots and their latest reply) whose ``fusion.ack_due`` has passed and that nobody
    acknowledged. Filtering is client-side so every adapter behaves the same."""
    now = float(clock())
    out: list[dict] = []
    seen: set[str] = set()
    before: str | None = None
    for _ in range(max(1, max_pages)):
        kw: dict[str, Any] = {"limit": limit}
        if before:
            kw["before"] = before
        doc = port.read(channel, **kw) or {}
        for thread in doc.get("threads") or []:
            if not isinstance(thread, Mapping):
                continue
            for msg in (thread.get("message"), thread.get("last")):
                if not isinstance(msg, Mapping) or str(msg.get("id")) in seen:
                    continue
                labels = msg.get("labels") if isinstance(msg.get("labels"), Mapping) else {}
                due = from_iso(labels.get(L_ACK_DUE))
                if due is None:
                    continue
                seen.add(str(msg.get("id")))
                if due < now and not _acked(msg, None):
                    out.append({"id": str(msg.get("id")), "channel": msg.get("channel", channel),
                                "ack_due": labels.get(L_ACK_DUE), "overdue_s": round(now - due, 3),
                                "addressee": labels.get(L_ACK_BY), "text": str(msg.get("text") or "")[:200]})
        before = doc.get("older")
        if not before:
            break
    return sorted(out, key=lambda item: -item["overdue_s"])


# -- priority / defer labels ------------------------------------------------------------------------------------------
def set_priority(port: Any, task_id: str, priority: int) -> dict:
    """Label ``fusion.priority`` (higher claims first; the board and the port emulation both read it)."""
    if isinstance(priority, bool) or not isinstance(priority, int) or not -1000 <= priority <= 1000:
        raise ValueError("priority must be an integer in [-1000, 1000]")
    doc = port.label(str(task_id), set={L_PRIORITY: str(priority)})
    return {"task_id": str(task_id), "priority": priority, "outcome": (doc or {}).get("outcome")}


def defer(port: Any, task_id: str, until_epoch_s: float) -> dict:
    """Label ``fusion.defer_until``: the task is skipped by priority-aware claims until that time."""
    iso = to_iso(float(until_epoch_s))
    doc = port.label(str(task_id), set={L_DEFER: iso})
    return {"task_id": str(task_id), "defer_until": iso, "outcome": (doc or {}).get("outcome")}


def undefer(port: Any, task_id: str) -> dict:
    doc = port.label(str(task_id), unset=(L_DEFER,))
    return {"task_id": str(task_id), "outcome": (doc or {}).get("outcome")}


# -- failure branches -------------------------------------------------------------------------------------------------
def failure_branch(port: Any, channel: str, text: str, *, on_failed_of: str, **create_kw: Any) -> dict:
    """Create a task that runs only if ``on_failed_of`` FAILS.

    Native (``capabilities().conditional_needs``): ``task_create(needs=[{"id": X, "if": "failed"}])``. Otherwise the task is
    created blocked (kind ``awaiting_failure``) and ``resolve_failure_branches`` settles it later.
    Returns ``{"task", "emulated", "outcome"}``."""
    pred = str(on_failed_of).strip()
    if not pred:
        raise ValueError("on_failed_of is required")
    if create_kw.get("needs") or create_kw.get("claim"):
        raise ValueError("failure_branch owns needs and claim")
    labels = dict(create_kw.pop("labels", None) or {})
    labels[L_FAILURE_OF] = pred
    caps = port.capabilities()
    if getattr(caps, "conditional_needs", False):
        doc = port.task_create(channel, text, labels=labels, needs=[{"id": pred, "if": "failed"}], **create_kw)
        return {"task": doc["task"], "emulated": False, "outcome": doc.get("outcome")}
    doc = port.task_create(channel, text, labels=labels, **create_kw)
    task = doc["task"]
    tid = str(task["id"])
    try:
        blocked = port.task_block(tid, f"awaiting failure of {pred}", kind=AWAITING_FAILURE)
    except Exception:
        try:
            port.task_cancel(tid, "failure_branch could not block the emulated task")
        except Exception:  # noqa: BLE001 - best effort cleanup; the original error matters
            pass
        raise
    return {"task": blocked.get("task", task), "emulated": True, "outcome": doc.get("outcome")}


def _task_view(item: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    msg = item.get("message") if isinstance(item.get("message"), Mapping) else item
    task = msg.get("task") if isinstance(msg.get("task"), Mapping) else {}
    return msg, task


def resolve_failure_branches(port: Any, channel: str) -> list[dict]:
    """Settle emulated failure branches: predecessor failed -> unblock; completed/canceled/missing -> cancel; still
    open -> leave. Returns one ``{"task_id", "predecessor", "predecessor_state", "action"}`` per branch seen."""
    doc = port.tasks(channel, state="blocked") or {}
    out: list[dict] = []
    for item in doc.get("tasks") or []:
        msg, task = _task_view(item)
        labels = msg.get("labels") if isinstance(msg.get("labels"), Mapping) else {}
        pred = labels.get(L_FAILURE_OF)
        blocked = task.get("blocked") if isinstance(task.get("blocked"), Mapping) else {}
        if not pred or blocked.get("kind") != AWAITING_FAILURE:
            continue
        tid = str(msg.get("id"))
        entry: dict[str, Any] = {"task_id": tid, "predecessor": str(pred), "predecessor_state": None, "action": "waiting"}
        try:
            state = ((port.read(str(pred)) or {}).get("message") or {}).get("task", {}).get("state")
        except Exception as exc:  # noqa: BLE001 - classify by code, never crash the sweep
            code = getattr(exc, "code", "")
            if code in ("not_found", "task_not_found", "message_not_found"):
                state = "missing"
            else:
                entry["action"] = "error"
                entry["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                out.append(entry)
                continue
        entry["predecessor_state"] = state
        try:
            if state == "failed":
                port.task_unblock(tid)
                entry["action"] = "unblocked"
            elif state in ("completed", "canceled", "missing"):
                port.task_cancel(tid, f"predecessor {state}: failure branch not needed")
                entry["action"] = "canceled"
        except Exception as exc:  # noqa: BLE001
            entry["action"] = "error"
            entry["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        out.append(entry)
    return out


# -- activity-aware staleness -----------------------------------------------------------------------------------------
def _latest_mtime(paths: Iterable[str], workspace: str | None, budget: int = 20000) -> float:
    latest = 0.0
    seen = 0
    for raw in paths:
        p = Path(raw)
        if not p.is_absolute() and workspace:
            p = Path(workspace) / p
        stack = [p]
        while stack and seen < budget:
            cur = stack.pop()
            try:
                st = cur.lstat()
            except OSError:
                continue
            seen += 1
            latest = max(latest, st.st_mtime)
            if cur.is_dir() and not cur.is_symlink():
                try:
                    with os.scandir(cur) as it:
                        for entry in it:
                            if entry.name == ".git":
                                continue
                            stack.append(Path(entry.path))
                except OSError:
                    continue
    return latest


def is_stale(last_heartbeat_s: float, paths: Sequence[str], *, now: float | None = None, idle_s: float = 1800,
             workspace: str | None = None) -> bool:
    """Stale only when the heartbeat is older than ``idle_s`` AND no file under ``paths`` changed within ``idle_s``."""
    current = time.time() if now is None else float(now)
    if current - float(last_heartbeat_s) <= idle_s:
        return False
    return current - _latest_mtime(paths or (), workspace) > idle_s


# -- durable handoff jobs ---------------------------------------------------------------------------------------------
@dataclass
class HandoffJob:
    job_id: str
    task_id: str | None
    channel: str
    to: str | None
    state: str                      # board task state, or "lost" | "unknown"
    result: str | None = None
    reason: str | None = None
    ok: bool = False                # FAIL CLOSED: True only for a completed task that carries a result

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _job_from_task(job_id: str, channel: str, msg: Mapping[str, Any]) -> HandoffJob:
    task = msg.get("task") if isinstance(msg.get("task"), Mapping) else {}
    state = str(task.get("state") or "unknown")
    result = task.get("result")
    result = None if result is None else str(result)
    return HandoffJob(job_id=job_id, task_id=str(msg.get("id")), channel=str(msg.get("channel") or channel),
                      to=task.get("for"), state=state, result=result,
                      reason=(str(task["reason"]) if task.get("reason") else None),
                      ok=state == "completed" and bool(result))


def submit_handoff(port: Any, channel: str, brief: str, *, job_id: str, to: str | None = None,
                   payload: Mapping[str, Any] | None = None, needs: Sequence[Any] = (), labels: Mapping[str, str] | None = None,
                   ack_due_s: float | None = None, clock: Callable[[], float] = time.time) -> HandoffJob:
    """Create the job task (idempotent per ``job_id``: a resubmit returns the same task). ``ack_due_s`` opts in to an
    ack deadline label."""
    job = str(job_id).strip()
    if not job or not brief.strip():
        raise ValueError("job_id and brief are required")
    text = brief.strip()
    if payload:
        text += "\n\n```json\n" + json.dumps(dict(payload), ensure_ascii=False, sort_keys=True) + "\n```"
    all_labels = {**dict(labels or {}), L_HANDOFF: "1", L_JOB_ID: job}
    if ack_due_s:
        all_labels[L_ACK_DUE] = to_iso(float(clock()) + float(ack_due_s))
    doc = port.task_create(channel, text[:TEXT_CAP], labels=all_labels, needs=list(needs), for_=to,
                           idempotency_key=f"handoff:{job}")
    return _job_from_task(job, channel, doc["task"])


def handoff_status(port: Any, task_id: str, *, job_id: str = "", channel: str = "") -> HandoffJob:
    """Current state of a job. Any read failure or unrecognised shape is ``unknown`` with ``ok=False``."""
    try:
        msg = (port.read(str(task_id)) or {}).get("message") or {}
        if not isinstance(msg.get("task"), Mapping):
            raise ValueError("not a task")
    except Exception as exc:  # noqa: BLE001 - fail closed
        return HandoffJob(job_id=job_id, task_id=str(task_id), channel=channel, to=None, state="unknown",
                          reason=f"{type(exc).__name__}: {str(exc)[:200]}", ok=False)
    labels = msg.get("labels") if isinstance(msg.get("labels"), Mapping) else {}
    return _job_from_task(job_id or str(labels.get(L_JOB_ID) or ""), channel, msg)


def finish_handoff(port: Any, task_id: str, *, result: str | None = None, error: str | None = None,
                   job_id: str = "", channel: str = "") -> HandoffJob:
    """Worker side (the caller holds the claim): persist ``result`` and complete, or ``error`` and fail. A long result is
    posted as a reply first and the task result points at it. A lost claim returns state ``lost`` (never success)."""
    tid = str(task_id)
    try:
        if error is not None:
            port.task_fail(tid, str(error)[:900])
        else:
            body = "" if result is None else str(result)
            if not body.strip():
                port.task_fail(tid, "handoff produced an empty result")
            elif len(body) > 1000:
                posted = port.reply(tid, body[:TEXT_CAP])
                port.task_complete(tid, f"see reply #{(posted.get('message') or {}).get('id')}")
            else:
                port.task_complete(tid, body)
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", "")
        if code in ("claim_lost", "claim_not_held", "task_finished"):
            return HandoffJob(job_id=job_id, task_id=tid, channel=channel, to=None, state="lost", reason=code, ok=False)
        raise
    return handoff_status(port, tid, job_id=job_id, channel=channel)
