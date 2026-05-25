"""Unit tests for `api_relay_audit.perf.detectors`.

Covers design §4.3 four verdict paths (real / pseudo-2chunk / pseudo-burst /
inconclusive-error) for `pseudo_stream_detect`, plus all branches of
`slow_start_detect`. Thresholds (§10 Q1) are frozen — these tests anchor
them so future drift is caught immediately.

Risk anchors: R2 (false-positive guard), R3 (slow_start ignores failed rounds),
R8 (threshold freeze).
"""

from __future__ import annotations

import pytest

from api_relay_audit.perf.detectors import (
    MIN_CHUNKS_FOR_REAL,
    MIN_DETECTOR_ROUNDS,
    MIN_ROUNDS_FOR_SLOW_START,
    PSEUDO_GAP_BEFORE_LAST_RATIO,
    PSEUDO_LAST_BURST_SHARE,
    PSEUDO_MAJORITY_THRESHOLD,
    PseudoStreamVerdict,
    SLOW_START_RATIO,
    SLOW_START_WARMUP_DEFAULT,
    SlowStartVerdict,
    pseudo_stream_detect,
    slow_start_detect,
)
from tests._perf_mock import (
    inconclusive_error_rows,
    make_row,
    pseudo_2chunk_rows,
    pseudo_burst_rows,
    real_dense_rows,
)


# ---- §10 Q1 — threshold freeze (R8) ---------------------------------------

def test_thresholds_frozen_per_design_q1():
    """design §4.3 / §10 Q1 — dev must not adjust; this anchors all 7+1 values."""
    assert MIN_CHUNKS_FOR_REAL == 3
    assert PSEUDO_LAST_BURST_SHARE == 0.7
    assert PSEUDO_GAP_BEFORE_LAST_RATIO == 5.0
    assert PSEUDO_MAJORITY_THRESHOLD == 0.5
    assert SLOW_START_WARMUP_DEFAULT == 3
    assert SLOW_START_RATIO == 1.3
    assert MIN_ROUNDS_FOR_SLOW_START == 6
    assert MIN_DETECTOR_ROUNDS == 3


# ---- pseudo_stream_detect — 4 verdict paths (DoD) -------------------------

def test_pseudo_stream_real_dense_intervals():
    """Path 1: dense uniform intervals → real (rule 5)."""
    rows = real_dense_rows(n_rounds=5, n_chunks=87, gap=0.02)
    result = pseudo_stream_detect(rows)
    assert isinstance(result, PseudoStreamVerdict)
    assert result.verdict == "real"
    assert result.evidence["ok_rounds_used"] == 5
    assert result.evidence["per_round_verdicts"] == ["real"] * 5
    assert result.evidence["chunk_gap_p50"] == pytest.approx(0.02)


def test_pseudo_stream_two_chunk_with_text():
    """Path 2: chunk_count=2 + output_chars=580 → pseudo (rule 2)."""
    rows = pseudo_2chunk_rows(n_rounds=5, output_chars=580)
    result = pseudo_stream_detect(rows)
    assert result.verdict == "pseudo"
    assert result.evidence["per_round_verdicts"] == ["pseudo"] * 5
    assert "lone-chunk-with-text" in result.evidence["reason"]


def test_pseudo_stream_buffered_burst_last_gap():
    """Path 3: dense chunks then final-chunk gap → pseudo (rule 4)."""
    rows = pseudo_burst_rows(n_rounds=5, dense_chunks=6, last_gap=0.5, dense_gap=0.02)
    result = pseudo_stream_detect(rows)
    assert result.verdict == "pseudo"
    assert result.evidence["per_round_verdicts"] == ["pseudo"] * 5
    assert "buffered-burst" in result.evidence["reason"]


def test_pseudo_stream_inconclusive_all_errors():
    """Path 4: all rows ok=False → inconclusive (rule 1 + min rounds guard)."""
    rows = inconclusive_error_rows(n_rounds=5)
    result = pseudo_stream_detect(rows)
    assert result.verdict == "inconclusive"
    assert result.evidence["ok_rounds_used"] == 0


# ---- R2 — low-bandwidth real stream must NOT be flagged pseudo ------------

def test_pseudo_stream_low_bandwidth_real_not_pseudo():
    """R2: gradual slow-but-uniform intervals → real (median ≈ last; ratio < 5)."""
    intervals = [0.5, 0.51, 0.49, 0.5, 0.52, 0.5]
    rows = [make_row(chunk_intervals=intervals, output_chars=300, ttft_seconds=0.6)
            for _ in range(5)]
    result = pseudo_stream_detect(rows)
    assert result.verdict == "real"


def test_pseudo_stream_short_round_below_min_chunks_no_text_inconclusive():
    """chunk_count below MIN_CHUNKS_FOR_REAL but tiny output → single-round inconclusive."""
    rows = [make_row(chunk_count=2, chunk_intervals=[0.1], output_chars=10,
                     ttft_seconds=0.3) for _ in range(5)]
    result = pseudo_stream_detect(rows)
    assert result.verdict == "inconclusive"


