"""P9 Auction (spec 9.8): award each task to a bidder chosen by the reliability LEDGER, never by self-reported bids.

* Award per task: ``ctx.ledger.choose(bidders, task_class, role, method=..., explore=..., cost=...)`` (Thompson sampling or
  UCB with an exploration floor, deterministic under a seeded ledger rng). A bidder's own "bid" (confidence, price, ...)
  may be supplied via ``self_bids`` and is journaled for later analysis, but it is NEVER an input to the award.
* ``execute=True`` dispatches each awarded bidder (in parallel, one call per task), then verifies the output with
  ``ctx.verify``. The ledger is updated ONLY from outcomes decided by an EXECUTABLE verdict (``kind == "exec"``); a call
  that failed to return, a form without a verifier and an ``llm`` verdict update nothing (latency is the one exception:
  it needs no verification).
* ``extra.allocation = [{"task_id", "bidder", "score", "explored"}]``. ``score`` is the bidder's ledger posterior mean
  divided by its cost; ``explored`` is True when the exploration floor fired for that award.
* Stop: every task awarded (and executed) or the budget/cancel token. Salvage: the allocation record plus the finished
  candidates and verdicts in the journal. Default budget ``{"max_calls": len(tasks)}``.
"""
from __future__ import annotations

import random
from typing import Any, Callable, Mapping

from .runtime import (Budget, CallResult, CallSpec, FormContext, FormError, FormResult, FormSpec)

FORM = FormSpec(
    name="auction",
    summary="Award each task to the bidder the reliability ledger favors (Thompson/UCB + exploration floor); "
            "ledger updates come only from executable verdicts.",
    inputs={
        "tasks": "list of {id, text, task_class}; one award per task",
        "bidders": "providers eligible for an award",
        "method": "thompson (default) | ucb | mean",
        "explore": "exploration floor probability (default 0.10)",
        "cost": "optional callable bidder -> relative cost; score is divided by it",
        "execute": "dispatch the awarded bidder and verify (default true); false = allocation only",
        "role": "ledger role label (default worker)",
    },
    stop_rule="all tasks awarded (and executed) or budget/cancel",
    aggregation="none: one result per task; winner = best executable-verified candidate",
    default_budget={"max_calls": 1},
)

_METHODS = ("thompson", "ucb", "mean")


def default_budget(n_tasks: int) -> dict:
    return {"max_calls": max(1, int(n_tasks))}


def _validate_tasks(tasks: Any) -> list[dict]:
    if not isinstance(tasks, (list, tuple)) or not tasks:
        raise FormError("invalid_input", "tasks must be a non-empty list of {id, text, task_class}")
    out: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(tasks):
        if not isinstance(item, Mapping):
            raise FormError("invalid_input", f"task #{i} must be an object")
        tid = str(item.get("id", i + 1)).strip()
        text = str(item.get("text") or "").strip()
        if not tid or not text:
            raise FormError("invalid_input", f"task #{i} needs a non-empty id and text")
        if tid in seen:
            raise FormError("invalid_input", f"duplicate task id {tid!r}")
        seen.add(tid)
        out.append({"id": tid, "text": text, "task_class": str(item.get("task_class") or "general").strip() or "general"})
    return out


def _would_explore(ledger: Any, explore: float) -> bool:
    """True when ``ledger.choose`` will take its exploration branch: the first rng draw inside ``choose`` is
    ``rng.random() < explore``, so replaying the draw on a copy of the rng state predicts it exactly."""
    try:
        probe = random.Random()
        probe.setstate(ledger.rng.getstate())
        return probe.random() < float(explore)
    except Exception:  # noqa: BLE001 - diagnostic only
        return False


def award(ledger: Any, bidders: list[str], task_class: str, role: str, *, method: str = "thompson",
          explore: float = 0.10, cost: Callable[[str], float] | None = None) -> dict:
    """One award decision (also useful standalone): ``{"bidder", "score", "explored", "n"}``."""
    explored = _would_explore(ledger, explore)
    bidder = ledger.choose(bidders, task_class, role, method=method, explore=explore, cost=cost)
    divisor = 1.0
    if cost is not None:
        try:
            divisor = max(float(cost(bidder)), 1e-9)
        except (TypeError, ValueError):
            divisor = 1.0
    score = ledger.mean(bidder, task_class, role) / divisor
    return {"bidder": bidder, "score": round(score, 6), "explored": bool(explored),
            "n": ledger.cell(bidder, task_class, role).n}


def _default_prompt(task: dict) -> str:
    return (f"Task id: {task['id']}\nTask class: {task['task_class']}\n\n{task['text']}\n\n"
            "Reply with the complete result for this task only.")


