"""Latency aggregation helpers (avg, p95, p99) for perf benchmark results."""

from __future__ import annotations

import math
from typing import Iterable, Optional


def percentile(values: list[float], pct: float) -> Optional[float]:
    """Linear-interpolation percentile (numpy-compatible)."""
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    s = sorted(values)
    rank = (pct / 100.0) * (len(s) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(s[lo])
    frac = rank - lo
    return float(s[lo] + (s[hi] - s[lo]) * frac)


def summarize_latencies(values: Iterable[float]) -> dict:
    """Return a dict of count/min/avg/max/p50/p90/p95/p99/stdev.

    Filters out ``None`` automatically. Empty input yields zeros / ``None``.
    """
    clean = [v for v in values if v is not None]
    if not clean:
        return {"count": 0, "min": None, "avg": None, "max": None,
                "p50": None, "p90": None, "p95": None, "p99": None,
                "stdev": None}
    n = len(clean)
    avg = sum(clean) / n
    if n > 1:
        var = sum((x - avg) ** 2 for x in clean) / (n - 1)
        stdev = math.sqrt(var)
    else:
        stdev = 0.0
    return {
        "count": n,
        "min": float(min(clean)),
        "avg": float(avg),
        "max": float(max(clean)),
        "p50": percentile(clean, 50),
        "p90": percentile(clean, 90),
        "p95": percentile(clean, 95),
        "p99": percentile(clean, 99),
        "stdev": float(stdev),
    }
