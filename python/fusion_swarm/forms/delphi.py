"""P8 Delphi (spec 9.7): blind round 0, a CODE facilitator, bounded revision rounds, robust final statistic + dissent log.

* Round 0 is independent: the prompt holds only the question and the reply schema (never another expert's output).
* The facilitator is code, not a model: it builds the anonymized answer histogram (casefolded, whitespace-collapsed keys),
  for numeric questions the median and IQR (``statistics.quantiles(n=4)``), and a de-identified, shuffled (seeded rng),
  length-capped (10 x 600 chars) rationale list. Rounds >= 1 see only that summary plus the expert's own previous answer.
* ``rounds`` counts ALL rounds including round 0 (the budget is ``len(experts) * rounds`` calls).
* Stop: histogram unchanged from the previous round (numeric: ``|median change| <= tol`` and IQR not increasing), or the
  rounds cap. No consensus is required.
* Final = plurality of the LAST complete round (ties: higher mean self-reported confidence, then lexicographic key) or, in
  numeric mode, its median. ``extra.dissent`` lists last-round answers that differ from the final (with rationale) and
  ``extra.flips`` counts answer changes per expert. Self-reported confidence only breaks ties; it never reaches the
  reliability ledger (this form writes none: there is no verifier-grounded outcome for an opinion question).
* Salvage: every round journals its responses, a provisional ``final-r<n>`` candidate and a checkpoint, so an interrupted
  run recovers the last complete round.
"""
from __future__ import annotations

import hashlib
import math
import random
import re
import statistics
from collections import Counter
from typing import Any, Callable

from .journal import write_atomic
from .runtime import (Budget, CallResult, CallSpec, FormContext, FormError, FormResult, FormSpec, json_object)

FORM = FormSpec(
    name="delphi",
    summary="Blind expert answers, code facilitator feedback (histogram/median/IQR), revise; robust final plus dissent log.",
    inputs={
        "question": "the question put to the panel",
        "experts": "providers on the panel (duplicates allowed: independent samples of one provider)",
        "rounds": "total rounds including round 0 (default 3)",
        "numeric": "numeric mode: estimates, median/IQR statistics, median final (default false)",
        "tol": "numeric stability tolerance on the median (default 0.0)",
        "seed": "rng seed for rationale shuffling (default derived from run_id)",
    },
    stop_rule="histogram unchanged for one round (numeric: median within tol and IQR not increasing) or rounds reached",
    aggregation="plurality (ties: mean confidence, then lexicographic) or median of the LAST round; dissent log",
    default_budget={"max_calls": 3 * 3},
)

MAX_RATIONALES = 10
RATIONALE_CHARS = 600

# Names of providers/models a rationale could leak (removed from facilitator output together with the panel's own names).
_KNOWN_NAMES = ("claude", "codex", "andrewcode", "opencode", "gpt", "gemini", "grok", "glm", "kimi", "muse", "fable",
                "agy", "copilot", "antigravity", "anthropic", "openai", "xai", "zhipu", "moonshot", "google",
                "chatgpt", "sonnet", "opus", "haiku")

_SELF_REFERENCE = (
    (re.compile(r"\bexpert[\s_#-]*\d+\b", re.I), "another expert"),
    (re.compile(r"\b(?:i|we)\s+(?:previously|earlier|originally)\s+(?:said|answered|estimated|argued|stated|thought)\b",
                re.I), "it was previously said"),
    (re.compile(r"\bmy\s+(?:previous|earlier|prior|original|first|last)\s+(?:answer|estimate|response|position|view"
                r"|round|guess)\b", re.I), "an earlier answer"),
    (re.compile(r"\bas\s+(?:an?\s+)?(?:ai|llm|language\s+model|assistant)(?:\s+model)?\b,?\s*", re.I), ""),
)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def answer_key(answer: str) -> str:
    """Histogram key: casefolded, whitespace-collapsed."""
    return " ".join(str(answer).split()).casefold()


