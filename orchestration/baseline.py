"""Baseline diff — current run vs previous run-record.json.

Design §2.4 baseline-diff pseudocode (S5-C summary):

  for cell in current.cells:
      base = baseline.lookup(step, endpoint, model)
      if base is None:                               -> "new"
      elif perf_delta(cell, base) >= PERF_THRESHOLD: -> "flagged" (perf)
      elif score_drop(cell, base)  >= SCORE_DROP:    -> "flagged" (score)
      else                                           -> "unchanged"

  diff.md top summary: "<F flagged>, <U unchanged>, <N new>"

R9 mitigation: when a cell has `cache_hit=True`, its `latency_ms` reflects the
local cache layer rather than the upstream relay. The perf threshold is
suppressed for that cell; the score threshold still fires because the cached
*payload* is what would surface in a downstream report.

Schema fail-fast: both `current` and `baseline` must carry `schema_version=1`,
matching the contract in `reporting.schema._require_schema_v1`.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from reporting.schema import all_sentinels
from security.redact import redact_response_text, redact_text

PERF_THRESHOLD = 0.10  # ≥10% latency_ms relative change
SCORE_DROP_THRESHOLD = 0.10  # ≥0.1 absolute drop in payload.score


def _require_v1(record: Mapping[str, Any], label: str) -> None:
    sv = record.get("schema_version")
    if sv != 1:
        raise ValueError(
            f"{label}: schema_version={sv!r} unsupported; baseline.compute_diff "
            f"is pinned to v1"
        )


def _index(cells: Iterable[Mapping[str, Any]]) -> Dict[Tuple[str, str, str], Mapping[str, Any]]:
    out: Dict[Tuple[str, str, str], Mapping[str, Any]] = {}
    for c in cells:
        key = (c.get("step", ""), c.get("endpoint", ""), c.get("model", ""))
        out[key] = c
    return out


def _score(cell: Mapping[str, Any]) -> float | None:
    pay = cell.get("payload") or {}
    s = pay.get("score") if isinstance(pay, Mapping) else None
    if isinstance(s, (int, float)):
        return float(s)
    return None


def _perf_delta(curr: Mapping[str, Any], base: Mapping[str, Any]) -> float | None:
    a = curr.get("latency_ms")
    b = base.get("latency_ms")
    if a is None or b is None or b == 0:
        return None
    return (float(a) - float(b)) / float(b)


def _redact(s: str, sentinels: Sequence[str]) -> str:
    if not s:
        return s
    return redact_text(redact_response_text(s, set(sentinels)))


def compute_diff(
    *,
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
    sentinel_keys: Iterable[str] | None = None,
) -> Dict[str, Any]:
    """Return a diff dict ready for `render_diff_md`.

    Shape:
      {
        "summary": {"flagged": int, "unchanged": int, "new": int, "missing": int},
        "cells": [
          {"step", "endpoint", "model", "verdict": "flagged"|"unchanged"|"new",
           "reason": "perf"|"score"|"new"|"unchanged",
           "perf_delta": float|None, "score_drop": float|None,
           "cache_hit": bool, "flagged": bool, "note": str}, ...
        ],
      }
    """
    _require_v1(current, "current")
    _require_v1(baseline, "baseline")

    sentinels = list(set(all_sentinels(current, sentinel_keys or [])
                          + all_sentinels(baseline, sentinel_keys or [])))

    base_idx = _index(baseline.get("cells") or [])
    curr_cells = current.get("cells") or []

    rows: List[Dict[str, Any]] = []
    flagged = unchanged = new = 0

    for cell in curr_cells:
        key = (cell.get("step", ""), cell.get("endpoint", ""), cell.get("model", ""))
        base = base_idx.get(key)

        cache_hit = bool(cell.get("cache_hit", False))
        row: Dict[str, Any] = {
            "step": key[0],
            "endpoint": key[1],
            "model": key[2],
            "cache_hit": cache_hit,
            "perf_delta": None,
            "score_drop": None,
            "verdict": "unchanged",
            "reason": "unchanged",
            "flagged": False,
            "note": "",
        }

        if base is None:
            row["verdict"] = "new"
            row["reason"] = "new"
            row["note"] = _redact(
                f"new cell — no baseline for {key[0]}/{key[1]}/{key[2]}", sentinels
            )
            new += 1
            rows.append(row)
            continue

        perf_delta = _perf_delta(cell, base)
        row["perf_delta"] = perf_delta

        # R9: cache-hit cells have latency dominated by the cache layer; suppress
        # the perf threshold for them.
        perf_flag = (
            perf_delta is not None
            and not cache_hit
            and abs(perf_delta) >= PERF_THRESHOLD
        )

        s_curr = _score(cell)
        s_base = _score(base)
        score_drop = (s_base - s_curr) if (s_curr is not None and s_base is not None) else None
        row["score_drop"] = score_drop
        score_flag = score_drop is not None and score_drop >= SCORE_DROP_THRESHOLD

        if perf_flag and score_flag:
            row["verdict"] = "flagged"
            row["reason"] = "perf+score"
        elif perf_flag:
            row["verdict"] = "flagged"
            row["reason"] = "perf"
        elif score_flag:
            row["verdict"] = "flagged"
            row["reason"] = "score"

        row["flagged"] = row["verdict"] == "flagged"
        if row["flagged"]:
            flagged += 1
            note_bits: List[str] = []
            if perf_flag:
                note_bits.append(f"perf {perf_delta * 100:+.1f}%")
            if score_flag:
                note_bits.append(f"score -{score_drop:.2f}")
            row["note"] = _redact(", ".join(note_bits), sentinels)
        else:
            unchanged += 1

        rows.append(row)

    missing = sum(1 for k in base_idx if k not in {
        (c.get("step", ""), c.get("endpoint", ""), c.get("model", ""))
        for c in curr_cells
    })

    return {
        "summary": {
            "flagged": flagged,
            "unchanged": unchanged,
            "new": new,
            "missing": missing,
            "total_current": len(curr_cells),
            "total_baseline": len(base_idx),
        },
        "cells": rows,
    }


def render_diff_md(diff: Mapping[str, Any]) -> str:
    """Render a compute_diff result as Markdown — flagged rows up top."""
    s = diff["summary"]
    icon = "🔴" if s["flagged"] else "🟢"
    # Keep the title + summary as one paragraph block so downstream consumers
    # (the first `\n\n`-split) see the numeric summary at the top.
    head_lines = [
        f"# Baseline Diff — {icon} {s['flagged']} flagged, {s['unchanged']} unchanged, "
        f"{s['new']} new, {s['missing']} missing "
        f"(current={s['total_current']}, baseline={s['total_baseline']})",
        "",
    ]

    flagged = [c for c in diff["cells"] if c.get("flagged")]
    others = [c for c in diff["cells"] if not c.get("flagged")]

    body: List[str] = []
    if flagged:
        body.append("## Flagged")
        body.append("")
        body.append("| step | endpoint | model | reason | perf Δ | score drop | cache | note |")
        body.append("|---|---|---|---|---|---|---|---|")
        for c in flagged:
            pd = "—" if c["perf_delta"] is None else f"{c['perf_delta'] * 100:+.1f}%"
            sd = "—" if c["score_drop"] is None else f"{c['score_drop']:+.2f}"
            body.append(
                f"| {c['step']} | {c['endpoint']} | {c['model']} | {c['reason']} "
                f"| {pd} | {sd} | {'yes' if c['cache_hit'] else 'no'} | {c['note']} |"
            )
        body.append("")

    if others:
        body.append("## Other")
        body.append("")
        body.append("| step | endpoint | model | verdict | perf Δ | score drop | cache |")
        body.append("|---|---|---|---|---|---|---|")
        for c in others:
            pd = "—" if c["perf_delta"] is None else f"{c['perf_delta'] * 100:+.1f}%"
            sd = "—" if c["score_drop"] is None else f"{c['score_drop']:+.2f}"
            body.append(
                f"| {c['step']} | {c['endpoint']} | {c['model']} | {c['verdict']} "
                f"| {pd} | {sd} | {'yes' if c['cache_hit'] else 'no'} |"
            )
        body.append("")

    return "\n".join(head_lines + body)


__all__ = [
    "PERF_THRESHOLD",
    "SCORE_DROP_THRESHOLD",
    "compute_diff",
    "render_diff_md",
]
