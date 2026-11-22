"""refute_panel (P1, spec 9.1): authors propose, cold-start refuters with a kill mandate try to falsify, an empirical
gate decides.

A candidate is promoted only when it has ZERO verified refutations AND the empirical gate (``ctx.verify``) passes.
A refutation is verified only with reproducible evidence (``evidence_type`` test/trace plus a non-empty reproduction and,
when a ``reproduce`` callable is supplied, a reproduction that actually demonstrates the failure); anything else is a
"concern" that never kills. Refuters that disagree escalate the panel from ``k`` to ``max_k``. LLM votes never promote.

Evidence: notes/x3-forms.md P1 (2604.19049 kill rate, 2610.35930 agreement is not corroboration, 2402.06782).
"""
from __future__ import annotations

import json
import traceback
from typing import Any, Callable

from .journal import write_atomic
from .runtime import (Budget, CallSpec, FormContext, FormError, FormResult, FormSpec, Verdict, json_object)

FORM = FormSpec(
    name="refute_panel",
    summary="Authors propose; k cold-start refuters with a kill mandate try to falsify; survivors need 0 verified "
            "refutations and a passing empirical gate.",
    inputs={"task": "str: the task / review prompt", "authors": "list[str]: providers that each write a candidate",
            "refuters": "list[str] (optional): refuter providers, default authors", "k": "int: refuters per candidate (2)",
            "max_k": "int: escalation ceiling on disagreement (3)",
            "candidates": "list[{id, provider, output}] (optional): skip round 0",
            "reproduce": "callable(cwd, reproduction) -> Verdict (optional): demonstrates a refutation"},
    stop_rule="every candidate judged (refuters, escalation, gate), or budget/cancel/deadline",
    aggregation="survivor iff 0 verified refutations AND gate passes; winner = survivor with best verdict "
                "(exec > llm), ties by fewer concerns",
    default_budget={"max_calls": 2 + 3 * 3 * 2},   # 2 + 3*max_k*len(authors) for 2 authors; run() recomputes it
)

_KILL_EVIDENCE = frozenset({"test", "trace"})
_LENSES_FALLBACK = (
    "correctness: is the claim actually true as stated?",
    "evidence: does the cited source or observation really support it?",
    "reproduction: would this reproduce, or is it a plausible story?",
    "security: does it hold under adversarial input or misuse?",
    "currency: is it still true today, or stale?",
)


# -- helpers -------------------------------------------------------------------------------------------------------
def _lenses() -> tuple:
    try:
        from .. import graph
        lenses = tuple(getattr(graph, "VERIFY_LENSES", ()))
        return lenses or _LENSES_FALLBACK
    except Exception:  # noqa: BLE001
        return _LENSES_FALLBACK


def _family(provider: str) -> str:
    """Correlation family (adapter.CORRELATION_GROUPS first, then model_family); unknown providers stay distinct."""
    try:
        from .. import adapter
        for group, members in getattr(adapter, "CORRELATION_GROUPS", {}).items():
            if provider in members:
                return str(group)
        return str(adapter.model_family(provider))
    except Exception:  # noqa: BLE001
        return provider


def _text(task: Any) -> str:
    return task if isinstance(task, str) else json.dumps(task, ensure_ascii=False, default=str)


def _clean(providers: Any) -> list[str]:
    return [str(p) for p in (providers or []) if isinstance(p, str) and p.strip()]


def _stop_for(ctx: FormContext, budget_hit: bool) -> tuple[str, str] | None:
    if ctx.cancel.is_set():
        reason = ctx.cancel.reason or "cancelled"
        return ("budget_exhausted", "deadline") if reason == "deadline" else ("cancelled", reason)
    if budget_hit:
        return "budget_exhausted", ctx.budget.exhausted() or "calls"
    return None


def _verdict_view(v: Verdict | None) -> dict | None:
    if v is None:
        return None
    return {"passed": v.passed, "score": v.score, "kind": v.kind, "evidence": v.evidence[:4000]}


def _checkpoint(ctx: FormContext, *, round_no: int, best: dict | None, stop_reason: str | None = None, **extra: Any) -> None:
    if ctx.journal.ended:
        return
    state = {"round": round_no, "calls": ctx.budget.calls_used, "tokens": ctx.budget.tokens_used, "best": best,
             "stop_reason": stop_reason}
    state.update(extra)
    ctx.journal.checkpoint(state)


