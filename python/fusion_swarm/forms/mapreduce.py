"""mapreduce (P5, spec 9.4): disjoint shards -> mappers (with backup tasks for stragglers) -> one reducer.

Shards come from a deterministic partitioner (``partition``) and each shard has an id. Shards are dispatched round-robin
over mappers under a concurrency limit (``ctx.max_workers``); a shard whose attempt has run longer than
``backup_after * median(done durations)`` (once ``min_done_for_median`` shards are done) gets ONE backup on a different
mapper, capped at ``ceil(max_backup_frac * len(shards))`` backups in total (a failed first attempt also uses a backup).
The first valid result per shard id wins and its twin is cancelled (process group killed); late twins are dropped.
The reducer sees only shard results, must cite shard ids and surface conflicts instead of silently picking one;
coverage = every shard id is both mapped and cited, otherwise the run is ``partial`` with ``extra.missing`` /
``extra.uncited``. Salvage keeps per-shard results and the missing list; the reducer is never re-run implicitly.

Evidence: notes/x3-forms.md P5 (LLMxMapReduce 2410.09342, MapReduce backup tasks, 2512.08296 decomposable tasks).
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import math
import statistics
import time
import traceback
from typing import Any, Callable

from .runtime import (Budget, CallResult, CallSpec, CancelToken, FormContext, FormError, FormResult, FormSpec, Verdict,
                      json_object)

FORM = FormSpec(
    name="mapreduce",
    summary="Deterministic shards -> mappers with backup tasks for stragglers (2x median, <=10%) -> reducer that cites "
            "shard ids; coverage check; conflicts surfaced.",
    inputs={"task": "str: what to extract/answer from each shard",
            "shards": "list[{id, payload}]: unique ids (see partition())", "mappers": "list[str]: mapper providers",
            "reducer": "str: reducer provider", "backup_after": "float: straggler factor over median (2.0)",
            "max_backup_frac": "float: backup cap as a fraction of shards (0.10)",
            "min_done_for_median": "int: shards done before stragglers are judged (3)"},
    stop_rule="every shard has a valid result (or exhausted its attempts), then one reduce; budget/cancel/deadline",
    aggregation="first valid result per shard id (dedupe); reducer merges citing shard ids; conflicts surfaced; "
                "coverage union == all ids else partial",
    default_budget={"max_calls": math.ceil(10 * 1.1 + 2)},   # ceil(len(shards)*1.1 + 2) for 10 shards; run() recomputes
)

_POLL_S = 0.05


# -- partitioner ---------------------------------------------------------------------------------------------------
def partition(items: list[str], n_shards: int, *, by: str = "chunk", prefix: str = "s") -> list[dict]:
    """Deterministic partitioner: ``by="chunk"`` keeps order in contiguous groups, ``by="hash"`` assigns an item to
    ``sha256(item) % n_shards``. Returns ``[{"id", "payload"}]`` (empty groups dropped); same input -> same shards."""
    if not isinstance(n_shards, int) or n_shards < 1:
        raise FormError("invalid_input", "n_shards must be an integer >= 1")
    items = [str(i) for i in items]
    groups: list[list[str]] = [[] for _ in range(n_shards)]
    if by == "hash":
        for item in items:
            groups[int(hashlib.sha256(item.encode("utf-8")).hexdigest(), 16) % n_shards].append(item)
    elif by == "chunk":
        size = max(1, math.ceil(len(items) / n_shards))
        for i, item in enumerate(items):
            groups[min(n_shards - 1, i // size)].append(item)
    else:
        raise FormError("invalid_input", "by must be 'chunk' or 'hash'")
    return [{"id": f"{prefix}{i:04d}", "payload": "\n".join(g)} for i, g in enumerate(groups) if g]


# -- helpers -------------------------------------------------------------------------------------------------------
def _text(task: Any) -> str:
    return task if isinstance(task, str) else json.dumps(task, ensure_ascii=False, default=str)


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


def _map_prompt(task: Any, shard: dict) -> str:
    return (f"{_text(task)}\n\nYou are one mapper working on ONE disjoint shard. Use only this shard.\n"
            f"SHARD ID: {shard['id']}\n=== SHARD ===\n{shard['payload']}\n=== END SHARD ===\n\n"
            'Reply with ONE JSON object and nothing else: {"shard_id": "<the shard id above>", '
            '"facts": ["<fact>", ...], "confidence": <number 0..1>}')


def _parse_map(res: CallResult | None, shard_id: str) -> dict | None:
    """Valid mapper result or None (wrong shard id, missing facts, bad confidence)."""
    if res is None or not res.ok:
        return None
    obj = json_object(res.output)
    if not obj or str(obj.get("shard_id")) != shard_id or not isinstance(obj.get("facts"), list):
        return None
    conf = obj.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        return None
    return {"shard_id": shard_id, "facts": obj["facts"], "confidence": min(1.0, max(0.0, float(conf)))}


def _reduce_prompt(task: Any, results: list[dict], retry: bool) -> str:
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in results)
    return (f"{_text(task)}\n\nYou are the reducer. Merge ONLY the shard results below (one JSON object per line). "
            "Cite the shard id of every fact you rely on. If shard results contradict each other, list the conflict "
            "with the involved shard ids instead of silently picking one.\n"
            f"=== SHARD RESULTS ===\n{body}\n=== END ===\n\n"
            'Reply with ONE JSON object and nothing else: {"merged": <your merged answer>, '
            '"conflicts": [{"claim": "...", "shards": ["<id>", ...]}], "cited": ["<every shard id you used>"]}'
            + ("\nYour previous reply was not valid JSON of that shape; return only the JSON object." if retry else ""))


def _parse_reduce(res: CallResult | None) -> dict | None:
    if res is None or not res.ok:
        return None
    obj = json_object(res.output)
    if not obj or "merged" not in obj:
        return None
    cited = obj.get("cited")
    conflicts = obj.get("conflicts")
    return {"merged": obj["merged"], "conflicts": conflicts if isinstance(conflicts, list) else [],
            "cited": [str(c) for c in cited] if isinstance(cited, list) else []}


class _Attempt:
    def __init__(self, shard_id: str, idx: int, mapper: str, kind: str, token: CancelToken):
        self.shard_id = shard_id
        self.idx = idx
        self.mapper = mapper
        self.kind = kind                    # primary | backup
        self.token = token
        self.future: cf.Future | None = None
        self.start: float | None = None     # set by the worker when the call actually begins
        self.end: float | None = None
        self.result: CallResult | None = None
        self.parsed: dict | None = None
        self.processed = False
        self.cancelled = False


def _validate(shards: Any, mappers: Any, reducer: Any, backup_after: Any, frac: Any, min_done: Any) -> tuple[list[dict], list[str]]:
    if not isinstance(shards, list) or not shards:
        raise FormError("invalid_input", "shards must be a non-empty list of {id, payload}")
    seen: set[str] = set()
    for s in shards:
        if not isinstance(s, dict) or not isinstance(s.get("id"), str) or not s["id"] or not isinstance(s.get("payload"), str):
            raise FormError("invalid_input", "each shard needs a non-empty str id and a str payload")
        if s["id"] in seen:
            raise FormError("invalid_input", f"duplicate shard id {s['id']!r}")
        seen.add(s["id"])
    clean = [m for m in (mappers or []) if isinstance(m, str) and m.strip()]
    if not clean:
        raise FormError("no_providers", "mappers must be non-empty")
    if not isinstance(reducer, str) or not reducer.strip():
        raise FormError("no_providers", "reducer must be a provider name")
    if not isinstance(backup_after, (int, float)) or backup_after < 0:
        raise FormError("invalid_input", "backup_after must be >= 0")
    if not isinstance(frac, (int, float)) or not 0 <= frac <= 1:
        raise FormError("invalid_input", "max_backup_frac must be within 0..1")
    if not isinstance(min_done, int) or min_done < 1:
        raise FormError("invalid_input", "min_done_for_median must be an integer >= 1")
    return shards, clean


def run(task: Any, *, ctx: FormContext | None = None, shards: list[dict] | None = None, mappers: list[str] | None = None,
        reducer: str | None = None, backup_after: float = 2.0, max_backup_frac: float = 0.10,
        min_done_for_median: int = 3, task_class: str = "map", timeout: float = 300,
        dispatch_fn: Callable | None = None, verifier: Callable | None = None, ledger: Any = None,
        budget: Budget | None = None, cancel: Any = None, run_id: str | None = None,
        run_dir: str | None = None) -> FormResult:
    shards, mapper_list = _validate(shards, mappers, reducer, backup_after, max_backup_frac, min_done_for_median)
    if ctx is None:
        ctx = FormContext(FORM.name, run_id=run_id, run_dir=run_dir,
                          budget=budget or Budget(max_calls=math.ceil(len(shards) * 1.1 + 2)), cancel=cancel,
                          dispatch_fn=dispatch_fn, ledger=ledger, verifier=verifier,
                          params={"shards": [s["id"] for s in shards], "mappers": mapper_list, "reducer": reducer,
                                  "backup_after": backup_after, "max_backup_frac": max_backup_frac,
                                  "min_done_for_median": min_done_for_median, "task_class": task_class})
    else:
        if dispatch_fn is not None and ctx.dispatch_fn is None:
            ctx.dispatch_fn = dispatch_fn
        if verifier is not None and ctx.verifier is None:
            ctx.verifier = verifier
            bind = getattr(verifier, "bind", None)
            if callable(bind):
                bind(cancel=ctx.cancel, budget=ctx.budget)
    box: dict[str, Any] = {"done": {}, "round": 0}
    try:
        return _run(ctx, task, shards, mapper_list, reducer, backup_after, max_backup_frac, min_done_for_median,
                    task_class, timeout, box)
    except Exception as exc:  # noqa: BLE001
        ctx.journal.append("stop", reason=f"exception: {type(exc).__name__}")
        ids = [s["id"] for s in shards]
        return ctx.finish("failed", winner=None, candidates=list(box["done"].values()), rounds=box["round"],
                          stop_reason=f"exception: {type(exc).__name__}",
                          missing=[i for i in ids if i not in box["done"]],
                          error="".join(traceback.format_exception_only(type(exc), exc))[:500])


def _run(ctx: FormContext, task: Any, shards: list[dict], mappers: list[str], reducer: str, backup_after: float,
         max_backup_frac: float, min_done: int, task_class: str, timeout: float, box: dict) -> FormResult:
    ids = [s["id"] for s in shards]
    by_id = {s["id"]: s for s in shards}
    cap = math.ceil(max_backup_frac * len(shards)) if max_backup_frac > 0 else 0
    done: dict[str, dict] = box["done"]            # shard id -> candidate record of the winning attempt
    results: dict[str, dict] = {}                  # shard id -> parsed mapper result
    durations: list[float] = []
    attempts: list[_Attempt] = []
    by_shard: dict[str, list[_Attempt]] = {i: [] for i in ids}
    tried: dict[str, list[str]] = {i: [] for i in ids}
    backups_used = 0
    budget_hit = False
    pending: set[cf.Future] = set()
    primary_pool = cf.ThreadPoolExecutor(max_workers=max(1, ctx.max_workers), thread_name_prefix="mr-map")
    backup_pool = cf.ThreadPoolExecutor(max_workers=max(1, cap), thread_name_prefix="mr-backup")

    def work(att: _Attempt) -> CallResult:
        att.start = time.monotonic()
        spec = CallSpec(key=f"map:{att.shard_id}:{att.idx}:{att.mapper}", provider=att.mapper,
                        prompt=_map_prompt(task, by_id[att.shard_id]), role="mapper", timeout=timeout)
        try:
            return ctx.call(spec, cancel=att.token)
        finally:
            att.end = time.monotonic()

    def launch(shard_id: str, mapper: str, kind: str) -> _Attempt:
        att = _Attempt(shard_id, len(attempts), mapper, kind, ctx.cancel.child())
        attempts.append(att)
        by_shard[shard_id].append(att)
        tried[shard_id].append(mapper)
        ctx.journal.append("lease", shard_id=shard_id, mapper=mapper, attempt=att.idx, kind=kind)
        att.future = (backup_pool if kind == "backup" else primary_pool).submit(work, att)
        pending.add(att.future)
        return att

    def pick_backup_mapper(shard_id: str) -> str | None:
        fresh = [m for m in mappers if m not in tried[shard_id]]
        if fresh:
            return fresh[0]
        return mappers[0] if len(mappers) == 1 else None

    def running(shard_id: str) -> list[_Attempt]:
        return [a for a in by_shard[shard_id] if not a.processed and not a.cancelled]

    def cancel_twins(shard_id: str, winner: _Attempt) -> None:
        for a in by_shard[shard_id]:
            if a is winner or a.processed:
                continue
            a.cancelled = True
            if a.future is not None and a.future.cancel():
                pending.discard(a.future)
                a.processed = True
                continue
            ctx.journal.append("cancel", key=f"map:{a.shard_id}:{a.idx}:{a.mapper}", reason="dedupe")
            a.token.cancel("dedupe")
            a.processed = True              # no barrier on a loser: the main loop stops waiting for it
            if a.future is not None:
                pending.discard(a.future)

    def process(att: _Attempt) -> None:
        nonlocal budget_hit
        att.processed = True
        res = att.result
        if res is None:
            return
        if res.status == "budget":
            budget_hit = True
        if res.status not in ("cancelled", "budget", "absent", "blocked"):
            try:
                ctx.ledger.observe_latency(att.mapper, task_class, res.elapsed_s)
            except Exception:  # noqa: BLE001
                pass
        if att.shard_id in done or att.cancelled:
            return                                   # dedupe by shard id: the first valid result already won
        parsed = _parse_map(res, att.shard_id)
        if parsed is None:
            return
        att.parsed = parsed
        results[att.shard_id] = parsed
        cand = {"id": f"shard:{att.shard_id}", "provider": att.mapper, "output": res.output, "output_ref": res.output_ref,
                "round": 0, "shard_id": att.shard_id, "attempt": att.idx, "kind": att.kind, "result": parsed}
        done[att.shard_id] = cand
        if att.start is not None and att.end is not None:
            durations.append(max(0.0, att.end - att.start))
        ctx.journal.append("candidate", id=cand["id"], provider=att.mapper, output_ref=res.output_ref, round=0)
        cancel_twins(att.shard_id, att)
        _checkpoint(ctx, round_no=1, best=None, shards_done=[i for i in ids if i in done],
                    missing=[i for i in ids if i not in done], backups=backups_used)

    def maybe_backup(shard_id: str, reason: str, delay_s: float) -> None:
        nonlocal backups_used
        if backups_used >= cap or shard_id in done:
            return
        if any(a.kind == "backup" for a in by_shard[shard_id]):
            return
        mapper = pick_backup_mapper(shard_id)
        if mapper is None:
            return
        backups_used += 1
        att = launch(shard_id, mapper, "backup")
        ctx.journal.append("hedge", attempt=f"{shard_id}:{att.idx}", provider=mapper, delay_s=round(delay_s, 4), reason=reason)

    for i, shard in enumerate(shards):
        launch(shard["id"], mappers[i % len(mappers)], "primary")

    try:
        while pending and not ctx.cancel.is_set():
            finished, _ = cf.wait(pending, timeout=_POLL_S, return_when=cf.FIRST_COMPLETED)
            by_future = {a.future: a for a in attempts}
            for fut in sorted(finished, key=lambda f: by_future[f].idx):
                pending.discard(fut)
                att = by_future[fut]
                try:
                    att.result = fut.result()
                except Exception as exc:  # noqa: BLE001
                    att.result = CallResult(f"map:{att.shard_id}", att.mapper, "mapper", False,
                                            f"{type(exc).__name__}: {exc}", "error", 0.0)
                process(att)
                if att.shard_id not in done and not running(att.shard_id):
                    maybe_backup(att.shard_id, "attempt_failed", 0.0)      # no live attempt left: use a backup
            if len(durations) >= min_done and backups_used < cap:
                threshold = backup_after * statistics.median(durations)
                now = time.monotonic()
                stragglers = sorted((a for a in attempts if a.start is not None and not a.processed and not a.cancelled
                                     and a.shard_id not in done and a.future is not None and not a.future.done()),
                                    key=lambda a: a.start)
                for a in stragglers:
                    age = now - (a.start or now)
                    if age > threshold:
                        maybe_backup(a.shard_id, "straggler", age)
    finally:
        for a in attempts:      # results that landed but were not consumed yet (cancel/stop raced the loop): keep them
            if not a.processed and not a.cancelled and a.future is not None and a.future.done() and not a.future.cancelled():
                try:
                    a.result = a.future.result()
                except Exception:  # noqa: BLE001
                    a.result = None
                process(a)
        for a in attempts:
            if not a.processed and not a.cancelled and a.future is not None and not a.future.done():
                a.cancelled = True
                a.token.cancel("stopped")
        live = [a.future for a in attempts if a.future is not None and not a.future.done()]
        if live:
            cf.wait(live, timeout=2.0)
        primary_pool.shutdown(wait=False, cancel_futures=True)
        backup_pool.shutdown(wait=False, cancel_futures=True)

    stop = _stop_for(ctx, budget_hit)
    missing = [i for i in ids if i not in done]
    ctx.journal.append("round", n=1, summary={"phase": "map", "done": [i for i in ids if i in done], "missing": missing,
                                              "backups": backups_used})
    box["round"] = 1
    _checkpoint(ctx, round_no=1, best=None, shards_done=[i for i in ids if i in done], missing=missing, backups=backups_used)
    shard_records = [done[i] for i in ids if i in done]
    base_extra = {"missing": missing, "backups": backups_used, "backup_cap": cap,
                  "shard_results": {i: results[i] for i in ids if i in results}}

    if stop or not done:
        status, reason = stop if stop else ("failed", "no_shard_results")
        ctx.journal.append("stop", reason=reason)
        _checkpoint(ctx, round_no=1, best=None, stop_reason=reason, shards_done=[i for i in ids if i in done], missing=missing)
        return ctx.finish(status, winner=None, candidates=shard_records, rounds=1, stop_reason=reason, **base_extra)

    # Reduce (one retry when the reply is not the required JSON shape).
    parsed_reduce = None
    reduce_res = None
    ordered = [results[i] for i in ids if i in results]
    for attempt_no in (0, 1):
        reduce_res = ctx.call(CallSpec(key=f"reduce:{attempt_no}", provider=reducer, prompt=_reduce_prompt(task, ordered, attempt_no > 0),
                                       role="reducer", timeout=timeout))
        if reduce_res.status == "budget":
            budget_hit = True
        parsed_reduce = _parse_reduce(reduce_res)
        if parsed_reduce is not None or ctx.cancel.is_set() or budget_hit:
            break
    stop = _stop_for(ctx, budget_hit)
    box["round"] = 2
    if parsed_reduce is None:
        status, reason = stop if stop else ("failed", "reduce_failed")
        ctx.journal.append("round", n=2, summary={"phase": "reduce", "ok": False})
        ctx.journal.append("stop", reason=reason)
        return ctx.finish(status, winner=None, candidates=shard_records, rounds=2, stop_reason=reason, **base_extra)

    cited = set(parsed_reduce["cited"])
    uncited = [i for i in ids if i in done and i not in cited]
    unknown = sorted(c for c in cited if c not in by_id)
    merged_text = parsed_reduce["merged"] if isinstance(parsed_reduce["merged"], str) else json.dumps(
        parsed_reduce["merged"], ensure_ascii=False, default=str)
    winner = {"id": "reduce", "provider": reducer, "output": merged_text, "output_ref": reduce_res.output_ref, "round": 2,
              "merged": parsed_reduce["merged"], "conflicts": parsed_reduce["conflicts"], "cited": sorted(cited & set(ids)),
              "verified": False, "verdict": None}
    ctx.journal.append("candidate", id="reduce", provider=reducer, output_ref=reduce_res.output_ref, round=2)
    verdict = None
    if ctx.verifier is not None and not ctx.cancel.is_set():
        verdict = ctx.verify({"id": "reduce", "provider": reducer, "output": merged_text})
        winner["verdict"] = _verdict_view(verdict)
        winner["verified"] = bool(verdict.kind == "exec" and verdict.passed)
    ctx.journal.append("round", n=2, summary={"phase": "reduce", "cited": sorted(cited), "uncited": uncited,
                                              "conflicts": len(parsed_reduce["conflicts"])})
    complete = not missing and not uncited and (verdict is None or verdict.passed)
    if stop:
        status, reason = stop
    elif complete:
        status, reason = "ok", "all_shards_reduced"
    elif missing:
        status, reason = "partial", "coverage_missing"
    elif uncited:
        status, reason = "partial", "reducer_did_not_cite_all_shards"
    else:
        status, reason = "partial", "verifier_failed"
    ctx.journal.append("stop", reason=reason)
    _checkpoint(ctx, round_no=2, best={"id": "reduce", "provider": reducer, "output_ref": reduce_res.output_ref, "round": 2,
                                       "output": merged_text, "verdict": winner["verdict"]},
                stop_reason=reason, shards_done=[i for i in ids if i in done], missing=missing)
    return ctx.finish(status, winner=winner, candidates=shard_records + [winner], rounds=2, stop_reason=reason,
                      conflicts=parsed_reduce["conflicts"], uncited=uncited, unknown_cited=unknown, **base_extra)
