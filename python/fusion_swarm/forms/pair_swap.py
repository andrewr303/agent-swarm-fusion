"""P7 cross-provider pair programming with swap (spec 9.6; judge J-16 verifier-first, J-08/J-09 via the peers package).

One provider writes, the other reviews and repairs, and the verifier arbitrates. Which provider should write is NEVER
assumed symmetric (arXiv 2607.21656: Claude reviewing Codex helped, the reverse hurt), so the direction is chosen from
the reliability ledger's pair stats per task class:

* ``direction="auto"``: pairings whose ledger mean delta is negative after n >= 3 are DISABLED and skipped (when both are
  disabled the writer works alone: the review step is what the ledger says hurts). With two enabled pairings and
  n < 3 for both, both directions run (exploration, same verifier); otherwise the direction with the higher mean delta
  runs (a tie runs both).
* Explicit ``"ab"`` (a writes, b reviews), ``"ba"`` or ``"both"`` override the ledger.

Per direction: writer drafts -> verify (score0) -> reviewer reviews+repairs -> verify (score1). The arbiter picks the best
version by verifier verdict across everything produced; ties go to the version with fewer changed lines (difflib count
against its own writer's version). ``ledger.record_pair(writer, reviewer, task_class, score1 - score0)`` is called per
executed direction, and only when both verdicts are ``exec`` (verified) outcomes.

Default dispatch (``ctx.dispatch_fn`` unset and ``use_peers``) is ``peers.exec_json.as_dispatch_fn`` so ``a``/``b`` may be
peer names (``claude``, ``codex``, ``andrewcode``, ``opencode``) routed by the D2 policy (a blocked seat returns
``status="blocked"``, an unknown name ``status="absent"``; neither raises). The peers adapter runs with tools ``none``:
the exchange is the full solution text, which the verifier receives as the candidate ``output``. Pass your own
``dispatch_fn`` for tool-using, working-directory pair programming.
"""
from __future__ import annotations

import difflib
import hashlib
from typing import Any, Callable

from .runtime import (Budget, CallResult, CallSpec, FormContext, FormError, FormResult, FormSpec, Verdict)

FORM = FormSpec(
    name="pair_swap",
    summary="Two providers pair: one writes, the other reviews and repairs; direction chosen from the reliability ledger.",
    inputs={
        "task": "the task text",
        "a": "first provider / peer name",
        "b": "second provider / peer name",
        "task_class": "ledger task class (default implement)",
        "direction": "auto | both | ab | ba (default auto: ledger pair stats decide, disabled pairings skipped)",
        "use_peers": "route through peers.exec_json.as_dispatch_fn when ctx has no dispatch_fn (default true)",
        "verifier": "REQUIRED (on ctx or as a keyword when ctx is None)",
    },
    stop_rule="each executed direction is write -> verify -> review/repair -> verify; no loop",
    aggregation="best verifier verdict (exec > llm > none, then score); ties -> fewer changed lines",
    default_budget={"max_calls": 4},
)

_MAX_EMBED = 60_000
_DIRECTIONS = ("auto", "both", "ab", "ba")


def _clip(text: str, limit: int = _MAX_EMBED) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def _tier(kind: str) -> int:
    return {"exec": 2, "llm": 1}.get(kind, 0)


def _view(verdict: Verdict) -> dict:
    return {"passed": bool(verdict.passed), "score": float(verdict.score), "kind": str(verdict.kind),
            "evidence": str(verdict.evidence or "")[:4000]}