def run(tasks: list[dict], *, ctx: FormContext | None = None, bidders: list[str], method: str = "thompson",
        explore: float = 0.10, cost: Callable[[str], float] | None = None, execute: bool = True, role: str = "worker",
        self_bids: Mapping[str, Mapping[str, Any]] | None = None, prompt_fn: Callable[[dict], str] | None = None,
        cwd: str | None = None, timeout: float = 300, dispatch_fn: Callable | None = None, verifier: Callable | None = None,
        ledger: Any = None, budget: Budget | None = None, cancel: Any = None, run_id: str | None = None,
        run_dir: str | None = None) -> FormResult:
    """Award and (optionally) execute ``tasks``. The keyword-only extras (``dispatch_fn`` ... ``run_dir``) are used only
    when ``ctx`` is None."""
    items = _validate_tasks(tasks)
    if not isinstance(bidders, (list, tuple)) or not bidders:
        raise FormError("no_providers", "bidders must be a non-empty list")
    bidders = [str(b).strip() for b in bidders]
    if not all(bidders):
        raise FormError("invalid_input", "bidders must be non-empty provider names")
    if method not in _METHODS:
        raise FormError("invalid_input", f"method must be one of {', '.join(_METHODS)}")
    try:
        explore = float(explore)
    except (TypeError, ValueError):
        raise FormError("invalid_input", "explore must be a number in [0, 1]") from None
    if not 0.0 <= explore <= 1.0:
        raise FormError("invalid_input", "explore must be a number in [0, 1]")
    if ctx is None:
        ctx = FormContext(FORM.name, run_id=run_id, run_dir=run_dir,
                          budget=budget or Budget(**default_budget(len(items))), cancel=cancel, dispatch_fn=dispatch_fn,
                          ledger=ledger, verifier=verifier,
                          params={"bidders": bidders, "method": method, "explore": explore, "execute": execute,
                                  "role": role, "tasks": [t["id"] for t in items]})
    make_prompt = prompt_fn or _default_prompt

    # -- 1. award (sequential: each decision is a draw from the same seeded rng) ------------------------------
    allocation: list[dict] = []
    by_task: dict[str, dict] = {}
    for task in items:
        a = award(ctx.ledger, bidders, task["task_class"], role, method=method, explore=explore, cost=cost)
        entry = {"task_id": task["id"], "bidder": a["bidder"], "score": a["score"], "explored": a["explored"]}
        allocation.append(entry)
        by_task[task["id"]] = entry
    summary: dict[str, Any] = {"allocation": allocation, "method": method, "explore": explore, "role": role}
    if self_bids:  # analysis only: never read back into the award
        summary["self_bids"] = {str(k): dict(v) for k, v in self_bids.items() if isinstance(v, Mapping)}
        summary["self_bids_used_in_scoring"] = False
    ctx.journal.append("round", n=0, summary=summary)
    ctx.journal.checkpoint({"status": "running", "stop_reason": None, "round": 1, "calls": ctx.budget.calls_used,
                            "tokens": ctx.budget.tokens_used, "best": None, "allocation": allocation})

    if not execute:
        ctx.journal.append("stop", reason="awarded")
        return ctx.finish("ok", winner=None, candidates=[], rounds=1, stop_reason="awarded", allocation=allocation,
                          outcomes=[], method=method, executed=False)

    # -- 2. execute (parallel, one call per task) ----------------------------------------------------------------
    specs = [CallSpec(f"auction-{t['id']}", by_task[t["id"]]["bidder"], make_prompt(t), role=role, cwd=cwd,
                      timeout=timeout) for t in items]
    results = {r.key: r for r in ctx.parallel(specs)}

    candidates: list[dict] = []
    outcomes: list[dict] = []
    interrupted: str | None = None
    for task in items:
        res: CallResult | None = results.get(f"auction-{task['id']}")
        bidder = by_task[task["id"]]["bidder"]
        outcome: dict[str, Any] = {"task_id": task["id"], "bidder": bidder, "status": "skipped", "verified": False,
                                   "passed": None, "ledger_updated": False}
        if res is None or res.status in ("budget", "cancelled"):
            if res is not None and res.status == "budget":
                interrupted = interrupted or "budget"
            else:
                interrupted = "cancelled"
            outcomes.append(outcome)
            continue
        outcome["status"] = res.status
        if not res.ok:
            outcome["status"] = res.status if res.status != "returned" else "error"
            outcomes.append(outcome)  # an unreturned call is not an outcome the ledger may learn from
            continue
        candidate = {"id": task["id"], "provider": bidder, "output": res.output, "task_id": task["id"],
                     "task_class": task["task_class"], "round": 0, "output_ref": res.output_ref}
        if cwd:
            candidate["cwd"] = cwd
        ctx.journal.append("candidate", id=task["id"], provider=bidder, output_ref=res.output_ref, round=0)
        verdict = ctx.verify(candidate)
        outcome["verified"] = verdict.kind == "exec"
        outcome["passed"] = bool(verdict.passed) if verdict.kind != "none" else None
        candidate["verdict"] = {"passed": verdict.passed, "score": verdict.score, "kind": verdict.kind,
                                "evidence": verdict.evidence[:400]}
        candidates.append(candidate)
        if verdict.kind == "exec":
            ctx.ledger.record(bidder, task["task_class"], role, bool(verdict.passed), verified=True,
                              latency_s=res.elapsed_s)
            ctx.journal.append("ledger", worker=bidder, task_class=task["task_class"], role=role,
                               success=bool(verdict.passed))
            outcome["ledger_updated"] = True
        else:
            ctx.ledger.observe_latency(bidder, task["task_class"], res.elapsed_s)
        outcomes.append(outcome)

    passed = [c for c in candidates if c["verdict"]["kind"] == "exec" and c["verdict"]["passed"]]
    winner = max(passed, key=lambda c: (c["verdict"]["score"], -candidates.index(c))) if passed else None
    if interrupted is None:
        stop = "all_executed"
    else:
        stop = interrupted
    ctx.journal.append("stop", reason=stop)
    if interrupted == "cancelled" or (ctx.cancel.is_set() and interrupted is not None):
        status = "cancelled"
    elif interrupted == "budget":
        status = "budget_exhausted"
    elif not candidates:
        status = "failed"
    elif ctx.verifier is not None and len(passed) == len(items):
        status = "ok"
    else:
        status = "partial"
    return ctx.finish(status, winner=winner, candidates=candidates, rounds=1, stop_reason=stop,
                      allocation=allocation, outcomes=outcomes, method=method, executed=True)