def test_pseudo_stream_majority_vote_split_inconclusive():
    """Mixed verdicts with neither side ≥ majority → inconclusive.

    2 real + 2 pseudo + 1 single-round-inconclusive (below-min-chunks/no-text)
    → 2/5 = 0.4 for both real and pseudo, neither hits the 0.5 majority.
    """
    rows = (
        real_dense_rows(n_rounds=2)
        + pseudo_2chunk_rows(n_rounds=2)
        + [make_row(chunk_count=2, chunk_intervals=[0.1], output_chars=10,
                    ttft_seconds=0.3)]
    )
    result = pseudo_stream_detect(rows)
    assert result.verdict == "inconclusive"
    assert result.evidence["ok_rounds_used"] == 5


def test_pseudo_stream_fewer_than_min_rounds_inconclusive():
    """ok rounds < MIN_DETECTOR_ROUNDS → inconclusive regardless of votes."""
    rows = real_dense_rows(n_rounds=2)
    result = pseudo_stream_detect(rows)
    assert result.verdict == "inconclusive"


def test_pseudo_stream_empty_rows_inconclusive():
    """Empty input → inconclusive (no error)."""
    result = pseudo_stream_detect([])
    assert result.verdict == "inconclusive"
    assert result.evidence["ok_rounds_used"] == 0


def test_pseudo_stream_capture_disabled_inconclusive():
    """chunk_intervals=None (capture off) → all rounds inconclusive."""
    rows = [make_row(chunk_count=20, chunk_intervals=None, output_chars=400,
                     ttft_seconds=0.4) for _ in range(5)]
    result = pseudo_stream_detect(rows)
    assert result.verdict == "inconclusive"


# ---- slow_start_detect ----------------------------------------------------

def test_slow_start_detected_warmup_3_steady_7():
    """ttft 前 3 轮 2s + 后 7 轮 0.5s → slow_start (ratio 2/0.5 = 4 > 1.3)."""
    rows = [make_row(chunk_intervals=[0.02] * 10, output_chars=300, ttft_seconds=2.0)
            for _ in range(3)]
    rows += [make_row(chunk_intervals=[0.02] * 10, output_chars=300, ttft_seconds=0.5)
             for _ in range(7)]
    result = slow_start_detect(rows)
    assert isinstance(result, SlowStartVerdict)
    assert result.verdict == "slow_start"
    assert result.evidence["warmup_rounds"] == 3
    assert result.evidence["warmup_avg"] == pytest.approx(2.0)
    assert result.evidence["steady_avg"] == pytest.approx(0.5)
    assert result.evidence["ratio"] == pytest.approx(4.0)
    assert result.evidence["ok_rounds_used"] == 10


def test_slow_start_stable_uniform_ttft():
    """Uniform TTFT → stable (ratio ≈ 1 < 1.3)."""
    rows = [make_row(chunk_intervals=[0.02] * 10, output_chars=300, ttft_seconds=0.5)
            for _ in range(10)]
    result = slow_start_detect(rows)
    assert result.verdict == "stable"


def test_slow_start_inconclusive_too_few_rounds():
    """Fewer than MIN_ROUNDS_FOR_SLOW_START → inconclusive."""
    rows = [make_row(chunk_intervals=[0.02] * 10, output_chars=300, ttft_seconds=0.5)
            for _ in range(5)]
    result = slow_start_detect(rows)
    assert result.verdict == "inconclusive"


def test_slow_start_ignores_failed_rounds_R3():
    """R3: failed rounds (ok=False) must not be counted as warmup samples."""
    rows = [make_row(ok=False, status_code=429, ttft_seconds=None, error="HTTP 429")
            for _ in range(3)]
    rows += [make_row(chunk_intervals=[0.02] * 10, output_chars=300, ttft_seconds=2.0)
             for _ in range(3)]
    rows += [make_row(chunk_intervals=[0.02] * 10, output_chars=300, ttft_seconds=0.5)
             for _ in range(7)]
    result = slow_start_detect(rows)
    assert result.verdict == "slow_start"
    assert result.evidence["ok_rounds_used"] == 10
    assert result.evidence["warmup_avg"] == pytest.approx(2.0)


def test_slow_start_steady_window_empty_inconclusive():
    """ok rounds <= warmup window → inconclusive (steady set empty)."""
    rows = [make_row(chunk_intervals=[0.02] * 10, output_chars=300, ttft_seconds=2.0)
            for _ in range(6)]
    rows += [make_row(ok=False, status_code=500, ttft_seconds=None) for _ in range(4)]
    result = slow_start_detect(rows, warmup=6)
    assert result.verdict == "inconclusive"


def test_slow_start_custom_warmup():
    """warmup keyword should override the default."""
    rows = [make_row(chunk_intervals=[0.02] * 10, output_chars=300, ttft_seconds=2.0)
            for _ in range(2)]
    rows += [make_row(chunk_intervals=[0.02] * 10, output_chars=300, ttft_seconds=0.5)
             for _ in range(6)]
    result = slow_start_detect(rows, warmup=2)
    assert result.verdict == "slow_start"
    assert result.evidence["warmup_rounds"] == 2
    assert result.evidence["warmup_avg"] == pytest.approx(2.0)