class _State:
    """Mutable judging state of one candidate."""

    def __init__(self, cand: dict, index: int):
        self.cand = cand
        self.index = index
        self.id = cand["id"]
        self.author_family = _family(cand.get("provider") or "")
        self.used: list[str] = []
        self.votes: list[dict] = []
        self.gate: Verdict | None = None
        self.status = "pending"        # pending | killed | unjudged | rejected | survivor

    @property
    def valid(self) -> list[dict]:
        return [v for v in self.votes if v["valid"]]

    @property
    def killed(self) -> bool:
        return any(v["verified"] for v in self.votes)

    @property
    def concerns(self) -> int:
        return sum(1 for v in self.votes if v["concern"])


def _choose_refuters(pool: list[str], author_family: str, used: list[str], want: int, ledger: Any, task_class: str) -> list[str]:
    """Pick up to ``want`` unused refuters: distinct families from the author and from each other first, then any
    family different from the author, then anything left. Ledger mean orders ties of equal rank (stable)."""
    avail = [p for p in pool if p not in used]
    if not avail or want <= 0:
        return []
    try:
        avail = [p for p, _ in ledger.rank(avail, task_class, "refuter", method="mean")]
    except Exception:  # noqa: BLE001
        pass
    fams = {_family(p) for p in used}
    chosen: list[str] = []
    for p in avail:
        f = _family(p)
        if f != author_family and f not in fams and len(chosen) < want:
            chosen.append(p)
            fams.add(f)
    for p in avail:
        if p not in chosen and _family(p) != author_family and len(chosen) < want:
            chosen.append(p)
    for p in avail:
        if p not in chosen and len(chosen) < want:
            chosen.append(p)
    return chosen


def _author_prompt(task: Any) -> str:
    return (f"{_text(task)}\n\nWork independently and return your best complete answer. Another panel will try to "
            "refute it, so state claims precisely and include how they can be checked.")


def _refuter_prompt(task: Any, cand: dict, lens: str) -> str:
    return (
        "You are an adversarial refuter with a KILL MANDATE. Your only job is to falsify the CANDIDATE below.\n"
        f"Lens: {lens}\n\nTASK:\n{_text(task)}\n\n=== CANDIDATE ===\n{cand.get('output', '')}\n=== END CANDIDATE ===\n\n"
        "Reply with ONE JSON object and nothing else:\n"
        '{"claim_refuted": true|false, "evidence_type": "test"|"trace"|"quote"|"none", '
        '"reproduction": "<exact test code, commands or trace steps that demonstrate the failure; empty if none>", '
        '"summary": "<one sentence>"}\n'
        "A refutation without reproducible evidence (evidence_type test or trace plus a reproduction) is only a "
        "concern and will NOT kill the candidate."
    )


def _parse_vote(res: Any, refuter: str, lens: str) -> dict:
    vote = {"refuter": refuter, "lens": lens, "valid": False, "claim_refuted": False, "verified": False,
            "concern": False, "evidence_type": "none", "summary": "", "reproduction": "", "repro": None,
            "status": getattr(res, "status", "missing")}
    if res is None or not getattr(res, "ok", False):
        return vote
    obj = json_object(res.output)
    if not obj or not isinstance(obj.get("claim_refuted"), bool):
        return vote
    vote["valid"] = True
    vote["claim_refuted"] = obj["claim_refuted"]
    vote["evidence_type"] = str(obj.get("evidence_type") or "none").strip().lower()
    vote["reproduction"] = str(obj.get("reproduction") or "").strip()
    vote["summary"] = str(obj.get("summary") or "")[:500]
    return vote


def _apply_reproduction(vote: dict, cand: dict, reproduce: Callable[[str, str], Verdict] | None) -> None:
    """Decide verified vs concern for one parsed vote (main thread; ``reproduce`` may run commands)."""
    if not vote["valid"] or not vote["claim_refuted"]:
        return
    structurally = vote["evidence_type"] in _KILL_EVIDENCE and bool(vote["reproduction"])
    if structurally and reproduce is not None:
        try:
            v = reproduce(str(cand.get("cwd") or ""), vote["reproduction"])
            ran, passed = True, bool(getattr(v, "passed", False))
        except Exception as exc:  # noqa: BLE001
            ran, passed = True, False
            vote["summary"] = f"{vote['summary']} [reproduce error: {type(exc).__name__}]"[:500]
        vote["repro"] = {"ran": ran, "passed": passed}
        vote["verified"] = passed
    else:
        vote["verified"] = structurally
    vote["concern"] = not vote["verified"]


