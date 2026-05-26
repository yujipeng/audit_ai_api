"""Tests for api_relay_audit.pricing.pipeline (Story TES-137 / S4-D).

Pipeline aggregator collapses up to three layered ``PricingVerdict``s
(L0 character-ratio, L1 tokenizer, L2 balance triangle) into a single
``AggregatedPricingVerdict``. Aggregation rules (DoD bullet 1):

  * **Strictest severity wins** — the layer with the highest severity
    rank decides the final verdict.
  * **Confidence is the max layer confidence** — L0 = 0.4, L1 = 0.7,
    L2 = 0.9 (design §8 S6 confidence ladder).
  * **suppressed_by chain** — every layer that fired a non-pass
    severity but was outranked by a stricter layer is recorded so the
    transparent log preserves the downgrade trail.
  * **Inconclusive fallback** — if every layer is INCONCLUSIVE_BALANCE,
    the aggregate is INCONCLUSIVE; if at least one layer is
    PRICING_COMPLIANT and no stricter layer fires, aggregate is
    PRICING_COMPLIANT.
"""

from __future__ import annotations

import pytest

from api_relay_audit.pricing import pipeline
from api_relay_audit.pricing.evaluators.base import (
    PricingVerdict,
    Severity,
)


def _v(layer: str, severity: Severity, drift: float = 0.0,
       details: dict | None = None) -> PricingVerdict:
    return PricingVerdict(
        severity=severity,
        evaluator=layer,
        drift_pct=drift,
        details=details or {},
    )


class TestStrictestWins:
    def test_l1_critical_overrides_l0_warn(self):
        v0 = _v("L0_character_ratio", Severity.TOKEN_DRIFT_WARN, 35.0)
        v1 = _v("L1_tokenizer", Severity.TOKEN_DRIFT_CRITICAL, 120.0)
        agg = pipeline.aggregate([v0, v1])
        assert agg.severity is Severity.TOKEN_DRIFT_CRITICAL
        assert agg.layer == "L1_tokenizer"

    def test_l2_balance_overrides_l0_warn(self):
        v0 = _v("L0_character_ratio", Severity.TOKEN_DRIFT_WARN, 40.0)
        v2 = _v("L2_balance_triangle", Severity.BALANCE_DRIFT_HIGH, 30.0)
        agg = pipeline.aggregate([v0, v2])
        assert agg.severity is Severity.BALANCE_DRIFT_HIGH
        assert agg.layer == "L2_balance_triangle"

    def test_token_critical_outranks_balance_drift(self):
        """Token-drift-critical and balance-drift-high are both 'high'
        severity but token critical surfaces first per design §8 S6."""
        v1 = _v("L1_tokenizer", Severity.TOKEN_DRIFT_CRITICAL, 200.0)
        v2 = _v("L2_balance_triangle", Severity.BALANCE_DRIFT_HIGH, 30.0)
        agg = pipeline.aggregate([v1, v2])
        assert agg.severity is Severity.TOKEN_DRIFT_CRITICAL


class TestSuppressedByChain:
    def test_outranked_layers_recorded(self):
        v0 = _v("L0_character_ratio", Severity.TOKEN_DRIFT_WARN, 35.0)
        v1 = _v("L1_tokenizer", Severity.TOKEN_DRIFT_CRITICAL, 120.0)
        agg = pipeline.aggregate([v0, v1])
        assert "L0_character_ratio" in agg.suppressed_by
        assert "L1_tokenizer" not in agg.suppressed_by  # the winner is not suppressed

    def test_pass_layers_not_in_suppressed(self):
        """A layer that fired PRICING_COMPLIANT was not 'suppressed', it
        simply agreed it was clean."""
        v0 = _v("L0_character_ratio", Severity.PRICING_COMPLIANT, 5.0)
        v1 = _v("L1_tokenizer", Severity.TOKEN_DRIFT_WARN, 12.0)
        agg = pipeline.aggregate([v0, v1])
        assert agg.severity is Severity.TOKEN_DRIFT_WARN
        assert "L0_character_ratio" not in agg.suppressed_by

    def test_empty_suppressed_when_one_layer(self):
        v0 = _v("L0_character_ratio", Severity.PRICING_COMPLIANT, 2.0)
        agg = pipeline.aggregate([v0])
        assert agg.suppressed_by == []


class TestConfidenceLadder:
    @pytest.mark.parametrize("layer,expected", [
        ("L0_character_ratio", 0.4),
        ("L1_tokenizer", 0.7),
        ("L2_balance_triangle", 0.9),
    ])
    def test_confidence_per_layer(self, layer, expected):
        v = _v(layer, Severity.TOKEN_DRIFT_WARN, 35.0)
        agg = pipeline.aggregate([v])
        assert agg.confidence == pytest.approx(expected)

    def test_aggregate_uses_max_confidence_of_winning_layer(self):
        v0 = _v("L0_character_ratio", Severity.TOKEN_DRIFT_WARN, 35.0)
        v1 = _v("L1_tokenizer", Severity.TOKEN_DRIFT_CRITICAL, 120.0)
        agg = pipeline.aggregate([v0, v1])
        assert agg.confidence == pytest.approx(0.7)


class TestInconclusiveFallback:
    def test_all_inconclusive_aggregates_inconclusive(self):
        v0 = _v("L0_character_ratio", Severity.INCONCLUSIVE_BALANCE)
        v1 = _v("L1_tokenizer", Severity.INCONCLUSIVE_BALANCE)
        agg = pipeline.aggregate([v0, v1])
        assert agg.severity is Severity.INCONCLUSIVE_BALANCE

    def test_compliant_overrides_inconclusive(self):
        """A solid L0 PASS beats an L1 'inconclusive' (e.g. tiktoken
        missing) — the audit at least has the cheap layer's verdict."""
        v0 = _v("L0_character_ratio", Severity.PRICING_COMPLIANT, 4.0)
        v1 = _v("L1_tokenizer", Severity.INCONCLUSIVE_BALANCE)
        agg = pipeline.aggregate([v0, v1])
        assert agg.severity is Severity.PRICING_COMPLIANT

    def test_empty_input_raises(self):
        with pytest.raises(ValueError):
            pipeline.aggregate([])


class TestAggregatedShape:
    def test_aggregated_carries_per_layer_details(self):
        v0 = _v("L0_character_ratio", Severity.TOKEN_DRIFT_WARN, 35.0,
                {"token_drift_pct": 35.0})
        v1 = _v("L1_tokenizer", Severity.TOKEN_DRIFT_CRITICAL, 120.0,
                {"token_drift_pct": 120.0})
        agg = pipeline.aggregate([v0, v1])
        assert "L0_character_ratio" in agg.per_layer
        assert "L1_tokenizer" in agg.per_layer
        assert agg.per_layer["L1_tokenizer"]["token_drift_pct"] == 120.0

    def test_aggregated_drift_pct_from_winning_layer(self):
        v0 = _v("L0_character_ratio", Severity.TOKEN_DRIFT_WARN, 35.0)
        v1 = _v("L1_tokenizer", Severity.TOKEN_DRIFT_CRITICAL, 120.0)
        agg = pipeline.aggregate([v0, v1])
        assert agg.drift_pct == pytest.approx(120.0)