def deidentify(text: str, names: list[str] | tuple[str, ...] = ()) -> str:
    """Remove provider/model names and self-references, collapse whitespace, cap at RATIONALE_CHARS."""
    out = _CONTROL.sub(" ", str(text or ""))
    all_names = sorted({n.strip() for n in list(names) + list(_KNOWN_NAMES) if n and n.strip()}, key=len, reverse=True)
    if all_names:
        pattern = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(n) for n in all_names) + r")(?![\w-])", re.I)
        out = pattern.sub("another expert", out)
    for rx, repl in _SELF_REFERENCE:
        out = rx.sub(repl, out)
    out = " ".join(out.split())
    return out[:RATIONALE_CHARS]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        x = float(value)
    elif isinstance(value, str):
        try:
            x = float(value.strip().replace(",", ""))
        except ValueError:
            return None
    else:
        return None
    return x if math.isfinite(x) else None


def _confidence(value: Any) -> float:
    x = _number(value)
    if x is None:
        return 0.5
    return min(1.0, max(0.0, x))


def parse_response(text: str, *, numeric: bool) -> dict | None:
    """Validate one expert reply. None when it is unusable (a numeric panel needs a finite ``estimate``)."""
    obj = json_object(text)
    if not isinstance(obj, dict):
        return None
    estimate = _number(obj.get("estimate"))
    raw = obj.get("answer")
    if isinstance(raw, bool) or raw is None:
        answer = ""
    elif isinstance(raw, (str, int, float)):
        answer = str(raw).strip()
    else:
        answer = ""
    if numeric:
        if estimate is None:
            return None
        if not answer:
            answer = format_number(estimate)
    elif not answer:
        return None
    return {"answer": answer[:2000], "key": answer_key(answer), "estimate": estimate,
            "confidence": _confidence(obj.get("confidence")),
            "rationale": str(obj.get("rationale") or "")[:4000]}


def format_number(x: float) -> str:
    if float(x).is_integer() and abs(x) < 1e15:
        return str(int(x))
    return repr(float(x))


