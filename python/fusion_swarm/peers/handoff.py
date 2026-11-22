"""Durable handoff over the board (D9/D10): a job is a board task labelled `fusion.handoff=1`.

    submit()     create the task (idempotent by key); job_id = task id
    status()     read the task; state/result/reason
    wait()       block until the task reaches completed/failed/canceled/blocked or the timeout passes
    get_result() the full result text of a completed job (long results live in a reply)
    serve_one()  claim the next handoff task, run a handler under a renewing lease, persist the outcome

FAIL CLOSED: success is only ever reported after the board accepted `task_complete`. If the claim is lost (lease
expired, task canceled or taken over) or delivery cannot be confirmed, nothing is posted and the returned job says so
(`lost` / `undelivered`). Results over 1000 characters are posted as a reply first (the board caps a task result at
1000 chars) and the task is completed with `see reply #<id>`.

Identity is the shared one (`peers.identity.resolve_name`: BOARD_PROFILE > FUSION_AGENT_ID > host default).
Board text is untrusted data; message text is capped at 10,000 characters and `board.` labels are reserved.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from ..vboard.model import VBoardError
from ..vboard.port import BoardPort, open_port
from . import identity as _identity
from .policy import PeerError

LABEL = "fusion.handoff"
TEXT_MAX = 10_000
RESULT_INLINE_MAX = 1000
REASON_MAX = 900
TERMINAL = ("completed", "failed", "canceled")
WAIT_LABEL = "board.task=completed|failed|blocked|canceled"
WAIT_SLICE_S = 25.0
LOST_CODES = frozenset({"claim_lost", "task_finished", "claim_not_held", "task_not_owned"})
_POINTER = re.compile(r"^see reply #(\S+)$")


@dataclass
class HandoffJob:
    job_id: str
    channel: str
    to: str | None
    state: str
    result: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def open_board(spec: str | None = None, *, as_: str | None = None,
               env: Mapping[str, str] | None = None) -> BoardPort:
    """Open the board named by `spec` (or env FUSION_VBOARD) as the shared identity."""
    e = os.environ if env is None else env
    name = as_ or _identity.resolve_name(e)
    try:
        return open_port(spec, as_=name, env=e)
    except ValueError as exc:
        raise PeerError("invalid_request", str(exc)) from None
    except VBoardError as exc:
        raise _wrap(exc) from None


def _wrap(exc: VBoardError) -> PeerError:
    if exc.code in ("invalid_request", "message_not_found", "not_a_task", "invalid_profile", "reserved_label",
                    "text_too_long", "invalid_needs", "duplicate_id"):
        return PeerError("invalid_request", f"{exc.code}: {exc.message}")
    return PeerError("peer_failed", f"board {exc.code}: {exc.message}")


def _job_from(message: Mapping, replies: Sequence[Mapping] | None = None) -> HandoffJob:
    task = message.get("task") if isinstance(message.get("task"), Mapping) else {}
    result = task.get("result")
    if isinstance(result, str):
        m = _POINTER.match(result.strip())
        if m is not None:
            found = _reply_text(replies or (), m.group(1))
            if found is not None:
                result = found
    for_ = task.get("for")
    return HandoffJob(job_id=str(message.get("id")), channel=str(message.get("channel") or ""),
                      to=str(for_) if for_ else None, state=str(task.get("state") or "unknown"),
                      result=result if isinstance(result, str) else None,
                      reason=task.get("reason") if isinstance(task.get("reason"), str) else None)


def _reply_text(replies: Sequence[Mapping], reply_id: str) -> str | None:
    for rep in replies:
        if str(rep.get("id")) == reply_id and isinstance(rep.get("text"), str):
            text = rep["text"]
            head, sep, body = text.partition("\n")
            return body if sep and head.startswith("[fusion.handoff result") else text
    return None


def _check_labels(labels: Mapping | None) -> dict:
    out: dict = {}
    for k, v in (labels or {}).items():
        if str(k).startswith("board."):
            raise PeerError("invalid_request", "board.* labels are reserved")
        out[str(k)] = v
    return out


def submit(port: BoardPort, channel: str, brief: str, *, to: str | None = None, payload: dict | None = None,
           idempotency_key: str, needs: Sequence[str] = (), labels: dict | None = None) -> HandoffJob:
    if not isinstance(brief, str) or not brief.strip():
        raise PeerError("invalid_request", "brief is required")
    if not idempotency_key:
        raise PeerError("invalid_request", "idempotency_key is required")
    text = brief
    if payload is not None:
        text += "\n\n```json\n" + json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n```"
    if len(text) > TEXT_MAX:
        raise PeerError("invalid_request", f"brief too long ({len(text)} > {TEXT_MAX} chars)")
    all_labels = {LABEL: "1", **_check_labels(labels)}
    try:
        doc = port.task_create(channel, text, labels=all_labels, needs=list(needs), idempotency_key=idempotency_key,
                               for_=_identity.normalize(to) if to else None)
    except VBoardError as exc:
        raise _wrap(exc) from None
    job = _job_from(doc["task"])
    if to and not job.to:
        job.to = _identity.normalize(to)
    return job


def _read(port: BoardPort, job_id: str) -> dict:
    try:
        view = port.read(str(job_id))
    except VBoardError as exc:
        if exc.code in ("message_not_found", "not_found"):
            raise PeerError("invalid_request", f"unknown job {job_id}") from None
        raise
    return view


def status(port: BoardPort, job_id: str) -> HandoffJob:
    try:
        view = _read(port, job_id)
    except VBoardError as exc:
        raise _wrap(exc) from None
    return _job_from(view["message"], view.get("replies"))


def wait(port: BoardPort, job_id: str, *, timeout: float = 1800, poll_after: str | None = None,
         mono: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> HandoffJob:
    """Return the job once it is completed/failed/canceled/blocked; on timeout return it in its current open state."""
    deadline = mono() + max(0.0, float(timeout))
    after = poll_after
    while True:
        try:
            view = _read(port, job_id)
            job = _job_from(view["message"], view.get("replies"))
            if job.state in TERMINAL or job.state == "blocked":
                return job
            remaining = deadline - mono()
            if remaining <= 0:
                return job
            event = port.wait(str(job_id), after=after, timeout=min(remaining, WAIT_SLICE_S), label=(WAIT_LABEL,))
            if isinstance(event, Mapping) and event.get("cursor") is not None:
                after = str(event["cursor"])
        except VBoardError as exc:
            if not exc.retryable or mono() >= deadline:
                raise _wrap(exc) from None
            sleep(min(0.5, max(0.0, deadline - mono())))


def get_result(port: BoardPort, job_id: str) -> str | None:
    """Full result text of a completed job; None while it is still open; PeerError if it failed or was canceled."""
    job = status(port, job_id)
    if job.state == "completed":
        return job.result if job.result is not None else ""
    if job.state in ("failed", "canceled"):
        raise PeerError("peer_failed", f"job {job_id} {job.state}: {job.reason or 'no reason given'}")
    return None


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 15] + "\n[truncated]"


def serve_one(port: BoardPort, channel: str, handler: Callable[[dict], str], *, lease: float = 300,
              wait_s: float = 0, label: dict | None = None, renew_every: float | None = None) -> HandoffJob | None:
    """Claim the next handoff task on `channel`, run `handler(task_view)` and settle the task. Returns None when
    nothing was ready. See the module docstring for the fail-closed rules."""
    labels = {LABEL: "1", **_check_labels(label)}
    try:
        claimed = port.task_claim_next(channel, label=labels, wait=wait_s, lease=lease)
    except VBoardError as exc:
        if exc.code in ("nothing_ready", "no_tasks"):
            return None
        raise _wrap(exc) from None
    if not claimed:
        return None
    task = claimed["task"]
    tid = str(task["id"])
    base = _job_from(task)
    lost = threading.Event()
    stop = threading.Event()
    interval = renew_every if renew_every is not None else max(0.5, float(lease) / 3.0)

    def renewer() -> None:
        while not stop.wait(interval):
            try:
                port.task_renew(tid, lease=lease)
            except VBoardError as exc:
                if exc.code in LOST_CODES:
                    lost.set()
                    return
                if exc.retryable:
                    continue
                lost.set()
                return
            except Exception:  # noqa: BLE001 - unknown failure: fail closed
                lost.set()
                return

    thread = threading.Thread(target=renewer, name=f"handoff-renew-{tid}", daemon=True)
    thread.start()

    def finish(state: str, result: str | None = None, reason: str | None = None) -> HandoffJob:
        return HandoffJob(job_id=tid, channel=base.channel, to=base.to, state=state, result=result, reason=reason)

    try:
        try:
            output = handler(task)
            failure = None
        except Exception as exc:  # noqa: BLE001 - the handler's failure becomes the task's failure
            output, failure = None, f"{type(exc).__name__}: {str(exc)[:REASON_MAX]}"
    finally:
        stop.set()
        thread.join(timeout=5)
    if lost.is_set():
        return finish("lost", reason="claim lost during handler")
    try:
        if failure is not None:
            port.task_fail(tid, failure[:REASON_MAX])
            return finish("failed", reason=failure[:REASON_MAX])
        text = output if isinstance(output, str) else ("" if output is None else str(output))
        recorded = text
        if len(text) > RESULT_INLINE_MAX:
            header = f"[fusion.handoff result for job {tid}]\n"
            try:
                rep = port.reply(tid, header + _truncate(text, TEXT_MAX - len(header)),
                                 idempotency_key=f"handoff-{tid}-result-{claimed.get('claim', {}).get('epoch', 0)}")
            except VBoardError as exc:
                if exc.code in LOST_CODES:
                    return finish("lost", reason=f"claim lost: {exc.code}")
                port.task_fail(tid, f"result reply failed: {exc.code}"[:REASON_MAX])
                return finish("failed", reason=f"result reply failed: {exc.code}")
            rid = str((rep.get("message") or {}).get("id"))
            recorded = f"see reply #{rid}"
        port.task_complete(tid, recorded or None)
        return finish("completed", result=text)
    except VBoardError as exc:
        if exc.code in LOST_CODES:
            return finish("lost", reason=f"claim lost: {exc.code}")
        return finish("undelivered", reason=f"board {exc.code}: {exc.message}"[:REASON_MAX])
