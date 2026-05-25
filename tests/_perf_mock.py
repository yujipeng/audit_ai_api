"""Row fixture builder used by detectors tests.

Detectors are pure functions over runner-produced rows. To avoid spinning a real
SSE server (overkill for pure-function unit tests), this helper constructs row
dicts that mirror what `runner._run_rounds_for_model` would produce with
`capture_chunk_timings=True`. Field names and types follow design §4.1 / §4.6.
"""

from __future__ import annotations

from typing import Optional


def make_row(*,
             ok: bool = True,
             chunk_count: Optional[int] = None,
             chunk_intervals: Optional[list[float]] = None,
             output_chars: int = 0,
             ttft_seconds: Optional[float] = None,
             status_code: int = 200,
             error: Optional[str] = None) -> dict:
    """Build a minimal row mirroring runner output for detector input."""
    if chunk_intervals is not None and chunk_count is None:
        chunk_count = len(chunk_intervals) + 1
    return {
        "ok": ok,
        "status_code": status_code,
        "ttft_seconds": ttft_seconds,
        "output_chars": output_chars,
        "chunk_count": chunk_count if chunk_count is not None else 0,
        "chunk_intervals": chunk_intervals,
        "error": error,
    }


def real_dense_rows(n_rounds: int = 5, n_chunks: int = 87, gap: float = 0.02) -> list[dict]:
    """n_rounds of dense-interval rounds — should vote real."""
    return [
        make_row(
            chunk_intervals=[gap] * (n_chunks - 1),
            output_chars=580,
            ttft_seconds=0.4,
        )
        for _ in range(n_rounds)
    ]


def pseudo_2chunk_rows(n_rounds: int = 5, output_chars: int = 580) -> list[dict]:
    """n_rounds of 2-chunk rounds with substantive text — should vote pseudo."""
    return [
        make_row(
            chunk_count=2,
            chunk_intervals=[0.3],
            output_chars=output_chars,
            ttft_seconds=0.5,
        )
        for _ in range(n_rounds)
    ]


def pseudo_burst_rows(n_rounds: int = 5, dense_chunks: int = 6, last_gap: float = 0.5,
                     dense_gap: float = 0.02) -> list[dict]:
    """Dense early chunks then a large final gap — should vote pseudo (rule 4)."""
    intervals = [dense_gap] * dense_chunks + [last_gap]
    return [
        make_row(
            chunk_intervals=list(intervals),
            output_chars=600,
            ttft_seconds=0.4,
        )
        for _ in range(n_rounds)
    ]


def inconclusive_error_rows(n_rounds: int = 5) -> list[dict]:
    """All rounds failed — should vote inconclusive."""
    return [
        make_row(
            ok=False,
            chunk_count=0,
            chunk_intervals=None,
            output_chars=0,
            status_code=500,
            error="HTTP 500",
            ttft_seconds=None,
        )
        for _ in range(n_rounds)
    ]