def iqr(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    q = statistics.quantiles(values, n=4)
    return float(q[2] - q[0])


def facilitate(responses: list[dict], *, numeric: bool) -> dict:
    """Deterministic round statistics from validated responses (no model involved)."""
    hist = Counter(r["key"] for r in responses)
    conf: dict[str, list[float]] = {}
    for r in responses:
        conf.setdefault(r["key"], []).append(r["confidence"])
    stats: dict[str, Any] = {
        "n": len(responses),
        "histogram": {k: hist[k] for k in sorted(hist)},
        "mean_confidence": {k: sum(v) / len(v) for k, v in sorted(conf.items())},
    }
    if numeric:
        ests = sorted(r["estimate"] for r in responses if r["estimate"] is not None)
        stats["median"] = float(statistics.median(ests)) if ests else None
        stats["iqr"] = iqr(ests)
        stats["min"] = ests[0] if ests else None
        stats["max"] = ests[-1] if ests else None
    return stats


def plurality_key(stats: dict) -> str | None:
    hist = stats["histogram"]
    if not hist:
        return None
    mc = stats["mean_confidence"]
    return sorted(hist, key=lambda k: (-hist[k], -mc[k], k))[0]


def is_stable(prev: dict | None, cur: dict, *, numeric: bool, tol: float) -> bool:
    if prev is None:
        return False
    if numeric:
        if prev.get("median") is None or cur.get("median") is None:
            return False
        return abs(cur["median"] - prev["median"]) <= tol and cur["iqr"] <= prev["iqr"]
    return prev["histogram"] == cur["histogram"]


def _round0_prompt(question: str, numeric: bool) -> str:
    schema = ('{"answer":"your answer as short text","estimate":<number>,"confidence":<0..1>,'
              '"rationale":"why"}' if numeric else
              '{"answer":"your answer as short text","estimate":null,"confidence":<0..1>,"rationale":"why"}')
    extra = " The estimate MUST be a single finite number." if numeric else ""
    return ("You are one expert on an independent panel. Answer the question on your own.\n"
            f"Reply with ONLY one JSON object: {schema}{extra}\n\nQUESTION:\n{question}\n")


def _revise_prompt(question: str, numeric: bool, stats: dict, rationales: list[dict], own: dict | None) -> str:
    lines = [f"Panel summary after the previous round ({stats['n']} anonymous responses):", "Answer histogram:"]
    for key, count in stats["histogram"].items():
        lines.append(f'- "{key}": {count}')
    if numeric and stats.get("median") is not None:
        lines.append(f"Median estimate: {format_number(stats['median'])}; interquartile range: "
                     f"{format_number(stats['iqr'])}; min {format_number(stats['min'])}, max {format_number(stats['max'])}")
    lines.append("Anonymous rationales (shuffled):")
    if rationales:
        for i, item in enumerate(rationales, 1):
            lines.append(f'{i}. [answer: "{item["answer"]}"] {item["rationale"]}')
    else:
        lines.append("(none)")
    if own is not None:
        est = f", estimate {format_number(own['estimate'])}" if own.get("estimate") is not None else ""
        lines.append(f'Your previous answer: "{own["answer"]}"{est} (confidence {own["confidence"]:.2f}).')
    else:
        lines.append("You gave no usable answer in the previous round.")
    schema = ('{"answer":"...","estimate":<number>,"confidence":<0..1>,"rationale":"why"}' if numeric else
              '{"answer":"...","estimate":null,"confidence":<0..1>,"rationale":"why"}')
    return ("You are one expert on an anonymous panel. Reconsider using the summary below; keep your answer if you "
            "still believe it, change it if the evidence persuades you. Do not try to guess who said what.\n"
            f"Reply with ONLY one JSON object: {schema}\n\nQUESTION:\n{question}\n\n" + "\n".join(lines) + "\n")


def run(question: str, *, ctx: FormContext | None = None, experts: list[str], rounds: int = 3, numeric: bool = False,
        tol: float = 0.0, seed: int | None = None, dispatch_fn: Callable | None = None, ledger: Any = None,
        timeout: float = 300) -> FormResult:
    """Run a Delphi panel. ``dispatch_fn`` / ``ledger`` are used only when ``ctx`` is None."""
    if not isinstance(question, str) or not question.strip():
        raise FormError("invalid_input", "question is required")
    if not isinstance(experts, (list, tuple)) or not experts:
        raise FormError("no_providers", "experts must be a non-empty list")
    experts = [str(e) for e in experts]
    if not all(e.strip() for e in experts):
        raise FormError("invalid_input", "experts must be non-empty provider names")
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
        raise FormError("invalid_input", "rounds must be an integer >= 1")
    tol = _number(tol)
    if tol is None or tol < 0:
        raise FormError("invalid_input", "tol must be a finite number >= 0")
    if ctx is None:
        ctx = FormContext(FORM.name, budget=Budget(max_calls=len(experts) * rounds), dispatch_fn=dispatch_fn,
                          ledger=ledger, params={"experts": experts, "rounds": rounds, "numeric": numeric,
                                                 "tol": tol})
    rng_seed = seed if isinstance(seed, int) and not isinstance(seed, bool) else int(
        hashlib.sha256(ctx.run_id.encode()).hexdigest()[:8], 16)
    rng = random.Random(rng_seed)
    labels = [e if experts.count(e) == 1 else f"{e}#{i}" for i, e in enumerate(experts)]
    names = list(dict.fromkeys(experts))

    history: list[dict] = []          # complete rounds: {"n", "responses": [{idx,...}], "stats"}
    previous: dict[int, dict] = {}    # expert index -> own previous valid response
    flips = {label: 0 for label in labels}
    state: dict[str, Any] = {"stop": None, "winner": None, "final": None}

    def finalize(entry: dict) -> dict:
        stats = entry["stats"]
        responses = entry["responses"]
        if numeric:
            median = stats["median"]
            text = format_number(median)
            final: dict[str, Any] = {"answer": text, "estimate": median, "basis": "median", "iqr": stats["iqr"]}
            dissent_src = [r for r in responses if r["estimate"] is None or abs(r["estimate"] - median) > tol]
        else:
            key = plurality_key(stats)
            members = [r for r in responses if r["key"] == key]
            rep = max(members, key=lambda r: (r["confidence"], -r["idx"]))
            text = rep["answer"]
            final = {"answer": text, "estimate": None, "basis": "plurality", "key": key,
                     "votes": stats["histogram"][key], "of": stats["n"]}
            dissent_src = [r for r in responses if r["key"] != key]
        final["dissent"] = [{"expert": labels[r["idx"]], "answer": r["answer"], "estimate": r["estimate"],
                             "confidence": r["confidence"], "rationale": deidentify(r["rationale"], names)}
                            for r in sorted(dissent_src, key=lambda r: r["idx"])]
        return final

    def winner_dict(final: dict, round_n: int) -> dict:
        return {"id": "delphi-final", "provider": "panel", "round": round_n, "output": final["answer"],
                "answer": final["answer"], "estimate": final["estimate"], "basis": final["basis"]}

    def candidates_view() -> list[dict]:
        if not history:
            return []
        last = history[-1]
        return [{"id": f"r{last['n']}-e{r['idx']}", "provider": experts[r["idx"]], "round": last["n"],
                 "output": r["answer"], "answer_key": r["key"], "estimate": r["estimate"],
                 "confidence": r["confidence"]} for r in last["responses"]]

    for n in range(rounds):
        if ctx.cancel.is_set():
            state["stop"] = "cancelled"
            break
        if n == 0 or not history:
            prompts = [_round0_prompt(question, numeric) for _ in experts]
        else:
            last = history[-1]
            pool = [{"answer": r["answer"], "rationale": deidentify(r["rationale"], names)} for r in last["responses"]]
            rng.shuffle(pool)
            shown = [p for p in pool if p["rationale"]][:MAX_RATIONALES]
            prompts = [_revise_prompt(question, numeric, last["stats"], shown, previous.get(i))
                       for i in range(len(experts))]
        specs = [CallSpec(f"delphi-r{n}-e{i}", experts[i], prompts[i], role="expert", timeout=timeout)
                 for i in range(len(experts))]
        results = ctx.parallel(specs)
        by_key = {r.key: r for r in results}
        interrupted = None
        valid: list[dict] = []
        for i in range(len(experts)):
            res: CallResult | None = by_key.get(f"delphi-r{n}-e{i}")
            if res is None:
                continue
            if res.status in ("budget", "cancelled"):
                interrupted = "budget" if res.status == "budget" else "cancelled"
                continue
            parsed = parse_response(res.output, numeric=numeric) if res.ok else None
            if parsed is None:
                continue
            parsed["idx"] = i
            parsed["output_ref"] = res.output_ref
            valid.append(parsed)
        if interrupted is not None:  # the round did not complete: salvage keeps the last complete round
            state["stop"] = interrupted
            break
        if not valid:
            state["stop"] = "no_valid_responses"
            break
        stats = facilitate(valid, numeric=numeric)
        prev_stats = history[-1]["stats"] if history else None
        for r in valid:
            before = previous.get(r["idx"])
            if before is not None and before["key"] != r["key"]:
                flips[labels[r["idx"]]] += 1
            previous[r["idx"]] = r
            ctx.journal.append("candidate", id=f"r{n}-e{r['idx']}", provider=experts[r["idx"]],
                               output_ref=r["output_ref"], round=n)
        entry = {"n": n, "responses": valid, "stats": stats}
        history.append(entry)
        final = finalize(entry)
        state["final"] = final
        state["winner"] = winner_dict(final, n)
        ref = f"outputs/final-r{n}.txt"
        try:
            write_atomic(ctx.run_dir / ref, final["answer"])
        except OSError:
            ref = None
        ctx.journal.append("candidate", id=f"final-r{n}", provider="panel", output_ref=ref, round=n)
        stable = is_stable(prev_stats, stats, numeric=numeric, tol=tol)
        ctx.journal.append("round", n=n, summary={"stats": stats, "responses": len(valid), "stable": stable,
                                                  "final": final["answer"]})
        ctx.journal.checkpoint({"status": "running", "stop_reason": None, "round": n + 1,
                                "calls": ctx.budget.calls_used, "tokens": ctx.budget.tokens_used,
                                "best": state["winner"]})
        if stable:
            state["stop"] = "stable_histogram"
            break
    else:
        state["stop"] = "rounds_reached"
    stop = state["stop"] or "rounds_reached"
    ctx.journal.append("stop", reason=stop)
    winner = state["winner"]
    if winner is None:
        status = {"cancelled": "cancelled", "budget": "budget_exhausted"}.get(stop, "failed")
    elif stop in ("stable_histogram", "rounds_reached"):
        status = "ok"
    else:
        status = {"cancelled": "cancelled", "budget": "budget_exhausted"}.get(stop, "partial")
    final = state["final"] or {}
    return ctx.finish(status, winner=winner, candidates=candidates_view(), rounds=len(history), stop_reason=stop,
                      final=({k: v for k, v in final.items() if k != "dissent"} if final else None),
                      dissent=final.get("dissent", []), flips=flips,
                      histograms=[{"round": h["n"], **{k: v for k, v in h["stats"].items()}} for h in history],
                      numeric=numeric, seed=rng_seed)
