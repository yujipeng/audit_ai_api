"""Pricing JSON artifact writer (Story TES-137 / S4-D, PRD §6.1.5).

Writes one artifact per audit run at
``reports/pricing/<provider>/<run-id>.json``. Schema is frozen at
``PRICING_SCHEMA_VERSION`` — the S5 yaml-matrix orchestrator reads
this file back, so changes must bump the schema version (design §4.5).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from api_relay_audit.pricing.evaluators.base import Severity
from api_relay_audit.pricing.pipeline import (
    AggregatedPricingVerdict,
    PRICING_SCHEMA_VERSION,
)


# PRD §6.5.1 exit-code matrix.
_EXIT_CODE_BY_SEVERITY: dict[Severity, int] = {
    Severity.PRICING_COMPLIANT: 0,
    Severity.INCONCLUSIVE_BALANCE: 0,
    Severity.TOKEN_DRIFT_WARN: 0,
    Severity.TOKEN_DRIFT_CRITICAL: 1,
    Severity.UNIT_PRICE_MISMATCH: 1,
    Severity.BALANCE_DRIFT_HIGH: 1,
}


def compute_exit_code(verdicts: Iterable[AggregatedPricingVerdict]) -> int:
    """Return the worst exit code across the run (0 if no verdicts)."""
    code = 0
    for v in verdicts:
        c = _EXIT_CODE_BY_SEVERITY.get(v.severity, 0)
        if c > code:
            code = c
    return code


def _serialise_verdict(sample: dict, agg: AggregatedPricingVerdict) -> dict:
    return {
        "sample": dict(sample) if sample else {},
        "severity": agg.severity.value,
        "layer": agg.layer,
        "confidence": agg.confidence,
        "drift_pct": agg.drift_pct,
        "suppressed_by": list(agg.suppressed_by),
        "per_layer": {k: dict(v) for k, v in agg.per_layer.items()},
    }


def write_artifact(
    *,
    path: Path,
    run_id: str,
    provider: str,
    model: str,
    started_at: str,
    finished_at: str,
    verdicts: Iterable[tuple[dict, AggregatedPricingVerdict]],
) -> dict:
    """Serialise the run as JSON at ``path`` and return the payload."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    items = list(verdicts)
    serialised = [_serialise_verdict(s, a) for s, a in items]

    by_severity: dict[str, int] = {}
    aggs = [a for _, a in items]
    for agg in aggs:
        by_severity[agg.severity.value] = (
            by_severity.get(agg.severity.value, 0) + 1
        )

    payload = {
        "schema_version": PRICING_SCHEMA_VERSION,
        "run_id": run_id,
        "provider": provider,
        "model": model,
        "started_at": started_at,
        "finished_at": finished_at,
        "verdicts": serialised,
        "summary": {
            "total": len(items),
            "by_severity": by_severity,
            "exit_code": compute_exit_code(aggs),
        },
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


__all__ = [
    "compute_exit_code",
    "write_artifact",
]
