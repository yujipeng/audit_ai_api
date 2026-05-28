"""Tests for pricing module core data models (PricingSample, PriceQuote,
PricingVerdict, PricingEvaluator Protocol). Story TES-134 / S4-A."""

from __future__ import annotations

import pytest

from api_relay_audit.pricing import (
    PriceQuote,
    PricingEvaluator,
    PricingSample,
    PricingVerdict,
    Severity,
)


class TestSeverity:
    def test_closed_enum_values(self):
        assert Severity.PRICING_COMPLIANT.value == "pricing_compliant"
        assert Severity.TOKEN_DRIFT_WARN.value == "token_drift_warn"
        assert Severity.TOKEN_DRIFT_CRITICAL.value == "token_drift_critical"
        assert Severity.UNIT_PRICE_MISMATCH.value == "unit_price_mismatch"
        assert Severity.BALANCE_DRIFT_HIGH.value == "balance_drift_high"
        assert Severity.INCONCLUSIVE_BALANCE.value == "inconclusive_balance"

    def test_enum_is_closed(self):
        # Cross-Story invariant: 6 members, no extras allowed.
        assert {s.value for s in Severity} == {
            "pricing_compliant",
            "token_drift_warn",
            "token_drift_critical",
            "unit_price_mismatch",
            "balance_drift_high",
            "inconclusive_balance",
        }


class TestPriceQuote:
    def test_required_fields(self):
        q = PriceQuote(
            vendor="anthropic",
            model="claude-haiku-4-5",
            input_per_1k=0.001,
            output_per_1k=0.005,
            currency="USD",
        )
        assert q.vendor == "anthropic"
        assert q.input_per_1k == 0.001
        assert q.currency == "USD"

    def test_default_currency_is_usd(self):
        q = PriceQuote(vendor="x", model="y", input_per_1k=0.0, output_per_1k=0.0)
        assert q.currency == "USD"

    def test_cost_for_tokens_usd(self):
        q = PriceQuote(
            vendor="anthropic",
            model="claude-haiku-4-5",
            input_per_1k=1.0,
            output_per_1k=5.0,
            currency="USD",
        )
        # 1000 input tokens at $1/1k + 200 output tokens at $5/1k = 1.0 + 1.0 = 2.0
        assert q.cost_for_tokens(1000, 200) == pytest.approx(2.0)

    def test_cost_with_fx(self):
        q = PriceQuote(
            vendor="deepseek",
            model="deepseek-chat",
            input_per_1k=10.0,
            output_per_1k=20.0,
            currency="CNY",
        )
        # fx CNY->USD = 0.14 means 10 CNY * 0.14 = 1.4 USD
        # 1000 input + 1000 output = 10 CNY + 20 CNY = 30 CNY = 4.2 USD
        cost_usd = q.cost_for_tokens(1000, 1000, fx_to_usd=0.14)
        assert cost_usd == pytest.approx(30.0 * 0.14)

    def test_cost_default_fx_is_one(self):
        q = PriceQuote(vendor="x", model="y", input_per_1k=2.0, output_per_1k=3.0)
        assert q.cost_for_tokens(1000, 1000) == pytest.approx(5.0)


class TestPricingSample:
    def test_basic_sample(self):
        s = PricingSample(
            vendor="anthropic",
            model="claude-haiku-4-5",
            input_text="hello",
            output_text="world",
            reported_input_tokens=10,
            reported_output_tokens=8,
        )
        assert s.input_chars == 5
        assert s.output_chars == 5

    def test_input_chars_uses_text_length(self):
        s = PricingSample(
            vendor="x", model="y",
            input_text="你好世界",  # 4 chars
            output_text="hi",
            reported_input_tokens=4,
            reported_output_tokens=2,
        )
        assert s.input_chars == 4
        assert s.output_chars == 2

    def test_optional_balance_fields(self):
        s = PricingSample(
            vendor="x", model="y",
            input_text="a", output_text="b",
            reported_input_tokens=1, reported_output_tokens=1,
            balance_before_usd=10.0,
            balance_after_usd=9.5,
            reported_cost_usd=0.5,
        )
        assert s.balance_delta_usd == pytest.approx(-0.5)
        assert s.reported_cost_usd == pytest.approx(0.5)

    def test_balance_delta_none_when_missing(self):
        s = PricingSample(
            vendor="x", model="y",
            input_text="a", output_text="b",
            reported_input_tokens=1, reported_output_tokens=1,
        )
        assert s.balance_delta_usd is None


class TestPricingVerdict:
    def test_compliant_verdict(self):
        v = PricingVerdict(
            severity=Severity.PRICING_COMPLIANT,
            evaluator="L0_character_ratio",
            drift_pct=0.0,
            details={"expected": 100, "reported": 100},
        )
        assert v.is_pass() is True
        assert v.exit_code() == 0
        assert v.severity is Severity.PRICING_COMPLIANT

    def test_warn_verdict_exit_zero_with_log(self):
        v = PricingVerdict(
            severity=Severity.TOKEN_DRIFT_WARN,
            evaluator="L0_character_ratio",
            drift_pct=35.0,
        )
        assert v.is_pass() is False
        assert v.exit_code() == 0  # PRD §6.5.1: warn=0+log

    def test_critical_verdict_exit_one(self):
        v = PricingVerdict(
            severity=Severity.TOKEN_DRIFT_CRITICAL,
            evaluator="L0_character_ratio",
            drift_pct=200.0,
        )
        assert v.exit_code() == 1  # PRD §6.5.1: critical=1

    def test_balance_drift_high_exit_one(self):
        v = PricingVerdict(
            severity=Severity.BALANCE_DRIFT_HIGH,
            evaluator="balance_observer",
            drift_pct=30.0,
        )
        assert v.exit_code() == 1

    def test_inconclusive_exit_zero(self):
        v = PricingVerdict(
            severity=Severity.INCONCLUSIVE_BALANCE,
            evaluator="balance_observer",
        )
        assert v.exit_code() == 0  # PRD §6.5.1: inconclusive=0


class TestPricingEvaluatorProtocol:
    def test_l0_implements_protocol(self):
        from api_relay_audit.pricing.evaluators.l0_character_ratio import (
            L0CharacterRatioEvaluator,
        )
        evaluator: PricingEvaluator = L0CharacterRatioEvaluator()
        assert hasattr(evaluator, "name")
        assert hasattr(evaluator, "evaluate")
        assert isinstance(evaluator.name, str)
