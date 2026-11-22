"""tournament (P2, spec 9.2): seeded single-elimination bracket over n candidates.

Seed = ``seed`` or ``int(sha256(run_id)[:8], 16)``; the candidate order is shuffled with ``random.Random(seed)`` and the
position is the seed number (1 = top seed). The bracket is padded to a power of two with byes for the top seeds, so
there are exactly n-1 matches over ceil(log2 n) rounds. Each match: (1) execution beats judgment: when a verifier gives
both candidates an exec-kind verdict and they differ, the better verdict wins with no judge call; (2) otherwise a judge
that did not author either candidate decides in BOTH orders; (3) if the orders disagree (or a vote is unusable) a third
judge decides; (4) last resort is the higher seed. Every match is journaled in the ``round`` record.

Evidence: notes/x3-forms.md P2 (ExPairT-LLM 2511.10855, Knockout 2506.03785, USC 2311.17311).
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import traceback
from typing import Any, Callable

from .journal import write_atomic
from .runtime import (Budget, CallSpec, FormContext, FormError, FormResult, FormSpec, Verdict, json_object)

FORM = FormSpec(
    name="tournament",
    summary="Seeded single-elimination bracket; each match judged in both orders, execution beats judgment, byes for "
            "non powers of two, n-1 matches.",
    inputs={"task": "str: the task the candidates answer", "providers": "list[str]: providers that each write a candidate",
            "judges": "list[str] (optional): judge providers, default providers",
            "candidates": "list[{id, provider, output}] (optional): skip round 0",
            "seed": "int (optional): bracket seed, default hash(run_id)"},
    stop_rule="a single survivor remains, or budget/cancel/deadline (winner = best survivor of the latest completed round)",
    aggregation="bracket winner; per match: exec verdicts, else two-order judge agreement, else tie-break by third judge, "
                "else higher seed",
    default_budget={"max_calls": 5 + 3 * 4},   # len(providers) + 3*(len(providers)-1) for 5 providers; run() recomputes
)


def _text(task: Any) -> str:
    return task if isinstance(task, str) else json.dumps(task, ensure_ascii=False, default=str)


def _clean(providers: Any) -> list[str]:
    return [str(p) for p in (providers or []) if isinstance(p, str) and p.strip()]


def _family(provider: str) -> str:
    try:
        from .. import adapter
        for group, members in getattr(adapter, "CORRELATION_GROUPS", {}).items():
            if provider in members:
                return str(group)
        return str(adapter.model_family(provider))
    except Exception:  # noqa: BLE001
        return provider


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


def bracket_order(size: int) -> list[int]:
    """Standard single-elimination seed layout for ``size`` (a power of two); adjacent entries meet in round 1."""
    order = [1]
    while len(order) < size:
        total = len(order) * 2
        order = [x for s in order for x in (s, total + 1 - s)]
    return order


def default_seed(run_id: str) -> int:
    return int(hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8], 16)


def _gen_prompt(task: Any) -> str:
    return f"{_text(task)}\n\nWork independently and return your best complete answer."


def _judge_prompt(task: Any, first: dict, second: dict) -> str:
    return ("You are an impartial judge comparing two candidate answers to the same task. Prefer correctness and "
            "completeness; ignore position, length and style.\n\n"
            f"TASK:\n{_text(task)}\n\n=== CANDIDATE A ===\n{first['output']}\n=== END A ===\n\n"
            f"=== CANDIDATE B ===\n{second['output']}\n=== END B ===\n\n"
            'Reply with ONE JSON object and nothing else: {"winner": "A"|"B", "reason": "<one sentence>"}')


def _vote(res: Any, first: dict, second: dict) -> str | None:
    """Candidate id chosen by one judge call (``first`` is shown as A), or None when unusable."""
    if res is None or not getattr(res, "ok", False):
        return None
    obj = json_object(res.output)
    w = str((obj or {}).get("winner", "")).strip().upper()
    if w == "A":
        return first["id"]
    if w == "B":
        return second["id"]
    return None


def _pick_judge(pool: list[str], a: dict, b: dict, exclude: tuple[str, ...] = ()) -> str | None:
    """A judge that authored neither candidate (and differs in family when possible); None when the pool is empty."""
    authors = {a.get("provider"), b.get("provider")}
    fams = {_family(p) for p in authors if p} | {_family(p) for p in exclude}
    ok = [p for p in pool if p not in authors and p not in exclude]
    for p in ok:
        if _family(p) not in fams:
            return p
    if ok:
        return ok[0]
    rest = [p for p in pool if p not in exclude]
    return rest[0] if rest else None


def _validate(providers: list[str], judges: list[str] | None, candidates: Any, seed: Any) -> list[str]:
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
    elif not providers:
        raise FormError("no_providers", "providers must be non-empty")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool)):
        raise FormError("invalid_input", "seed must be an integer")
    pool = judges or providers or _clean([c.get("provider") for c in (candidates or [])])
    if not pool:
        raise FormError("no_providers", "judges must be non-empty")
    return pool


def run(task: Any, *, ctx: FormContext | None = None, providers: list[str] | None = None,
        judges: list[str] | None = None, candidates: list[dict] | None = None, seed: int | None = None,
        task_class: str = "general", timeout: float = 300, dispatch_fn: Callable | None = None,
        verifier: Callable | None = None, ledger: Any = None, budget: Budget | None = None, cancel: Any = None,
        run_id: str | None = None, run_dir: str | None = None) -> FormResult:
    providers = _clean(providers)
    pool = _validate(providers, _clean(judges), candidates, seed)
    n_hint = len(candidates) if candidates else len(providers)
    if ctx is None:
        ctx = FormContext(FORM.name, run_id=run_id, run_dir=run_dir,
                          budget=budget or Budget(max_calls=(0 if candidates else n_hint) + 3 * max(0, n_hint - 1)),
                          cancel=cancel, dispatch_fn=dispatch_fn, ledger=ledger, verifier=verifier,
                          params={"providers": providers, "judges": pool, "seed": seed, "task_class": task_class,
                                  "candidates": [c["id"] for c in candidates] if candidates else None})
    else:
        if dispatch_fn is not None and ctx.dispatch_fn is None:
            ctx.dispatch_fn = dispatch_fn
        if verifier is not None and ctx.verifier is None:
            ctx.verifier = verifier
            bind = getattr(verifier, "bind", None)
            if callable(bind):
                bind(cancel=ctx.cancel, budget=ctx.budget)
    box: dict[str, Any] = {"cands": [], "survivors": [], "round": 0, "matches": []}
    try:
        return _run(ctx, task, providers, pool, candidates, seed, task_class, timeout, box)
    except FormError:
        raise
    except Exception as exc:  # noqa: BLE001
        ctx.journal.append("stop", reason=f"exception: {type(exc).__name__}")
        return ctx.finish("failed", winner=None, candidates=list(box["cands"]), rounds=box["round"],
                          stop_reason=f"exception: {type(exc).__name__}",
                          error="".join(traceback.format_exception_only(type(exc), exc))[:500])


def _run(ctx: FormContext, task: Any, providers: list[str], pool: list[str], candidates: list[dict] | None,
         seed: int | None, task_class: str, timeout: float, box: dict) -> FormResult:
    budget_hit = False
    cands: list[dict] = box["cands"]
    if candidates:
        for i, c in enumerate(candidates):
            ref = f"outputs/given-{i:04d}.txt"
            try:
                write_atomic(ctx.run_dir / ref, c["output"])
            except OSError:
                ref = None
            cand = {"id": c["id"], "provider": str(c.get("provider") or ""), "output": c["output"], "output_ref": ref, "round": 0}
            if c.get("cwd"):
                cand["cwd"] = c["cwd"]
            cands.append(cand)
            ctx.journal.append("candidate", id=cand["id"], provider=cand["provider"], output_ref=ref, round=0)
    else:
        specs = [CallSpec(key=f"gen:{i}:{p}", provider=p, prompt=_gen_prompt(task), role="author", timeout=timeout)
                 for i, p in enumerate(providers)]
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
                cands.append(cand)
                ctx.journal.append("candidate", id=cand["id"], provider=cand["provider"], output_ref=res.output_ref, round=0)
    stop = _stop_for(ctx, budget_hit)
    if not cands:
        reason = stop[1] if stop else "no_candidates"
        ctx.journal.append("stop", reason=reason)
        return ctx.finish(stop[0] if stop else "failed", winner=None, candidates=[], rounds=0, stop_reason=reason)

    used_seed = seed if seed is not None else default_seed(ctx.run_id)
    order = list(cands)
    random.Random(used_seed).shuffle(order)
    seed_of = {c["id"]: i + 1 for i, c in enumerate(order)}
    size = 1 if len(order) <= 1 else 2 ** math.ceil(math.log2(len(order)))
    slots: list[dict | None] = [order[s - 1] if s <= len(order) else None for s in bracket_order(size)]
    verdicts: dict[str, Verdict] = {}
    all_matches: list[dict] = []

    def verdict_of(c: dict) -> Verdict | None:
        if ctx.verifier is None:
            return None
        if c["id"] not in verdicts:
            v = ctx.verify({"id": c["id"], "provider": c.get("provider"), "output": c["output"], "cwd": c.get("cwd")})
            verdicts[c["id"]] = v
            if v.kind == "exec":
                try:
                    ctx.ledger.record(c.get("provider") or "unknown", task_class, "author", success=v.passed, verified=True)
                except Exception:  # noqa: BLE001
                    pass
        return verdicts[c["id"]]

    def best_of(survivors: list[dict]) -> dict | None:
        if not survivors:
            return None

        def key(c: dict) -> tuple:
            v = verdicts.get(c["id"])
            rank = 2 if v is not None and v.kind == "exec" and v.passed else (1 if v is not None and v.kind == "exec" else 0)
            return (rank, v.score if v is not None else 0.0, -seed_of[c["id"]])

        return max(survivors, key=key)

    def winner_view(c: dict | None, basis: str = "") -> dict | None:
        if c is None:
            return None
        v = verdicts.get(c["id"])
        out = dict(c)
        out["seed"] = seed_of.get(c["id"])
        out["verdict"] = _verdict_view(v)
        out["verified"] = bool(v is not None and v.kind == "exec" and v.passed)
        return out

    def ckpt_best(c: dict | None) -> dict | None:
        if c is None:
            return None
        v = verdicts.get(c["id"])
        return {"id": c["id"], "provider": c.get("provider"), "output_ref": c.get("output_ref"), "round": c.get("round", 0),
                "output": c["output"], "verdict": _verdict_view(v)}

    survivors = [c for c in slots if c is not None]
    box["survivors"] = survivors
    round_no = 0
    while len([s for s in slots if s is not None]) > 1 and not stop:
        round_no += 1
        pairs = [(slots[i], slots[i + 1]) for i in range(0, len(slots), 2)]
        matches: list[dict] = []
        advance: list[dict | None] = []
        byes: list[str] = []
        for a, b in pairs:
            if a is None or b is None:
                w = a or b
                advance.append(w)
                if w is not None:
                    byes.append(w["id"])
                continue
            if seed_of[a["id"]] > seed_of[b["id"]]:
                a, b = b, a            # a is always the higher seed (lower number)
            matches.append({"a": a, "b": b, "votes": {}, "winner": None, "basis": None, "judge": None})
            advance.append(None)       # filled after the match
        # (1) execution beats judgment
        for m in matches:
            va, vb = verdict_of(m["a"]), verdict_of(m["b"])
            if va is not None and vb is not None and va.kind == "exec" and vb.kind == "exec":
                ka, kb = (va.passed, va.score), (vb.passed, vb.score)
                if ka != kb:
                    m["winner"] = m["a"] if ka > kb else m["b"]
                    m["basis"] = "exec"
            if ctx.cancel.is_set():
                break
        stop = _stop_for(ctx, budget_hit)
        # (2) two-order judging for the rest, one parallel batch
        undecided = [m for m in matches if m["winner"] is None]
        if not stop and undecided:
            specs: list[CallSpec] = []
            for idx, m in enumerate(undecided):
                j = _pick_judge(pool, m["a"], m["b"])
                m["judge"] = j
                if j is None:
                    continue
                for tag, first, second in (("AB", m["a"], m["b"]), ("BA", m["b"], m["a"])):
                    specs.append(CallSpec(key=f"judge:{round_no}:{m['a']['id']}:{m['b']['id']}:{tag}", provider=j,
                                          prompt=_judge_prompt(task, first, second), role="judge", timeout=timeout))
            results = {r.key: r for r in ctx.parallel(specs)} if specs else {}
            if any(r.status == "budget" for r in results.values()):
                budget_hit = True
            stop = _stop_for(ctx, budget_hit)
            if not stop:
                need_third: list[dict] = []
                for m in undecided:
                    a, b = m["a"], m["b"]
                    prefix = f"judge:{round_no}:{a['id']}:{b['id']}"
                    v_ab = _vote(results.get(prefix + ":AB"), a, b)
                    v_ba = _vote(results.get(prefix + ":BA"), b, a)
                    m["votes"] = {"AB": v_ab, "BA": v_ba}
                    if v_ab is not None and v_ab == v_ba:
                        m["winner"] = a if v_ab == a["id"] else b
                        m["basis"] = "judge"
                    else:
                        need_third.append(m)
                # (3) third judge on disagreement or unusable votes
                specs = []
                for m in need_third:
                    j3 = _pick_judge(pool, m["a"], m["b"], exclude=(m["judge"],) if m["judge"] else ())
                    m["judge3"] = j3
                    if j3 is not None:
                        specs.append(CallSpec(key=f"judge3:{round_no}:{m['a']['id']}:{m['b']['id']}", provider=j3,
                                              prompt=_judge_prompt(task, m["a"], m["b"]), role="judge", timeout=timeout))
                results3 = {r.key: r for r in ctx.parallel(specs)} if specs else {}
                if any(r.status == "budget" for r in results3.values()):
                    budget_hit = True
                stop = _stop_for(ctx, budget_hit)
                if not stop:
                    for m in need_third:
                        a, b = m["a"], m["b"]
                        v3 = _vote(results3.get(f"judge3:{round_no}:{a['id']}:{b['id']}"), a, b)
                        m["votes"]["third"] = v3
                        if v3 is not None:
                            m["winner"] = a if v3 == a["id"] else b
                            m["basis"] = "judge3"
                        else:                                        # (4) higher seed
                            m["winner"], m["basis"] = a, "seed"
        if stop or any(m["winner"] is None for m in matches):
            if not stop:
                stop = _stop_for(ctx, budget_hit) or ("budget_exhausted", "incomplete_round")
            round_no -= 1          # this round did not complete; survivors stay those of the previous round
            break
        # round complete: apply winners
        mi = 0
        for idx, (a, b) in enumerate(pairs):
            if a is None or b is None:
                continue
            advance[idx] = matches[mi]["winner"]
            mi += 1
        summary_matches = [{"a": m["a"]["id"], "b": m["b"]["id"], "judge_votes": m["votes"], "judge": m["judge"],
                            "winner": m["winner"]["id"], "basis": m["basis"]} for m in matches]
        all_matches.extend(summary_matches)
        slots = advance
        survivors = [c for c in slots if c is not None]
        box["survivors"], box["round"] = survivors, round_no
        ctx.journal.append("round", n=round_no, summary={"matches": summary_matches, "byes": byes,
                                                         "survivors": [c["id"] for c in survivors]})
        _checkpoint(ctx, round_no=round_no, best=ckpt_best(best_of(survivors)),
                    survivors=[c["id"] for c in survivors], seed=used_seed, matches=len(all_matches))
        stop = _stop_for(ctx, budget_hit)

    survivors = [c for c in slots if c is not None] if not stop else box["survivors"]
    extra = {"seed": used_seed, "matches": len(all_matches), "bracket": all_matches,
             "seeds": {c["id"]: seed_of[c["id"]] for c in order}, "survivors": [c["id"] for c in survivors]}
    if stop:
        status, reason = stop
        best = best_of(survivors)
        ctx.journal.append("stop", reason=reason)
        _checkpoint(ctx, round_no=round_no, best=ckpt_best(best), stop_reason=reason, survivors=extra["survivors"])
        return ctx.finish(status, winner=winner_view(best), candidates=[winner_view(c) for c in cands], rounds=round_no,
                          stop_reason=reason, **extra)
    champion = survivors[0]
    verdict_of(champion)                     # make sure the champion carries an execution verdict when one exists
    ctx.journal.append("stop", reason="bracket_complete")
    _checkpoint(ctx, round_no=round_no, best=ckpt_best(champion), stop_reason="bracket_complete", survivors=[champion["id"]])
    return ctx.finish("ok", winner=winner_view(champion), candidates=[winner_view(c) for c in cands], rounds=round_no,
                      stop_reason="bracket_complete", verified=bool(winner_view(champion)["verified"]), **extra)