def _settle_ledger(ctx: FormContext, st: _State, task_class: str) -> None:
    """Record refuter outcomes from EMPIRICAL evidence only (a passing reproduction, or an exec-kind gate)."""
    truth: str | None = None
    if any(v["repro"] and v["repro"]["passed"] for v in st.votes):
        truth = "defective"
    elif st.gate is not None and st.gate.kind == "exec":
        truth = "sound" if st.gate.passed else "defective"
    if truth is None:
        return
    for v in st.votes:
        if not v["valid"]:
            continue
        if truth == "defective":
            success = v["verified"] or (v["claim_refuted"] and st.gate is not None and st.gate.kind == "exec"
                                        and not st.gate.passed)
        else:
            success = not v["claim_refuted"]
        try:
            ctx.ledger.record(v["refuter"], task_class, "refuter", success=bool(success), verified=True)
            ctx.journal.append("ledger", worker=v["refuter"], task_class=task_class, role="refuter", success=bool(success))
        except Exception:  # noqa: BLE001 - ledger is best-effort
            pass


def _view(st: _State) -> dict:
    c = dict(st.cand)
    c.update({"status": st.status, "concerns": st.concerns, "verified_refutations": sum(1 for v in st.votes if v["verified"]),
              "refuters": list(st.used), "verdict": _verdict_view(st.gate), "verified": bool(
                  st.gate is not None and st.gate.kind == "exec" and st.gate.passed)})
    return c


def _best_survivor(states: list[_State]) -> _State | None:
    surv = [s for s in states if s.status == "survivor"]
    if not surv:
        return None

    def key(s: _State) -> tuple:
        g = s.gate
        rank = 2 if g is not None and g.kind == "exec" and g.passed else (1 if g is not None and g.kind == "llm" else 0)
        return (rank, g.score if g is not None else 0.0, -s.concerns, -s.index)

    return max(surv, key=key)


def _validate(authors: list[str], refuters: list[str] | None, k: Any, max_k: Any, candidates: Any) -> tuple[list[str], int, int]:
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise FormError("invalid_input", "k must be an integer >= 1")
    if not isinstance(max_k, int) or isinstance(max_k, bool):
        raise FormError("invalid_input", "max_k must be an integer")
    max_k = max(max_k, k)
    pool = refuters if refuters else None
    if candidates is not None:
        if not isinstance(candidates, list) or not candidates:
            raise FormError("invalid_input", "candidates must be a non-empty list")
        seen: set[str] = set()
        for c in candidates:
            if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not c["id"] or not isinstance(c.get("output"), str):
                raise FormError("invalid_input", "each candidate needs str id and str output")
            if c["id"] in seen:
                raise FormError("invalid_input", f"duplicate candidate id {c['id']!r}")
            seen.add(c["id"])
        if pool is None:
            pool = authors or _clean([c.get("provider") for c in candidates])
    elif not authors:
        raise FormError("no_providers", "authors must be non-empty")
    if pool is None:
        pool = authors
    if not pool:
        raise FormError("no_providers", "refuters must be non-empty")
    return pool, k, max_k


