"""Shared runtime for swarm forms (spec sections 0.6, 0.7, 8.2; judge J-14, J-16).

Everything a form needs: a budget envelope (calls / tokens / wall deadline), a cancellation token that kills process
groups, a cancellable command verifier, a call/parallel API with first-k early cancellation and no barrier on losers,
and the journal/checkpoint/ledger wiring. Stdlib only; ``spawn`` and ``process_runtime`` are imported lazily.
"""
from __future__ import annotations

import concurrent.futures as cf
import dataclasses
import hashlib
import inspect
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .journal import Journal, write_atomic
from .ledger import ReliabilityLedger

FUSION_HOME = Path(os.environ.get("FUSION_HOME", "~/.fusion")).expanduser()
RUNS_DIR = FUSION_HOME / "forms" / "runs"

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DISPATCH_STATUSES = {"returned", "timeout", "absent", "error", "cancelled", "blocked"}


def fusion_home() -> Path:
    """FUSION_HOME resolved at call time (tests point it at a temp dir after import)."""
    return Path(os.environ.get("FUSION_HOME", "~/.fusion")).expanduser()


def runs_dir() -> Path:
    return fusion_home() / "forms" / "runs"


class FormError(Exception):
    """code in {"budget_exhausted","cancelled","invalid_input","no_providers","verifier_required"}."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


def new_run_id(form: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", form)[:48] or "form"
    return f"{safe}-{stamp}-{secrets.token_hex(3)}"


# -- budget -----------------------------------------------------------------------------------------------------
@dataclass
class Budget:
    max_calls: int | None = None
    max_tokens: int | None = None
    deadline_s: float | None = None
    monotonic: Callable[[], float] = time.monotonic
    calls_used: int = field(default=0, init=False)
    tokens_used: int = field(default=0, init=False)
    _t0: float | None = field(default=None, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)

    def start(self) -> "Budget":
        with self._lock:
            if self._t0 is None:
                self._t0 = self.monotonic()
        return self

    def elapsed(self) -> float:
        with self._lock:
            return 0.0 if self._t0 is None else max(0.0, self.monotonic() - self._t0)

    def _exhausted_locked(self) -> str | None:
        if self.max_calls is not None and self.calls_used >= self.max_calls:
            return "calls"
        if self.max_tokens is not None and self.tokens_used >= self.max_tokens:
            return "tokens"
        if self.deadline_s is not None and self._t0 is not None and self.monotonic() - self._t0 >= self.deadline_s:
            return "deadline"
        return None

    def try_reserve_call(self) -> bool:
        """Reserve one call. False when calls, tokens or the deadline are exhausted (judge J-16b)."""
        with self._lock:
            self.start()
            if self._exhausted_locked() is not None:
                return False
            self.calls_used += 1
            return True

    def charge_tokens(self, n: int) -> None:
        try:
            amount = int(n)
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return
        with self._lock:
            self.tokens_used += amount

    def exhausted(self) -> str | None:
        with self._lock:
            return self._exhausted_locked()

    def remaining(self) -> dict:
        with self._lock:
            calls = None if self.max_calls is None else max(0, self.max_calls - self.calls_used)
            tokens = None if self.max_tokens is None else max(0, self.max_tokens - self.tokens_used)
            seconds = None
            if self.deadline_s is not None:
                used = 0.0 if self._t0 is None else max(0.0, self.monotonic() - self._t0)
                seconds = max(0.0, self.deadline_s - used)
            return {"calls": calls, "tokens": tokens, "seconds": seconds}

    def to_dict(self) -> dict:
        with self._lock:
            return {"max_calls": self.max_calls, "max_tokens": self.max_tokens, "deadline_s": self.deadline_s,
                    "calls_used": self.calls_used, "tokens_used": self.tokens_used,
                    "elapsed_s": round(self.elapsed(), 3), "exhausted": self._exhausted_locked()}


# -- process-group kill (judge J-14) ------------------------------------------------------------------------------
def _sigkill_group(pid: int) -> None:
    if os.name == "nt" or not pid or pid <= 1:
        return
    try:
        if pid == os.getpgrp():  # never signal our own group
            return
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _local_terminate(proc: subprocess.Popen, grace_int: float, grace_term: float) -> None:
    """SIGINT -> SIGTERM escalation on the process group (the caller finishes with SIGKILL)."""
    if os.name == "nt":
        try:
            proc.terminate()
            proc.wait(timeout=grace_int + grace_term)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except OSError:
                pass
        return
    for sig, wait_s in ((signal.SIGINT, grace_int), (signal.SIGTERM, grace_term)):
        if proc.poll() is not None:
            break
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        except OSError:
            try:
                proc.send_signal(sig)
            except OSError:
                break
        try:
            proc.wait(timeout=max(0.0, wait_s))
        except subprocess.TimeoutExpired:
            pass


def kill_group(proc: subprocess.Popen | None, identity: str | None = None, *, grace_int: float = 0.5,
               grace_term: float = 1.0) -> None:
    """Terminate ``proc``'s process group and ALWAYS finish with one SIGKILL of the group (even when the leader already
    exited), so grandchildren cannot outlive a reaped leader. ``proc`` must have been started with
    ``start_new_session=True``. Uses ``process_runtime.terminate_process_group`` when importable (lazy; sp-fix owns it),
    else a local ladder with the same semantics."""
    if proc is None:
        return
    pid = getattr(proc, "pid", 0) or 0
    pr = None
    try:
        from .. import process_runtime as pr  # type: ignore[no-redef]
    except Exception:  # noqa: BLE001
        pr = None
    # A reused pid must never get a stray SIGKILL: verify creation identity while the leader is still alive.
    if identity is not None and pr is not None and proc.poll() is None:
        try:
            current = pr.process_identity(pid)
        except Exception:  # noqa: BLE001
            current = identity
        if current is not None and current != identity:
            return
    terminated = False
    if pr is not None:
        try:
            pr.terminate_process_group(proc, identity, grace_int=grace_int, grace_term=grace_term)
            terminated = True
        except TypeError:
            terminated = False
        except Exception:  # noqa: BLE001
            terminated = True
    if not terminated:
        _local_terminate(proc, grace_int, grace_term)
    _sigkill_group(pid)
    if os.name == "nt":
        return
    try:
        proc.wait(timeout=2.0)  # reap the leader so callers see a final returncode
    except (subprocess.TimeoutExpired, OSError):
        pass


# -- cancellation -------------------------------------------------------------------------------------------------
class CancelToken:
    """Implements the 0.6 cancel protocol (``is_set``, ``register``) plus parent/child propagation."""

    def __init__(self, parent: "CancelToken | None" = None):
        self._lock = threading.RLock()
        self._event = threading.Event()
        self.reason: str | None = None
        self._procs: dict[int, tuple[subprocess.Popen, str | None]] = {}
        self._next = 0
        self._children: list[CancelToken] = []
        self.parent = parent
        if parent is not None:
            parent._adopt(self)

    def _adopt(self, child: "CancelToken") -> None:
        with self._lock:
            already = self._event.is_set()
            if not already:
                self._children.append(child)
            reason = self.reason
        if already:
            child.cancel(reason or "cancelled")

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def child(self) -> "CancelToken":
        return CancelToken(self)

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if self._event.is_set():
                return
            self.reason = reason
            self._event.set()
            procs = list(self._procs.values())
            self._procs.clear()
            children = list(self._children)
            self._children.clear()
        threads = []
        for proc, ident in procs:
            t = threading.Thread(target=kill_group, args=(proc, ident), kwargs={"grace_int": 0.5, "grace_term": 1.0},
                                 daemon=True)
            t.start()
            threads.append(t)
        for child in children:
            child.cancel(reason)
        for t in threads:
            t.join(timeout=10.0)

    def register(self, proc: subprocess.Popen, identity: str | None = None) -> Callable[[], None]:
        """Register a child started with ``start_new_session=True``. If the token is already set the group is killed
        immediately and a no-op unregister is returned."""
        with self._lock:
            if not self._event.is_set():
                handle = self._next
                self._next += 1
                self._procs[handle] = (proc, identity)

                def unregister() -> None:
                    with self._lock:
                        self._procs.pop(handle, None)

                return unregister
        kill_group(proc, identity, grace_int=0.5, grace_term=1.0)
        return lambda: None


# -- verdicts and verifiers ---------------------------------------------------------------------------------------
@dataclass
class Verdict:
    passed: bool
    score: float = 0.0
    kind: str = "none"          # "exec" | "llm" | "none"
    evidence: str = ""
    detail: dict = field(default_factory=dict)


Verifier = Callable[[dict], Verdict]


def _find_bash() -> str:
    try:
        from ..adapter import BASH  # type: ignore[attr-defined]
        if BASH:
            return str(BASH)
    except Exception:  # noqa: BLE001
        pass
    return "bash"


class CommandVerifier:
    """Cancellable command verifier (judge J-14). Runs the command in its own session, registers it with the cancel token
    (so a cancelled form kills the whole group), and bounds it by min(timeout, budget seconds remaining).

    ``FormContext`` calls ``bind(cancel=..., budget=...)`` automatically; standalone use works without binding."""

    def __init__(self, cmd: str | list[str], *, cwd: str | None = None, timeout: float = 600, login: bool = False):
        self.cmd = cmd
        self.cwd = cwd
        self.timeout = float(timeout)
        self.login = bool(login)
        self.cancel: CancelToken | None = None
        self.budget: Budget | None = None

    def bind(self, *, cancel: "CancelToken | None" = None, budget: Budget | None = None) -> "CommandVerifier":
        if cancel is not None:
            self.cancel = cancel
        if budget is not None:
            self.budget = budget
        return self

    def _argv(self) -> list[str]:
        if isinstance(self.cmd, (list, tuple)):
            return [str(a) for a in self.cmd]
        return [_find_bash(), "-lc" if self.login else "-c", str(self.cmd)]

    def __call__(self, candidate: dict) -> Verdict:
        cwd = (candidate or {}).get("cwd") or self.cwd
        timeout = self.timeout
        if self.budget is not None:
            seconds = self.budget.remaining()["seconds"]
            if seconds is not None:
                if seconds <= 0:
                    return Verdict(False, 0.0, "none", "verifier skipped: deadline exhausted", {"deadline": True})
                timeout = min(timeout, seconds)
        if self.cancel is not None and self.cancel.is_set():
            return Verdict(False, 0.0, "none", "verifier skipped: cancelled", {"cancelled": True})
        env = dict(os.environ)
        env["FUSION_CANDIDATE_ID"] = str((candidate or {}).get("id", ""))
        env["FUSION_CANDIDATE_PROVIDER"] = str((candidate or {}).get("provider", ""))
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(self._argv(), cwd=cwd or None, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", **kwargs)
        except (OSError, ValueError) as exc:
            return Verdict(False, 0.0, "exec", f"verifier failed to start: {exc}", {"error": str(exc)})
        unregister = self.cancel.register(proc, None) if self.cancel is not None else (lambda: None)
        timed_out = False
        try:
            try:
                out, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_group(proc, None)
                try:
                    out, _ = proc.communicate(timeout=5)
                except (subprocess.TimeoutExpired, ValueError):
                    out = ""
        finally:
            unregister()
            _sigkill_group(proc.pid)  # leftovers of a finished leader
        cancelled = self.cancel is not None and self.cancel.is_set()
        rc = proc.returncode if proc.returncode is not None else -1
        tail = (out or "")[-4000:]
        if timed_out:
            return Verdict(False, 0.0, "exec", f"verifier timed out after {timeout:.1f}s\n{tail}"[-4000:],
                           {"returncode": 124, "timed_out": True})
        if cancelled and rc != 0:
            return Verdict(False, 0.0, "none", f"verifier cancelled\n{tail}"[-4000:], {"cancelled": True, "returncode": rc})
        passed = rc == 0
        return Verdict(passed, 1.0 if passed else 0.0, "exec", tail, {"returncode": rc})


def command_verifier(cmd: str | list[str], *, cwd: str | None = None, timeout: float = 600,
                     login: bool = False) -> CommandVerifier:
    """passed = exit 0; score 1.0/0.0; kind "exec"; evidence = log tail (<= 4000 chars)."""
    return CommandVerifier(cmd, cwd=cwd, timeout=timeout, login=login)


def _balanced_end(text: str, start: int) -> int | None:
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def json_object(text: str) -> dict | None:
    """First balanced top-level ``{...}`` in ``text`` that parses as a JSON object; None when there is none."""
    if not isinstance(text, str):
        return None
    s = text[:2_000_000]
    i = 0
    for _ in range(200):
        start = s.find("{", i)
        if start < 0:
            return None
        end = _balanced_end(s, start)
        if end is not None:
            try:
                obj = json.loads(s[start:end + 1])
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                return obj
        i = start + 1
    return None


# -- dataclasses --------------------------------------------------------------------------------------------------
@dataclass
class CallResult:
    key: str
    provider: str
    role: str
    ok: bool
    output: str
    status: str
    elapsed_s: float
    tokens: int | None = None
    cost_usd: float | None = None
    model: str | None = None
    session_id: str | None = None
    output_ref: str | None = None      # run_dir-relative path of the saved output (for candidate records)


@dataclass
class CallSpec:
    key: str
    provider: str
    prompt: str
    role: str = ""
    cwd: str | None = None
    timeout: float = 300


@dataclass
class FormSpec:
    name: str
    summary: str
    inputs: dict[str, str]
    stop_rule: str
    aggregation: str
    default_budget: dict


@dataclass
class FormResult:
    form: str
    run_id: str
    status: str            # "ok"|"partial"|"failed"|"cancelled"|"budget_exhausted"
    winner: dict | None
    candidates: list[dict]
    rounds: int
    stop_reason: str
    calls: int
    tokens: int
    elapsed_s: float
    run_dir: str
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def default_dispatch() -> Callable:
    """Lazily resolve ``spawn.run_provider`` (owned by sp-fix)."""
    try:
        from .. import spawn
        return spawn.run_provider
    except (ImportError, AttributeError) as exc:
        raise FormError("invalid_input", f"spawn.run_provider is not available: {exc}") from exc


def _accepts(fn: Callable, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# -- context ------------------------------------------------------------------------------------------------------
class FormContext:
    def __init__(self, form: str, *, run_id: str | None = None, run_dir: str | Path | None = None,
                 budget: Budget | None = None, cancel: CancelToken | None = None, dispatch_fn: Callable | None = None,
                 ledger: ReliabilityLedger | None = None, verifier: Verifier | None = None,
                 monotonic: Callable[[], float] = time.monotonic, max_workers: int = 8,
                 params: dict | None = None):
        self.form = form
        self.run_id = run_id or new_run_id(form)
        if not _RUN_ID_RE.match(self.run_id):
            raise FormError("invalid_input", f"unsafe run_id: {self.run_id!r}")
        self.run_dir = Path(run_dir) if run_dir is not None else runs_dir() / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "outputs").mkdir(exist_ok=True)
        self.budget = (budget or Budget()).start()
        self.cancel = cancel or CancelToken()
        self.dispatch_fn = dispatch_fn
        self.ledger = ledger if ledger is not None else ReliabilityLedger()
        self.verifier = verifier
        self.monotonic = monotonic
        self.max_workers = max(1, int(max_workers))
        self.journal = Journal(self.run_dir, run_id=self.run_id, form=form)
        self._t0 = monotonic()
        self._lock = threading.Lock()
        self._call_index = 0
        self._verdicts: dict[str, Verdict] = {}
        self._result: FormResult | None = None
        self._timer: threading.Timer | None = None
        bind = getattr(verifier, "bind", None)
        if callable(bind):
            bind(cancel=self.cancel, budget=self.budget)
        self.journal.append("run.start", params=_jsonable(params or {}), budget=self.budget.to_dict())
        deadline = self.budget.remaining()["seconds"]
        if deadline is not None:  # judge J-16b: the wall deadline cancels the token even if no call is in flight
            self._timer = threading.Timer(max(0.0, deadline), self.cancel.cancel, args=("deadline",))
            self._timer.daemon = True
            self._timer.start()

    # -- helpers --------------------------------------------------------------------------------------------
    def _synthetic(self, spec: CallSpec, status: str) -> CallResult:
        return CallResult(key=spec.key, provider=spec.provider, role=spec.role, ok=False, output="", status=status,
                          elapsed_s=0.0)

    def _next_index(self) -> int:
        with self._lock:
            self._call_index += 1
            return self._call_index

    def _resolve_dispatch(self) -> Callable:
        with self._lock:
            if self.dispatch_fn is None:
                self.dispatch_fn = default_dispatch()
            return self.dispatch_fn

    # -- single call ----------------------------------------------------------------------------------------
    def call(self, spec: CallSpec, *, cancel: CancelToken | None = None) -> CallResult:
        """Run one provider call under the budget. Never raises on provider failure."""
        token = cancel or self.cancel
        if token.is_set():
            self.journal.append("cancel", key=spec.key, reason=token.reason or "cancelled")
            return self._synthetic(spec, "cancelled")
        if not self.budget.try_reserve_call():
            self.journal.append("budget", kind=self.budget.exhausted() or "calls", key=spec.key)
            return self._synthetic(spec, "budget")
        fn = self._resolve_dispatch()
        index = self._next_index()
        timeout: float | None = spec.timeout
        seconds = self.budget.remaining()["seconds"]
        if seconds is not None:
            timeout = max(0.001, min(timeout, seconds)) if timeout is not None else max(0.001, seconds)
        self.journal.append("call.start", key=spec.key, provider=spec.provider, role=spec.role)
        kwargs: dict[str, Any] = {"role": spec.role, "cwd": spec.cwd, "timeout": timeout}
        if _accepts(fn, "cancel"):
            kwargs["cancel"] = token
        started = self.monotonic()
        try:
            raw = fn(spec.provider, spec.prompt, **kwargs)
        except Exception as exc:  # noqa: BLE001 - provider failure must not crash a form
            raw = {"ok": False, "output": f"{type(exc).__name__}: {exc}", "status": "error"}
        elapsed = max(0.0, self.monotonic() - started)
        if not isinstance(raw, dict):
            raw = {"ok": False, "output": str(raw), "status": "error"}
        status = str(raw.get("status") or ("returned" if raw.get("ok") else "error"))
        if status not in _DISPATCH_STATUSES:
            status = "error"
        ok = bool(raw.get("ok")) and status == "returned"
        output = raw.get("output")
        output = "" if output is None else str(output)
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else None
        tokens = None
        cost = None
        if usage:
            total = usage.get("total_tokens")
            if not isinstance(total, (int, float)):
                parts = [usage.get(k) for k in ("input_tokens", "output_tokens")]
                total = sum(p for p in parts if isinstance(p, (int, float))) if any(
                    isinstance(p, (int, float)) for p in parts) else None
            tokens = int(total) if isinstance(total, (int, float)) else None
            c = usage.get("cost_usd")
            cost = float(c) if isinstance(c, (int, float)) else None
            if tokens:
                self.budget.charge_tokens(tokens)
        # Safe output file name (judge J-16c): index + hash of the key, never the key itself.
        digest = hashlib.sha256(spec.key.encode("utf-8", errors="replace")).hexdigest()
        ref = f"outputs/{index:04d}-{digest[:16]}.txt"
        if not self.journal.ended:
            try:
                write_atomic(self.run_dir / ref, output)
            except OSError:
                ref = None
        else:
            ref = None
        result = CallResult(key=spec.key, provider=spec.provider, role=spec.role, ok=ok, output=output, status=status,
                            elapsed_s=elapsed, tokens=tokens, cost_usd=cost,
                            model=raw.get("model") if isinstance(raw.get("model"), str) else None,
                            session_id=raw.get("session_id") if isinstance(raw.get("session_id"), str) else None,
                            output_ref=ref)
        self.journal.append("call.end", key=spec.key, provider=spec.provider, status=status, ok=ok,
                            elapsed_s=round(elapsed, 3), tokens=tokens, output_ref=ref,
                            output_sha256=hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest())
        return result

    # -- parallel calls -------------------------------------------------------------------------------------
    def parallel(self, specs: list[CallSpec], *, first_k: int | None = None,
                 accept: Callable[[CallResult], bool] | None = None, max_workers: int | None = None,
                 loser_grace_s: float = 2.0) -> list[CallResult]:
        """Run ``specs`` concurrently; each call gets ``cancel.child()``. Once ``first_k`` results satisfy ``accept``
        (default: ``ok``) the remaining calls are cancelled (their process groups are killed) and reported with
        ``status="cancelled"``. Results are returned in completion order; there is no barrier on losers (they are only
        given ``loser_grace_s`` to unwind after being killed)."""
        if not specs:
            return []
        test = accept or (lambda r: r.ok)
        workers = max(1, min(int(max_workers or self.max_workers), len(specs)))
        tokens = [self.cancel.child() for _ in specs]
        pool = cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"form-{self.form}")
        futures = {pool.submit(self.call, spec, cancel=tok): i for i, (spec, tok) in enumerate(zip(specs, tokens))}
        results: list[tuple[int, CallResult]] = []
        accepted = 0
        pending = set(futures)
        decided = False
        try:
            while pending and not decided:
                done, pending = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
                for fut in sorted(done, key=lambda f: futures[f]):
                    idx = futures[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        res = CallResult(specs[idx].key, specs[idx].provider, specs[idx].role, False,
                                         f"{type(exc).__name__}: {exc}", "error", 0.0)
                    results.append((idx, res))
                    if first_k is not None and test(res):
                        accepted += 1
                if first_k is not None and accepted >= first_k:
                    decided = True
            if pending:  # first_k satisfied: cancel the rest
                losers = sorted(pending, key=lambda f: futures[f])
                for fut in losers:
                    idx = futures[fut]
                    if fut.cancel():  # never started (queue ahead of max_workers)
                        continue
                    tokens[idx].cancel("first_k")
                for fut in losers:
                    self.journal.append("cancel", key=specs[futures[fut]].key, reason="first_k")
                cf.wait(losers, timeout=max(0.0, loser_grace_s))
                for fut in losers:
                    idx = futures[fut]
                    spec = specs[idx]
                    res = None
                    if fut.done() and not fut.cancelled():
                        try:
                            res = fut.result()
                        except Exception:  # noqa: BLE001
                            res = None
                    results.append((idx, CallResult(spec.key, spec.provider, spec.role, False,
                                                    res.output if res else "", "cancelled",
                                                    res.elapsed_s if res else 0.0,
                                                    tokens=res.tokens if res else None,
                                                    cost_usd=res.cost_usd if res else None,
                                                    model=res.model if res else None,
                                                    session_id=res.session_id if res else None,
                                                    output_ref=res.output_ref if res else None)))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return [r for _, r in results]

    # -- verification ---------------------------------------------------------------------------------------
    def verify(self, candidate: dict) -> Verdict:
        """Run the verifier (or a ``none`` verdict) and journal the result."""
        if self.verifier is None:
            verdict = Verdict(False, 0.0, "none", "no verifier")
        else:
            try:
                verdict = self.verifier(candidate)
                if not isinstance(verdict, Verdict):
                    raise TypeError("verifier must return Verdict")
            except Exception as exc:  # noqa: BLE001
                verdict = Verdict(False, 0.0, "none", f"verifier error: {type(exc).__name__}: {exc}")
        cid = str((candidate or {}).get("id", ""))
        with self._lock:
            old = self._verdicts.get(cid)
            if old is None or (verdict.kind == "exec" and verdict.passed) or not (old.kind == "exec" and old.passed):
                self._verdicts[cid] = verdict
        self.journal.append("verdict", candidate_id=cid, passed=verdict.passed, score=verdict.score,
                            kind=verdict.kind, evidence=verdict.evidence[:4000])
        return verdict

    # -- finish ---------------------------------------------------------------------------------------------
    def finish(self, status: str, *, winner: dict | None, candidates: list[dict], rounds: int, stop_reason: str,
               **extra: Any) -> FormResult:
        """Write ``run.end`` and a final checkpoint, persist the ledger, return the FormResult (idempotent)."""
        with self._lock:
            if self._result is not None:
                return self._result
        if self._timer is not None:
            self._timer.cancel()
        elapsed = max(0.0, self.monotonic() - self._t0)
        winner_id = None if not winner else winner.get("id")
        end = self.journal.append("run.end", status=status, winner_id=winner_id)
        best = _jsonable(winner) if winner else None
        if isinstance(best, dict) and "verdict" not in best:
            v = self._verdicts.get(str(best.get("id", "")))
            if v is not None:
                best["verdict"] = {"passed": v.passed, "score": v.score, "kind": v.kind, "evidence": v.evidence[:4000]}
        state = {"status": status, "stop_reason": stop_reason, "round": rounds, "calls": self.budget.calls_used,
                 "tokens": self.budget.tokens_used, "elapsed_s": round(elapsed, 3), "best": best,
                 "end_seq": end.get("seq")}
        self.journal.checkpoint(state)
        result = FormResult(form=self.form, run_id=self.run_id, status=status, winner=winner,
                            candidates=list(candidates), rounds=rounds, stop_reason=stop_reason,
                            calls=self.budget.calls_used, tokens=self.budget.tokens_used,
                            elapsed_s=round(elapsed, 3), run_dir=str(self.run_dir), extra=dict(extra))
        try:
            write_atomic(self.run_dir / "result.json", json.dumps(_jsonable(result.to_dict()), ensure_ascii=False, indent=1))
        except OSError:
            pass
        try:
            if self.ledger is not None and self.ledger.dirty:
                self.ledger.save()
        except Exception:  # noqa: BLE001 - ledger persistence is best-effort
            pass
        self.journal.close()
        with self._lock:
            self._result = result
        return result
