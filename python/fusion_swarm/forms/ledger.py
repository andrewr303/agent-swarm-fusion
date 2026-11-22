"""Reliability ledger (spec 8.4, judge J-16): Beta posteriors per (worker, task_class, role).

Rules:
- ``record`` accepts only VERIFIED outcomes (an exec verdict or a confirmed reproduction decided it). Self-reported
  confidence is never an input. Latency needs no verification.
- Decay: before each update ``alpha-1`` and ``beta-1`` are multiplied by ``decay`` (non-stationary providers).
- Selection: Thompson sampling or UCB with an exploration floor, deterministic under a seeded ``random.Random``.
- Persistence: JSON via an atomic write under an inter-process lock with merge-on-save. Each cell stores raw
  success/failure counts; on save the pending local events are replayed on top of the freshly re-read file, so
  alpha/beta are recomputed after the merge and two writers never lose each other's updates.
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .capacity import quantile
from .journal import file_lock, write_atomic

LATENCY_ROLE = "*"        # latency windows live on the (worker, task_class, "*") cell
LATENCY_WINDOW = 200
SCHEMA_VERSION = 1


def default_ledger_path() -> Path:
    env = os.environ.get("FUSION_LEDGER_PATH")
    if env:
        return Path(env).expanduser()
    home = Path(os.environ.get("FUSION_HOME", "~/.fusion")).expanduser()
    return home / "ledger" / "reliability.json"


@dataclass
class Cell:
    alpha: float = 1.0
    beta: float = 1.0
    n: int = 0
    last_ts: float = 0.0
    latencies: list[float] = field(default_factory=list)
    successes: int = 0
    failures: int = 0

    def to_dict(self) -> dict:
        return {"alpha": self.alpha, "beta": self.beta, "n": self.n, "last_ts": self.last_ts,
                "latencies": list(self.latencies), "successes": self.successes, "failures": self.failures}

    @classmethod
    def from_dict(cls, data: dict) -> "Cell":
        def f(key: str, default: float) -> float:
            try:
                return float(data.get(key, default))
            except (TypeError, ValueError):
                return default

        lat = data.get("latencies")
        return cls(alpha=max(1.0, f("alpha", 1.0)), beta=max(1.0, f("beta", 1.0)), n=int(f("n", 0)),
                   last_ts=f("last_ts", 0.0),
                   latencies=[float(x) for x in lat if isinstance(x, (int, float))][-LATENCY_WINDOW:]
                   if isinstance(lat, list) else [],
                   successes=int(f("successes", 0)), failures=int(f("failures", 0)))


class ReliabilityLedger:
    def __init__(self, path: str | Path | None = None, *, decay: float = 0.99, rng: random.Random | None = None,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path).expanduser() if path is not None else default_ledger_path()
        self.decay = float(decay)
        self.rng = rng if rng is not None else random.Random()
        self.clock = clock
        self._lock = threading.RLock()
        self._cells: dict[tuple[str, str, str], Cell] = {}
        self._pairs: dict[tuple[str, str, str], dict] = {}   # {"n": int, "sum_delta": float}
        self._pending: list[tuple] = []
        self._loaded = False

    # -- persistence ----------------------------------------------------------------------------------------
    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    @property
    def dirty(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def _read_disk(self) -> tuple[dict, dict]:
        cells: dict[tuple[str, str, str], Cell] = {}
        pairs: dict[tuple[str, str, str], dict] = {}
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cells, pairs
        if not isinstance(doc, dict):
            return cells, pairs
        for item in doc.get("cells") or []:
            if isinstance(item, dict) and item.get("worker") is not None:
                key = (str(item["worker"]), str(item.get("task_class", "")), str(item.get("role", "")))
                cells[key] = Cell.from_dict(item)
        for item in doc.get("pairs") or []:
            if isinstance(item, dict) and item.get("writer") is not None:
                key = (str(item["writer"]), str(item.get("reviewer", "")), str(item.get("task_class", "")))
                try:
                    pairs[key] = {"n": int(item.get("n", 0)), "sum_delta": float(item.get("sum_delta", 0.0))}
                except (TypeError, ValueError):
                    continue
        return cells, pairs

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._cells, self._pairs = self._read_disk()
            self._loaded = True

    def refresh(self) -> None:
        """Re-read the file (keeps unsaved local events applied on top)."""
        with self._lock:
            cells, pairs = self._read_disk()
            pending = self._pending
            self._cells, self._pairs, self._pending, self._loaded = cells, pairs, [], True
            self._replay(pending, self._cells, self._pairs)
            self._pending = pending

    def save(self) -> None:
        """Merge-on-save: lock, re-read, replay our pending events, write atomically."""
        with self._lock:
            self._ensure_loaded()
            pending = self._pending
            with file_lock(self.lock_path):
                cells, pairs = self._read_disk()
                self._replay(pending, cells, pairs)
                doc = {
                    "version": SCHEMA_VERSION,
                    "cells": [dict(worker=w, task_class=t, role=r, **c.to_dict()) for (w, t, r), c in sorted(cells.items())],
                    "pairs": [dict(writer=w, reviewer=r, task_class=t, **p) for (w, r, t), p in sorted(pairs.items())],
                }
                write_atomic(self.path, json.dumps(doc, ensure_ascii=False, indent=1))
            self._cells, self._pairs, self._pending = cells, pairs, []

    # -- event application (shared by live updates and merge replay) ------------------------------------------
    def _replay(self, events: Sequence[tuple], cells: dict, pairs: dict) -> None:
        for ev in events:
            kind = ev[0]
            if kind == "o":
                _, worker, tc, role, success, ts = ev
                self._apply_outcome(cells.setdefault((worker, tc, role), Cell()), success, ts)
            elif kind == "l":
                _, worker, tc, latency = ev
                self._apply_latency(cells.setdefault((worker, tc, LATENCY_ROLE), Cell()), latency)
            elif kind == "p":
                _, writer, reviewer, tc, delta = ev
                p = pairs.setdefault((writer, reviewer, tc), {"n": 0, "sum_delta": 0.0})
                p["n"] += 1
                p["sum_delta"] += delta

    def _apply_outcome(self, cell: Cell, success: bool, ts: float) -> None:
        cell.alpha = 1.0 + (cell.alpha - 1.0) * self.decay
        cell.beta = 1.0 + (cell.beta - 1.0) * self.decay
        if success:
            cell.alpha += 1.0
            cell.successes += 1
        else:
            cell.beta += 1.0
            cell.failures += 1
        cell.n += 1
        cell.last_ts = max(cell.last_ts, ts)

    @staticmethod
    def _apply_latency(cell: Cell, latency: float) -> None:
        cell.latencies.append(latency)
        if len(cell.latencies) > LATENCY_WINDOW:
            del cell.latencies[: len(cell.latencies) - LATENCY_WINDOW]

    # -- recording ------------------------------------------------------------------------------------------
    def record(self, worker: str, task_class: str, role: str, success: bool, *, verified: bool,
               latency_s: float | None = None) -> Cell:
        """Record one VERIFIED outcome. Raises ValueError("unverified outcome") when ``verified`` is False."""
        if not verified:
            raise ValueError("unverified outcome")
        ts = float(self.clock())
        with self._lock:
            self._ensure_loaded()
            events: list[tuple] = [("o", worker, task_class, role, bool(success), ts)]
            if latency_s is not None and latency_s >= 0:
                events.append(("l", worker, task_class, float(latency_s)))
            self._replay(events, self._cells, self._pairs)
            self._pending.extend(events)
            return self.cell(worker, task_class, role)

    def observe_latency(self, worker: str, task_class: str, latency_s: float) -> None:
        if latency_s is None or latency_s < 0:
            return
        with self._lock:
            self._ensure_loaded()
            ev = ("l", worker, task_class, float(latency_s))
            self._replay([ev], self._cells, self._pairs)
            self._pending.append(ev)

    def record_pair(self, writer: str, reviewer: str, task_class: str, delta: float) -> None:
        with self._lock:
            self._ensure_loaded()
            ev = ("p", writer, reviewer, task_class, float(delta))
            self._replay([ev], self._cells, self._pairs)
            self._pending.append(ev)

    # -- reads ----------------------------------------------------------------------------------------------
    def cell(self, worker: str, task_class: str, role: str) -> Cell:
        """A copy of the cell (prior Beta(1,1) when unseen)."""
        with self._lock:
            self._ensure_loaded()
            c = self._cells.get((worker, task_class, role))
            return Cell.from_dict(c.to_dict()) if c else Cell()

    def mean(self, worker: str, task_class: str, role: str) -> float:
        c = self.cell(worker, task_class, role)
        return c.alpha / (c.alpha + c.beta)

    def thompson(self, worker: str, task_class: str, role: str) -> float:
        c = self.cell(worker, task_class, role)
        with self._lock:
            return self.rng.betavariate(c.alpha, c.beta)

    def ucb(self, worker: str, task_class: str, role: str, *, total: int, c: float = 2.0) -> float:
        cell = self.cell(worker, task_class, role)
        mean = cell.alpha / (cell.alpha + cell.beta)
        return mean + math.sqrt(c * math.log(max(0, total) + 1) / (cell.n + 1))

    def _score(self, method: str, worker: str, task_class: str, role: str, total: int) -> float:
        if method == "thompson":
            return self.thompson(worker, task_class, role)
        if method == "ucb":
            return self.ucb(worker, task_class, role, total=total)
        if method == "mean":
            return self.mean(worker, task_class, role)
        raise ValueError(f"unknown method: {method}")

    def choose(self, candidates: Sequence[str], task_class: str, role: str, *, method: str = "thompson",
               explore: float = 0.10, min_n: int = 3, cost: Callable[[str], float] | None = None) -> str:
        """Exploration floor: with probability ``explore`` pick uniformly among under-sampled candidates (n < min_n),
        or among all when none is under-sampled; otherwise argmax(score / cost). Ties keep the earliest candidate."""
        cands = list(candidates)
        if not cands:
            raise ValueError("no candidates")
        if method not in ("thompson", "ucb", "mean"):
            raise ValueError(f"unknown method: {method}")
        with self._lock:
            draw = self.rng.random()
            if draw < explore:
                under = [w for w in cands if self.cell(w, task_class, role).n < min_n]
                return self.rng.choice(under or cands)
            total = sum(self.cell(w, task_class, role).n for w in cands)
            best, best_score = cands[0], -math.inf
            for w in cands:
                score = self._score(method, w, task_class, role, total)
                divisor = max(float(cost(w)) if cost is not None else 1.0, 1e-9)
                score /= divisor
                if score > best_score:
                    best, best_score = w, score
            return best

    def rank(self, candidates: Sequence[str], task_class: str, role: str, *, method: str = "mean") -> list[tuple[str, float]]:
        cands = list(candidates)
        total = sum(self.cell(w, task_class, role).n for w in cands)
        scored = [(w, self._score(method, w, task_class, role, total)) for w in cands]
        return sorted(scored, key=lambda item: -item[1])  # stable: ties keep input order

    def latency_quantile(self, worker: str, task_class: str, q: float = 0.9, *, min_samples: int = 20) -> float | None:
        lat = self.cell(worker, task_class, LATENCY_ROLE).latencies
        if len(lat) < min_samples:
            return None
        return quantile(lat, q)

    def pair_stats(self, writer: str, reviewer: str, task_class: str) -> dict:
        with self._lock:
            self._ensure_loaded()
            p = self._pairs.get((writer, reviewer, task_class)) or {"n": 0, "sum_delta": 0.0}
            n = int(p["n"])
            mean_delta = p["sum_delta"] / n if n else 0.0
            return {"n": n, "mean_delta": mean_delta, "disabled": n >= 3 and mean_delta < 0}