# -- entry point ---------------------------------------------------------------------------------------------------
def run(task: Any, *, ctx: FormContext | None = None, authors: list[str] | None = None,
        refuters: list[str] | None = None, k: int = 2, max_k: int = 3, candidates: list[dict] | None = None,
        task_class: str = "review", reproduce: Callable[[str, str], Verdict] | None = None, timeout: float = 300,
        dispatch_fn: Callable | None = None, verifier: Callable | None = None, ledger: Any = None,
        budget: Budget | None = None, cancel: Any = None, run_id: str | None = None,
        run_dir: str | None = None) -> FormResult:
    authors = _clean(authors)
    pool, k, max_k = _validate(authors, _clean(refuters), k, max_k, candidates)
    if ctx is None:
        ctx = FormContext(FORM.name, run_id=run_id, run_dir=run_dir,
                          budget=budget or Budget(max_calls=2 + 3 * max_k * max(1, len(authors or pool))),
                          cancel=cancel, dispatch_fn=dispatch_fn, ledger=ledger, verifier=verifier,
                          params={"authors": authors, "refuters": pool, "k": k, "max_k": max_k, "task_class": task_class,
                                  "candidates": [c["id"] for c in candidates] if candidates else None})
    else:
        if dispatch_fn is not None and ctx.dispatch_fn is None:
            ctx.dispatch_fn = dispatch_fn
        if verifier is not None and ctx.verifier is None:
            ctx.verifier = verifier
            bind = getattr(verifier, "bind", None)
            if callable(bind):
                bind(cancel=ctx.cancel, budget=ctx.budget)
    states: list[_State] = []
    rounds = [0]
    try:
        return _run(ctx, task, authors, pool, k, max_k, candidates, task_class, reproduce, timeout, states, rounds)
    except FormError:
        raise
    except Exception as exc:  # noqa: BLE001 - a form never loses the journal to a bug; the run ends "failed"
        ctx.journal.append("stop", reason=f"exception: {type(exc).__name__}")
        best = _best_survivor(states)
        return ctx.finish("failed", winner=_view(best) if best else None, candidates=[_view(s) for s in states],
                          rounds=rounds[0], stop_reason=f"exception: {type(exc).__name__}",
                          error="".join(traceback.format_exception_only(type(exc), exc))[:500])


