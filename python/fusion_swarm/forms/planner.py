"""P11 effort-scaled orchestrator + P12 difficulty-gated escalation ladder (spec 10.1).

``classify`` derives a ``TaskShape`` from the task text with deterministic keyword rules plus explicit hints (no model
call). ``choose`` walks the selection table (first match wins; ``Plan.reasons`` records the row that fired) and sizes the
swarm with ``capacity.size_width``. ``ladder`` is the escalation order for P12: escalate ONLY on a verifier failure or a
disagreement; the ledger may start higher when a task class historically resolves at a later stage
(``ledger.mean(stage, task_class, "stage") < 0.3``).

Guard from notes/x3-forms.md section 0.3 ("never fan out a sequential/stateful write task"): a fan-out row (auction,
mapreduce) is replaced by ``single`` (one writer plus reviewers) when the work writes (``read_only`` False), has more than
one item and no isolation (``isolated`` False). Rows that need isolation (hedged_race) are skipped without it.

``run_single`` is the executable form of the ``single`` plan (ladder stage 0): one dispatch plus the verifier.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from . import capacity
from .runtime import Budget, CallSpec, FormContext, FormError, FormResult, FormSpec

KINDS = ("implement", "review", "discovery", "estimate", "decomposable", "allocation", "compare", "open")
DIFFICULTIES = ("easy", "medium", "hard")
STAKES = ("low", "high")
FULL_LADDER = ["single", "hedged_race", "tournament", "refute_panel"]
ESTIMATE_LADDER = ["single", "delphi"]
LADDER_START_THRESHOLD = 0.3

SINGLE_SPEC = FormSpec(
    name="single",
    summary="Ladder stage 0: one dispatch plus the verifier (the cheapest plan; escalate only on verifier failure).",
    inputs={"task": "prompt", "provider": "the writer", "role": "ledger role label (default writer)"},
    stop_rule="one call", aggregation="the single candidate, verified when a verifier exists",
    default_budget={"max_calls": 1},
)


@dataclass
class TaskShape:
    kind: str                      # implement|review|discovery|estimate|decomposable|allocation|compare|open
    read_only: bool = False
    has_verifier: bool = False
    n_items: int = 1
    difficulty: str = "medium"     # easy|medium|hard
    stakes: str = "low"            # low|high (security, data loss, money, auth)
    latency_sensitive: bool = False
    heterogeneous: bool = False    # items have different task classes (auction row)
    isolated: bool = False         # each parallel writer gets its own workspace


@dataclass
class Plan:
    form: str
    params: dict
    width: int
    budget: dict
    ladder: list[str]
    reasons: list[str]
    capacity: dict = field(default_factory=dict)
    fallbacks: list[dict] = field(default_factory=list)


# -- classify ---------------------------------------------------------------------------------------------------------
def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


_KIND_RULES: list[tuple[str, re.Pattern]] = [
    ("allocation", _rx(r"\b(allocate|assign (?:these|the|each|them)|distribute (?:these|the|tasks|work)|auction|"
                       r"route (?:these|the) tasks|who should (?:do|handle|take))\b")),
    ("decomposable", _rx(r"\b(each of|for each|for every|per[- ](?:file|module|shard|item|repo|service)|shards?|sharded|"
                         r"split (?:it |this )?(?:into|across)|map[- ]?reduce|fan[- ]?out|"
                         r"across (?:all|every|the) (?:files|modules|repos|items|services))\b")),
    ("review", _rx(r"\b(review|critique|code review|inspect the (?:diff|pr|patch)|audit the (?:diff|pr|patch))\b")),
    ("discovery", _rx(r"\b(find (?:all|any|every)|audit|discover|enumerate|hunt for|look for (?:bugs|issues|vulnerabilit\w*)|"
                      r"list (?:all|every)|scan for|sweep|exhaustive(?:ly)?)\b")),
    ("estimate", _rx(r"\b(estimate|forecast|predict|how (?:long|many|much)|probability|likelihood|ballpark)\b")),
    ("compare", _rx(r"\b(compare|versus|vs\.?|which (?:is|one|of)|choose between|pick between|rank|"
                    r"trade-?offs? between)\b")),
    ("implement", _rx(r"\b(implement|fix|add|build|create|write|refactor|rename|migrate|port|patch|update|bug|"
                      r"make (?:the )?tests? pass)\b")),
]
_READ_ONLY = _rx(r"\b(read[- ]only|do not (?:modify|change|edit)|don't (?:modify|change|edit)|without "
                 r"(?:modifying|changing|editing)|no (?:code )?changes)\b")
_VERIFIER = _rx(r"\b(tests? (?:pass|must pass)|make the tests? pass|run (?:the )?tests?|pytest|unittest|npm test|cargo test|"
                r"go test|jest|vitest|typecheck|tsc|lint|ci (?:passes|green)|acceptance (?:command|test)|verifier)\b")
_HARD = _rx(r"\b(hard|difficult|complex|tricky|subtle|race conditions?|deadlock|concurren\w*|distributed|intermittent|"
            r"flaky|heisenbug|memory leak|cryptograph\w*|consensus)\b")
_EASY = _rx(r"\b(typo|trivial|simple|one[- ]liner|one[- ]line|rename|small|minor|straightforward|easy)\b")
_HIGH_STAKES = _rx(r"\b(security|vulnerab\w*|auth(?:entication|orization)?|passwords?|secrets?|credentials?|payments?|"
                   r"billing|money|financial|data[- ]loss|drop table|migration|production|pii|gdpr|encryption)\b")
_LATENCY = _rx(r"\b(urgent|asap|hotfix|immediately|right now|time[- ]sensitive|quickly)\b")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S", re.M)
_COUNTED = _rx(r"\b(\d{1,4})\s+(?:files|modules|items|tasks|repos|repositories|services|shards|packages|endpoints)\b")

_ALIASES = {"verifier": "has_verifier", "items": "n_items", "latency": "latency_sensitive"}


def _bool_hint(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "y"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("0", "false", "no", "n"):
        return False
    raise FormError("invalid_input", f"hint {name} must be a boolean")


def classify(task: str, *, hints: dict | None = None) -> TaskShape:
    """Deterministic ``TaskShape`` from the task text; explicit ``hints`` always win. No model call."""
    text = str(task or "")
    hints = {_ALIASES.get(k, k): v for k, v in (hints or {}).items() if v is not None}
    kind = hints.get("kind")
    if kind is None:
        kind = "open"
        for name, rx in _KIND_RULES:
            if rx.search(text):
                kind = name
                break
    kind = str(kind).strip().lower()
    if kind not in KINDS:
        raise FormError("invalid_input", f"kind must be one of {', '.join(KINDS)}")
    if "n_items" in hints:
        try:
            n_items = int(hints["n_items"])
        except (TypeError, ValueError):
            raise FormError("invalid_input", "hint n_items must be an integer") from None
    else:
        bullets = len(_BULLET.findall(text))
        counted = _COUNTED.search(text)
        n_items = bullets if bullets >= 2 else (int(counted.group(1)) if counted and int(counted.group(1)) >= 2 else 1)
    classes = hints.get("task_classes")
    heterogeneous = False
    if isinstance(classes, (list, tuple)) and classes:
        heterogeneous = len({str(c) for c in classes}) > 1
        if "n_items" not in hints:
            n_items = max(n_items, len(classes))
    if n_items < 1:
        raise FormError("invalid_input", "hint n_items must be >= 1")
    read_only = _bool_hint("read_only", hints["read_only"]) if "read_only" in hints else (
        kind in ("review", "discovery", "estimate", "compare") or bool(_READ_ONLY.search(text)))
    has_verifier = _bool_hint("has_verifier", hints["has_verifier"]) if "has_verifier" in hints else bool(
        _VERIFIER.search(text))
    difficulty = str(hints.get("difficulty") or ("hard" if _HARD.search(text) else "easy" if _EASY.search(text)
                                                 else "medium")).lower()
    if difficulty not in DIFFICULTIES:
        raise FormError("invalid_input", f"difficulty must be one of {', '.join(DIFFICULTIES)}")
    stakes = str(hints.get("stakes") or ("high" if _HIGH_STAKES.search(text) else "low")).lower()
    if stakes not in STAKES:
        raise FormError("invalid_input", f"stakes must be one of {', '.join(STAKES)}")
    latency = _bool_hint("latency_sensitive", hints["latency_sensitive"]) if "latency_sensitive" in hints else bool(
        _LATENCY.search(text))
    if "heterogeneous" in hints:
        heterogeneous = _bool_hint("heterogeneous", hints["heterogeneous"])
    isolated = _bool_hint("isolated", hints["isolated"]) if "isolated" in hints else False
    return TaskShape(kind=kind, read_only=read_only, has_verifier=has_verifier, n_items=n_items, difficulty=difficulty,
                     stakes=stakes, latency_sensitive=latency, heterogeneous=heterogeneous, isolated=isolated)


# -- ladder -----------------------------------------------------------------------------------------------------------
def ladder(shape: TaskShape, *, ledger: Any = None) -> list[str]:
    """Escalation stages for ``shape`` (P12). With a ledger, leading stages whose historical resolve rate for this task
    class is below 0.3 are skipped (start higher); the last stage is always kept."""
    if shape.kind == "estimate":
        stages = list(ESTIMATE_LADDER)
    elif shape.kind in ("implement", "review"):
        stages = list(FULL_LADDER)
    else:
        stages = ["single"]
    if ledger is not None and len(stages) > 1:
        while len(stages) > 1 and ledger.mean(stages[0], shape.kind, "stage") < LADDER_START_THRESHOLD:
            stages.pop(0)
    return stages


def next_stage(stages: Sequence[str], current: str, *, verifier_failed: bool = False, disagreement: bool = False) -> str | None:
    """The next ladder stage, only on a verifier failure or a disagreement; None at the top or when neither holds."""
    if not (verifier_failed or disagreement):
        return None
    try:
        i = list(stages).index(current)
    except ValueError:
        return None
    return stages[i + 1] if i + 1 < len(stages) else None


# -- choose -----------------------------------------------------------------------------------------------------------
def _family(provider: str) -> str:
    try:
        from .. import adapter
        return str(adapter.model_family(provider))
    except Exception:  # noqa: BLE001 - family is a soft preference
        return provider


def _diverse(providers: Sequence[str]) -> list[str]:
    """Round-robin over model families so adjacent entries come from different families."""
    buckets: dict[str, list[str]] = {}
    for p in providers:
        buckets.setdefault(_family(p), []).append(p)
    out: list[str] = []
    while any(buckets.values()):
        for fam in list(buckets):
            if buckets[fam]:
                out.append(buckets[fam].pop(0))
    return out


def _ranked(providers: list[str], ledger: Any, task_class: str, role: str) -> list[str]:
    if ledger is None:
        return list(providers)
    return [w for w, _ in ledger.rank(providers, task_class, role, method="mean")]


def _cycle(providers: Sequence[str], n: int) -> list[str]:
    return [providers[i % len(providers)] for i in range(n)]


def choose(shape: TaskShape, *, providers: list[str], ledger: Any = None, service_s: float = 120.0,
           deadline_s: float | None = None) -> Plan:
    """Walk the selection table (first match wins). ``Plan.reasons`` names the row that fired."""
    if not isinstance(providers, (list, tuple)) or not [p for p in providers if str(p).strip()]:
        raise FormError("no_providers", "providers must be a non-empty list")
    provs = list(dict.fromkeys(str(p).strip() for p in providers if str(p).strip()))
    if service_s <= 0:
        raise FormError("invalid_input", "service_s must be > 0")
    stages = ladder(shape, ledger=ledger)
    ranked = _ranked(provs, ledger, shape.kind, "worker")
    best = ranked[0]
    others = [p for p in ranked if p != best] or [best]
    n = max(1, shape.n_items)
    reasons: list[str] = []
    sizing: dict = {}
    fallbacks: list[dict] = []

    def plan(form: str, params: dict, width: int, budget: dict) -> Plan:
        return Plan(form=form, params=params, width=max(1, int(width)), budget=budget, ladder=stages,
                    reasons=reasons, capacity=sizing, fallbacks=fallbacks)

    def single(why: str) -> Plan:
        reasons.append(why)
        writer = best
        reviewers = others[:2] if others != [writer] else []
        return plan("single", {"provider": writer, "reviewers": reviewers, "verify": shape.has_verifier},
                    1, {"max_calls": 1 + len(reviewers)})

    fan_out_unsafe = (not shape.read_only) and n > 1 and not shape.isolated

    # row 1: allocation / heterogeneous items
    if shape.kind == "allocation" or (n > 1 and shape.heterogeneous):
        if fan_out_unsafe:
            return single("row 1 (auction) skipped: write tasks with n_items>1 and no isolation -> single writer + reviewers")
        sizing.update(capacity.size_width(n, service_s, deadline_s=deadline_s))
        reasons.append("row 1: allocation or heterogeneous items -> auction")
        return plan("auction", {"bidders": ranked, "method": "thompson", "explore": 0.10, "execute": True,
                                "role": "worker"}, sizing["width"], {"max_calls": n})
    # row 2: decomposable / read-only multi-item
    if shape.kind == "decomposable" or (shape.read_only and n > 1):
        if fan_out_unsafe:
            return single("row 2 (mapreduce) skipped: write tasks with n_items>1 and no isolation -> single writer + reviewers")
        sizing.update(capacity.size_width(n, service_s, deadline_s=deadline_s))
        reasons.append("row 2: decomposable or read-only multi-item -> mapreduce")
        return plan("mapreduce", {"mappers": ranked, "reducer": best, "backup_after": 2.0}, sizing["width"],
                    {"max_calls": math.ceil(n * 1.1 + 2)})
    # row 3: discovery
    if shape.kind == "discovery":
        finders = _diverse(provs)
        per_round = min(len(finders), 4)
        reasons.append("row 3: discovery -> loop_until_dry (finders from diverse families)")
        return plan("loop_until_dry", {"finders": finders, "per_round": per_round, "dry_rounds": 2, "max_rounds": 6},
                    per_round, {"max_calls": 6 * per_round})
    # row 4: estimate
    if shape.kind == "estimate":
        experts = _cycle(_diverse(provs), max(3, len(provs)))
        reasons.append("row 4: estimate -> delphi (>= 3 experts" + (
            "; providers repeated as independent samples" if len(provs) < 3 else "") + ")")
        return plan("delphi", {"experts": experts, "rounds": 3, "numeric": True}, len(experts),
                    {"max_calls": len(experts) * 3})
    # row 5: high-stakes review
    if shape.kind == "review" and shape.stakes == "high":
        authors = _diverse(provs)[: max(1, math.ceil(len(provs) / 2))]
        refuters = [p for p in provs if p not in authors] or provs
        k = 3 if shape.difficulty == "hard" else 2
        reasons.append(f"row 5: high-stakes review -> refute_panel (k={k})")
        return plan("refute_panel", {"authors": authors, "refuters": refuters, "k": k, "max_k": 3},
                    len(authors) + k, {"max_calls": 2 + 3 * 3 * len(authors)})
    # row 6: hard implement with a verifier
    if shape.kind == "implement" and shape.has_verifier and shape.difficulty == "hard":
        critics = others if others != [best] else [best]
        reasons.append("row 6: hard implement with verifier -> critic_repair (writer = ledger best), pair_swap on failure")
        fallbacks.append({"form": "pair_swap", "when": "critic_repair verifier failure",
                          "params": {"a": best, "b": (others[0] if others else best)}})
        return plan("critic_repair", {"writer": best, "critics": critics, "max_rounds": 3, "patience": 2}, 1 + len(critics),
                    {"max_calls": 1 + 3 * 3})
    # row 7: latency-sensitive implement with a verifier (needs isolation)
    if shape.kind == "implement" and shape.has_verifier and shape.latency_sensitive:
        if shape.isolated:
            backups = others[:2] if others != [best] else []
            reasons.append("row 7: latency-sensitive implement with verifier -> hedged_race (isolated)")
            return plan("hedged_race", {"primary": best, "backups": backups, "max_hedges": len(backups) or 1},
                        1 + len(backups), {"max_calls": 1 + max(1, len(backups))})
        reasons.append("row 7 (hedged_race) skipped: hedged races need per-attempt isolation")
    # row 8: free-form comparison
    if shape.kind == "compare" and not shape.has_verifier:
        reasons.append("row 8: free-form compare without a scalar verifier -> tournament")
        judges = others if others != [best] else provs
        calls = len(provs) + 3 * max(0, len(provs) - 1)
        return plan("tournament", {"providers": provs, "judges": judges}, len(provs), {"max_calls": max(1, calls)})
    # row 9: open task with several roles
    if shape.kind == "open" and len(provs) > 1:
        roles = {r: provs[i % len(provs)] for i, r in enumerate(("resolver", "researcher", "decider", "judge"))}
        reasons.append("row 9: open task with multiple roles -> blackboard")
        return plan("blackboard", {"roles": roles, "max_cycles": 12}, 1, {"max_calls": 12})
    return single("row 10: otherwise -> single dispatch + verifier (ladder stage 0)")


# -- the executable 'single' plan -------------------------------------------------------------------------------------
def run_single(task: str, *, ctx: FormContext | None = None, provider: str, role: str = "writer", cwd: str | None = None,
               timeout: float = 300, task_class: str = "general", dispatch_fn: Callable | None = None,
               verifier: Callable | None = None, ledger: Any = None, budget: Budget | None = None, cancel: Any = None,
               run_id: str | None = None, run_dir: str | None = None) -> FormResult:
    """Ladder stage 0: one dispatch, verified when a verifier exists. Ledger updates only from an exec verdict."""
    if not isinstance(task, str) or not task.strip():
        raise FormError("invalid_input", "task is required")
    if not isinstance(provider, str) or not provider.strip():
        raise FormError("no_providers", "provider is required")
    if ctx is None:
        ctx = FormContext("single", run_id=run_id, run_dir=run_dir, budget=budget or Budget(max_calls=1), cancel=cancel,
                          dispatch_fn=dispatch_fn, ledger=ledger, verifier=verifier,
                          params={"provider": provider, "role": role})
    res = ctx.call(CallSpec("single-0", provider, task, role=role, cwd=cwd, timeout=timeout))
    if res.status in ("budget", "cancelled"):
        status = "budget_exhausted" if res.status == "budget" else "cancelled"
        ctx.journal.append("stop", reason=res.status)
        return ctx.finish(status, winner=None, candidates=[], rounds=0, stop_reason=res.status)
    if not res.ok:
        ctx.journal.append("stop", reason="call_failed")
        return ctx.finish("failed", winner=None, candidates=[], rounds=1, stop_reason="call_failed",
                          call_status=res.status)
    candidate = {"id": "single-0", "provider": provider, "output": res.output, "round": 0, "output_ref": res.output_ref}
    if cwd:
        candidate["cwd"] = cwd
    ctx.journal.append("candidate", id="single-0", provider=provider, output_ref=res.output_ref, round=0)
    verdict = None
    if ctx.verifier is not None:
        verdict = ctx.verify(candidate)
        if verdict.kind == "exec":
            ctx.ledger.record(provider, task_class, role, bool(verdict.passed), verified=True, latency_s=res.elapsed_s)
            ctx.journal.append("ledger", worker=provider, task_class=task_class, role=role, success=bool(verdict.passed))
    ctx.journal.append("round", n=0, summary={"provider": provider, "verified": verdict is not None,
                                              "passed": None if verdict is None else bool(verdict.passed)})
    ctx.journal.append("stop", reason="single")
    status = "ok" if (verdict is not None and verdict.passed) else "partial"
    return ctx.finish(status, winner=candidate, candidates=[candidate], rounds=1, stop_reason="single",
                      verified=verdict is not None and bool(verdict.passed))
