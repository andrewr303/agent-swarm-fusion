"""Capacity and tail-latency helpers for swarm forms (spec 8.5; notes/x5-throughput.md section 2 and
notes/g1-captain-claim-bottleneck.md).

Little's law: concurrent workers = arrival_rate * service_time. A captain model that spawns one worker per sampling turn
(median 4.24 s between ``spawn_agent`` calls) can keep at most ``service_s / 4.24`` workers busy, so wide swarms need
``ceil(width * 4.24 / service_s)`` captains.
"""
from __future__ import annotations

import math
import statistics
import threading
import time
from collections import deque
from typing import Callable, Sequence

SPAWN_GAP_S = 4.24  # measured captain spawn_agent cadence (g1)


def _ceil(x: float) -> int:
    """ceil() tolerant of binary float noise (0.5 * 104 -> 52, not 53)."""
    return int(math.ceil(round(x, 9)))


def quantile(samples: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile (type 7). ``samples`` must be non-empty."""
    if not samples:
        raise ValueError("quantile of empty sample")
    data = sorted(float(s) for s in samples)
    q = min(1.0, max(0.0, float(q)))
    pos = (len(data) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return data[lo]
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def workers_needed(arrival_rate_per_s: float, service_time_s: float) -> int:
    """Little's law: ceil(lambda * S), at least 1."""
    if arrival_rate_per_s < 0 or service_time_s < 0:
        raise ValueError("rate and service time must be >= 0")
    return max(1, _ceil(arrival_rate_per_s * service_time_s))


def captains_needed(width: int, service_s: float, *, spawn_gap_s: float = SPAWN_GAP_S, cap: int = 8) -> int:
    """min(cap, max(1, ceil(width * spawn_gap_s / service_s)))."""
    if service_s <= 0:
        raise ValueError("service_s must be > 0")
    return min(max(1, int(cap)), max(1, _ceil(max(0, width) * spawn_gap_s / service_s)))


def slots_per_captain(service_s: float, *, spawn_gap_s: float = SPAWN_GAP_S) -> int:
    """max(1, ceil(service_s / spawn_gap_s)): workers one captain can keep running."""
    if spawn_gap_s <= 0:
        raise ValueError("spawn_gap_s must be > 0")
    return max(1, _ceil(max(0.0, service_s) / spawn_gap_s))


def size_width(n_tasks: int, service_s: float, *, deadline_s: float | None = None, max_width: int = 64,
               spawn_gap_s: float = SPAWN_GAP_S) -> dict:
    """Pick a swarm width and captain count.

    width = min(max_width, n_tasks, ceil(n_tasks * service_s / deadline_s) if deadline else n_tasks)  (>= 1 when n_tasks >= 1).
    expected_makespan_s = ramp (width spawns at ``captains / spawn_gap_s`` per second) + waves * service_s.
    """
    if service_s <= 0:
        raise ValueError("service_s must be > 0")
    n = max(0, int(n_tasks))
    if n == 0:
        return {"width": 0, "captains": 0, "slots_per_captain": slots_per_captain(service_s, spawn_gap_s=spawn_gap_s),
                "expected_makespan_s": 0.0, "dispatch_ceiling_per_min": 0.0}
    wanted = n if not deadline_s else _ceil(n * service_s / deadline_s)
    width = max(1, min(int(max_width), n, wanted))
    captains = captains_needed(width, service_s, spawn_gap_s=spawn_gap_s)
    waves = _ceil(n / width)
    ramp = width * spawn_gap_s / captains
    return {
        "width": width,
        "captains": captains,
        "slots_per_captain": slots_per_captain(service_s, spawn_gap_s=spawn_gap_s),
        "expected_makespan_s": round(ramp + waves * service_s, 3),
        "dispatch_ceiling_per_min": round(captains * 60.0 / spawn_gap_s, 3),
    }


def hedge_delay_s(samples: Sequence[float], *, q: float = 0.9, floor_s: float = 30, ceil_s: float = 600,
                  min_samples: int = 20) -> float | None:
    """Hedge after the q-quantile of recent completions, clamped; None until ``min_samples`` exist."""
    if len(samples) < min_samples:
        return None
    return float(min(ceil_s, max(floor_s, quantile(samples, q))))


class HedgeBudget:
    """gRPC retryThrottling-style token bucket (x5 rank 1). Starts full; a hedge costs 1; each completed primary refunds
    ``per_primary``; hedging is refused while tokens <= max/2 (so extra load stays around ``per_primary`` of primaries)."""

    def __init__(self, max_tokens: float = 10, per_primary: float = 0.05):
        self.max_tokens = float(max_tokens)
        self.per_primary = float(per_primary)
        self.tokens = float(max_tokens)
        self._lock = threading.Lock()

    def on_primary_done(self) -> None:
        with self._lock:
            self.tokens = min(self.max_tokens, self.tokens + self.per_primary)

    def try_hedge(self) -> bool:
        with self._lock:
            if self.tokens <= self.max_tokens / 2:
                return False
            self.tokens -= 1.0
            return True


class AIMDLimiter:
    """Additive-increase / multiplicative-decrease concurrency limiter (x5 rank 3, Netflix AIMDLimit).

    +1 per ``limit`` successes (about one per RTT window), counted only when not app-limited
    (``inflight_at_send * 2 >= limit``). Overload (429/usage/timeouts) multiplies by ``backoff`` at most once per
    ``backoff_window_s`` so a burst of 429s counts once. ``on_usage_exhausted`` drops to ``min_limit`` and pauses
    admission until ``reset_at`` (epoch seconds on ``clock``).
    """

    def __init__(self, initial: int = 8, *, min_limit: int = 2, max_limit: int = 64, backoff: float = 0.9,
                 backoff_window_s: float = 5.0, monotonic: Callable[[], float] = time.monotonic,
                 clock: Callable[[], float] = time.time):
        self.min_limit = max(1, int(min_limit))
        self.max_limit = max(self.min_limit, int(max_limit))
        self.backoff = float(backoff)
        self.backoff_window_s = float(backoff_window_s)
        self._monotonic = monotonic
        self._clock = clock
        self._limit = min(self.max_limit, max(self.min_limit, int(initial)))
        self._cond = threading.Condition()
        self._inflight = 0
        self._successes = 0
        self._last_backoff: float | None = None
        self._paused_until: float | None = None
        self._cap_fn: Callable[[], int] | None = None  # CompositeLimiter hook: external ceiling

    # -- state ----------------------------------------------------------------------------------------------
    @property
    def limit(self) -> int:
        """The AIMD limit (before any external cap)."""
        with self._cond:
            return self._limit

    @property
    def effective_limit(self) -> int:
        with self._cond:
            return self._effective_locked()

    @property
    def inflight(self) -> int:
        with self._cond:
            return self._inflight

    @property
    def paused_until(self) -> float | None:
        with self._cond:
            return self._paused_until

    def _effective_locked(self) -> int:
        cap = self._cap_fn() if self._cap_fn is not None else self._limit
        return max(1, min(self._limit, cap))

    def _pause_remaining_locked(self) -> float:
        if self._paused_until is None:
            return 0.0
        remaining = self._paused_until - self._clock()
        if remaining <= 0:
            self._paused_until = None
            return 0.0
        return remaining

    def notify(self) -> None:
        with self._cond:
            self._cond.notify_all()

    # -- admission ------------------------------------------------------------------------------------------
    def acquire(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else self._monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                pause = self._pause_remaining_locked()
                if pause <= 0 and self._inflight < self._effective_locked():
                    self._inflight += 1
                    return True
                wait_s = pause if pause > 0 else None
                if deadline is not None:
                    left = deadline - self._monotonic()
                    if left <= 0:
                        return False
                    wait_s = left if wait_s is None else min(wait_s, left)
                # Poll in small slices so a swapped fake clock or an external cap change cannot strand a waiter.
                self._cond.wait(timeout=min(wait_s, 0.25) if wait_s is not None else 0.25)

    def release(self) -> None:
        with self._cond:
            if self._inflight > 0:
                self._inflight -= 1
            self._cond.notify()

    # -- feedback -------------------------------------------------------------------------------------------
    def on_success(self, inflight_at_send: int) -> None:
        with self._cond:
            if inflight_at_send * 2 >= self._limit:
                self._successes += 1
                if self._successes >= self._limit:
                    self._limit = min(self.max_limit, self._limit + 1)
                    self._successes = 0
                    self._cond.notify_all()

    def on_overload(self) -> None:
        with self._cond:
            now = self._monotonic()
            if self._last_backoff is not None and now - self._last_backoff < self.backoff_window_s:
                return
            self._limit = max(self.min_limit, int(math.floor(self._limit * self.backoff)))
            self._last_backoff = now
            self._successes = 0

    def on_usage_exhausted(self, reset_at: float | None) -> None:
        with self._cond:
            self._limit = self.min_limit
            self._successes = 0
            self._paused_until = reset_at
            self._cond.notify_all()


class Gradient2Limiter:
    """Netflix Gradient2-style limiter on RTT inflation (x5 rank 3 note: median window, tolerance 2.0).

    short RTT = median of the last ``window`` samples; long RTT = EWMA (``long_window`` samples) that drifts down when it is
    more than 2x the short RTT. gradient = clamp(tolerance * long / short, 0.5, 1.0); new = limit*gradient + queue_size,
    smoothed. No growth while app-limited (inflight < limit / 2).
    """

    def __init__(self, initial: int = 8, *, min_limit: int = 2, max_limit: int = 64, tolerance: float = 2.0,
                 smoothing: float = 0.2, queue_size: int = 4, window: int = 20, long_window: int = 600):
        self.min_limit = max(1, int(min_limit))
        self.max_limit = max(self.min_limit, int(max_limit))
        self.tolerance = float(tolerance)
        self.smoothing = float(smoothing)
        self.queue_size = int(queue_size)
        self._window: deque[float] = deque(maxlen=max(1, int(window)))
        self._alpha = 2.0 / (max(1, int(long_window)) + 1.0)
        self._long: float | None = None
        self._estimate = float(min(self.max_limit, max(self.min_limit, int(initial))))
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        with self._lock:
            return int(max(self.min_limit, min(self.max_limit, math.floor(self._estimate))))

    def on_sample(self, rtt_s: float, inflight: int) -> int:
        if rtt_s <= 0:
            return self.limit
        with self._lock:
            self._window.append(float(rtt_s))
            short = statistics.median(self._window)
            if self._long is None:
                self._long = float(rtt_s)
            else:
                self._long += self._alpha * (float(rtt_s) - self._long)
            if self._long / short > 2.0:
                self._long *= 0.95
            if inflight < self._estimate / 2:  # app-limited: do not move the limit
                return int(max(self.min_limit, min(self.max_limit, math.floor(self._estimate))))
            gradient = max(0.5, min(1.0, self.tolerance * self._long / short))
            proposed = self._estimate * gradient + self.queue_size
            proposed = self._estimate * (1.0 - self.smoothing) + proposed * self.smoothing
            self._estimate = max(float(self.min_limit), min(float(self.max_limit), proposed))
            return int(max(self.min_limit, min(self.max_limit, math.floor(self._estimate))))


class CompositeLimiter:
    """Effective limit = min(aimd.limit, gradient.limit); same acquire/release API as AIMDLimiter."""

    def __init__(self, aimd: AIMDLimiter, gradient: Gradient2Limiter | None = None):
        self.aimd = aimd
        self.gradient = gradient
        if gradient is not None:
            aimd._cap_fn = lambda: gradient.limit

    @property
    def limit(self) -> int:
        return self.aimd.effective_limit

    @property
    def inflight(self) -> int:
        return self.aimd.inflight

    def acquire(self, timeout: float | None = None) -> bool:
        return self.aimd.acquire(timeout)

    def release(self) -> None:
        self.aimd.release()

    def on_success(self, inflight_at_send: int) -> None:
        self.aimd.on_success(inflight_at_send)

    def on_overload(self) -> None:
        self.aimd.on_overload()

    def on_usage_exhausted(self, reset_at: float | None) -> None:
        self.aimd.on_usage_exhausted(reset_at)

    def on_sample(self, rtt_s: float, inflight: int) -> int:
        if self.gradient is not None:
            self.gradient.on_sample(rtt_s, inflight)
            self.aimd.notify()
        return self.limit