def _run(ctx: FormContext, task: Any, authors: list[str], pool: list[str], k: int, max_k: int,
         candidates: list[dict] | None, task_class: str, reproduce: Callable | None, timeout: float,
         states: list[_State], rounds: list[int]) -> FormResult:
    budget_hit = False

    # Round 0: independent candidates (the author prompt never contains another candidate).
    if candidates:
        for i, c in enumerate(candidates):
            ref = f"outputs/given-{i:04d}.txt"
            try:
                write_atomic(ctx.run_dir / ref, c["output"])
            except OSError:
                ref = None
            cand = {"id": c["id"], "provider": str(c.get("provider") or ""), "output": c["output"], "output_ref": ref,
                    "round": 0}
            if c.get("cwd"):
                cand["cwd"] = c["cwd"]
            states.append(_State(cand, i))
            ctx.journal.append("candidate", id=cand["id"], provider=cand["provider"], output_ref=ref, round=0)
    else:
        specs = [CallSpec(key=f"author:{i}:{p}", provider=p, prompt=_author_prompt(task), role="author", timeout=timeout)
                 for i, p in enumerate(authors)]
        results = {r.key: r for r in ctx.parallel(specs)}
        for i, spec in enumerate(specs):
            res = results.get(spec.key)
            if res is None:
                continue
            if res.status == "budget":
                budget_hit = True
            if res.ok and res.output.strip():
                cand = {"id": f"c{i}-{spec.provider}", "provider": spec.provider, "output": res.output,
                        "output_ref": res.output_ref, "round": 0}
                states.append(_State(cand, i))
                ctx.journal.append("candidate", id=cand["id"], provider=cand["provider"], output_ref=res.output_ref, round=0)
        ctx.journal.append("round", n=0, summary={"phase": "authors", "candidates": [s.id for s in states]})
        _checkpoint(ctx, round_no=0, best=None, candidates=[s.id for s in states])
    stop = _stop_for(ctx, budget_hit)
    if not states and not stop:
        ctx.journal.append("stop", reason="no_candidates")
        return ctx.finish("failed", winner=None, candidates=[], rounds=0, stop_reason="no_candidates")

    # Refutation rounds with escalation k -> max_k on disagreement.
    lenses = _lenses()
    target = {s.id: k for s in states}
    round_no = 0
    while not stop:
        plan: list[tuple[_State, str, int]] = []
        for st in states:
            if st.killed:
                continue
            want = target[st.id] - len(st.used)
            for p in _choose_refuters(pool, st.author_family, st.used, want, ctx.ledger, task_class):
                st.used.append(p)
                plan.append((st, p, len(st.used) - 1))
        if not plan:
            break
        round_no += 1
        specs = []
        for st, p, j in plan:
            lens = lenses[(st.index + j) % len(lenses)]
            specs.append(CallSpec(key=f"refute:{st.id}:{j}:{p}", provider=p, role="refuter", timeout=timeout,
                                  prompt=_refuter_prompt(task, st.cand, lens), cwd=st.cand.get("cwd")))
        results = {r.key: r for r in ctx.parallel(specs)}
        summary: dict[str, Any] = {"phase": "refute", "votes": {}}
        for (st, p, j), spec in zip(plan, specs):
            res = results.get(spec.key)
            if res is not None and res.status == "budget":
                budget_hit = True
            vote = _parse_vote(res, p, lenses[(st.index + j) % len(lenses)])
            _apply_reproduction(vote, st.cand, reproduce)
            st.votes.append(vote)
            ctx.journal.append("refutation", candidate_id=st.id, refuter=p, valid=vote["valid"],
                               claim_refuted=vote["claim_refuted"], verified=vote["verified"], concern=vote["concern"],
                               evidence_type=vote["evidence_type"], repro=vote["repro"], summary=vote["summary"])
            summary["votes"].setdefault(st.id, []).append(
                {"refuter": p, "verified": vote["verified"], "concern": vote["concern"], "valid": vote["valid"]})
        rounds[0] = round_no
        ctx.journal.append("round", n=round_no, summary=summary)
        _checkpoint(ctx, round_no=round_no, best=None, candidates=[s.id for s in states])
        stop = _stop_for(ctx, budget_hit)
        if stop:
            break
        for st in states:  # escalation decision
            if st.killed:
                continue
            valid = st.valid
            disagree = any(v["concern"] for v in valid) and any(not v["claim_refuted"] for v in valid)
            if disagree:
                target[st.id] = max_k
            elif len(valid) < k:
                target[st.id] = min(max_k, len(st.used) + (k - len(valid)))

    # Classify, then run the empirical gate for fully judged, un-killed candidates.
    for st in states:
        if st.killed:
            st.status = "killed"
        elif not st.valid:
            st.status = "unjudged"
        elif stop and len(st.valid) < k:
            st.status = "unjudged"
    for st in states:
        if st.status == "killed":
            _settle_ledger(ctx, st, task_class)
    for st in states:
        if st.status != "pending":
            continue
        if ctx.cancel.is_set():
            break
        if ctx.verifier is not None:
            st.gate = ctx.verify({"id": st.id, "provider": st.cand.get("provider"), "output": st.cand["output"],
                                  "cwd": st.cand.get("cwd")})
            st.status = "survivor" if st.gate.passed else "rejected"
        else:
            st.status = "survivor"
            score = round(1.0 / (1 + st.concerns), 4)
            st.gate = Verdict(True, score, "llm", "no verified refutation; no empirical gate configured")
            ctx.journal.append("verdict", candidate_id=st.id, passed=True, score=score, kind="llm",
                               evidence="no verified refutation; no empirical gate configured")
        _settle_ledger(ctx, st, task_class)
        best = _best_survivor(states)
        _checkpoint(ctx, round_no=round_no, best=_best_view(best), candidates=[s.id for s in states])
    gate_round = round_no + 1
    rounds[0] = gate_round
    ctx.journal.append("round", n=gate_round, summary={"phase": "gate", "survivors": [s.id for s in states if s.status == "survivor"],
                                                      "killed": [s.id for s in states if s.status == "killed"],
                                                      "rejected": [s.id for s in states if s.status == "rejected"]})
    stop = _stop_for(ctx, budget_hit)
    best = _best_survivor(states)
    winner = _view(best) if best else None
    if best is not None and best.gate is not None and best.gate.kind == "llm":
        winner["verified"] = False
    if stop:
        status, reason = stop
    elif winner is not None:
        status, reason = "ok", "all_judged"
    else:
        status, reason = "failed", "no_survivor"
    ctx.journal.append("stop", reason=reason)
    _checkpoint(ctx, round_no=gate_round, best=_best_view(best), stop_reason=reason, candidates=[s.id for s in states])
    return ctx.finish(status, winner=winner, candidates=[_view(s) for s in states], rounds=gate_round, stop_reason=reason,
                      survivors=[s.id for s in states if s.status == "survivor"],
                      killed=[s.id for s in states if s.status == "killed"],
                      verified=bool(winner and winner.get("verified")))


def _best_view(st: _State | None) -> dict | None:
    if st is None:
        return None
    v = _view(st)
    return {"id": v["id"], "provider": v.get("provider"), "output_ref": v.get("output_ref"), "round": v.get("round", 0),
            "output": v.get("output", ""), "verdict": v.get("verdict")}
