"""P6 critic-repair (spec 9.5; judge J-16 verifier-first).

A writer drafts, an INDEPENDENT-family critic reviews, the writer repairs; every version is run through the verifier
and the loop obeys deterministic stop rules. "The critic says it looks fine" is never enough: the winner is the best
version by verifier verdict, and a regression restores the best-so-far snapshot.

Stop rules (the first that holds wins; evaluated in this order after each round):
  (a) the verifier passes AND the critic's verdict is ``ok``            -> ``verified_and_critic_ok``
  (c) the repaired output repeats an earlier version (sha256)           -> ``output_repeated``
  (e) the repaired version scores below the best so far                 -> ``regression_reverted`` (best restored)
  (b) no verifier-score improvement for ``patience`` repair rounds      -> ``no_improvement``
  (d) ``max_rounds`` repair rounds done                                 -> ``max_rounds``
Extra non-rule exits: ``verified_critic_unavailable`` (verifier passes but the critic gave no parseable verdict),
``writer_failed`` / ``repair_failed`` (a provider call failed), ``budget`` and ``cancelled``.

Ledger writes happen only from verified (``exec``) verdicts: the writer's first draft, and each critic by whether the
repair it triggered improved the verified score.
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable

from .runtime import (Budget, CallResult, CallSpec, FormContext, FormError, FormResult, FormSpec, Verdict,
                      json_object)

FORM = FormSpec(
    name="critic_repair",
    summary="Writer drafts, a different-family critic reviews, the writer repairs; the verifier (not the critic) decides.",
    inputs={
        "task": "the task text",
        "writer": "provider that drafts and repairs",
        "critics": "providers that critique (a different model family from the writer is preferred; rotated per round)",
        "max_rounds": "repair rounds cap (default 3)",
        "patience": "stop after this many rounds without a verifier-score improvement (default 2)",
        "task_class": "ledger task class (default implement)",
        "verifier": "REQUIRED (on ctx or as a keyword when ctx is None)",
    },
    stop_rule=("verifier pass + critic ok; no verified improvement for `patience` rounds; repeated output hash; "
               "max_rounds; score regression restores the best-so-far snapshot"),
    aggregation="best version by verifier verdict (exec > llm > none, then score); never the critic's opinion alone",
    default_budget={"max_calls": 1 + 3 * 3},
)

_MAX_EMBED = 60_000


def _clip(text: str, limit: int = _MAX_EMBED) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def _family(provider: str) -> str:
    try:
        from .. import adapter
        return str(adapter.model_family(provider))
    except Exception:  # noqa: BLE001 - family is a soft preference
        return provider


def _tier(kind: str) -> int:
    return {"exec": 2, "llm": 1}.get(kind, 0)


def _key(version: dict) -> tuple:
    """Larger is better: exec > llm > none, passed, then verifier score."""
    v = version["verdict"]
    return (_tier(v["kind"]), bool(v["passed"]), float(v["score"]))


def _view(verdict: Verdict) -> dict:
    return {"passed": bool(verdict.passed), "score": float(verdict.score), "kind": str(verdict.kind),
            "evidence": str(verdict.evidence or "")[:4000]}


def _status_for(stop: str, winner: dict | None) -> str:
    if stop == "cancelled":
        return "cancelled"
    if stop == "budget":
        return "budget_exhausted"
    if winner is not None and winner["verdict"]["passed"]:
        return "ok"
    return "failed"


def _write_prompt(task: str) -> str:
    return ("You are the writer. Complete the task below. Reply with ONLY the complete solution, no commentary.\n\n"
            f"TASK:\n{_clip(task)}\n")


def _critic_prompt(task: str, version: dict) -> str:
    v = version["verdict"]
    return ("You are an independent critic. Review the candidate solution against the task. Report only concrete, "
            "checkable problems; do not rewrite the solution.\n"
            'Reply with ONLY one JSON object: {"issues":[{"summary":"...","evidence":"...","severity":'
            '"low|medium|high"}],"verdict":"ok|issues"}\n'
            'Use "ok" with an empty issues list when you find no real problem.\n\n'
            f"TASK:\n{_clip(task)}\n\nCANDIDATE SOLUTION:\n{_clip(version['output'])}\n\n"
            f"VERIFIER RESULT: passed={v['passed']} score={v['score']} kind={v['kind']}\n"
            f"VERIFIER EVIDENCE:\n{_clip(v['evidence'], 4000)}\n")


def _repair_prompt(task: str, version: dict, issues: list[dict]) -> str:
    v = version["verdict"]
    lines = []
    for i, issue in enumerate(issues, 1):
        lines.append(f"{i}. [{issue['severity']}] {issue['summary']}"
                     + (f" (evidence: {issue['evidence']})" if issue["evidence"] else ""))
    issue_text = "\n".join(lines) if lines else "(none reported; use the verifier evidence)"
    return ("You are the writer. Repair your solution so the verifier passes and the critic's issues are fixed. "
            "Reply with ONLY the complete revised solution, no commentary.\n\n"
            f"TASK:\n{_clip(task)}\n\nCURRENT SOLUTION:\n{_clip(version['output'])}\n\n"
            f"VERIFIER RESULT: passed={v['passed']} score={v['score']} kind={v['kind']}\n"
            f"VERIFIER EVIDENCE:\n{_clip(v['evidence'], 4000)}\n\nCRITIC ISSUES:\n{issue_text}\n")


def _parse_critic(res: CallResult) -> tuple[str | None, list[dict]]:
    """(verdict, issues); verdict None when the critic gave no usable answer."""
    if not res.ok:
        return None, []
    obj = json_object(res.output)
    if not isinstance(obj, dict):
        return None, []
    issues: list[dict] = []
    raw = obj.get("issues")
    if isinstance(raw, list):
        for item in raw[:20]:
            if isinstance(item, dict):
                summary = str(item.get("summary") or "").strip()
                if not summary:
                    continue
                sev = str(item.get("severity") or "medium").strip().lower()
                issues.append({"summary": summary[:1000], "evidence": str(item.get("evidence") or "")[:1000],
                               "severity": sev if sev in ("low", "medium", "high") else "medium"})
            elif isinstance(item, str) and item.strip():
                issues.append({"summary": item.strip()[:1000], "evidence": "", "severity": "medium"})
    verdict = str(obj.get("verdict") or "").strip().lower()
    if verdict not in ("ok", "issues"):
        if "issues" not in obj:
            return None, []
        verdict = "issues" if issues else "ok"
    # The issue list is authoritative: listed issues mean "issues", an empty "issues" verdict has nothing to act on.
    return ("issues" if issues else "ok"), issues


def _pick_critic(critics: list[str], writer: str, round_n: int) -> str:
    wf = _family(writer)
    independent = [c for c in critics if _family(c) != wf]
    pool = independent or critics
    return pool[(round_n - 1) % len(pool)]


def run(task: str, *, ctx: FormContext | None = None, writer: str, critics: list[str], max_rounds: int = 3,
        patience: int = 2, task_class: str = "implement", cwd: str | None = None, timeout: float = 300,
        restore: Callable[[dict], None] | None = None, verifier: Callable[[dict], Verdict] | None = None,
        dispatch_fn: Callable | None = None, ledger: Any = None) -> FormResult:
    """Run critic-repair. ``verifier`` / ``dispatch_fn`` / ``ledger`` are used only when ``ctx`` is None.
    ``restore(best_candidate)`` is called when a regression is reverted (use it to roll back files in ``cwd``)."""
    if not isinstance(task, str) or not task.strip():
        raise FormError("invalid_input", "task is required")
    if not isinstance(writer, str) or not writer.strip():
        raise FormError("invalid_input", "writer is required")
    if not isinstance(critics, (list, tuple)) or not critics:
        raise FormError("no_providers", "critics must be a non-empty list")
    critics = [str(c) for c in critics]
    if not all(c.strip() for c in critics):
        raise FormError("invalid_input", "critics must be non-empty provider names")
    if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or max_rounds < 0:
        raise FormError("invalid_input", "max_rounds must be an integer >= 0")
    if not isinstance(patience, int) or isinstance(patience, bool) or patience < 1:
        raise FormError("invalid_input", "patience must be an integer >= 1")
    effective_verifier = ctx.verifier if ctx is not None else verifier
    if effective_verifier is None:
        raise FormError("verifier_required", "critic_repair needs a verifier (the critic alone never decides)")
    if ctx is None:
        ctx = FormContext(FORM.name, budget=Budget(max_calls=1 + 3 * max_rounds), dispatch_fn=dispatch_fn,
                          ledger=ledger, verifier=verifier,
                          params={"writer": writer, "critics": critics, "max_rounds": max_rounds,
                                  "patience": patience, "task_class": task_class})

    versions: list[dict] = []
    seen: dict[str, str] = {}
    state: dict[str, Any] = {"best": None, "stale": 0, "stop": None, "rounds": 0}
    round_log: list[dict] = []

    def best_view() -> dict | None:
        b = state["best"]
        if b is None:
            return None
        return {k: v for k, v in b.items() if k != "cwd"}

    def checkpoint(stop: str | None = None) -> None:
        ctx.journal.checkpoint({"status": "running", "stop_reason": stop, "round": state["rounds"],
                                "calls": ctx.budget.calls_used, "tokens": ctx.budget.tokens_used,
                                "best": best_view()})

    def ledger_record(worker: str, role: str, success: bool, verdicts: list[dict]) -> None:
        if ctx.ledger is None or not all(v["kind"] == "exec" for v in verdicts):
            return  # only verified outcomes reach the ledger
        try:
            ctx.ledger.record(worker, task_class, role, success, verified=True)
        except (ValueError, OSError):
            return
        ctx.journal.append("ledger", worker=worker, task_class=task_class, role=role, success=bool(success))

    def add_version(round_n: int, provider: str, res: CallResult) -> dict:
        digest = hashlib.sha256(res.output.encode("utf-8", errors="replace")).hexdigest()
        cand: dict[str, Any] = {"id": f"v{round_n}", "provider": provider, "output": res.output, "round": round_n,
                                "sha256": digest}
        if cwd:
            cand["cwd"] = cwd
        ctx.journal.append("candidate", id=cand["id"], provider=provider, output_ref=res.output_ref, round=round_n)
        verdict = ctx.verify(cand)
        cand["verdict"] = _view(verdict)
        cand["output_ref"] = res.output_ref
        seen[digest] = cand["id"]
        versions.append(cand)
        return cand

    def finish(stop: str) -> FormResult:
        state["stop"] = stop
        ctx.journal.append("stop", reason=stop)
        winner = state["best"]
        verified = bool(winner and winner["verdict"]["passed"] and winner["verdict"]["kind"] == "exec")
        status = _status_for(stop, winner)
        return ctx.finish(status, winner=best_view(), candidates=[{k: v for k, v in c.items() if k != "cwd"}
                                                                  for c in versions],
                          rounds=state["rounds"], stop_reason=stop, verified=verified, round_log=round_log,
                          task_class=task_class, writer=writer, critics=critics)

    def failure_stop(res: CallResult, default: str) -> str:
        if res.status == "budget":
            return "budget"
        if res.status == "cancelled" or ctx.cancel.is_set():
            return "cancelled"
        return default

    # -- round 0: the writer's independent draft ---------------------------------------------------------------
    res = ctx.call(CallSpec("write-0", writer, _write_prompt(task), role="writer", cwd=cwd, timeout=timeout))
    if not res.ok or not res.output.strip():
        return finish(failure_stop(res, "writer_failed"))
    v0 = add_version(0, writer, res)
    state["best"] = v0
    ledger_record(writer, "writer", bool(v0["verdict"]["passed"]), [v0["verdict"]])
    ctx.journal.append("round", n=0, summary={"score": v0["verdict"]["score"], "passed": v0["verdict"]["passed"]})
    checkpoint()

    # -- repair rounds -------------------------------------------------------------------------------------------
    for r in range(1, max_rounds + 1):
        if ctx.cancel.is_set():
            return finish("cancelled")
        state["rounds"] = r
        current = versions[-1]
        critic = _pick_critic(critics, writer, r)
        cres = ctx.call(CallSpec(f"critic-{r}", critic, _critic_prompt(task, current), role="critic", cwd=cwd,
                                 timeout=timeout))
        if cres.status in ("budget", "cancelled"):
            return finish(failure_stop(cres, "critic_failed"))
        critic_verdict, issues = _parse_critic(cres)
        entry = {"round": r, "critic": critic, "critic_verdict": critic_verdict, "issues": len(issues),
                 "score_before": current["verdict"]["score"]}
        round_log.append(entry)
        # (a) verifier passes AND the critic is satisfied (or gave no usable signal while the verifier is green)
        if current["verdict"]["passed"] and critic_verdict == "ok":
            ctx.journal.append("round", n=r, summary=entry)
            checkpoint("verified_and_critic_ok")
            return finish("verified_and_critic_ok")
        if current["verdict"]["passed"] and critic_verdict is None:
            ctx.journal.append("round", n=r, summary=entry)
            checkpoint("verified_critic_unavailable")
            return finish("verified_critic_unavailable")
        rres = ctx.call(CallSpec(f"repair-{r}", writer, _repair_prompt(task, current, issues), role="repairer",
                                 cwd=cwd, timeout=timeout))
        if not rres.ok or not rres.output.strip():
            ctx.journal.append("round", n=r, summary={**entry, "repair": rres.status})
            return finish(failure_stop(rres, "repair_failed"))
        digest = hashlib.sha256(rres.output.encode("utf-8", errors="replace")).hexdigest()
        if digest in seen:  # (c) the repair repeated an earlier version
            entry["repeat_of"] = seen[digest]
            ctx.journal.append("round", n=r, summary=entry)
            return finish("output_repeated")
        new = add_version(r, writer, rres)
        entry["score_after"] = new["verdict"]["score"]
        improved = _key(new) > _key(current)
        ledger_record(critic, "critic", improved, [current["verdict"], new["verdict"]])
        best = state["best"]
        if _key(new) < _key(best):  # (e) regression: restore the best-so-far snapshot and stop
            entry["regression_from"] = best["id"]
            ctx.journal.append("round", n=r, summary=entry)
            if restore is not None:
                try:
                    restore(best_view() or best)
                except Exception as exc:  # noqa: BLE001 - restore hook failure is reported, never fatal
                    entry["restore_error"] = f"{type(exc).__name__}: {exc}"
            checkpoint("regression_reverted")
            return finish("regression_reverted")
        if _key(new) > _key(best):
            state["best"] = new
            state["stale"] = 0
        else:
            state["stale"] += 1
        ctx.journal.append("round", n=r, summary=entry)
        checkpoint()
        if state["stale"] >= patience:  # (b)
            return finish("no_improvement")
    return finish("max_rounds")  # (d)
