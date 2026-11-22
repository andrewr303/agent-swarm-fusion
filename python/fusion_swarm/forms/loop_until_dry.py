"""P10 Loop-until-dry (spec 9.9): keep sampling finders until N consecutive rounds yield nothing new.

* Finding schema ``{"file","symbol","issue_class","summary","evidence"}``; the canonical key defaults to
  ``f"{file}:{symbol}:{issue_class}".casefold()`` (whitespace collapsed). A key already in the seen-set is never counted
  twice and never re-verified.
* Each round prompts ``per_round`` finders (default: all of them), rotating the provider window and the scope
  (``scopes[round % len(scopes)]``) for diversity. The prompt lists the keys already found (the most recent 200) and asks
  only for NEW findings; round 0 therefore carries no other finder's output (independent first pass).
* With a verifier every new finding goes through ``ctx.verify``; only passed findings are kept and counted. Without a
  verifier the dry detector counts NEW findings (not verified ones) and the result status is ``partial``.
* Stop: ``dry_rounds`` consecutive INFORMATIVE rounds with zero new kept findings, or ``max_rounds`` / budget / cancel.
  A round in which no finder returned a parseable reply carries no information and does not count as dry.
* ``extra.coverage`` is the Chao1 estimate ``S_obs + f1^2 / (2 * max(f2, 1))`` where ``f1``/``f2`` are the numbers of kept
  findings reported by exactly one / two distinct finders (a finder is a provider name); ``extra.coverage_detail`` has the
  parts. Exec-verified outcomes feed the ledger under ``(finder, task_class, "finder")``.
* Salvage: every round journals the kept findings as candidates plus a ``union-r<n>`` candidate and a checkpoint.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping

from .journal import write_atomic
from .runtime import (Budget, CallResult, CallSpec, FormContext, FormError, FormResult, FormSpec, json_object)

FORM = FormSpec(
    name="loop_until_dry",
    summary="Sample finders round after round (rotating provider and scope, novelty prompts, dedupe by canonical key) "
            "until N consecutive rounds find nothing new; Chao1 coverage estimate.",
    inputs={
        "task": "what to hunt for",
        "finders": "providers that produce findings",
        "per_round": "finders asked per round (default all)",
        "dry_rounds": "consecutive rounds with zero new findings that end the loop (default 2)",
        "max_rounds": "hard round cap (default 6)",
        "canon_key": "callable finding -> canonical key (default file:symbol:issue_class casefolded)",
        "scopes": "optional list of scope strings rotated per round",
    },
    stop_rule="dry_rounds consecutive informative rounds with zero new verified findings, or max_rounds/budget",
    aggregation="union of kept findings, deduplicated by canonical key; Chao1 coverage estimate",
    default_budget={"max_calls": 6 * 1},
)

MAX_KNOWN_KEYS = 200
_FIELDS = ("file", "symbol", "issue_class", "summary", "evidence")
_FIELD_CAPS = {"file": 500, "symbol": 300, "issue_class": 200, "summary": 1000, "evidence": 4000}
_WS = re.compile(r"\s+")


def default_budget(max_rounds: int = 6, per_round: int | None = None, n_finders: int = 1) -> dict:
    return {"max_calls": max(1, int(max_rounds) * int(per_round or n_finders))}


def default_canon_key(finding: Mapping[str, Any]) -> str:
    parts = [_WS.sub(" ", str(finding.get(k) or "")).strip() for k in ("file", "symbol", "issue_class")]
    return ":".join(parts).casefold()


def chao1(counts: Mapping[str, int]) -> dict:
    """Chao1 richness from ``{key: number of distinct finders that reported it}``."""
    s_obs = len(counts)
    f1 = sum(1 for c in counts.values() if c == 1)
    f2 = sum(1 for c in counts.values() if c == 2)
    estimate = s_obs + (f1 * f1) / (2 * max(f2, 1))
    return {"estimate": round(estimate, 3), "observed": s_obs, "f1": f1, "f2": f2,
            "completeness": round(s_obs / estimate, 3) if estimate else 1.0}


def _balanced_array(text: str) -> list | None:
    start = text.find("[")
    for _ in range(50):
        if start < 0:
            return None
        depth, in_str, esc = 0, False, False
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
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:i + 1])
                    except ValueError:
                        value = None
                    if isinstance(value, list):
                        return value
                    break
        start = text.find("[", start + 1)
    return None


def parse_findings(text: str) -> list[dict] | None:
    """Findings from a reply: ``{"findings": [...]}`` or a bare JSON array. None when nothing parseable (an empty list
    is a valid, informative answer)."""
    if not isinstance(text, str):
        return None
    raw: Any = None
    obj = json_object(text)
    if isinstance(obj, dict) and isinstance(obj.get("findings"), list):
        raw = obj["findings"]
    elif isinstance(obj, dict) and any(k in obj for k in _FIELDS) and not text.lstrip().startswith("["):
        raw = [obj]
    if raw is None:
        raw = _balanced_array(text[:2_000_000])
    if raw is None:
        return None
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        finding = {k: str(item.get(k) or "").strip()[:_FIELD_CAPS[k]] for k in _FIELDS}
        if not (finding["summary"] or finding["issue_class"] or finding["file"]):
            continue
        out.append(finding)
    return out


def _prompt(task: str, scope: str | None, known: list[str]) -> str:
    lines = [f"Task: {task}", ""]
    if scope:
        lines += [f"Scope for this pass: {scope}", ""]
    if known:
        lines += ["Findings already reported (canonical keys; do NOT repeat them, report only NEW distinct findings):"]
        lines += [f"- {k}" for k in known]
        lines.append("")
    lines += ["Reply with JSON only, exactly this shape (an empty list when you find nothing new):",
              '{"findings": [{"file": "", "symbol": "", "issue_class": "", "summary": "", "evidence": ""}]}']
    return "\n".join(lines)


def run(task: str, *, ctx: FormContext | None = None, finders: list[str], per_round: int | None = None,
        dry_rounds: int = 2, max_rounds: int = 6, canon_key: Callable[[dict], str] | None = None,
        scopes: list[str] | None = None, task_class: str = "discovery", cwd: str | None = None,
        timeout: float = 300,
        dispatch_fn: Callable | None = None, verifier: Callable | None = None, ledger: Any = None,
        budget: Budget | None = None, cancel: Any = None, run_id: str | None = None,
        run_dir: str | None = None) -> FormResult:
    if not isinstance(task, str) or not task.strip():
        raise FormError("invalid_input", "task is required")
    if not isinstance(finders, (list, tuple)) or not finders:
        raise FormError("no_providers", "finders must be a non-empty list")
    finders = [str(f).strip() for f in finders]
    if not all(finders):
        raise FormError("invalid_input", "finders must be non-empty provider names")
    for name, value in (("dry_rounds", dry_rounds), ("max_rounds", max_rounds)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise FormError("invalid_input", f"{name} must be an integer >= 1")
    if per_round is not None and (not isinstance(per_round, int) or isinstance(per_round, bool) or per_round < 1):
        raise FormError("invalid_input", "per_round must be an integer >= 1")
    scope_list = [str(s) for s in scopes] if scopes else []
    per = per_round or len(finders)
    key_fn = canon_key or default_canon_key
    if ctx is None:
        ctx = FormContext(FORM.name, run_id=run_id, run_dir=run_dir,
                          budget=budget or Budget(**default_budget(max_rounds, per_round, len(finders))),
                          cancel=cancel, dispatch_fn=dispatch_fn, ledger=ledger, verifier=verifier,
                          params={"finders": finders, "per_round": per, "dry_rounds": dry_rounds,
                                  "max_rounds": max_rounds, "scopes": scope_list})
    has_verifier = ctx.verifier is not None

    kept: dict[str, dict] = {}            # canonical key -> candidate (insertion ordered)
    seen_keys: list[str] = []             # every key ever reported (kept or rejected), insertion ordered
    seen_set: set[str] = set()
    seen_by: dict[str, set[str]] = {}     # key -> distinct finder names that reported it
    next_id = 0
    dry_streak = 0
    blind_streak = 0
    rounds_done = 0
    rejected = 0
    stop: str | None = None

    def union_view() -> list[dict]:
        return [dict(c["finding"], key=c["key"], found_by=sorted(seen_by.get(c["key"], ()))) for c in kept.values()]

    def winner_dict(n: int) -> dict | None:
        if not kept:
            return None
        return {"id": "union", "provider": "finders", "round": n, "output": json.dumps(union_view(), ensure_ascii=False),
                "findings": union_view()}

    for rnd in range(max_rounds):
        if ctx.cancel.is_set():
            stop = "cancelled"
            break
        if ctx.budget.exhausted() is not None:
            stop = "budget"
            break
        slots = [finders[(rnd * per + i) % len(finders)] for i in range(per)]
        scope = scope_list[rnd % len(scope_list)] if scope_list else None
        known = seen_keys[-MAX_KNOWN_KEYS:]
        specs = [CallSpec(f"dry-r{rnd}-f{i}", slots[i], _prompt(task, scope, known), role="finder", cwd=cwd, timeout=timeout)
                 for i in range(per)]
        results = {r.key: r for r in ctx.parallel(specs)}
        interrupted: str | None = None
        informative = False
        reported = 0
        new_kept = 0
        new_rejected = 0
        for i in range(per):  # slot order: deterministic first-reporter regardless of completion order
            res: CallResult | None = results.get(f"dry-r{rnd}-f{i}")
            if res is None:
                continue
            if res.status in ("budget", "cancelled"):
                interrupted = "budget" if res.status == "budget" else "cancelled"
                continue
            findings = parse_findings(res.output) if res.ok else None
            if findings is None:
                continue
            informative = True
            finder = slots[i]
            for finding in findings:
                try:
                    key = str(key_fn(finding)).strip().casefold()
                except Exception:  # noqa: BLE001 - a bad canon_key must not kill the run
                    key = default_canon_key(finding)
                if not key.strip(":"):
                    continue
                reported += 1
                seen_by.setdefault(key, set()).add(finder)
                if key in seen_set:
                    continue
                seen_set.add(key)
                seen_keys.append(key)
                next_id += 1
                cid = f"f{next_id}"
                payload = json.dumps(finding, ensure_ascii=False, sort_keys=True)
                ref: str | None = f"outputs/finding-{cid}.txt"
                try:
                    write_atomic(ctx.run_dir / ref, payload)
                except OSError:
                    ref = None
                ctx.journal.append("candidate", id=cid, provider=finder, output_ref=ref, round=rnd)
                candidate = {"id": cid, "provider": finder, "output": payload, "finding": finding, "key": key,
                             "round": rnd, "output_ref": ref}
                if has_verifier:
                    vin = {"id": cid, "provider": finder, "output": payload, "finding": finding}
                    if cwd:
                        vin["cwd"] = cwd
                    verdict = ctx.verify(vin)
                    candidate["verdict"] = {"passed": verdict.passed, "score": verdict.score, "kind": verdict.kind,
                                            "evidence": verdict.evidence[:400]}
                    if verdict.kind == "exec":
                        ctx.ledger.record(finder, task_class, "finder", bool(verdict.passed), verified=True)
                        ctx.journal.append("ledger", worker=finder, task_class=task_class, role="finder",
                                           success=bool(verdict.passed))
                    if not verdict.passed:
                        new_rejected += 1
                        rejected += 1
                        continue
                kept[key] = candidate
                new_kept += 1
        rounds_done = rnd + 1
        if interrupted is None:
            if not informative:
                blind_streak += 1
                dry_streak = 0
            else:
                blind_streak = 0
                dry_streak = dry_streak + 1 if new_kept == 0 else 0
        coverage = chao1({k: len(seen_by.get(k, ())) for k in kept})
        union_ref: str | None = f"outputs/union-r{rnd}.txt"
        union_text = json.dumps(union_view(), ensure_ascii=False)
        try:
            write_atomic(ctx.run_dir / union_ref, union_text)
        except OSError:
            union_ref = None
        ctx.journal.append("candidate", id=f"union-r{rnd}", provider="finders", output_ref=union_ref, round=rnd)
        ctx.journal.append("round", n=rnd, summary={
            "finders": slots, "scope": scope, "reported": reported, "new": new_kept, "rejected": new_rejected,
            "total": len(kept), "dry_streak": dry_streak, "informative": informative, "interrupted": interrupted,
            "coverage": coverage["estimate"]})
        best = winner_dict(rnd)
        ctx.journal.checkpoint({"status": "running", "stop_reason": None, "round": rounds_done,
                                "calls": ctx.budget.calls_used, "tokens": ctx.budget.tokens_used,
                                "best": ({"id": best["id"], "provider": best["provider"], "round": rnd,
                                          "output": best["output"], "output_ref": union_ref} if best else None),
                                "found": len(kept)})
        if interrupted is not None:
            stop = interrupted
            break
        if dry_streak >= dry_rounds:
            stop = "dry"
            break
        if blind_streak >= dry_rounds:
            stop = "no_valid_responses"
            break
    else:
        stop = "max_rounds"
    stop = stop or "max_rounds"
    ctx.journal.append("stop", reason=stop)
    winner = winner_dict(rounds_done)
    if stop == "cancelled":
        status = "cancelled"
    elif stop == "budget":
        status = "budget_exhausted"
    elif stop == "no_valid_responses":
        status = "failed" if winner is None else "partial"
    elif stop == "dry":
        status = "ok" if has_verifier else "partial"
    else:
        status = "partial"
    coverage = chao1({k: len(seen_by.get(k, ())) for k in kept})
    return ctx.finish(status, winner=winner, candidates=list(kept.values()), rounds=rounds_done, stop_reason=stop,
                      findings=union_view(), coverage=coverage["estimate"], coverage_detail=coverage,
                      rejected=rejected, dry_streak=dry_streak, verified=has_verifier)
