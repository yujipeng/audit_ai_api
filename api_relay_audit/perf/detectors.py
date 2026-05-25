"""Detectors for pseudo-stream and slow-start patterns in perf-bench v2.

Pure functions over runner-produced rows (design §4.3). All thresholds are
frozen at design freeze (§10 Q1, R8) — adjust only by amending the design,
not in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from api_relay_audit.perf.metrics import percentile

# Imported per design §4.3 to keep the link explicit; v2 MVP detector does
# not use bimodality (P1 enhancement only). Lint suppression noqa is fine
# because the import documents the dependency from §8.5.
from api_relay_audit.latency_variance import detect_bimodality  # noqa: F401


# ---- 阈值（design 阶段冻结，dev 不得调整；调整需回炉本 design）----
MIN_CHUNKS_FOR_REAL = 3                # 少于此值且 output_chars >= 50 → pseudo
PSEUDO_LAST_BURST_SHARE = 0.7          # 最末位 chunk 文本占比 ≥ 70% → 突发模式
PSEUDO_GAP_BEFORE_LAST_RATIO = 5.0     # 最后一个 chunk 间隔 ≥ 5×中位 → 缓冲突发
PSEUDO_MAJORITY_THRESHOLD = 0.5        # 多轮 vote 占比阈值
SLOW_START_WARMUP_DEFAULT = 3          # 默认前 N 轮算 warmup
SLOW_START_RATIO = 1.3                 # warmup avg / steady avg > 1.3 → slow_start
MIN_ROUNDS_FOR_SLOW_START = 6          # 少于此值（warmup * 2）→ inconclusive
MIN_DETECTOR_ROUNDS = 3                # 任何 detector 需要至少 3 个 ok=True 轮次

_LONE_CHUNK_TEXT_FLOOR = 50            # output_chars 阈值用于 rule 2


@dataclass
class PseudoStreamVerdict:
    verdict: str  # "real" | "pseudo" | "inconclusive"
    evidence: dict = field(default_factory=dict)


@dataclass
class SlowStartVerdict:
    verdict: str  # "stable" | "slow_start" | "inconclusive"
    evidence: dict = field(default_factory=dict)


def _classify_round(row: dict) -> tuple[str, Optional[str]]:
    """Classify a single round per design §4.3 single-round rules.

    Returns (verdict, reason) where verdict ∈ {"real", "pseudo", "inconclusive"}.

    Rule 3 (`last_burst_share >= 0.7`) requires per-chunk text and is therefore
    inert in v2 MVP — runner only retains aggregate `output_chars`. v2.1 will
    enable rule 3 once chunk_text capture lands (PRD P2). Rule 4 (last_gap_ratio)
    still distinguishes buffered-burst from uniform streams using only
    `chunk_intervals`, so the missing rule 3 does not weaken pseudo coverage.
    """
    if not row.get("ok"):
        return "inconclusive", "round-not-ok"
    intervals = row.get("chunk_intervals")
    if not intervals:
        return "inconclusive", "no-chunk-intervals"

    chunk_count = row.get("chunk_count") or 0
    output_chars = row.get("output_chars") or 0

    if chunk_count < MIN_CHUNKS_FOR_REAL:
        if output_chars >= _LONE_CHUNK_TEXT_FLOOR:
            return "pseudo", "lone-chunk-with-text"
        return "inconclusive", "below-min-chunks-no-text"

    last_gap_ratio = _last_gap_ratio(intervals)
    if last_gap_ratio is not None and last_gap_ratio >= PSEUDO_GAP_BEFORE_LAST_RATIO:
        return "pseudo", "buffered-burst"

    return "real", "uniform-stream"


def _last_gap_ratio(intervals: list[float]) -> Optional[float]:
    if not intervals:
        return None
    median = percentile(intervals, 50)
    if median is None or median <= 0:
        return None
    return intervals[-1] / median


def pseudo_stream_detect(rows: list[dict]) -> PseudoStreamVerdict:
    """Multi-round vote over single-round verdicts (design §4.3 / §10 Q1).

    `ok_rounds_used` is the count of `ok=True` rows — this is the denominator
    against which pseudo / real majorities are measured (design §4.3:
    "取 ok 轮的单轮 verdict"). Single-round inconclusives (e.g. capture off
    or below-min-chunks-no-text) stay in the count, which is what lets the
    detector return an overall `inconclusive` when no decisive verdict
    achieves majority.
    """
    per_round: list[str] = []
    reasons: list[str] = []
    ok_intervals_p50: list[float] = []
    ok_intervals_p99: list[float] = []
    total_ok = 0

    for row in rows:
        if row.get("ok"):
            total_ok += 1
        verdict, reason = _classify_round(row)
        per_round.append(verdict)
        reasons.append(reason or "")
        if verdict in ("real", "pseudo"):
            intervals = row.get("chunk_intervals") or []
            if intervals:
                p50 = percentile(intervals, 50)
                p99 = percentile(intervals, 99)
                if p50 is not None:
                    ok_intervals_p50.append(p50)
                if p99 is not None:
                    ok_intervals_p99.append(p99)

    pseudo_count = sum(1 for v in per_round if v == "pseudo")
    real_count = sum(1 for v in per_round if v == "real")

    evidence = {
        "per_round_verdicts": per_round,
        "ok_rounds_used": total_ok,
        "chunk_gap_p50": percentile(ok_intervals_p50, 50) if ok_intervals_p50 else None,
        "chunk_gap_p99": percentile(ok_intervals_p99, 99) if ok_intervals_p99 else None,
        "last_burst_share_p50": None,
        "reason": "",
    }

    if total_ok < MIN_DETECTOR_ROUNDS:
        evidence["reason"] = f"only {total_ok} ok rounds (< {MIN_DETECTOR_ROUNDS})"
        return PseudoStreamVerdict(verdict="inconclusive", evidence=evidence)

    if pseudo_count / total_ok >= PSEUDO_MAJORITY_THRESHOLD:
        evidence["reason"] = _summarize_majority_reason(reasons, "pseudo")
        return PseudoStreamVerdict(verdict="pseudo", evidence=evidence)
    if real_count / total_ok >= PSEUDO_MAJORITY_THRESHOLD:
        evidence["reason"] = "majority real (uniform-stream)"
        return PseudoStreamVerdict(verdict="real", evidence=evidence)

    evidence["reason"] = "no majority — split votes"
    return PseudoStreamVerdict(verdict="inconclusive", evidence=evidence)


def _summarize_majority_reason(reasons: list[str], verdict: str) -> str:
    relevant = [r for r in reasons if r and r != "round-not-ok" and r != "no-chunk-intervals"]
    if not relevant:
        return f"majority {verdict}"
    counts: dict[str, int] = {}
    for r in relevant:
        counts[r] = counts.get(r, 0) + 1
    top = max(counts.items(), key=lambda kv: kv[1])[0]
    return f"majority {verdict} ({top})"


def slow_start_detect(rows_or_ttft, *,
                      warmup: int = SLOW_START_WARMUP_DEFAULT
                      ) -> SlowStartVerdict:
    """Compare warmup-window TTFT avg vs steady-state avg (design §4.3).

    Accepts either a list of row dicts (preferred — drops ok=False rounds per
    R3) or a list of pre-filtered TTFT floats.
    """
    ttfts = _extract_ok_ttfts(rows_or_ttft)
    n = len(ttfts)
    evidence: dict = {
        "warmup_rounds": warmup,
        "warmup_avg": None,
        "steady_avg": None,
        "ratio": None,
        "ok_rounds_used": n,
        "reason": "",
    }

    if n < MIN_ROUNDS_FOR_SLOW_START:
        evidence["reason"] = f"only {n} ok rounds (< {MIN_ROUNDS_FOR_SLOW_START})"
        return SlowStartVerdict(verdict="inconclusive", evidence=evidence)

    if n <= warmup:
        evidence["reason"] = f"steady window empty (ok={n}, warmup={warmup})"
        return SlowStartVerdict(verdict="inconclusive", evidence=evidence)

    warmup_slice = ttfts[:warmup]
    steady_slice = ttfts[warmup:]
    warmup_avg = sum(warmup_slice) / len(warmup_slice)
    steady_avg = sum(steady_slice) / len(steady_slice)
    evidence["warmup_avg"] = warmup_avg
    evidence["steady_avg"] = steady_avg

    if steady_avg <= 0:
        evidence["reason"] = "steady_avg non-positive"
        return SlowStartVerdict(verdict="inconclusive", evidence=evidence)

    ratio = warmup_avg / steady_avg
    evidence["ratio"] = ratio
    if ratio > SLOW_START_RATIO:
        evidence["reason"] = f"warmup_avg/steady_avg = {ratio:.2f} > {SLOW_START_RATIO}"
        return SlowStartVerdict(verdict="slow_start", evidence=evidence)
    evidence["reason"] = f"warmup_avg/steady_avg = {ratio:.2f} <= {SLOW_START_RATIO}"
    return SlowStartVerdict(verdict="stable", evidence=evidence)


def _extract_ok_ttfts(rows_or_ttft) -> list[float]:
    """Pull TTFTs from rows (filtering ok=True) or accept a raw list."""
    out: list[float] = []
    for item in rows_or_ttft:
        if isinstance(item, dict):
            if not item.get("ok"):
                continue
            ttft = item.get("ttft_seconds")
            if ttft is None:
                continue
            out.append(float(ttft))
        elif isinstance(item, (int, float)):
            out.append(float(item))
    return out
