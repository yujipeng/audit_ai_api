"""Internal view-model schema for the audit-matrix report renderers.

Schema inspiration: `web/data-example.json` @ commit ``5d8eedc3`` (per
project-lead ruling on 2026-05-25). That file is the *platform-user-facing*
dataset shape; this module is the *offline-report-facing* shape. They share
field semantics by design, but this schema is fully independent — at runtime
none of the renderers read anything under ``web/``.

The contract:

* `ReportView.from_record(record)` is the only legal entry into the renderers.
  It asserts `schema_version == 1` and refuses anything else with a fail-fast
  ``ValueError``. Downstream renderers do not re-validate.
* `cells_by_step` groups cells deterministically (the run-record `cells`
  array is in insertion order); the renderers all walk steps in the same
  fixed order (``probe → purity → perf → pricing``) and fall back to
  insertion order for any unknown step.
* `summary_view` exposes the same six fields as the run-record's `summary`
  block plus a rounded ``hit_rate_pct`` for display.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Sequence

SUPPORTED_SCHEMA_VERSION = 1

STEP_ORDER: tuple[str, ...] = ("probe", "purity", "perf", "pricing")


def _require_schema_v1(record: Mapping[str, Any]) -> None:
    """Fail-fast guard. Raised at every public render entry point."""
    if "schema_version" not in record:
        raise ValueError(
            "run-record missing schema_version; expected v1 — refuse to render"
        )
    sv = record["schema_version"]
    if sv != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema_version={sv!r}; renderer is pinned to v"
            f"{SUPPORTED_SCHEMA_VERSION}. Bump the consumer deliberately."
        )


def _rating_for_cell(cell: Mapping[str, Any]) -> str:
    """Map a cell to a dual-distribution rating colour.

    Inspired by `web/data-example.json` ``rating`` field semantics:
      * ``status == "error"`` → red
      * ``cache_hit`` AND high latency_ms → yellow (caveat: cache layer noise)
      * default ok → green
    """
    if cell.get("status") == "error":
        return "red"
    if cell.get("cache_hit") and (cell.get("latency_ms") or 0.0) > 1000.0:
        return "yellow"
    return "green"


@dataclass(frozen=True)
class CellView:
    step: str
    endpoint: str
    model: str
    status: str
    latency_ms: float
    cache_hit: bool
    redacted_key_id: str
    payload: Dict[str, Any]
    error: Dict[str, Any] | None
    rating: str  # red/yellow/green


@dataclass(frozen=True)
class SummaryView:
    total: int
    ok: int
    error: int
    cache_hits: int
    cache_misses: int
    hit_rate: float

    @property
    def hit_rate_pct(self) -> float:
        return round(self.hit_rate * 100.0, 1)


@dataclass(frozen=True)
class ReportView:
    generated_at: str
    tool: str
    elapsed_seconds: float
    code_version: str
    config_digest: str
    redacted_key_ids: Sequence[str]
    summary: SummaryView
    cells: List[CellView] = field(default_factory=list)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ReportView":
        _require_schema_v1(record)

        s = record.get("summary") or {}
        summary = SummaryView(
            total=int(s.get("total", 0)),
            ok=int(s.get("ok", 0)),
            error=int(s.get("error", 0)),
            cache_hits=int(s.get("cache_hits", 0)),
            cache_misses=int(s.get("cache_misses", 0)),
            hit_rate=float(s.get("hit_rate", 0.0)),
        )

        cells: List[CellView] = []
        for c in record.get("cells") or []:
            cells.append(
                CellView(
                    step=str(c.get("step", "")),
                    endpoint=str(c.get("endpoint", "")),
                    model=str(c.get("model", "")),
                    status=str(c.get("status", "")),
                    latency_ms=float(c.get("latency_ms") or 0.0),
                    cache_hit=bool(c.get("cache_hit", False)),
                    redacted_key_id=str(c.get("redacted_key_id", "")),
                    payload=dict(c.get("payload") or {}),
                    error=dict(c.get("error")) if c.get("error") else None,
                    rating=_rating_for_cell(c),
                )
            )

        return cls(
            generated_at=str(record.get("generated_at", "")),
            tool=str(record.get("tool", "")),
            elapsed_seconds=float(record.get("elapsed_seconds") or 0.0),
            code_version=str(record.get("code_version", "")),
            config_digest=str(record.get("config_digest", "")),
            redacted_key_ids=list(record.get("redacted_key_ids") or []),
            summary=summary,
            cells=cells,
        )

    def cells_by_step(self) -> Dict[str, List[CellView]]:
        groups: Dict[str, List[CellView]] = {}
        for c in self.cells:
            groups.setdefault(c.step, []).append(c)
        known = [s for s in STEP_ORDER if s in groups]
        extra = [s for s in groups if s not in STEP_ORDER]
        return {s: groups[s] for s in (known + extra)}


def all_sentinels(record: Mapping[str, Any], extra: Iterable[str] | None = None) -> List[str]:
    """Collect candidate sentinel substrings for the redact stage.

    Empty / 1-char strings are dropped because they would over-match on every
    artifact and the redactor would refuse anyway.
    """
    out: List[str] = []
    for k in (record.get("redacted_key_ids") or []):
        if isinstance(k, str) and len(k) > 1:
            out.append(k)
    for k in (extra or []):
        if isinstance(k, str) and len(k) > 1:
            out.append(k)
    return out


__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "STEP_ORDER",
    "CellView",
    "ReportView",
    "SummaryView",
    "all_sentinels",
]
