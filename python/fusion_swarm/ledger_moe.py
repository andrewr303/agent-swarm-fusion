"""Pillar 2F: Empirical Ledger Router (`ledger-moe`) with Wilson-Score Lower Bound.

Computes advisory routing hints from `memory/runs.jsonl` using the Wilson score
95% lower confidence bound:

    W_-(w, n, z) = (p + z^2/(2n) - z * sqrt((p(1-p) + z^2/(4n)) / n)) / (1 + z^2/n)

Guardrails:
  1. Never emits confident rankings until a `(provider, task_type)` cell reaches
     `min_samples` (default 30). Cells with `n < 30` are marked
     `insufficient_data=True`.
  2. Advisory only: preserves model-family diversity and never overrides user
     family diversity rules.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from . import modes_catalog

DEFAULT_MIN_SAMPLES = 30
DEFAULT_Z = 1.96

PROVIDER_FAMILY = {
    "claude": "anthropic",
    "codex": "openai",
    "gemini": "google",
    "grok": "xai",
    "kimi": "moonshot",
    "glm": "zhipu",
    "copilot": "github",
    "opencode": "opencode",
    "fable": "fable",
    "agy": "google-agy",
}


def wilson_lower_bound(wins: int, total: int, z: float = DEFAULT_Z) -> float:
    """Return the Wilson score lower bound W_-(wins, total, z) in [0.0, 1.0]."""
    n = int(total)
    w = max(0, min(int(wins), n))
    if n <= 0:
        return 0.0
    p_hat = w / n
    z2 = z * z
    denom = 1.0 + (z2 / n)
    center = p_hat + (z2 / (2.0 * n))
    radicand = ((p_hat * (1.0 - p_hat)) + (z2 / (4.0 * n))) / n
    margin = z * math.sqrt(max(0.0, radicand))
    return max(0.0, min(1.0, (center - margin) / denom))


def _load_runs(runs_path: Path | None) -> list[dict]:
    if runs_path is None or not runs_path.exists():
        return []
    records: list[dict] = []
    for raw in runs_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
        except json.JSONDecodeError:
            continue
    return records


def rank_providers(
    runs_path: str | Path | None = None,
    *,
    task_type: str = "",
    min_samples: int = DEFAULT_MIN_SAMPLES,
    providers: list[str] | tuple[str, ...] | None = None,
    z: float = DEFAULT_Z,
) -> dict:
    """Compute Wilson-score lower bound routing hints from `memory/runs.jsonl`."""
    path = Path(runs_path) if runs_path else None
    records = _load_runs(path)
    roster = list(providers or modes_catalog.PROVIDER_NAMES)

    stats: dict[str, dict[str, int]] = {
        prov: {"wins": 0, "total": 0} for prov in roster
    }
    task_filter = (task_type or "").strip().lower()

    for rec in records:
        if task_filter:
            rec_type = str(rec.get("task_type") or rec.get("mode") or rec.get("form") or "").lower()
            if task_filter not in rec_type:
                continue

        prov = rec.get("provider") or rec.get("winner")
        providers_in_run = rec.get("providers")
        status = str(rec.get("status") or "").lower()
        ok = bool(rec.get("ok", status in ("ok", "pass", "completed", "success", "converged")))

        if isinstance(prov, str) and prov in stats:
            stats[prov]["total"] += 1
            if ok:
                stats[prov]["wins"] += 1
        elif isinstance(providers_in_run, list):
            for p in providers_in_run:
                if isinstance(p, str) and p in stats:
                    stats[p]["total"] += 1
                    if ok:
                        stats[p]["wins"] += 1

    rankings: list[dict] = []
    for prov in roster:
        wins = stats[prov]["wins"]
        total = stats[prov]["total"]
        lb = wilson_lower_bound(wins, total, z=z)
        emp_rate = (wins / total) if total > 0 else 0.0
        sufficient = total >= min_samples
        rankings.append(
            {
                "provider": prov,
                "family": PROVIDER_FAMILY.get(prov, prov),
                "wins": wins,
                "total": total,
                "empirical_rate": round(emp_rate, 4),
                "wilson_lb": round(lb, 4),
                "insufficient_data": not sufficient,
                "confident": sufficient,
            }
        )

    # Sort confident providers by wilson_lb descending first, then insufficient_data providers
    rankings.sort(
        key=lambda r: (
            0 if r["confident"] else 1,
            -r["wilson_lb"],
            -r["total"],
            r["provider"],
        )
    )

    # Select diverse top recommendation (never collapse to a single model family)
    diverse_hint: list[str] = []
    seen_families: set[str] = set()
    for entry in rankings:
        fam = entry["family"]
        if fam not in seen_families:
            seen_families.add(fam)
            diverse_hint.append(entry["provider"])

    confident_cells = sum(1 for r in rankings if r["confident"])
    return {
        "advisory_only": True,
        "task_type": task_type or "all",
        "min_samples": min_samples,
        "confident_cells": confident_cells,
        "cold_start": confident_cells == 0,
        "diverse_roster_hint": diverse_hint[:3],
        "rankings": rankings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fusion ledger-moe")
    parser.add_argument("--runs-file", default="memory/runs.jsonl")
    parser.add_argument("--task-type", default="")
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = rank_providers(
        args.runs_file,
        task_type=args.task_type,
        min_samples=args.min_samples,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    print(
        f"Fusion Ledger-MoE Advisory Hints (task_type={result['task_type']}, "
        f"min_samples={result['min_samples']}, cold_start={result['cold_start']})"
    )
    print(f"Diverse Roster Hint: {', '.join(result['diverse_roster_hint'])}")
    for row in result["rankings"]:
        badge = "CONFIDENT" if row["confident"] else f"WARMUP ({row['total']}/{result['min_samples']})"
        print(
            f"  - {row['provider']:<10} family={row['family']:<11} "
            f"wins={row['wins']}/{row['total']} wilson_lb={row['wilson_lb']:.4f} [{badge}]"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
