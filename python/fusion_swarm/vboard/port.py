"""Board port (D5): one interface, three adapters, identical JSON documents.

    BoardPort
      HttpBoardAdapter     primary: http.client against any board speaking the vercel/board HTTP API (a supervised
                           `vb serve`, or Fusion's Python server, see engine.open_board)
      LocalEngineAdapter   zero-dependency fallback over vboard.store.BoardStore (in-process sqlite)
      VercelCliAdapter     hosted board via `vercel board ... --json --as <name>`; summaries and handoffs only

Every adapter returns the documents the stock `vercel board --json` prints (design/v4-spec.md 4.1, judge J-01..J-05,
J-18; evidence notes/g1-board-engine-runtime.md and notes/b3-cli.md):

    post/reply            {message, outcome:"posted", mentions}
    edit/delete/react/label {message:{id}, outcome}
    read                  ThreadsPage {threads, cursor, older} | ThreadView {message, replies, cursor, schedules}
    tasks                 {tasks, cursor}
    task_create/adopt     {task, outcome:"created"[, claim:{epoch, until}]}      (the claim token is never returned)
    task_claim            {task, claim:{epoch, until}}
    task_renew            {task, claim:{epoch, until}}
    task_release/complete/fail/block/unblock/reopen/assign/needs/cancel   {task, outcome}
    task_create_batch     {tasks, outcome:"created"}

The port keeps claim proofs itself. Local and Http adapters hold `{task_id: (epoch, token)}` in memory AND write the
shared claim cache (vboard.claims, the real CLI's file format) so `python -m fusion_swarm.vboard` or `vercel board`
can settle a claim the port made (and vice versa). The VercelCliAdapter relies on the real CLI's cache and always
passes the same `--as`.

Hosted hygiene (A1.5), enforced in VercelCliAdapter: writes stay far below the 300/min login cap because (a) renews
are floored (`renew_floor_s`, default 120 s, never more than 0.4 of the granted lease) and callers get jittered
intervals from `renew_interval()`; (b) batches are chunked at 15 (`Capabilities.batch_max`); (c) at most ~20 CLI
processes run at once per interpreter; (d) leases are always sent as durations (`300s`, a bare integer is exit 2 in
the stock CLI) and clamped to 300 s, with `claim.until` read back from the answer.

Stdlib only. Never prints or logs tokens.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import random
import re as _re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from . import VERSION
from .claims import ClaimCache, board_key
from .model import (FUSION_LABELS, PROFILE_RE, REASONS, Caller, VBoardError, normalize_profile, parse_iso)
from .store import BoardStore

log = logging.getLogger("fusion_swarm.vboard")

HOSTED_LEASE_CAP_S = 300
HOSTED_BATCH_MAX = 15
LOCAL_BATCH_MAX = 100
WAIT_SLICE_S = 25.0
RENEW_FLOOR_S = 120.0
MAX_CLI_PROCESSES = 20

_ID_RE = _re.compile(r"^(#?\d+|msg_[A-Za-z0-9_-]+)$")
_CLAIM_RETRY_CODES = frozenset({"task_claimed", "task_assigned", "task_finished", "task_waiting", "task_blocked",
                                "claim_lost"})
_PROFILE = _re.compile(PROFILE_RE)


# ============================================================================================= small helpers
def is_message_id(target: str | None) -> bool:
    """`812`, `#812` or a hosted `msg_<...>` id; anything else is a channel path."""
    return bool(target) and bool(_ID_RE.match(str(target)))


def _tid(task_id: Any) -> str:
    text = str(task_id if task_id is not None else "").strip()
    if text.startswith("#"):
        text = text[1:]
    if not text:
        raise VBoardError("invalid_request", "a task id is required")
    return text


def _label_args(label: Any) -> list[str]:
    """dict or sequence of `k=v` / `k` -> list of strings."""
    if not label:
        return []
    if isinstance(label, Mapping):
        return [f"{k}={v}" for k, v in label.items()]
    if isinstance(label, str):
        return [label]
    return [str(x) for x in label]


def _need_entries(needs: Sequence[Any] | None) -> list[Any]:
    return list(needs or ())


def _is_conditional(entry: Any) -> bool:
    return isinstance(entry, Mapping) and entry.get("if") not in (None, "completed")


def _need_id(entry: Any) -> str:
    if isinstance(entry, Mapping):
        if "id" not in entry:
            raise VBoardError("invalid_needs", "a need names an id")
        return _tid(entry["id"])
    return _tid(entry)


def _clamp_lease(lease: Any, cap: int) -> int | None:
    if lease is None:
        return None
    try:
        secs = float(lease)
    except (TypeError, ValueError):
        raise VBoardError("invalid_request", "lease is a number of seconds") from None
    if secs <= 0:
        raise VBoardError("invalid_request", "lease must be positive")
    return int(min(secs, cap))


def _new_token() -> str:
    return secrets.token_urlsafe(32)          # 43 chars: valid for the store (16..128) and the stock server (43)


def _mini(message_id: Any, outcome: str, **extra: Any) -> dict:
    out = {"message": {"id": str(message_id)}, "outcome": outcome}
    out.update(extra)
    return out


def _post_doc(doc: Mapping[str, Any]) -> dict:
    out = {"message": doc.get("message"), "outcome": "posted", "mentions": list(doc.get("mentions") or [])}
    return out


def _claim_doc(claim: Mapping[str, Any] | None) -> dict | None:
    if not claim:
        return None
    return {"epoch": claim.get("epoch"), "until": claim.get("until")}


def renew_interval(lease_s: float, *, floor_s: float = RENEW_FLOOR_S, jitter: float = 0.2,
                   rng: random.Random | None = None) -> float:
    """Seconds to wait before the next renew: jittered upward from max(floor, 0.4 * lease), never past 0.8 * lease.

    Hosted boards cap leases at 300 s and writes at 300/min per login, so a 100-wide campaign must not renew in
    lock step: lease 300 gives 120..144 s. A short lease wins over the floor (lease 60 gives <= 48 s)."""
    rnd = rng or random
    base = max(float(floor_s), 0.4 * float(lease_s))
    return min(base * (1.0 + float(jitter) * rnd.random()), 0.8 * float(lease_s))


# ============================================================================================= capabilities
@dataclass(frozen=True)
class Capabilities:
    backend: str                 # "local" | "vercel" | "http"
    version: str                 # engine/CLI version string
    help_hash: str | None        # sha256 of `vercel board --help` + `tasks --help` (vercel only), cached
    lease_cap_s: int             # local 3600; http 300 (or advertised); vercel 300
    tasks: bool = True
    batch: bool = True
    claim_wait: bool = True
    idempotency: bool = True
    subtree_read: bool = True    # local True; vercel False (501 not_supported)
    conditional_needs: bool = False
    priority_native: bool = False
    events_long_poll: bool = True
    batch_max: int = HOSTED_BATCH_MAX

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class BoardPort(Protocol):
    """Exact method names are frozen (design/v4-spec.md 0.5 / 4.1). One actor per port instance."""

    actor_name: str | None

    def capabilities(self) -> Capabilities: ...
    def whoami(self) -> dict: ...
    def channels(self, prefix: str | None = None) -> dict: ...
    def read(self, target: str | None = None, *, after=None, before=None, limit=None, label=(), by=None,
             mentions=None, text=None) -> dict: ...
    def post(self, target: str, text: str, *, labels: dict | None = None, idempotency_key: str | None = None,
             after: str | None = None, no_sync: bool = True) -> dict: ...
    def reply(self, message_id: str, text: str, **kw) -> dict: ...
    def edit(self, message_id, text, *, if_revision=None) -> dict: ...
    def delete(self, message_id, *, if_revision=None) -> dict: ...
    def react(self, message_id, emoji, *, remove=False) -> dict: ...
    def label(self, message_id, *, set=None, unset=(), if_=None, if_unset=()) -> dict: ...
    def wait(self, target: str | None = None, *, after: str | None = None, timeout: float = 1800, label=(),
             by=None, re=None, mentions=None, text=None) -> dict | None: ...
    def head(self) -> str: ...
    def tasks(self, channel: str | None = None, *, state="open", label=(), by=None, for_=None, after=None,
              limit=None) -> dict: ...
    def task_create(self, channel: str, text: str, *, labels=None, needs=(), for_=None, claim=False, lease=None,
                    idempotency_key=None, no_sync=True) -> dict: ...
    def task_adopt(self, message_id: str, **kw) -> dict: ...
    def task_create_batch(self, channel: str, plan: list[dict], *, idempotency_key: str) -> dict: ...
    def task_claim(self, task_id, *, lease=None) -> dict: ...
    def task_claim_next(self, channel, *, label: dict | None = None, from_=(), wait: float = 0, lease=None,
                        respect_priority: bool = True) -> dict | None: ...
    def task_renew(self, task_id, *, lease=None) -> dict: ...
    def task_release(self, task_id) -> dict: ...
    def task_complete(self, task_id, result: str | None = None) -> dict: ...
    def task_fail(self, task_id, reason: str) -> dict: ...
    def task_block(self, task_id, reason: str, *, kind: str | None = None) -> dict: ...
    def task_unblock(self, task_id) -> dict: ...
    def task_reopen(self, task_id) -> dict: ...
    def task_assign(self, task_id, profile: str | None) -> dict: ...
    def task_needs(self, task_id, needs: list) -> dict: ...
    def task_cancel(self, task_id, reason: str) -> dict: ...
    def close(self) -> None: ...


def probe(port: "BoardPort") -> Capabilities:
    """Capabilities of an open port (vercel: runs/reads the cached `--help` probe once)."""
    return port.capabilities()


# ============================================================================================= shared behaviour
class _PortBase:
    """Behaviour every adapter shares: batch chunking, priority/defer claim emulation, thin delegations.

    Subclasses implement the `_primitive` methods plus the public verbs that differ per backend."""

    actor_name: str | None = None
    backend = "?"

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self._clock = clock
        self._mono = monotonic
        self._sleep = sleep
        self._identity: dict | None = None
        self._ident_lock = threading.Lock()

    # -- context manager
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:  # pragma: no cover - overridden
        pass

    # -- identity (cached; emulation needs the principal and actor ids)
    def _me(self) -> dict:
        with self._ident_lock:
            if self._identity is None:
                self._identity = self.whoami()
            return self._identity

    # -- thin delegations
    def post(self, target: str, text: str, *, labels: dict | None = None, idempotency_key: str | None = None,
             after: str | None = None, no_sync: bool = True) -> dict:
        if is_message_id(target):
            return self._post(None, str(target), text, labels, idempotency_key, after, no_sync)
        return self._post(target, None, text, labels, idempotency_key, after, no_sync)

    def reply(self, message_id: str, text: str, *, labels: dict | None = None, idempotency_key: str | None = None,
              after: str | None = None, no_sync: bool = True) -> dict:
        return self._post(None, str(message_id), text, labels, idempotency_key, after, no_sync)

    def task_create(self, channel: str, text: str, *, labels=None, needs=(), for_=None, claim=False, lease=None,
                    idempotency_key=None, no_sync=True) -> dict:
        self._check_needs(needs)
        return self._task_create(channel, None, text, labels, list(needs or ()), for_, claim, lease,
                                 idempotency_key, no_sync)

    def task_adopt(self, message_id: str, *, labels=None, needs=(), for_=None, claim=False, lease=None,
                   idempotency_key=None, no_sync=True) -> dict:
        self._check_needs(needs)
        return self._task_create(None, str(message_id), None, labels, list(needs or ()), for_, claim, lease,
                                 idempotency_key, no_sync)

    def task_needs(self, task_id, needs: list) -> dict:
        self._check_needs(needs)
        return self._task_needs(_tid(task_id), list(needs or ()))

    def _check_needs(self, needs: Sequence[Any] | None) -> None:
        if any(_is_conditional(n) for n in _need_entries(needs)) and not self.capabilities().conditional_needs:
            raise VBoardError("not_supported",
                              f"the {self.backend} board has no conditional needs; use forms.collab.failure_branch "
                              "(watcher pattern) instead")

    # -- batch (J-18)
    def task_create_batch(self, channel: str, plan: list[dict], *, idempotency_key: str) -> dict:
        """Create a task graph in chunks of `capabilities().batch_max`, halving on `batch_too_large`.

        Chunk idempotency keys are `<key>:<offset>` (offset = position of the chunk's first item in `plan`), so a
        retry of the whole call after a partial failure re-creates nothing. A `{key}` need that points into an
        EARLIER chunk is rewritten to the created task's id; a reference into a later chunk is `invalid_needs`
        (order the plan so prerequisites come first)."""
        if not idempotency_key or not isinstance(idempotency_key, str):
            raise VBoardError("invalid_request", "task_create_batch needs an idempotency_key")
        items = list(plan or ())
        if not items:
            raise VBoardError("invalid_request", "the plan has no tasks")
        for item in items:
            for need in _need_entries(item.get("needs") if isinstance(item, Mapping) else None):
                if _is_conditional(need) and not self.capabilities().conditional_needs:
                    raise VBoardError("not_supported", f"the {self.backend} board has no conditional needs")
        caps = self.capabilities()
        if not caps.batch:
            raise VBoardError("not_supported", f"the {self.backend} board has no batch task creation")
        size = max(1, min(int(caps.batch_max or HOSTED_BATCH_MAX), len(items)))
        keys_ahead = {it["key"]: i for i, it in enumerate(items) if isinstance(it, Mapping) and it.get("key")}
        key_ids: dict[str, str] = {}
        created: list[dict] = []
        offset = 0
        while offset < len(items):
            chunk = [self._rewrite_keys(it, key_ids, keys_ahead, offset, offset + size) for it in
                     items[offset:offset + size]]
            try:
                res = self._batch_once(channel, chunk, f"{idempotency_key}:{offset}")
            except VBoardError as exc:
                if exc.code == "batch_too_large" and size > 1:
                    size = max(1, size // 2)
                    continue
                raise
            views = list(res.get("tasks") or [])
            for src, view in zip(items[offset:offset + len(chunk)], views):
                if isinstance(src, Mapping) and src.get("key") and isinstance(view, Mapping):
                    key_ids[src["key"]] = str(view.get("id"))
            created.extend(views)
            offset += len(chunk)
        return {"tasks": created, "outcome": "created"}

    @staticmethod
    def _rewrite_keys(item: Any, key_ids: dict[str, str], keys_ahead: dict[str, int], start: int, end: int) -> Any:
        if not isinstance(item, Mapping) or not item.get("needs"):
            return item
        needs: list[Any] = []
        for need in item["needs"]:
            if isinstance(need, Mapping) and "key" in need:
                key = need["key"]
                if key in key_ids:
                    rewritten: Any = {"id": key_ids[key]}
                    if need.get("if"):
                        rewritten["if"] = need["if"]
                    needs.append(rewritten if need.get("if") else key_ids[key])
                    continue
                pos = keys_ahead.get(key)
                if pos is not None and pos >= end:
                    raise VBoardError("invalid_needs",
                                      f"task key {key!r} is created in a later chunk; order the plan so "
                                      "prerequisites come first")
            needs.append(need)
        out = dict(item)
        out["needs"] = needs
        return out

    # -- ready-queue claim with priority/defer emulation (4.3)
    def _claim_next_emulated(self, channel: str, *, label: dict | None, from_: Sequence[str], wait: float,
                             lease: Any, sort_priority: bool) -> dict | None:
        labels = _label_args(label)
        deadline = self._mono() + max(0.0, float(wait or 0))
        cursor = self._head_or_none() if wait and wait > 0 else None
        while True:
            got = self._try_claim_ready(channel, labels, from_, lease, sort_priority)
            if got is not None:
                return got
            remaining = deadline - self._mono()
            if not wait or remaining <= 0:
                return None
            event = self.wait(channel, after=cursor, timeout=min(remaining, WAIT_SLICE_S),
                              label=["board.task=ready"])
            if event and event.get("cursor") is not None:
                cursor = event["cursor"]

    def _head_or_none(self) -> str | None:
        try:
            return self.head()
        except VBoardError:
            return None

    def _try_claim_ready(self, channel: str, labels: list[str], from_: Sequence[str], lease: Any,
                         sort_priority: bool) -> dict | None:
        me = self._me()
        owners = {me.get("principal")} | set(from_ or ())
        page = self.tasks(channel, state="ready", label=labels, limit=500)
        now_ms = int(self._clock() * 1000)
        cands: list[tuple[int, int, int, str]] = []
        for i, summary in enumerate(page.get("tasks") or []):
            msg = summary.get("message") or {}
            task = msg.get("task") or {}
            labs = msg.get("labels") or {}
            defer = labs.get(FUSION_LABELS["defer_until"])
            if defer:
                try:
                    if parse_iso(defer) > now_ms:
                        continue
                except VBoardError:
                    pass
            assigned = task.get("for")
            mine = bool(assigned) and assigned == me.get("actor")
            if assigned and not mine:
                continue
            owner = task.get("owner")
            if owner is not None and owner not in owners:
                continue
            prio = 0
            if sort_priority:
                try:
                    prio = int(labs.get(FUSION_LABELS["priority"], "0"))
                except (TypeError, ValueError):
                    prio = 0
            cands.append((0 if mine else 1, -prio, i, str(msg.get("id"))))
        for *_rank, tid in sorted(cands):
            try:
                return self.task_claim(tid, lease=lease)
            except VBoardError as exc:
                if exc.code in _CLAIM_RETRY_CODES:
                    continue
                raise
        return None


# ============================================================================================= claim proofs
class _Proofs:
    """In-memory `{task_id: (epoch, token)}` plus the shared claim cache (so the CLI can settle our claims)."""

    def _init_proofs(self, board: str, cache: ClaimCache | None, profile: str | None) -> None:
        self._board = board
        self._cache = cache
        self._profile = profile
        self._proofs: dict[str, tuple[int, str]] = {}
        self._plock = threading.Lock()

    def _proof_save(self, task_id: str, claim: Mapping[str, Any], token: str) -> None:
        epoch = int(claim["epoch"])
        with self._plock:
            self._proofs[task_id] = (epoch, token)
        if self._cache is not None:
            try:
                self._cache.put(self._board, task_id, self._profile, epoch, token, claim["until"])
            except OSError as exc:  # a read-only config dir must not break claiming
                log.warning("claim cache not written: %s", exc.__class__.__name__)

    def _proof_load(self, task_id: str) -> tuple[int, str]:
        with self._plock:
            hit = self._proofs.get(task_id)
        if hit is not None:
            return hit
        if self._cache is not None:
            doc = self._cache.get(self._board, task_id, self._profile)
            if doc and isinstance(doc.get("epoch"), int) and isinstance(doc.get("token"), str):
                return int(doc["epoch"]), doc["token"]
        who = f" as {self._profile}" if self._profile else ""
        raise VBoardError("claim_not_held", f"You hold no claim on task #{task_id}{who}.",
                          hint=f"claim it first (task_claim {task_id})")

    def _proof_touch(self, task_id: str, epoch: int, token: str, until: str) -> None:
        if self._cache is not None:
            try:
                self._cache.put(self._board, task_id, self._profile, epoch, token, until)
            except OSError:
                pass

    def _proof_forget(self, task_id: str) -> None:
        with self._plock:
            self._proofs.pop(task_id, None)
        if self._cache is not None:
            self._cache.drop(self._board, task_id, self._profile)

    def _with_proof(self, task_id: str, call: Callable[[int, str], dict], *, keep: bool = False) -> dict:
        epoch, token = self._proof_load(task_id)
        try:
            out = call(epoch, token)
        except VBoardError as exc:
            if exc.code in ("claim_lost", "task_finished"):
                self._proof_forget(task_id)
            raise
        if not keep:
            self._proof_forget(task_id)
        return out


# ============================================================================================= LocalEngineAdapter
class LocalEngineAdapter(_PortBase, _Proofs):
    """In-process adapter over `BoardStore` (zero dependencies). Claim proofs live in memory and in the claim cache."""

    backend = "local"

    def __init__(self, store: BoardStore | str | os.PathLike, *, as_: str | None = None,
                 caller: Caller | None = None, claim_cache: bool | ClaimCache = True,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        super().__init__(clock=clock, monotonic=monotonic, sleep=sleep)
        if isinstance(store, BoardStore):
            self.store = store
            self._own_store = False
        else:
            self.store = BoardStore(store)
            self._own_store = True
        self.caller = caller if caller is not None else Caller.local(normalize_profile(as_))
        self.actor_name = self.caller.profile
        cache = claim_cache if isinstance(claim_cache, ClaimCache) else (ClaimCache() if claim_cache else None)
        self._init_proofs(board_key(db_path=self.store.path), cache, self.caller.profile)

    def close(self) -> None:
        if self._own_store:
            self.store.close()

    # -- capabilities / identity
    def capabilities(self) -> Capabilities:
        return Capabilities(backend="local", version=VERSION, help_hash=None, lease_cap_s=3600, tasks=True,
                            batch=True, claim_wait=True, idempotency=True, subtree_read=True,
                            conditional_needs=True, priority_native=True, events_long_poll=True,
                            batch_max=LOCAL_BATCH_MAX)

    def whoami(self) -> dict:
        return self.store.me(self.caller)

    def channels(self, prefix: str | None = None) -> dict:
        out = self.store.channels(self.caller, prefix=prefix)
        out.pop("cursor", None)
        return out

    # -- messages
    def read(self, target: str | None = None, *, after=None, before=None, limit=None, label=(), by=None,
             mentions=None, text=None) -> dict:
        if target and is_message_id(target):
            kw: dict[str, Any] = {"after": after, "label": _label_args(label)}
            if limit is not None:
                kw["limit"] = limit
            return self.store.thread(self.caller, str(target), **kw)
        kw = {"channel": target, "by": by, "mentions": mentions, "label": _label_args(label), "text": text,
              "after": after, "before": before}
        if limit is not None:
            kw["limit"] = limit
        return self.store.threads(self.caller, **kw)

    def _post(self, channel, re, text, labels, idempotency_key, after, no_sync) -> dict:
        return _post_doc(self.store.post(self.caller, text=text, channel=channel, re=re, labels=labels,
                                         client_id=idempotency_key, after=after, no_sync=no_sync))

    def edit(self, message_id, text, *, if_revision=None) -> dict:
        out = self.store.edit(self.caller, str(message_id), text, revision=if_revision)
        return _mini((out.get("message") or {}).get("id", message_id), "edited")

    def delete(self, message_id, *, if_revision=None) -> dict:
        out = self.store.delete(self.caller, str(message_id), revision=if_revision)
        return _mini((out.get("message") or {}).get("id", message_id), "deleted")

    def react(self, message_id, emoji, *, remove=False) -> dict:
        out = self.store.react(self.caller, str(message_id), emoji, remove=remove)
        return _mini((out.get("message") or {}).get("id", message_id), "unreacted" if remove else "reacted")

    def label(self, message_id, *, set=None, unset=(), if_=None, if_unset=()) -> dict:
        cond = dict(if_ or {})
        cond.update({k: None for k in (if_unset or ())})
        out = self.store.label(self.caller, str(message_id), set=set, unset=tuple(unset or ()), if_=cond or None)
        return _mini((out.get("message") or {}).get("id", message_id), "labeled")

    # -- feed
    def head(self) -> str:
        return self.store.head()

    def wait(self, target: str | None = None, *, after: str | None = None, timeout: float = 1800, label=(),
             by=None, re=None, mentions=None, text=None) -> dict | None:
        channel, root = None, re
        if target:
            if is_message_id(target):
                root = self.store.thread(self.caller, str(target), limit=1)["message"]["thread"]
                if after is None:
                    after = str(target).lstrip("#")
            else:
                channel = target
        if after is None:
            after = self.store.head()
        deadline = self._mono() + max(0.0, float(timeout))
        while True:
            remaining = deadline - self._mono()
            res = self.store.events(self.caller, after=after, wait=max(0.0, min(remaining, WAIT_SLICE_S)),
                                    channel=channel, re=root, by=by, mentions=mentions, label=_label_args(label),
                                    text=text)
            if res["events"]:
                return res["events"][0]
            after = res["cursor"]
            if self._mono() >= deadline:
                return None

    # -- tasks
    def tasks(self, channel: str | None = None, *, state="open", label=(), by=None, for_=None, after=None,
              limit=None) -> dict:
        kw: dict[str, Any] = {"channel": channel, "state": state, "label": _label_args(label), "by": by,
                              "for_": for_, "after": after}
        if limit is not None:
            kw["limit"] = limit
        return self.store.list_tasks(self.caller, **kw)

    def _task_create(self, channel, re, text, labels, needs, for_, claim, lease, idempotency_key, no_sync) -> dict:
        token = _new_token() if claim else None
        res = self.store.create_task(self.caller, channel=channel, text=text, re=re, labels=labels, needs=needs,
                                     for_=for_, claim=bool(claim), lease=lease, token=token,
                                     client_id=idempotency_key, no_sync=no_sync)
        out: dict[str, Any] = {"task": res["task"], "outcome": "created"}
        if res.get("claim"):
            self._proof_save(str(res["task"]["id"]), res["claim"], res["claim"].get("token") or token)
            out["claim"] = _claim_doc(res["claim"])
        return out

    def _batch_once(self, channel: str, chunk: list, key: str) -> dict:
        return self.store.create_tasks(self.caller, channel, chunk, client_id=key)

    def task_claim(self, task_id, *, lease=None) -> dict:
        tid = _tid(task_id)
        token = _new_token()
        res = self.store.claim(self.caller, tid, lease=lease, token=token)
        self._proof_save(str(res["task"]["id"]), res["claim"], res["claim"].get("token") or token)
        return {"task": res["task"], "claim": _claim_doc(res["claim"])}

    def task_claim_next(self, channel, *, label: dict | None = None, from_=(), wait: float = 0, lease=None,
                        respect_priority: bool = True) -> dict | None:
        if not respect_priority:
            return self._claim_next_emulated(channel, label=label, from_=from_, wait=wait, lease=lease,
                                             sort_priority=False)
        token = _new_token()
        res = self.store.claim_next(self.caller, channel, label=dict(label or {}) or None, from_=tuple(from_ or ()),
                                    lease=lease, wait=float(wait or 0), token=token)
        if not res.get("task"):
            return None
        self._proof_save(str(res["task"]["id"]), res["claim"], res["claim"].get("token") or token)
        return {"task": res["task"], "claim": _claim_doc(res["claim"])}

    def task_renew(self, task_id, *, lease=None) -> dict:
        tid = _tid(task_id)

        def call(epoch: int, token: str) -> dict:
            res = self.store.renew(self.caller, tid, epoch=epoch, token=token, lease=lease)
            self._proof_touch(tid, epoch, token, res["until"])
            return {"task": res["task"], "claim": {"epoch": epoch, "until": res["until"]}}

        return self._with_proof(tid, call, keep=True)

    def task_release(self, task_id) -> dict:
        tid = _tid(task_id)
        return self._with_proof(tid, lambda e, t: self._settled(self.store.release(self.caller, tid, epoch=e, token=t)))

    def task_complete(self, task_id, result: str | None = None) -> dict:
        tid = _tid(task_id)
        return self._with_proof(
            tid, lambda e, t: self._settled(self.store.complete(self.caller, tid, epoch=e, token=t, result=result)))

    def task_fail(self, task_id, reason: str) -> dict:
        tid = _tid(task_id)
        return self._with_proof(
            tid, lambda e, t: self._settled(self.store.fail(self.caller, tid, epoch=e, token=t, reason=reason)))

    def task_block(self, task_id, reason: str, *, kind: str | None = None) -> dict:
        tid = _tid(task_id)
        try:
            epoch, token = self._proof_load(tid)
        except VBoardError:
            return self._settled(self.store.block(self.caller, tid, reason=reason, kind=kind))
        try:
            out = self.store.block(self.caller, tid, reason=reason, kind=kind, epoch=epoch, token=token)
        except VBoardError as exc:
            if exc.code in ("claim_lost", "task_finished"):
                self._proof_forget(tid)
            raise
        self._proof_forget(tid)
        return self._settled(out)

    def task_unblock(self, task_id) -> dict:
        return self._settled(self.store.unblock(self.caller, _tid(task_id)))

    def task_reopen(self, task_id) -> dict:
        return self._settled(self.store.reopen(self.caller, _tid(task_id)))

    def task_assign(self, task_id, profile: str | None) -> dict:
        return self._settled(self.store.assign(self.caller, _tid(task_id), profile))

    def _task_needs(self, task_id: str, needs: list) -> dict:
        return self._settled(self.store.set_needs(self.caller, task_id, needs))

    def task_cancel(self, task_id, reason: str) -> dict:
        tid = _tid(task_id)
        out = self.store.cancel(self.caller, tid, reason=reason)
        self._proof_forget(tid)
        return self._settled(out)

    @staticmethod
    def _settled(doc: Mapping[str, Any]) -> dict:
        return {"task": doc["task"], "outcome": doc.get("outcome") or "ok"}


# ============================================================================================= HttpBoardAdapter
_DISCONNECT = (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, ConnectionAbortedError,
               http.client.CannotSendRequest, http.client.ResponseNotReady, http.client.IncompleteRead)


def token_file_path(url: str, env: Mapping[str, str] | None = None) -> Path:
    """`~/.config/board/local/<sha256(url)[:32]>.json` (the loopback token file `vb serve` writes)."""
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or ""
    root = Path(base) if base else Path(env.get("HOME") or os.path.expanduser("~")) / ".config"
    digest = hashlib.sha256(url.rstrip("/").encode("utf-8")).hexdigest()[:32]
    return root / "board" / "local" / f"{digest}.json"


class HttpBoardAdapter(_PortBase, _Proofs):
    """http.client against the vercel/board HTTP API. Primary adapter (A1.1, J-02).

    - Sends `x-board-profile: <as_>`; bearer token from `token=`, else `BOARD_TOKEN`, else the loopback token file
      (re-read once on a 401 so an engine restart is survived). The token is never logged.
    - The CLIENT generates every claim token and always sends it, so a retried claim with the same token is
      success; claim proofs are kept in memory and in the shared claim cache.
    - Long waits are sliced to <= 25 s requests (socket timeout = slice + 5 s). Idle keep-alive connections are
      closed by the server after ~5 s: on a disconnect the call reconnects once and retries (settle verbs and
      token claims are idempotent; posts always carry a client id, generated when the caller gave none).
    - Capabilities come from the discovery document: Fusion's Python server advertises
      `fusion: {conditional_needs, priority, defer, batch_max}`; without it everything Fusion-specific is False.
    """

    backend = "http"

    def __init__(self, url: str, *, as_: str | None = None, token: str | None = None, timeout: float = 60,
                 env: Mapping[str, str] | None = None, claim_cache: bool | ClaimCache = True,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, default_lease_s: int = HOSTED_LEASE_CAP_S):
        super().__init__(clock=clock, monotonic=monotonic, sleep=sleep)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"not a board url: {url!r}")
        self.url = url.rstrip("/")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._base = parsed.path.rstrip("/")
        self.actor_name = normalize_profile(as_) if as_ else None
        self._env = os.environ if env is None else env
        self._explicit_token = token
        self._token: str | None = None
        self._timeout = float(timeout)
        self._default_lease = int(default_lease_s)
        self._tl = threading.local()
        self._caps: Capabilities | None = None
        cache = claim_cache if isinstance(claim_cache, ClaimCache) else (
            ClaimCache(env=self._env) if claim_cache else None)
        self._init_proofs(board_key(url=self.url), cache, self.actor_name)

    # -- connection plumbing
    def close(self) -> None:
        conn = getattr(self._tl, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
            self._tl.conn = None

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=timeout)
        self._tl.conn = conn
        return conn

    def _bearer(self, reload: bool = False) -> str | None:
        if self._explicit_token:
            return self._explicit_token
        if self._token is None or reload:
            tok = self._env.get("BOARD_TOKEN") or None
            if not tok:
                try:
                    doc = json.loads(token_file_path(self.url, self._env).read_text(encoding="utf-8"))
                    tok = doc.get("token") if isinstance(doc, dict) else None
                except (OSError, ValueError):
                    tok = None
            self._token = tok
        return self._token

    def _headers(self, body: bytes | None, auth: bool, reload: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json", "Connection": "keep-alive"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.actor_name:
            headers["x-board-profile"] = self.actor_name
        if auth:
            tok = self._bearer(reload)
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
        return headers

    def _request(self, method: str, path: str, *, query: Sequence[tuple[str, Any]] | None = None,
                 body: Mapping[str, Any] | None = None, auth: bool = True, retry: bool = True,
                 timeout: float | None = None) -> dict:
        full = self._base + path
        if query:
            pairs = [(k, str(v)) for k, v in query if v is not None]
            if pairs:
                full += "?" + urllib.parse.urlencode(pairs)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        sock_timeout = float(timeout if timeout is not None else self._timeout)
        reloaded = False
        throttled = 0
        reconnects = 0
        while True:
            conn = getattr(self._tl, "conn", None)
            if conn is None:
                conn = self._connect(sock_timeout)
            conn.timeout = sock_timeout
            if conn.sock is not None:
                conn.sock.settimeout(sock_timeout)
            try:
                conn.request(method, full, body=payload, headers=self._headers(payload, auth, reloaded))
                resp = conn.getresponse()
                raw = resp.read()
                status = resp.status
                retry_after = resp.getheader("Retry-After")
                if (resp.getheader("Connection") or "").lower() == "close":
                    conn.close()
                    self._tl.conn = None
            except _DISCONNECT as exc:
                self.close()
                if retry and reconnects < 1:
                    reconnects += 1
                    continue
                raise VBoardError("network_error", f"the board closed the connection ({exc.__class__.__name__})") \
                    from None
            except (socket.timeout, TimeoutError):
                self.close()
                raise VBoardError("network_error", "the board did not answer in time") from None
            except OSError as exc:
                self.close()
                raise VBoardError("network_error", f"cannot reach the board at {self._host}:{self._port} "
                                                   f"({exc.__class__.__name__})") from None
            doc = self._decode(raw)
            if 200 <= status < 300:
                return doc
            if status == 401 and auth and not reloaded and not self._explicit_token:
                reloaded = True
                if self._bearer(reload=True):
                    continue
            err = self._error(status, doc, retry_after)
            if err.code == "rate_limited" and throttled < 3:
                throttled += 1
                self._sleep(min(float(err.retry_after if err.retry_after is not None else 2 ** throttled), 30.0))
                continue
            raise err

    @staticmethod
    def _decode(raw: bytes) -> dict:
        if not raw:
            return {}
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"_raw": raw[:200].decode("utf-8", "replace")}
        return doc if isinstance(doc, dict) else {"_value": doc}

    @staticmethod
    def _error(status: int, doc: Mapping[str, Any], retry_after: str | None) -> VBoardError:
        inner = doc.get("error") if isinstance(doc.get("error"), Mapping) else None
        wait: float | None = None
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = None
        if inner and isinstance(inner.get("code"), str):
            return VBoardError(inner["code"], str(inner.get("message") or inner["code"]), status=status,
                               hint=inner.get("hint"), retry_after=wait)
        fallback = {400: "invalid_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                    409: "label_conflict", 413: "too_large", 429: "rate_limited", 501: "not_supported",
                    503: "unavailable"}.get(status, "internal" if status >= 500 else "unexpected_error")
        return VBoardError(fallback, f"the board answered HTTP {status}", status=status, retry_after=wait)

    # -- capabilities / identity
    def capabilities(self) -> Capabilities:
        if self._caps is None:
            disc = self._request("GET", "/", auth=False, retry=True)
            fus = disc.get("fusion") if isinstance(disc.get("fusion"), Mapping) else {}
            loop = self._host in ("127.0.0.1", "::1", "localhost")
            self._caps = Capabilities(
                backend="http", version=str(disc.get("version") or "unknown"), help_hash=None,
                lease_cap_s=int(fus.get("lease_cap_s") or HOSTED_LEASE_CAP_S), tasks=True, batch=True,
                claim_wait=True, idempotency=True, subtree_read=bool(fus.get("subtree_read", loop)),
                conditional_needs=bool(fus.get("conditional_needs", False)),
                priority_native=bool(fus.get("priority", False)), events_long_poll=True,
                batch_max=int(fus.get("batch_max") or HOSTED_BATCH_MAX))
        return self._caps

    def whoami(self) -> dict:
        me = dict(self._request("GET", "/me"))
        me.setdefault("board", self.url)
        me.setdefault("source", "http")
        if self.actor_name is None and me.get("profile"):
            self.actor_name = me["profile"]
        return me

    def channels(self, prefix: str | None = None) -> dict:
        out = self._request("GET", "/channels", query=[("prefix", prefix)])
        out.pop("cursor", None)
        out.setdefault("next", None)
        return out

    # -- messages
    def read(self, target: str | None = None, *, after=None, before=None, limit=None, label=(), by=None,
             mentions=None, text=None) -> dict:
        if target and is_message_id(target):
            q = [("after", after), ("limit", limit)] + [("label", x) for x in _label_args(label)]
            return self._request("GET", f"/messages/{urllib.parse.quote(str(target).lstrip('#'), safe='')}", query=q)
        q = [("channel", target), ("by", by), ("mentions", mentions), ("text", text), ("after", after),
             ("before", before), ("limit", limit)] + [("label", x) for x in _label_args(label)]
        return self._request("GET", "/threads", query=q)

    def _post(self, channel, re, text, labels, idempotency_key, after, no_sync) -> dict:
        body: dict[str, Any] = {"text": text, "noSync": bool(no_sync), "id": idempotency_key or secrets.token_hex(16)}
        if re is not None:
            body["re"] = str(re).lstrip("#")
            if after is not None:
                body["after"] = after
        else:
            body["channel"] = channel
        if labels:
            body["labels"] = dict(labels)
        return _post_doc(self._request("POST", "/messages", body=body))

    def _msg_path(self, message_id: Any, tail: str) -> str:
        return f"/messages/{urllib.parse.quote(str(message_id).lstrip('#'), safe='')}/{tail}"

    def edit(self, message_id, text, *, if_revision=None) -> dict:
        body: dict[str, Any] = {"text": text}
        if if_revision is not None:
            body["revision"] = if_revision
        self._request("POST", self._msg_path(message_id, "edit"), body=body, retry=if_revision is None)
        return _mini(str(message_id).lstrip("#"), "edited")

    def delete(self, message_id, *, if_revision=None) -> dict:
        body = {"revision": if_revision} if if_revision is not None else {}
        self._request("POST", self._msg_path(message_id, "delete"), body=body, retry=if_revision is None)
        return _mini(str(message_id).lstrip("#"), "deleted")

    def react(self, message_id, emoji, *, remove=False) -> dict:
        body: dict[str, Any] = {"emoji": emoji}
        if remove:
            body["remove"] = True
        self._request("POST", self._msg_path(message_id, "reactions"), body=body)
        return _mini(str(message_id).lstrip("#"), "unreacted" if remove else "reacted")

    def label(self, message_id, *, set=None, unset=(), if_=None, if_unset=()) -> dict:
        cond = dict(if_ or {})
        cond.update({k: None for k in (if_unset or ())})
        body: dict[str, Any] = {}
        if set:
            body["set"] = dict(set)
        if unset:
            body["unset"] = list(unset)
        if cond:
            body["if"] = cond
        self._request("POST", self._msg_path(message_id, "labels"), body=body, retry=not cond)
        return _mini(str(message_id).lstrip("#"), "labeled")

    # -- feed
    def head(self) -> str:
        return str(self._request("GET", "/events", query=[("wait", 0)]).get("cursor"))

    def wait(self, target: str | None = None, *, after: str | None = None, timeout: float = 1800, label=(),
             by=None, re=None, mentions=None, text=None) -> dict | None:
        channel, root = None, re
        if target:
            if is_message_id(target):
                view = self._request("GET", f"/messages/{urllib.parse.quote(str(target).lstrip('#'), safe='')}",
                                     query=[("limit", 1)])
                root = (view.get("message") or {}).get("thread") or str(target).lstrip("#")
                if after is None:
                    after = str(target).lstrip("#")
            else:
                channel = target
        if after is None:
            after = self.head()
        deadline = self._mono() + max(0.0, float(timeout))
        while True:
            slice_s = max(0.0, min(deadline - self._mono(), WAIT_SLICE_S))
            q = [("after", after), ("wait", int(slice_s)), ("channel", channel), ("re", root), ("by", by),
                 ("mentions", mentions), ("text", text)] + [("label", x) for x in _label_args(label)]
            res = self._request("GET", "/events", query=q, timeout=slice_s + 5.0)
            events = res.get("events") or []
            if events:
                return events[0]
            if res.get("cursor") is not None:
                after = res["cursor"]
            if self._mono() >= deadline:
                return None

    # -- tasks
    def tasks(self, channel: str | None = None, *, state="open", label=(), by=None, for_=None, after=None,
              limit=None) -> dict:
        q = [("channel", channel), ("state", state), ("by", by), ("for", for_), ("after", after),
             ("limit", limit)] + [("label", x) for x in _label_args(label)]
        return self._request("GET", "/tasks", query=q)

    def _lease(self, lease: Any) -> int:
        cap = self.capabilities().lease_cap_s
        got = _clamp_lease(lease, cap)
        return got if got is not None else min(self._default_lease, cap)

    def _task_create(self, channel, re, text, labels, needs, for_, claim, lease, idempotency_key, no_sync) -> dict:
        body: dict[str, Any] = {"id": idempotency_key or secrets.token_hex(16)}
        if re is not None:
            body["re"] = str(re).lstrip("#")
        else:
            body.update({"channel": channel, "text": text, "noSync": bool(no_sync)})
        if labels:
            body["labels"] = dict(labels)
        if needs:
            body["needs"] = [(_need_id(n) if not _is_conditional(n) else {"id": _need_id(n), "if": n["if"]})
                             for n in needs]
        if for_:
            body["for"] = for_
        token = None
        if claim:
            token = _new_token()
            body.update({"claim": True, "lease": self._lease(lease), "token": token})
        res = self._request("POST", "/tasks", body=body)
        out: dict[str, Any] = {"task": res["task"], "outcome": "created"}
        if res.get("claim"):
            self._proof_save(str(res["task"]["id"]), res["claim"], res["claim"].get("token") or token)
            out["claim"] = _claim_doc(res["claim"])
        return out

    def _batch_once(self, channel: str, chunk: list, key: str) -> dict:
        return self._request("POST", "/tasks/batch", body={"channel": channel, "tasks": chunk, "id": key})

    def task_claim(self, task_id, *, lease=None) -> dict:
        tid = _tid(task_id)
        token = _new_token()
        res = self._request("POST", f"/tasks/{urllib.parse.quote(tid, safe='')}/claim",
                            body={"lease": self._lease(lease), "token": token})
        self._proof_save(str(res["task"]["id"]), res["claim"], res["claim"].get("token") or token)
        return {"task": res["task"], "claim": _claim_doc(res["claim"])}

    def task_claim_next(self, channel, *, label: dict | None = None, from_=(), wait: float = 0, lease=None,
                        respect_priority: bool = True) -> dict | None:
        caps = self.capabilities()
        if respect_priority and not caps.priority_native or (not respect_priority and caps.priority_native):
            return self._claim_next_emulated(channel, label=label, from_=from_, wait=wait, lease=lease,
                                             sort_priority=bool(respect_priority))
        token = _new_token()
        lease_s = self._lease(lease)
        deadline = self._mono() + max(0.0, float(wait or 0))
        while True:
            slice_s = max(0.0, min(deadline - self._mono(), WAIT_SLICE_S))
            body: dict[str, Any] = {"channel": channel, "lease": lease_s, "wait": int(slice_s), "token": token}
            if label:
                body["label"] = {str(k): str(v) for k, v in label.items()}
            if from_:
                body["from"] = list(from_)
            res = self._request("POST", "/tasks/claim", body=body, timeout=slice_s + 5.0)
            if res.get("task"):
                self._proof_save(str(res["task"]["id"]), res["claim"], res["claim"].get("token") or token)
                return {"task": res["task"], "claim": _claim_doc(res["claim"])}
            if self._mono() >= deadline:
                return None

    def _tpath(self, task_id: str, verb: str) -> str:
        return f"/tasks/{urllib.parse.quote(task_id, safe='')}/{verb}"

    def task_renew(self, task_id, *, lease=None) -> dict:
        tid = _tid(task_id)

        def call(epoch: int, token: str) -> dict:
            body: dict[str, Any] = {"epoch": epoch, "token": token, "lease": self._lease(lease)}
            res = self._request("POST", self._tpath(tid, "renew"), body=body)
            until = res.get("until") or (res.get("claim") or {}).get("until")
            self._proof_touch(tid, epoch, token, until)
            return {"task": res["task"], "claim": {"epoch": epoch, "until": until}}

        return self._with_proof(tid, call, keep=True)

    def _settle(self, tid: str, verb: str, outcome: str, extra: Mapping[str, Any] | None = None) -> dict:
        def call(epoch: int, token: str) -> dict:
            body = {"epoch": epoch, "token": token, **(extra or {})}
            res = self._request("POST", self._tpath(tid, verb), body=body)
            return {"task": res["task"], "outcome": res.get("outcome") or outcome}

        return self._with_proof(tid, call)

    def task_release(self, task_id) -> dict:
        return self._settle(_tid(task_id), "release", "released")

    def task_complete(self, task_id, result: str | None = None) -> dict:
        return self._settle(_tid(task_id), "complete", "completed", {"result": result} if result is not None else None)

    def task_fail(self, task_id, reason: str) -> dict:
        return self._settle(_tid(task_id), "fail", "failed", {"reason": reason})

    def _plain(self, tid: str, verb: str, outcome: str, body: Mapping[str, Any] | None = None) -> dict:
        res = self._request("POST", self._tpath(tid, verb), body=dict(body or {}))
        return {"task": res["task"], "outcome": res.get("outcome") or outcome}

    def task_block(self, task_id, reason: str, *, kind: str | None = None) -> dict:
        tid = _tid(task_id)
        body: dict[str, Any] = {"reason": reason}
        if kind:
            body["kind"] = kind
        try:
            epoch, token = self._proof_load(tid)
        except VBoardError:
            return self._plain(tid, "block", "blocked", body)
        body.update({"epoch": epoch, "token": token})
        try:
            out = self._plain(tid, "block", "blocked", body)
        except VBoardError as exc:
            if exc.code in ("claim_lost", "task_finished"):
                self._proof_forget(tid)
            raise
        self._proof_forget(tid)
        return out

    def task_unblock(self, task_id) -> dict:
        return self._plain(_tid(task_id), "unblock", "unblocked")

    def task_reopen(self, task_id) -> dict:
        return self._plain(_tid(task_id), "reopen", "reopened")

    def task_assign(self, task_id, profile: str | None) -> dict:
        out = self._plain(_tid(task_id), "assign", "assigned" if profile else "pooled", {"for": profile})
        return out

    def _task_needs(self, task_id: str, needs: list) -> dict:
        wire = [(_need_id(n) if not _is_conditional(n) else {"id": _need_id(n), "if": n["if"]}) for n in needs]
        return self._plain(task_id, "needs", "set", {"needs": wire})

    def task_cancel(self, task_id, reason: str) -> dict:
        tid = _tid(task_id)
        out = self._plain(tid, "cancel", "canceled", {"reason": reason})
        self._proof_forget(tid)
        return out


# ============================================================================================= VercelCliAdapter
_CLI_SLOTS = threading.BoundedSemaphore(MAX_CLI_PROCESSES)
_DURATION_RE = _re.compile(r"(\d+(?:\.\d+)?)")


def fusion_home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("FUSION_HOME") or (Path(env.get("HOME") or os.path.expanduser("~")) / ".fusion"))


def parse_failure_document(stderr: str | None, stdout: str | None) -> dict | None:
    """Failure document of a `vercel board --json` run: whole JSON object from stderr, else from stdout, else the
    first balanced `{...}` in either stream, else the last non-empty stderr line (J-05). Standalone `vb` prints one
    stderr line; vercel 66.2.0 prints a pretty-printed document on BOTH streams with extra `cliVersion`,
    `invocationId`, `feedback` keys and no `next[].argv`: callers read only status/reason/message/data."""
    dec = json.JSONDecoder()
    texts = [t for t in (stderr, stdout) if t and t.strip()]

    def usable(doc: Any) -> bool:
        return isinstance(doc, dict) and any(k in doc for k in ("reason", "status", "data", "error"))

    for text in texts:
        try:
            doc = json.loads(text.strip())
        except ValueError:
            continue
        if usable(doc):
            return doc
    for text in texts:
        pos = text.find("{")
        while pos != -1:
            try:
                doc, _end = dec.raw_decode(text, pos)
            except ValueError:
                pos = text.find("{", pos + 1)
                continue
            if usable(doc):
                return doc
            pos = text.find("{", pos + 1)
    if stderr:
        for line in reversed([ln for ln in stderr.splitlines() if ln.strip()]):
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if usable(doc):
                return doc
    return None


def _failure_to_error(doc: Mapping[str, Any], rc: int) -> VBoardError:
    data = doc.get("data") if isinstance(doc.get("data"), Mapping) else {}
    inner = data.get("error") if isinstance(data.get("error"), Mapping) else {}
    reason = doc.get("reason") if isinstance(doc.get("reason"), str) else inner.get("code")
    reason = reason if isinstance(reason, str) and reason else "unexpected_error"
    message = str(doc.get("message") or inner.get("message") or reason)
    status = inner.get("status")
    retry_after = inner.get("retryAfter")
    if not isinstance(retry_after, (int, float)) or isinstance(retry_after, bool):
        retry_after = None
    keep: dict[str, Any] = {"cli_rc": rc}
    if isinstance(doc.get("next"), list):
        keep["next"] = [n for n in doc["next"] if isinstance(n, Mapping)][:5]
    if doc.get("status") == "action_required":
        keep["action_required"] = True
    return VBoardError(reason, message, status=status if isinstance(status, int) else None,
                       hint=doc.get("hint") if isinstance(doc.get("hint"), str) else None,
                       retry_after=float(retry_after) if retry_after is not None else None, data=keep)


class VercelCliAdapter(_PortBase):
    """Hosted board through `vercel board <verb> --json --as <name>`. Summaries and handoffs only.

    argv is always `[vercel_bin, "board", *verb, "--json", "--as", as_ (+ --url URL | --name NAME)]`; never `--sync`,
    never `--scope` unless given in `scope_args`. Success is the JSON document on stdout; a failure is parsed with
    `parse_failure_document` and raised as `VBoardError`. Retries: `rate_limited` (sleep retryAfter, max 3) and
    `network_error` (1/2/4 s, max 3, only for calls that are safe to repeat); exit 2 is never retried; `wait`
    timeout is `None`. Claim proofs are the real CLI's claim cache (same `--as`, same XDG_CONFIG_HOME)."""

    backend = "vercel"

    def __init__(self, *, as_: str, vercel_bin: str = "vercel", url: str | None = None, name: str | None = None,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run, timeout: float = 120,
                 env: Mapping[str, str] | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 scope_args: Sequence[str] = (), renew_floor_s: float = RENEW_FLOOR_S,
                 cache_path: str | os.PathLike | None = None):
        super().__init__(clock=clock, monotonic=monotonic, sleep=sleep)
        if not as_ or not isinstance(as_, str) or not _PROFILE.match(as_):
            raise VBoardError("invalid_profile", "VercelCliAdapter needs a profile name (--as): letters, digits, "
                                                 "hyphen, starting with a letter, at most 32 characters",
                              data={"action_required": True})
        if url and name:
            raise VBoardError("conflicting_options", "name a board by url or by name, not both")
        self.as_ = as_.lower()
        self.actor_name = self.as_
        self.vercel_bin = vercel_bin
        self.url = url
        self.name = name
        self._runner = runner
        self._timeout = float(timeout)
        self._env_override = None if env is None else dict(env)
        self._scope_args = list(scope_args)
        self._renew_floor = float(renew_floor_s)
        self._cache_path = Path(cache_path) if cache_path else None
        self._caps: Capabilities | None = None
        self._features: dict[str, Any] = {}
        self._renew_seen: dict[str, tuple[float, float, dict]] = {}
        self._rlock = threading.Lock()

    # -- argv / execution
    def argv(self, *verb: str) -> list[str]:
        """`[vercel_bin, "board", *verb, "--json", "--as", as_ (+ "--url", url | "--name", name)]`.

        A literal `--` inside `verb` ends the options: everything after it is passed after the option block."""
        verb_l = list(verb)
        tail: list[str] = []
        if "--" in verb_l:
            cut = verb_l.index("--")
            verb_l, tail = verb_l[:cut], verb_l[cut:]
        scope = ["--url", self.url] if self.url else (["--name", self.name] if self.name else [])
        return [self.vercel_bin, "board", *verb_l, "--json", "--as", self.as_, *scope, *self._scope_args, *tail]

    def _env(self) -> dict[str, str]:
        env = dict(os.environ if self._env_override is None else self._env_override)
        if self.url:
            env.pop("BOARD_NAME", None)
        elif self.name:
            env.pop("BOARD_URL", None)
        return env

    def _cache_file(self) -> Path:
        return self._cache_path or (fusion_home(self._env()) / "cache" / "vboard-capabilities.json")

    def _spawn(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        try:
            with _CLI_SLOTS:
                return self._runner(argv, capture_output=True, text=True, timeout=timeout, env=self._env())
        except FileNotFoundError:
            raise VBoardError("command_not_found", f"cannot run {self.vercel_bin!r}: is the Vercel CLI installed?",
                              status=501) from None
        except subprocess.TimeoutExpired:
            raise VBoardError("network_error", "the vercel CLI did not finish in time") from None
        except OSError as exc:
            raise VBoardError("network_error", f"cannot run the vercel CLI ({exc.__class__.__name__})") from None

    def _exec(self, verb: Sequence[str], *, repeatable: bool = True, extra_timeout: float = 0.0,
              allow: Sequence[str] = ()) -> dict:
        argv = self.argv(*verb)
        limited = 0
        netfail = 0
        while True:
            proc = self._spawn(argv, self._timeout + extra_timeout)
            rc = proc.returncode
            if rc == 0:
                out = (proc.stdout or "").strip()
                try:
                    doc = json.loads(out) if out else {}
                except ValueError:
                    raise VBoardError("unexpected_error", "the vercel CLI printed a document that is not JSON",
                                      data={"cli_rc": 0}) from None
                if not isinstance(doc, dict):
                    raise VBoardError("unexpected_error", "the vercel CLI printed a JSON value, not an object")
                return doc
            fail = parse_failure_document(proc.stderr, proc.stdout)
            if fail is None:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
                raise VBoardError("unexpected_error", f"vercel board exited {rc}: {tail[0][:200]}",
                                  data={"cli_rc": rc})
            err = _failure_to_error(fail, rc)
            if err.code in allow:
                raise err
            if rc == 2:
                raise err                                    # usage / missing input: never retried
            if err.code == "rate_limited" and limited < 3:
                limited += 1
                self._sleep(min(float(err.retry_after if err.retry_after is not None else 2 ** limited), 30.0))
                continue
            if err.code == "network_error" and repeatable and netfail < 3:
                self._sleep(float(2 ** netfail))
                netfail += 1
                continue
            raise err

    # -- capability probe (D3)
    def capabilities(self) -> Capabilities:
        if self._caps is None:
            self._caps = self._probe()
        return self._caps

    def _help(self, *verb: str) -> str:
        proc = self._spawn([self.vercel_bin, *verb], 30.0)
        return (proc.stdout or "") + (proc.stderr or "")

    def _probe(self) -> Capabilities:
        version_out = self._help("--version").strip()
        board_help = self._help("board", "--help")
        tasks_help = self._help("board", "tasks", "--help")
        digest = hashlib.sha256((board_help + "\0" + tasks_help).encode("utf-8")).hexdigest()
        path = self._cache_file()
        cache: dict[str, Any] = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            cache = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            cache = {}
        entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
        hit = entries.get(digest)
        if not isinstance(hit, dict):
            hit = {"file": "--file" in tasks_help, "ready": "--ready" in tasks_help,
                   "wait": "--wait" in tasks_help, "idempotency_key": "--idempotency-key" in (board_help + tasks_help)}
            entries[digest] = hit
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
                tmp.write_text(json.dumps({"entries": entries}), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                pass
        self._features = dict(hit)
        match = _re.search(r"\d+\.\d+\.\d+\S*", version_out)
        version = match.group(0) if match else (version_out.splitlines() or ["unknown"])[0][:40]
        return Capabilities(backend="vercel", version=version, help_hash=digest, lease_cap_s=HOSTED_LEASE_CAP_S,
                            tasks=True, batch=bool(hit.get("file")), claim_wait=bool(hit.get("wait")),
                            idempotency=bool(hit.get("idempotency_key")), subtree_read=False,
                            conditional_needs=False, priority_native=False, events_long_poll=True,
                            batch_max=HOSTED_BATCH_MAX)

    # -- helpers
    @staticmethod
    def _dur(seconds: Any) -> str:
        return f"{int(seconds)}s"

    def _lease_flag(self, lease: Any) -> list[str]:
        got = _clamp_lease(lease, HOSTED_LEASE_CAP_S)
        return ["--lease", self._dur(got if got is not None else HOSTED_LEASE_CAP_S)]

    def _build(self, verb: Sequence[str], positionals: Sequence[str] = (), flags: Sequence[str] = ()) -> list[str]:
        """verb words, then positionals, then flags; positionals starting with `-` go after a `--`."""
        pos = [str(p) for p in positionals]
        if any(p.startswith("-") for p in pos):
            return [*verb, *flags, "--", *pos]
        return [*verb, *pos, *flags]

    @staticmethod
    def _multi(flag: str, values: Sequence[str]) -> list[str]:
        out: list[str] = []
        for v in values:
            out += [flag, v]
        return out

    # -- identity / reads
    def whoami(self) -> dict:
        return self._exec(["whoami"])

    def channels(self, prefix: str | None = None) -> dict:
        out = self._exec(self._build(["channels"], [prefix] if prefix else []))
        out.pop("cursor", None)
        out.setdefault("next", None)
        return out

    def read(self, target: str | None = None, *, after=None, before=None, limit=None, label=(), by=None,
             mentions=None, text=None) -> dict:
        flags = self._multi("--label", _label_args(label))
        for flag, val in (("--after", after), ("--before", before), ("--limit", limit), ("--by", by),
                          ("--text", text)):
            if val is not None:
                flags += [flag, str(val)]
        if mentions is not None:
            flags += ["--mentions", str(mentions)]
        return self._exec(self._build(["read"], [target] if target else [], flags))

    def _post(self, channel, re, text, labels, idempotency_key, after, no_sync) -> dict:
        flags = self._multi("--label", _label_args(labels))
        if idempotency_key:
            flags += ["--idempotency-key", idempotency_key]
        if after is not None and re is not None:
            flags += ["--after", str(after)]
        if no_sync:
            flags += ["--no-sync"]
        verb = ["reply"] if re is not None else ["post"]
        doc = self._exec(self._build(verb, [str(re if re is not None else channel), text], flags),
                         repeatable=bool(idempotency_key))
        doc.setdefault("mentions", [])
        doc["outcome"] = doc.get("outcome") or "posted"
        return doc

    def edit(self, message_id, text, *, if_revision=None) -> dict:
        flags = ["--if-revision", str(if_revision)] if if_revision is not None else []
        doc = self._exec(self._build(["edit"], [_tid(message_id), text], flags), repeatable=if_revision is None)
        return _mini((doc.get("message") or {}).get("id", _tid(message_id)), doc.get("outcome") or "edited")

    def delete(self, message_id, *, if_revision=None) -> dict:
        flags = ["--if-revision", str(if_revision)] if if_revision is not None else []
        doc = self._exec(self._build(["delete"], [_tid(message_id)], flags), repeatable=if_revision is None)
        return _mini((doc.get("message") or {}).get("id", _tid(message_id)), doc.get("outcome") or "deleted")

    def react(self, message_id, emoji, *, remove=False) -> dict:
        doc = self._exec(self._build(["react"], [_tid(message_id), emoji], ["--remove"] if remove else []))
        return _mini((doc.get("message") or {}).get("id", _tid(message_id)),
                     doc.get("outcome") or ("unreacted" if remove else "reacted"))

    def label(self, message_id, *, set=None, unset=(), if_=None, if_unset=()) -> dict:
        pairs = [f"{k}={v}" for k, v in (set or {}).items()]
        flags = self._multi("--unset", list(unset or ()))
        flags += self._multi("--if", _label_args(if_))
        flags += self._multi("--if-unset", list(if_unset or ()))
        doc = self._exec(self._build(["label"], [_tid(message_id), *pairs], flags),
                         repeatable=not (if_ or if_unset))
        return _mini((doc.get("message") or {}).get("id", _tid(message_id)), doc.get("outcome") or "labeled")

    def head(self) -> str:
        """Feed cursor to start waiting from: the cursor of a one-thread `read` (the CLI documents passing a
        `read` cursor as `--after`)."""
        doc = self._exec(["read", "--limit", "1"])
        if doc.get("cursor") is None:
            raise VBoardError("not_supported", "this vercel CLI did not return a feed cursor")
        return str(doc["cursor"])

    def wait(self, target: str | None = None, *, after: str | None = None, timeout: float = 1800, label=(),
             by=None, re=None, mentions=None, text=None) -> dict | None:
        secs = max(1, int(float(timeout)))
        flags = ["--timeout", self._dur(secs)] + self._multi("--label", _label_args(label))
        for flag, val in (("--after", after), ("--by", by), ("--re", re), ("--text", text)):
            if val is not None:
                flags += [flag, str(val)]
        if mentions is not None:
            flags += ["--mentions", str(mentions)]
        try:
            doc = self._exec(self._build(["wait"], [target] if target else [], flags), extra_timeout=secs + 30.0,
                             allow=("timeout",))
        except VBoardError as exc:
            if exc.code == "timeout":
                return None
            raise
        return doc.get("event")

    # -- tasks
    def tasks(self, channel: str | None = None, *, state="open", label=(), by=None, for_=None, after=None,
              limit=None) -> dict:
        if channel and channel.endswith("/"):
            raise VBoardError("not_supported", "the hosted board does not read channel subtrees")
        flags = ["--state", state] + self._multi("--label", _label_args(label))
        for flag, val in (("--by", by), ("--for", for_), ("--after", after), ("--limit", limit)):
            if val is not None:
                flags += [flag, str(val)]
        return self._exec(self._build(["tasks"], [channel] if channel else [], flags))

    def _task_create(self, channel, re, text, labels, needs, for_, claim, lease, idempotency_key, no_sync) -> dict:
        flags = self._multi("--label", _label_args(labels))
        flags += self._multi("--needs", [_need_id(n) for n in needs])
        if for_:
            flags += ["--for", for_]
        if claim:
            flags += ["--claim", *self._lease_flag(lease)]
        if idempotency_key:
            flags += ["--idempotency-key", idempotency_key]
        if no_sync and re is None:
            flags += ["--no-sync"]
        pos = [str(re)] if re is not None else [channel, text]
        doc = self._exec(self._build(["tasks", "create"], pos, flags), repeatable=bool(idempotency_key))
        out: dict[str, Any] = {"task": doc["task"], "outcome": doc.get("outcome") or "created"}
        if doc.get("claim"):
            out["claim"] = _claim_doc(doc["claim"])
            self._remember_grant(str(doc["task"]["id"]), doc["claim"], doc["task"])
        return out

    def _batch_once(self, channel: str, chunk: list, key: str) -> dict:
        fd, name = tempfile.mkstemp(prefix="fusion-plan-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(chunk, fh, ensure_ascii=False)
            doc = self._exec(["tasks", "create", channel, "--file", name, "--idempotency-key", key])
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass
        return {"tasks": doc.get("tasks") or []}

    def _remember_grant(self, tid: str, claim: Mapping[str, Any], task: Mapping[str, Any] | None = None) -> None:
        until = claim.get("until")
        try:
            granted = max(0.0, parse_iso(until) / 1000.0 - self._clock()) if until else 0.0
        except VBoardError:
            granted = 0.0
        with self._rlock:
            self._renew_seen[tid] = (self._mono(), granted,
                                     {"task": task, "claim": _claim_doc(claim)} if task is not None else {})

    def task_claim(self, task_id, *, lease=None) -> dict:
        tid = _tid(task_id)
        doc = self._exec(["tasks", "claim", tid, *self._lease_flag(lease)], repeatable=False)
        self._remember_grant(str(doc["task"]["id"]), doc["claim"], doc["task"])
        return {"task": doc["task"], "claim": _claim_doc(doc["claim"])}

    def task_claim_next(self, channel, *, label: dict | None = None, from_=(), wait: float = 0, lease=None,
                        respect_priority: bool = True) -> dict | None:
        caps = self.capabilities()
        if respect_priority or not self._features.get("ready", True):
            return self._claim_next_emulated(channel, label=label, from_=from_, wait=wait, lease=lease,
                                             sort_priority=bool(respect_priority))
        flags = self._multi("--label", _label_args(label)) + self._multi("--from", list(from_ or ()))
        secs = int(max(0.0, float(wait or 0)))
        if secs and caps.claim_wait:
            flags += ["--wait", self._dur(secs)]
        flags += self._lease_flag(lease)
        try:
            doc = self._exec(["tasks", "claim", "--ready", channel, *flags], repeatable=False,
                             extra_timeout=secs + 30.0, allow=("nothing_ready", "timeout"))
        except VBoardError as exc:
            if exc.code in ("nothing_ready", "timeout"):
                return None
            raise
        self._remember_grant(str(doc["task"]["id"]), doc["claim"], doc["task"])
        return {"task": doc["task"], "claim": _claim_doc(doc["claim"])}

    def task_renew(self, task_id, *, lease=None) -> dict:
        tid = _tid(task_id)
        with self._rlock:
            seen = self._renew_seen.get(tid)
        if seen is not None and seen[1] > 0:
            floor = min(self._renew_floor, 0.4 * seen[1])
            if self._mono() - seen[0] < floor and seen[2]:
                return seen[2]                                # hosted hygiene: too soon, the claim is still good
        doc = self._exec(["tasks", "renew", tid, *self._lease_flag(lease)])
        out = {"task": doc["task"], "claim": _claim_doc(doc["claim"])}
        try:
            granted = max(0.0, parse_iso(out["claim"]["until"]) / 1000.0 - self._clock())
        except VBoardError:
            granted = 0.0
        with self._rlock:
            self._renew_seen[tid] = (self._mono(), granted, out)
        return out

    def _settle(self, tid: str, verb: str, positionals: Sequence[str] = (), flags: Sequence[str] = (),
                outcome: str = "") -> dict:
        try:
            doc = self._exec(self._build(["tasks", verb, tid], positionals, flags))
        finally:
            pass
        with self._rlock:
            self._renew_seen.pop(tid, None)
        return {"task": doc["task"], "outcome": doc.get("outcome") or outcome}

    def task_release(self, task_id) -> dict:
        return self._settle(_tid(task_id), "release", outcome="released")

    def task_complete(self, task_id, result: str | None = None) -> dict:
        return self._settle(_tid(task_id), "complete", [result] if result is not None else [], outcome="completed")

    def task_fail(self, task_id, reason: str) -> dict:
        return self._settle(_tid(task_id), "fail", [reason], outcome="failed")

    def task_block(self, task_id, reason: str, *, kind: str | None = None) -> dict:
        return self._settle(_tid(task_id), "block", [reason], ["--kind", kind] if kind else [], "blocked")

    def task_unblock(self, task_id) -> dict:
        return self._settle(_tid(task_id), "unblock", outcome="unblocked")

    def task_reopen(self, task_id) -> dict:
        return self._settle(_tid(task_id), "reopen", outcome="reopened")

    def task_assign(self, task_id, profile: str | None) -> dict:
        if profile:
            return self._settle(_tid(task_id), "assign", [profile], outcome="assigned")
        return self._settle(_tid(task_id), "assign", flags=["--pool"], outcome="pooled")

    def _task_needs(self, task_id: str, needs: list) -> dict:
        ids = [_need_id(n) for n in needs]
        if ids:
            return self._settle(task_id, "needs", ids, outcome="set")
        return self._settle(task_id, "needs", flags=["--none"], outcome="set")

    def task_cancel(self, task_id, reason: str) -> dict:
        return self._settle(_tid(task_id), "cancel", [reason], outcome="canceled")


# ============================================================================================= open_port
def _resolve_profile(as_: str | None, env: Mapping[str, str]) -> str | None:
    return as_ or env.get("BOARD_PROFILE") or env.get("FUSION_AGENT_ID") or None


def open_port(spec: str | None = None, *, as_: str | None = None, env: Mapping[str, str] | None = None) -> BoardPort:
    """Open a port from a spec (or env `FUSION_VBOARD`): `local` (default db), `local:/abs/path.db`,
    `http://127.0.0.1:4100/api/board` (or https), `vercel`, `vercel:<name>`. Anything else is `ValueError`.

    Identity: `as_` > `BOARD_PROFILE` > `FUSION_AGENT_ID`; the vercel backend requires one."""
    env = os.environ if env is None else env
    spec = (spec if spec is not None else env.get("FUSION_VBOARD")) or "local"
    spec = spec.strip()
    profile = _resolve_profile(as_, env)
    if spec == "local":
        path = fusion_home(env) / "vboard" / "default.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return LocalEngineAdapter(path, as_=profile)
    if spec.startswith("local:"):
        raw = spec[len("local:"):]
        if not raw:
            raise ValueError("local: needs a database path")
        path = Path(raw).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return LocalEngineAdapter(path, as_=profile)
    if spec.startswith(("http://", "https://")):
        return HttpBoardAdapter(spec, as_=profile, env=env)
    if spec == "vercel" or spec.startswith("vercel:"):
        name = spec[len("vercel:"):] or None if spec != "vercel" else None
        return VercelCliAdapter(as_=profile or "", name=name, env=dict(env) if env is not os.environ else None)
    raise ValueError(f"unknown board spec {spec!r}: use local, local:<path>, http(s)://<board url>, vercel or "
                     "vercel:<name>")


__all__ = ["BoardPort", "Capabilities", "HttpBoardAdapter", "LocalEngineAdapter", "VercelCliAdapter", "open_port",
           "probe", "renew_interval", "parse_failure_document", "token_file_path", "is_message_id", "fusion_home"]
