"""hedged_race (P3, spec 9.3): primary first, a backup on a different provider after the hedge deadline, FIRST VERIFIED
result wins (not first finished), losers are cancelled through the cancel token (real process-group kill).

Hedging is only legal for idempotent/read-only work or sandboxed writes: the caller must pass ``read_only=True`` or an
``isolate(attempt_index) -> cwd`` factory, otherwise ``FormError("invalid_input")``. At most 2 hedges per race, each gated
by a ``capacity.HedgeBudget`` token bucket (starts full, a hedge costs 1, refused at <= max/2). The hedge deadline is the
explicit ``hedge_delay_s``, else the ledger p90 for the primary clamped to [30 s, 600 s], else 120 s.

A verifier is required (first-VERIFIED is the aggregation rule); ``allow_unverified=True`` degrades to first-OK-wins and
the result is never marked verified. Evidence: notes/x3-forms.md P3 (Large Language Monkeys 2407.21787, The Tail at Scale).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import time
import traceback
from typing import Any, Callable

from . import capacity
from .runtime import (Budget, CallResult, CallSpec, CancelToken, FormContext, FormError, FormResult, FormSpec, Verdict)

FORM = FormSpec(
    name="hedged_race",
    summary="Primary first; after the hedge deadline launch a backup on a different provider; first VERIFIED result wins "
            "and losers' process groups are killed. Only for read-only or sandboxed tasks.",
    inputs={"task": "str: the task prompt", "primary": "str: primary provider", "backups": "list[str]: backup providers",
            "hedge_delay_s": "float (optional): hedge deadline, default ledger p90 clamped, else 120",
            "max_hedges": "int: at most 2", "read_only": "bool: task is idempotent / read-only",
            "isolate": "callable(attempt_index) -> cwd: sandbox per attempt for writing tasks"},
    stop_rule="first verified result, or every attempt finished/failed, or budget/cancel/deadline",
    aggregation="first VERIFIED result wins; all failed -> best partial by verdict score (status failed)",
    default_budget={"max_calls": 1 + 2},   # 1 + max_hedges
)

HEDGE_DEFAULT_S = 120.0
HEDGE_FLOOR_S = 30.0
HEDGE_CEIL_S = 600.0
_NO_LATENCY = frozenset({"cancelled", "budget", "absent", "blocked"})


def _text(task: Any) -> str:
    return task if isinstance(task, str) else json.dumps(task, ensure_ascii=False, default=str)


def _family(provider: str) -> str:
    try:
        from .. import adapter
        for group, members in getattr(adapter, "CORRELATION_GROUPS", {}).items():
            if provider in members:
                return str(group)
        return str(adapter.model_family(provider))
    except Exception:  # noqa: BLE001
        return provider


def _verdict_view(v: Verdict | None) -> dict | None:
    if v is None:
        return None
    return {"passed": v.passed, "score": v.score, "kind": v.kind, "evidence": v.evidence[:4000]}


def _stop_for(ctx: FormContext, budget_hit: bool) -> tuple[str, str] | None:
    if ctx.cancel.is_set():
        reason = ctx.cancel.reason or "cancelled"
        return ("budget_exhausted", "deadline") if reason == "deadline" else ("cancelled", reason)
    if budget_hit:
        return "budget_exhausted", ctx.budget.exhausted() or "calls"
    return None


def _checkpoint(ctx: FormContext, *, best: dict | None, stop_reason: str | None = None, **extra: Any) -> None:
    if ctx.journal.ended:
        return
    state = {"round": 1, "calls": ctx.budget.calls_used, "tokens": ctx.budget.tokens_used, "best": best,
             "stop_reason": stop_reason}
    state.update(extra)
    ctx.journal.checkpoint(state)


def _delay_for(ctx: FormContext, primary: str, task_class: str, explicit: float | None) -> float:
    if explicit is not None:
        return max(0.0, float(explicit))
    try:
        q = ctx.ledger.latency_quantile(primary, task_class, 0.9)
    except Exception:  # noqa: BLE001
        q = None
    if q is None:
        return HEDGE_DEFAULT_S
    return float(min(HEDGE_CEIL_S, max(HEDGE_FLOOR_S, q)))


class _Attempt:
    def __init__(self, idx: int, provider: str, kind: str, token: CancelToken, cwd: str | None):
        self.idx = idx
        self.provider = provider
        self.kind = kind                    # primary | hedge
        self.token = token
        self.cwd = cwd
        self.future: cf.Future | None = None
        self.result: CallResult | None = None
        self.verdict: Verdict | None = None
        self.candidate: dict | None = None
        self.processed = False
        self.cancelled_as_loser = False


def _validate(primary: Any, backups: Any, max_hedges: Any, read_only: bool, isolate: Any) -> tuple[str, list[str]]:
    if not read_only and isolate is None:
        raise FormError("invalid_input", "hedged_race refuses to hedge a writing task: pass read_only=True for "
                        "idempotent work or isolate(attempt_index)->cwd for sandboxed writes")
    if not isinstance(primary, str) or not primary.strip():
        raise FormError("no_providers", "primary must be a provider name")
    clean = [b for b in (backups or []) if isinstance(b, str) and b.strip()]
    if not clean:
        raise FormError("no_providers", "backups must be non-empty")
    if not isinstance(max_hedges, int) or isinstance(max_hedges, bool) or not 0 <= max_hedges <= 2:
        raise FormError("invalid_input", "max_hedges must be an integer in 0..2")
    return primary, clean


def run(task: Any, *, ctx: FormContext | None = None, primary: str | None = None, backups: list[str] | None = None,
        hedge_delay_s: float | None = None, max_hedges: int = 2, task_class: str = "general",
        isolate: Callable[[int], str] | None = None, timeout: float = 600, read_only: bool = False,
        allow_unverified: bool = False, hedge_budget: capacity.HedgeBudget | None = None,
        dispatch_fn: Callable | None = None, verifier: Callable | None = None, ledger: Any = None,
        budget: Budget | None = None, cancel: Any = None, run_id: str | None = None,
        run_dir: str | None = None) -> FormResult:
    primary, backup_list = _validate(primary, backups, max_hedges, read_only, isolate)
    have_verifier = verifier is not None or (ctx is not None and ctx.verifier is not None)
    if not have_verifier and not allow_unverified:
        raise FormError("verifier_required", "hedged_race needs a verifier (first VERIFIED wins); "
                        "pass allow_unverified=True for first-OK-wins")
    if ctx is None:
        ctx = FormContext(FORM.name, run_id=run_id, run_dir=run_dir, budget=budget or Budget(max_calls=1 + max_hedges),
                          cancel=cancel, dispatch_fn=dispatch_fn, ledger=ledger, verifier=verifier,
                          params={"primary": primary, "backups": backup_list, "hedge_delay_s": hedge_delay_s,
                                  "max_hedges": max_hedges, "task_class": task_class, "read_only": read_only,
                                  "isolated": isolate is not None, "allow_unverified": allow_unverified})
    else:
        if dispatch_fn is not None and ctx.dispatch_fn is None:
            ctx.dispatch_fn = dispatch_fn
        if verifier is not None and ctx.verifier is None:
            ctx.verifier = verifier
            bind = getattr(verifier, "bind", None)
            if callable(bind):
                bind(cancel=ctx.cancel, budget=ctx.budget)
    attempts: list[_Attempt] = []
    try:
        return _run(ctx, task, primary, backup_list, hedge_delay_s, max_hedges, task_class, isolate, timeout,
                    allow_unverified, hedge_budget or capacity.HedgeBudget(), attempts)
    except Exception as exc:  # noqa: BLE001
        for a in attempts:
            a.token.cancel("error")
        ctx.journal.append("stop", reason=f"exception: {type(exc).__name__}")
        return ctx.finish("failed", winner=None, candidates=[a.candidate for a in attempts if a.candidate],
                          rounds=1, stop_reason=f"exception: {type(exc).__name__}",
                          error="".join(traceback.format_exception_only(type(exc), exc))[:500])


def _run(ctx: FormContext, task: Any, primary: str, backups: list[str], explicit_delay: float | None, max_hedges: int,
         task_class: str, isolate: Callable[[int], str] | None, timeout: float, allow_unverified: bool,
         hedge_budget: capacity.HedgeBudget, attempts: list[_Attempt]) -> FormResult:
    delay = _delay_for(ctx, primary, task_class, explicit_delay)
    prompt = _text(task)
    pool = cf.ThreadPoolExecutor(max_workers=1 + max_hedges, thread_name_prefix="hedged-race")
    pending: set[cf.Future] = set()
    used = [primary]
    hedges = 0
    hedge_blocked = False
    budget_hit = False
    winner: _Attempt | None = None

    def next_backup() -> str | None:
        free = [b for b in backups if b not in used]
        if not free:
            return None
        fams = {_family(p) for p in used}
        for b in free:                      # prefer a family not used yet; a different PROVIDER is mandatory
            if _family(b) not in fams:
                return b
        return free[0]

    def launch(provider: str, kind: str) -> _Attempt:
        idx = len(attempts)
        cwd = None
        if isolate is not None:
            cwd = isolate(idx)
        att = _Attempt(idx, provider, kind, ctx.cancel.child(), cwd)
        attempts.append(att)
        spec = CallSpec(key=f"attempt:{idx}:{provider}", provider=provider, prompt=prompt, role="race", cwd=cwd,
                        timeout=timeout)
        att.future = pool.submit(ctx.call, spec, cancel=att.token)
        pending.add(att.future)
        return att

    def process(att: _Attempt) -> None:
        nonlocal winner, budget_hit
        att.processed = True
        res = att.result
        if att is attempts[0]:
            hedge_budget.on_primary_done()
        if res.status == "budget":
            budget_hit = True
        cand = None
        if res.ok and res.output.strip():
            cand = {"id": f"attempt-{att.idx}", "provider": att.provider, "output": res.output,
                    "output_ref": res.output_ref, "round": 0, "attempt": att.idx, "kind": att.kind}
            if att.cwd:
                cand["cwd"] = att.cwd
            att.candidate = cand
            ctx.journal.append("candidate", id=cand["id"], provider=att.provider, output_ref=res.output_ref, round=0)
            if ctx.verifier is not None:
                att.verdict = ctx.verify({"id": cand["id"], "provider": att.provider, "output": res.output, "cwd": att.cwd})
        # Ledger: latency for every attempt that was not cancelled; success only from an exec-verified verdict.
        try:
            exec_verified = att.verdict is not None and att.verdict.kind == "exec"
            if exec_verified:
                ctx.ledger.record(att.provider, task_class, "worker", success=att.verdict.passed, verified=True,
                                  latency_s=res.elapsed_s if res.status not in _NO_LATENCY else None)
                ctx.journal.append("ledger", worker=att.provider, task_class=task_class, role="worker",
                                   success=att.verdict.passed)
            elif res.status not in _NO_LATENCY:
                ctx.ledger.observe_latency(att.provider, task_class, res.elapsed_s)
        except Exception:  # noqa: BLE001
            pass
        if cand is not None:
            if att.verdict is not None and att.verdict.passed:
                winner = att
            elif ctx.verifier is None and allow_unverified:
                winner = att

    launch(primary, "primary")
    next_hedge_at = time.monotonic() + delay
    try:
        while winner is None:
            if ctx.cancel.is_set():
                break
            can_hedge = (not hedge_blocked) and hedges < max_hedges and next_backup() is not None
            if not pending and not can_hedge:
                break
            done: set[cf.Future] = set()
            if pending:
                wait_s = max(0.0, next_hedge_at - time.monotonic()) if can_hedge else None
                done, _ = cf.wait(pending, timeout=wait_s, return_when=cf.FIRST_COMPLETED)
            by_future = {a.future: a for a in attempts}
            for fut in sorted(done, key=lambda f: by_future[f].idx):
                pending.discard(fut)
                att = by_future[fut]
                try:
                    att.result = fut.result()
                except Exception as exc:  # noqa: BLE001
                    att.result = CallResult(f"attempt:{att.idx}", att.provider, "race", False,
                                            f"{type(exc).__name__}: {exc}", "error", 0.0)
                process(att)
                if winner is not None:
                    break
            if winner is not None or ctx.cancel.is_set():
                break
            can_hedge = (not hedge_blocked) and hedges < max_hedges and next_backup() is not None
            if can_hedge and (not pending or time.monotonic() >= next_hedge_at):
                if ctx.budget.exhausted() is not None:
                    budget_hit = budget_hit or ctx.budget.exhausted() in ("calls", "tokens")
                    hedge_blocked = True
                    continue
                backup = next_backup()
                if not hedge_budget.try_hedge():
                    hedge_blocked = True
                    ctx.journal.append("hedge", attempt=len(attempts), provider=backup, delay_s=delay, refused=True)
                    continue
                hedges += 1
                used.append(backup)
                att = launch(backup, "hedge")
                ctx.journal.append("hedge", attempt=att.idx, provider=backup, delay_s=delay)
                next_hedge_at = time.monotonic() + delay
    finally:
        losers = [a for a in attempts if a is not winner and a.future is not None and not a.future.done()]
        for a in losers:
            a.cancelled_as_loser = True
            ctx.journal.append("cancel", attempt=a.idx, reason="loser" if winner is not None else "stopped")
            a.token.cancel("loser" if winner is not None else "stopped")
        if losers:
            cf.wait([a.future for a in losers], timeout=2.0)
        pool.shutdown(wait=False, cancel_futures=True)

    for a in attempts:  # attempts that finished while we were deciding but were never processed (ledger/latency only)
        if not a.processed and a.future is not None and a.future.done() and not a.cancelled_as_loser:
            try:
                a.result = a.future.result()
            except Exception:  # noqa: BLE001
                a.result = None
    stop = _stop_for(ctx, budget_hit)
    cands = [a.candidate for a in attempts if a.candidate]
    views = []
    for a in attempts:
        if a.candidate:
            c = dict(a.candidate)
            c["verdict"] = _verdict_view(a.verdict)
            c["verified"] = bool(a.verdict is not None and a.verdict.kind == "exec" and a.verdict.passed)
            views.append(c)

    def partial_rank(a: _Attempt) -> tuple:
        v = a.verdict
        tier = 3 if v is not None and v.kind == "exec" and v.passed else (2 if v is not None and v.kind == "exec" else
                                                                           (1 if v is not None and v.kind == "llm" else 0))
        return (tier, v.score if v is not None else 0.0, -a.idx)

    extra = {"attempts": [{"attempt": a.idx, "provider": a.provider, "kind": a.kind,
                           "status": a.result.status if a.result else ("cancelled" if a.cancelled_as_loser else "unknown"),
                           "ok": bool(a.result and a.result.ok), "verified": bool(
                               a.verdict is not None and a.verdict.kind == "exec" and a.verdict.passed),
                           "elapsed_s": round(a.result.elapsed_s, 3) if a.result else None} for a in attempts],
             "hedges": hedges, "delay_s": delay, "hedge_blocked": hedge_blocked}
    if winner is not None:
        w = next(v for v in views if v["id"] == winner.candidate["id"])
        extra["winner_provider"] = winner.provider
        reason = "first_verified" if winner.verdict is not None else "first_ok_unverified"
        ctx.journal.append("stop", reason=reason)
        _checkpoint(ctx, best=_best(winner), stop_reason=reason)
        return ctx.finish("ok", winner=w, candidates=views, rounds=1, stop_reason=reason,
                          verified=bool(w["verified"]), **extra)
    scored = [a for a in attempts if a.candidate]
    best = max(scored, key=partial_rank) if scored else None
    bv = next((v for v in views if best is not None and v["id"] == best.candidate["id"]), None)
    if stop:
        status, reason = stop
    else:
        status, reason = "failed", "all_attempts_failed"
    ctx.journal.append("stop", reason=reason)
    _checkpoint(ctx, best=_best(best), stop_reason=reason)
    return ctx.finish(status, winner=bv, candidates=views, rounds=1, stop_reason=reason, verified=False, **extra)


def _best(att: _Attempt | None) -> dict | None:
    if att is None or att.candidate is None:
        return None
    return {"id": att.candidate["id"], "provider": att.provider, "output_ref": att.candidate.get("output_ref"),
            "round": 0, "output": att.candidate["output"], "verdict": _verdict_view(att.verdict)}
