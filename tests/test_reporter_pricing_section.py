"""Tests for pricing-compliance section in the reporter
(Story TES-137 / S4-D, PRD §6.1.4 / design §4.5).

Reporter section requirements:

  * ``reporter.pricing_section(aggregateds, runs=[...])`` injects a
    new ``## Pricing Compliance`` heading followed by three Markdown
    tables: token-drift, unit-price, balance.
  * Each table row carries ``confidence`` + ``layer`` + ``evidence``
    columns so the reader sees which evaluator fired and how trustable
    the verdict is.
  * A verdict badge (🟢 / 🟡 / 🔴) precedes the section title; mapping:
    ``pricing_compliant`` → 🟢, ``token_drift_warn`` /
    ``inconclusive_balance`` → 🟡, everything else → 🔴.
  * standalone ``audit.py`` only renders the L0 segment (PRD §6.3.1
    standalone parity); the reporter exposes ``standalone=True`` to
    drop L1/L2 rows.
  * ``schema_version`` bump compatibility (design §4.5): the JSON
    artifact carries ``schema_version`` and the reporter only prints
    sections whose layer is supported by the local schema.
"""

from __future__ import annotations

from api_relay_audit.pricing.evaluators.base import (
    PricingVerdict,
    Severity,
)
from api_relay_audit.pricing.pipeline import aggregate
from api_relay_audit.reporter import Reporter


def _three_layer_critical():
    v0 = PricingVerdict(
        severity=Severity.TOKEN_DRIFT_WARN,
        evaluator="L0_character_ratio",
        drift_pct=35.0,
        details={
            "expected_total_tokens": 100,
            "reported_total_tokens": 135,
            "token_drift_pct": 35.0,
        },
    )
    v1 = PricingVerdict(
        severity=Severity.TOKEN_DRIFT_CRITICAL,
        evaluator="L1_tokenizer",
        drift_pct=120.0,
        details={
            "tokenizer": "tiktoken",
            "true_total_tokens": 100,
            "reported_total_tokens": 220,
            "token_drift_pct": 120.0,
        },
    )
    v2 = PricingVerdict(
        severity=Severity.BALANCE_DRIFT_HIGH,
        evaluator="L2_balance_triangle",
        drift_pct=30.0,
        details={
            "claimed_cost_usd": 0.010,
            "actual_outflow_usd": 0.013,
            "balance_drift_pct": 30.0,
            "adapter": "sample",
        },
    )
    return aggregate([v0, v1, v2])


def _l0_only_compliant():
    v0 = PricingVerdict(
        severity=Severity.PRICING_COMPLIANT,
        evaluator="L0_character_ratio",
        drift_pct=4.0,
        details={
            "expected_total_tokens": 100,
            "reported_total_tokens": 104,
            "token_drift_pct": 4.0,
        },
    )
    return aggregate([v0])


class TestPricingSectionHeading:
    def test_section_title_present(self):
        rpt = Reporter()
        rpt.pricing_section([_three_layer_critical()])
        body = "".join(rpt.sections)
        assert "Pricing Compliance" in body

    def test_red_badge_for_critical(self):
        rpt = Reporter()
        rpt.pricing_section([_three_layer_critical()])
        body = "".join(rpt.sections)
        assert "\U0001f534" in body  # red circle precedes the title

    def test_green_badge_for_compliant(self):
        rpt = Reporter()
        rpt.pricing_section([_l0_only_compliant()])
        body = "".join(rpt.sections)
        assert "\U0001f7e2" in body  # green circle


class TestThreeTables:
    def test_token_table_present(self):
        rpt = Reporter()
        rpt.pricing_section([_three_layer_critical()])
        body = "".join(rpt.sections)
        assert "Token drift" in body
        # Reported vs expected token columns
        assert "expected" in body.lower() and "reported" in body.lower()

    def test_unit_price_table_present(self):
        rpt = Reporter()
        rpt.pricing_section([_three_layer_critical()])
        body = "".join(rpt.sections)
        assert "Unit price" in body or "unit_price" in body.lower()

    def test_balance_table_present(self):
        rpt = Reporter()
        rpt.pricing_section([_three_layer_critical()])
        body = "".join(rpt.sections)
        assert "Balance" in body
        # Outflow / claimed columns
        assert "outflow" in body.lower() or "claimed" in body.lower()

    def test_confidence_layer_evidence_columns(self):
        rpt = Reporter()
        rpt.pricing_section([_three_layer_critical()])
        body = "".join(rpt.sections)
        # Each table must have these three columns named explicitly
        # (case-insensitive); they appear in the header row.
        for col in ("confidence", "layer", "evidence"):
            assert col in body.lower(), (
                f"column {col!r} missing from pricing tables"
            )


class TestStandaloneL0Only:
    def test_standalone_drops_l1_l2_rows(self):
        rpt = Reporter()
        rpt.pricing_section([_three_layer_critical()], standalone=True)
        body = "".join(rpt.sections)
        # L1 / L2 layer names must NOT appear when rendered for standalone.
        assert "L1_tokenizer" not in body
        assert "L2_balance_triangle" not in body
        # L0 still rendered.
        assert "L0_character_ratio" in body

    def test_modular_renders_all_layers(self):
        rpt = Reporter()
        rpt.pricing_section([_three_layer_critical()], standalone=False)
        body = "".join(rpt.sections)
        assert "L0_character_ratio" in body
        assert "L1_tokenizer" in body
        assert "L2_balance_triangle" in body


class TestSummaryFlag:
    def test_critical_adds_red_summary(self):
        rpt = Reporter()
        rpt.pricing_section([_three_layer_critical()])
        levels = [s[0] for s in rpt.summary]
        assert "red" in levels

    def test_compliant_adds_green_summary(self):
        rpt = Reporter()
        rpt.pricing_section([_l0_only_compliant()])
        levels = [s[0] for s in rpt.summary]
        assert "green" in levels


class TestSchemaVersion:
    def test_schema_version_constant_exposed(self):
        from api_relay_audit.pricing import pipeline
        assert hasattr(pipeline, "PRICING_SCHEMA_VERSION")
        # Bump-safe: an integer or string ≥ "1".
        v = pipeline.PRICING_SCHEMA_VERSION
        assert v == "1" or v == 1 or (isinstance(v, str) and v.startswith("1"))


class TestEmptyInput:
    def test_no_aggregates_renders_nothing_destructive(self):
        rpt = Reporter()
        rpt.pricing_section([])
        # Either silently no-op or write a notice — but must not raise
        # and must not break later renders.
        rpt.h1("After")
        body = "".join(rpt.sections)
        assert "# After" in body
