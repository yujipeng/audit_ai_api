"""Tests for pricing-section fields in the transparent JSONL log
(Story TES-137 / S4-D, PRD §6.5.2).

The forensic log must record per-sample aggregate pricing data so the
operator can later cross-check the HTML report's verdict against the
raw signals. The contract:

  * ``log_pricing_entry(...)`` accepts an :class:`AggregatedPricingVerdict`
    and a sample-identifier dict; emits one JSONL line that contains
    namespaced fields ``pricing.token_drift / pricing.balance_drift /
    pricing.layer / pricing.confidence`` (PRD §6.5 second bullet).
  * The entry round-trips through ``json.loads`` and never carries the
    raw API key (the pricing path piggybacks on the existing redaction
    discipline).
  * ``per_layer`` raw verdict details are nested under
    ``pricing.per_layer`` so the audit run preserves L0/L1/L2 evidence
    even when the HTML report only renders the winner.
"""

from __future__ import annotations

import json

from api_relay_audit.pricing.evaluators.base import (
    PricingVerdict,
    Severity,
)
from api_relay_audit.pricing.pipeline import aggregate
from api_relay_audit.transparent_log import TransparentLogger


def _agg_with_critical():
    v0 = PricingVerdict(
        severity=Severity.TOKEN_DRIFT_WARN,
        evaluator="L0_character_ratio",
        drift_pct=35.0,
        details={"token_drift_pct": 35.0, "balance_drift_pct": 4.2},
    )
    v1 = PricingVerdict(
        severity=Severity.TOKEN_DRIFT_CRITICAL,
        evaluator="L1_tokenizer",
        drift_pct=120.0,
        details={"token_drift_pct": 120.0, "tokenizer": "tiktoken"},
    )
    v2 = PricingVerdict(
        severity=Severity.BALANCE_DRIFT_HIGH,
        evaluator="L2_balance_triangle",
        drift_pct=30.0,
        details={"balance_drift_pct": 30.0, "actual_outflow_usd": 0.012},
    )
    return aggregate([v0, v1, v2])


class TestPricingFields:
    def test_namespaced_fields_present(self, tmp_path):
        agg = _agg_with_critical()
        path = str(tmp_path / "audit.jsonl")
        logger = TransparentLogger(path)
        logger.log_pricing_entry(
            sample={"vendor": "anthropic", "model": "claude-opus-4-7"},
            aggregated=agg,
        )
        logger.close()

        entry = json.loads(open(path).readline())
        assert entry["pricing.layer"] == "L1_tokenizer"
        assert entry["pricing.confidence"] == 0.7
        assert entry["pricing.token_drift"] == 120.0
        # balance_drift comes from the L2 layer's details, NOT the winner.
        assert entry["pricing.balance_drift"] == 30.0

    def test_severity_recorded_as_string(self, tmp_path):
        agg = _agg_with_critical()
        path = str(tmp_path / "audit.jsonl")
        logger = TransparentLogger(path)
        logger.log_pricing_entry(sample={}, aggregated=agg)
        logger.close()
        entry = json.loads(open(path).readline())
        assert entry["pricing.severity"] == "token_drift_critical"

    def test_per_layer_round_trips(self, tmp_path):
        agg = _agg_with_critical()
        path = str(tmp_path / "audit.jsonl")
        logger = TransparentLogger(path)
        logger.log_pricing_entry(sample={}, aggregated=agg)
        logger.close()
        entry = json.loads(open(path).readline())
        per_layer = entry["pricing.per_layer"]
        assert "L0_character_ratio" in per_layer
        assert "L1_tokenizer" in per_layer
        assert "L2_balance_triangle" in per_layer
        assert per_layer["L1_tokenizer"]["tokenizer"] == "tiktoken"

    def test_suppressed_by_round_trips(self, tmp_path):
        agg = _agg_with_critical()
        path = str(tmp_path / "audit.jsonl")
        logger = TransparentLogger(path)
        logger.log_pricing_entry(sample={}, aggregated=agg)
        logger.close()
        entry = json.loads(open(path).readline())
        # L0 (warn) and L2 (balance high) lost to L1 critical.
        assert "L0_character_ratio" in entry["pricing.suppressed_by"]
        assert "L2_balance_triangle" in entry["pricing.suppressed_by"]

    def test_sample_identifier_carried(self, tmp_path):
        agg = _agg_with_critical()
        path = str(tmp_path / "audit.jsonl")
        logger = TransparentLogger(path)
        logger.log_pricing_entry(
            sample={"vendor": "openai", "model": "gpt-4o-mini",
                    "run_id": "abc123"},
            aggregated=agg,
        )
        logger.close()
        entry = json.loads(open(path).readline())
        assert entry["sample"]["vendor"] == "openai"
        assert entry["sample"]["model"] == "gpt-4o-mini"
        assert entry["sample"]["run_id"] == "abc123"

    def test_method_field_is_pricing(self, tmp_path):
        """Pricing entries are distinguishable from request entries by
        the ``method`` discriminator field, mirroring existing
        ``call`` / ``raw_request`` style."""
        agg = _agg_with_critical()
        path = str(tmp_path / "audit.jsonl")
        logger = TransparentLogger(path)
        logger.log_pricing_entry(sample={}, aggregated=agg)
        logger.close()
        entry = json.loads(open(path).readline())
        assert entry["method"] == "pricing"

    def test_balance_drift_absent_when_no_l2(self, tmp_path):
        v0 = PricingVerdict(
            severity=Severity.PRICING_COMPLIANT,
            evaluator="L0_character_ratio",
            drift_pct=4.0,
            details={"token_drift_pct": 4.0},
        )
        agg = aggregate([v0])
        path = str(tmp_path / "audit.jsonl")
        logger = TransparentLogger(path)
        logger.log_pricing_entry(sample={}, aggregated=agg)
        logger.close()
        entry = json.loads(open(path).readline())
        # Field is omitted (None or missing) when no balance evidence
        # exists — the audit must not synthesise a zero.
        assert entry.get("pricing.balance_drift") is None

    def test_timestamp_present(self, tmp_path):
        from datetime import datetime
        agg = _agg_with_critical()
        path = str(tmp_path / "audit.jsonl")
        logger = TransparentLogger(path)
        logger.log_pricing_entry(sample={}, aggregated=agg)
        logger.close()
        entry = json.loads(open(path).readline())
        # Must parse as ISO 8601 — same contract as call/raw_request entries.
        datetime.fromisoformat(entry["timestamp"])
