"""Pipeline aggregator for layered pricing verdicts (Story TES-137 / S4-D).

Collapses up to three :class:`PricingVerdict`s — one per evaluator layer
(L0 character-ratio, L1 tokenizer, L2 balance-triangle) — into a single
:class:`AggregatedPricingVerdict`. Aggregation rules are the contract
exercised by ``tests/test_pricing_pipeline_aggregate.py``:

  * Strictest non-pass severity wins. Ranking uses ``_SEVERITY_RANK``;
    ``TOKEN_DRIFT_CRITICAL`` outranks ``BALANCE_DRIFT_HIGH`` (design
    §8 S6).
  * Confidence is the winning layer's ladder confidence
    (L0=0.4 / L1=0.7 / L2=0.9); the cheap layer is honest about being
    cheap.
  * Layers that fired a non-pass severity but lost are recorded in
    ``suppressed_by`` so the transparent log preserves the downgrade
    trail.
  * If every layer is ``INCONCLUSIVE_BALANCE`` the aggregate is
    ``INCONCLUSIVE_BALANCE``; if at least one layer is
    ``PRICING_COMPLIANT`` and no stricter signal is present, the
    aggregate is ``PRICING_COMPLIANT`` (a clean cheap layer beats an
    inconclusive expensive layer).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from api_relay_audit.pricing.evaluators.base import (
    PricingVerdict,
    Severity,
)


_LAYER_CONFIDENCE: dict[str, float] = {
    "L0_character_ratio": 0.4,
    "L1_tokenizer": 0.7,
    "L2_balance_triangle": 0.9,
}


# Higher rank = stricter. Token-drift-critical outranks balance-drift-high
# because token drift is a deterministic over-billing signal whereas
# balance drift can be confounded by FX / cache discounts.
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INCONCLUSIVE_BALANCE: 0,
    Severity.PRICING_COMPLIANT: 1,
    Severity.TOKEN_DRIFT_WARN: 2,
    Severity.UNIT_PRICE_MISMATCH: 3,
    Severity.BALANCE_DRIFT_HIGH: 4,
    Severity.TOKEN_DRIFT_CRITICAL: 5,
}


@dataclass
class AggregatedPricingVerdict:
    """Final per-sample verdict after layer aggregation."""

    severity: Severity
    layer: str
    confidence: float
    drift_pct: float
    suppressed_by: list[str] = field(default_factory=list)
    per_layer: dict[str, dict] = field(default_factory=dict)


def _confidence_for(layer: str) -> float:
    return _LAYER_CONFIDENCE.get(layer, 0.4)


def aggregate(verdicts: Iterable[PricingVerdict]) -> AggregatedPricingVerdict:
    """Collapse layer verdicts into an :class:`AggregatedPricingVerdict`."""
    items = list(verdicts)
    if not items:
        raise ValueError("aggregate() requires at least one PricingVerdict")

    winner = max(items, key=lambda v: _SEVERITY_RANK[v.severity])

    suppressed_by = [
        v.evaluator
        for v in items
        if v is not winner
        and v.severity not in (Severity.PRICING_COMPLIANT,
                                Severity.INCONCLUSIVE_BALANCE)
    ]

    per_layer = {v.evaluator: dict(v.details) for v in items}

    return AggregatedPricingVerdict(
        severity=winner.severity,
        layer=winner.evaluator,
        confidence=_confidence_for(winner.evaluator),
        drift_pct=winner.drift_pct,
        suppressed_by=suppressed_by,
        per_layer=per_layer,
    )


__all__ = [
    "AggregatedPricingVerdict",
    "aggregate",
]