def changed_lines(base: str, other: str) -> int:
    """Number of added + removed lines turning ``base`` into ``other`` (difflib)."""
    count = 0
    for line in difflib.unified_diff(base.splitlines(), other.splitlines(), lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        if line[:1] in ("+", "-"):
            count += 1
    return count


def _write_prompt(task: str) -> str:
    return ("You are the writer in a pair. Complete the task below. Reply with ONLY the complete solution, "
            f"no commentary.\n\nTASK:\n{_clip(task)}\n")


def _review_prompt(task: str, draft: dict) -> str:
    v = draft["verdict"]
    return ("You are the reviewer in a pair. Review the draft below, find real defects, and return the improved "
            "solution. Reply with ONLY the complete revised solution (the draft unchanged if it is already right), "
            "no commentary.\n\n"
            f"TASK:\n{_clip(task)}\n\nDRAFT:\n{_clip(draft['output'])}\n\n"
            f"VERIFIER RESULT FOR THE DRAFT: passed={v['passed']} score={v['score']} kind={v['kind']}\n"
            f"VERIFIER EVIDENCE:\n{_clip(v['evidence'], 4000)}\n")


def plan_directions(ledger: Any, a: str, b: str, task_class: str, direction: str) -> dict:
    """Decide which direction(s) to run. Returns {"directions": [(label, writer, reviewer|None)], "reason", "stats"}.
    Pure function of the ledger's pair stats (never assumes symmetry)."""
    sab = ledger.pair_stats(a, b, task_class) if ledger is not None else {"n": 0, "mean_delta": 0.0, "disabled": False}
    sba = ledger.pair_stats(b, a, task_class) if ledger is not None else {"n": 0, "mean_delta": 0.0, "disabled": False}
    ab = ("ab", a, b)
    ba = ("ba", b, a)
    stats = {"ab": dict(sab), "ba": dict(sba)}
    if direction == "ab":
        return {"directions": [ab], "reason": "explicit", "stats": stats}
    if direction == "ba":
        return {"directions": [ba], "reason": "explicit", "stats": stats}
    if direction == "both":
        return {"directions": [ab, ba], "reason": "explicit_both", "stats": stats}
    enabled = [d for d, s in ((ab, sab), (ba, sba)) if not s["disabled"]]
    if not enabled:
        return {"directions": [("ab", a, None)], "reason": "all_pairings_disabled_solo", "stats": stats}
    if len(enabled) == 1:
        return {"directions": enabled, "reason": "other_pairing_disabled", "stats": stats}
    if sab["n"] < 3 and sba["n"] < 3:
        return {"directions": [ab, ba], "reason": "explore", "stats": stats}
    if sab["mean_delta"] > sba["mean_delta"]:
        return {"directions": [ab], "reason": "ledger", "stats": stats}
    if sba["mean_delta"] > sab["mean_delta"]:
        return {"directions": [ba], "reason": "ledger", "stats": stats}
    return {"directions": [ab, ba], "reason": "ledger_tie", "stats": stats}


def run(task: str, *, ctx: FormContext | None = None, a: str, b: str, task_class: str = "implement",
        direction: str = "auto", use_peers: bool = True, cwd: str | None = None, timeout: float = 300,
        verifier: Callable[[dict], Verdict] | None = None, dispatch_fn: Callable | None = None, ledger: Any = None,
        peer_env: dict | None = None, peer_runner: Callable | None = None) -> FormResult:
    """Run pair_swap. ``verifier`` / ``dispatch_fn`` / ``ledger`` are used only when ``ctx`` is None;
    ``peer_env`` / ``peer_runner`` are passed to ``as_dispatch_fn`` (test and embedding hooks)."""
    if not isinstance(task, str) or not task.strip():
        raise FormError("invalid_input", "task is required")
    if not isinstance(a, str) or not a.strip() or not isinstance(b, str) or not b.strip():
        raise FormError("invalid_input", "a and b must be non-empty provider names")
    if a == b:
        raise FormError("invalid_input", "a and b must be different providers")
    if direction not in _DIRECTIONS:
        raise FormError("invalid_input", f"direction must be one of {', '.join(_DIRECTIONS)}")
    effective_verifier = ctx.verifier if ctx is not None else verifier
    if effective_verifier is None:
        raise FormError("verifier_required", "pair_swap needs a verifier to arbitrate")
    if ctx is None:
        ctx = FormContext(FORM.name, budget=Budget(**FORM.default_budget), dispatch_fn=dispatch_fn, ledger=ledger,
                          verifier=verifier,
                          params={"a": a, "b": b, "task_class": task_class, "direction": direction})
    if ctx.dispatch_fn is None and use_peers:
        try:
            from ..peers import exec_json
            ctx.dispatch_fn = exec_json.as_dispatch_fn(env=peer_env, runner=peer_runner)
        except ImportError:
            pass  # peers package absent: ctx falls back to spawn.run_provider lazily

    plan = plan_directions(ctx.ledger, a, b, task_class, direction)
    ctx.journal.append("round", n=0, summary={"plan": [d[0] for d in plan["directions"]], "reason": plan["reason"],
                                              "pair_stats": plan["stats"]})
    versions: list[dict] = []
    reports: list[dict] = []
    state: dict[str, Any] = {"stop": None}

    def add_version(label: str, stage: int, provider: str, res: CallResult, writer_text: str) -> dict:
        cand: dict[str, Any] = {"id": f"{label}-v{stage}", "provider": provider, "output": res.output,
                                "round": stage, "direction": label,
                                "sha256": hashlib.sha256(res.output.encode("utf-8", errors="replace")).hexdigest(),
                                "changed_lines": 0 if stage == 0 else changed_lines(writer_text, res.output)}
        if cwd:
            cand["cwd"] = cwd
        ctx.journal.append("candidate", id=cand["id"], provider=provider, output_ref=res.output_ref, round=stage)
        cand["verdict"] = _view(ctx.verify(cand))
        cand["output_ref"] = res.output_ref
        cand["_order"] = len(versions)
        versions.append(cand)
        return cand

    def ledger_record(worker: str, role: str, success: bool) -> None:
        if ctx.ledger is None:
            return
        try:
            ctx.ledger.record(worker, task_class, role, success, verified=True)
        except (ValueError, OSError):
            return
        ctx.journal.append("ledger", worker=worker, task_class=task_class, role=role, success=bool(success))

    def interrupted(res: CallResult) -> str | None:
        if res.status == "budget":
            return "budget"
        if res.status == "cancelled" or ctx.cancel.is_set():
            return "cancelled"
        return None

    for index, (label, writer, reviewer) in enumerate(plan["directions"], start=1):
        if ctx.cancel.is_set():
            state["stop"] = "cancelled"
            break
        report: dict[str, Any] = {"direction": label, "writer": writer, "reviewer": reviewer}
        reports.append(report)
        wres = ctx.call(CallSpec(f"{label}-write", writer, _write_prompt(task), role="writer", cwd=cwd,
                                 timeout=timeout))
        report["write_status"] = wres.status
        if not wres.ok or not wres.output.strip():
            state["stop"] = interrupted(wres)
            if state["stop"]:
                break
            continue
        v0 = add_version(label, 0, writer, wres, wres.output)
        report["score0"] = v0["verdict"]["score"]
        if v0["verdict"]["kind"] == "exec":
            ledger_record(writer, "writer", bool(v0["verdict"]["passed"]))
        if reviewer is not None:
            rres = ctx.call(CallSpec(f"{label}-review", reviewer, _review_prompt(task, v0), role="reviewer", cwd=cwd,
                                     timeout=timeout))
            report["review_status"] = rres.status
            if rres.ok and rres.output.strip():
                v1 = add_version(label, 1, reviewer, rres, v0["output"])
                report["score1"] = v1["verdict"]["score"]
                report["delta"] = v1["verdict"]["score"] - v0["verdict"]["score"]
                report["changed_lines"] = v1["changed_lines"]
                if (ctx.ledger is not None and v0["verdict"]["kind"] == "exec"
                        and v1["verdict"]["kind"] == "exec"):  # verified outcomes only
                    ctx.ledger.record_pair(writer, reviewer, task_class, report["delta"])
                    ctx.journal.append("ledger", worker=f"{writer}>{reviewer}", task_class=task_class, role="pair",
                                       success=report["delta"] > 0)
                    report["recorded"] = True
                    ledger_record(reviewer, "reviewer", bool(v1["verdict"]["passed"]
                                                             and v1["verdict"]["score"] >= v0["verdict"]["score"]))
            else:
                state["stop"] = interrupted(rres)
        ctx.journal.append("round", n=index, summary=report)
        ctx.journal.checkpoint({"status": "running", "stop_reason": None, "round": index,
                                "calls": ctx.budget.calls_used, "tokens": ctx.budget.tokens_used,
                                "best": _best_view(versions)})
        if state["stop"]:
            break

    winner = _best(versions)
    stop = state["stop"] or ("no_candidates" if winner is None else "complete")
    ctx.journal.append("stop", reason=stop)
    if stop == "cancelled":
        status = "cancelled"
    elif stop == "budget":
        status = "budget_exhausted"
    elif winner is not None and winner["verdict"]["passed"]:
        status = "ok"
    else:
        status = "failed"
    verified = bool(winner and winner["verdict"]["passed"] and winner["verdict"]["kind"] == "exec")
    return ctx.finish(status, winner=_public(winner), candidates=[_public(v) for v in versions],
                      rounds=len(reports), stop_reason=stop, verified=verified, directions=reports,
                      selection={"reason": plan["reason"], "directions": [d[0] for d in plan["directions"]],
                                 "pair_stats": plan["stats"]},
                      task_class=task_class)


def _rank(v: dict) -> tuple:
    d = v["verdict"]
    return (_tier(d["kind"]), bool(d["passed"]), float(d["score"]), -int(v["changed_lines"]), -int(v["_order"]))


def _best(versions: list[dict]) -> dict | None:
    return max(versions, key=_rank) if versions else None


def _public(v: dict | None) -> dict | None:
    if v is None:
        return None
    return {k: val for k, val in v.items() if k not in ("cwd", "_order")}


def _best_view(versions: list[dict]) -> dict | None:
    return _public(_best(versions))
