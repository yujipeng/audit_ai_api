"""Tests for L0 character-ratio evaluator (PRD §5.1 L1 ±20-30% equivalence,
DoD 三类 fixture verdict)."""

from __future__ import annotations

from pathlib import Path

import pytest

from api_relay_audit.pricing import PricingSample, Severity
from api_relay_audit.pricing.evaluators.l0_character_ratio import (
    L0CharacterRatioEvaluator,
    estimate_tokens_by_chars,
    load_ratio_table,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pricing"


# ---------------------------------------------------------------------------
# Ratio table
# ---------------------------------------------------------------------------

class TestRatioTable:
    def test_load_default_ratio_table_shape(self):
        ratios = load_ratio_table()
        assert isinstance(ratios, dict)
        # Must contain at least anthropic, openai, deepseek
        assert "anthropic" in ratios
        assert "openai" in ratios
        # Each vendor entry is a dict of model -> chars-per-token
        anth = ratios["anthropic"]
        assert isinstance(anth, dict)
        assert any("claude" in k for k in anth)

    def test_estimate_tokens_zh_uses_zh_ratio(self):
        # If a vendor sets chars_per_token differently for zh vs en, both
        # paths should be exercised. With 1.5 chars/token, 12 zh chars → 8
        ratios = {"x": {"y": {"zh": 1.5, "en": 4.0, "default": 4.0}}}
        # 12 chars of zh
        n = estimate_tokens_by_chars("一二三四五六七八九十十一", "x", "y",
                                     ratios=ratios)
        assert n == pytest.approx(8.0, rel=0.01)

    def test_estimate_tokens_en_uses_en_ratio(self):
        ratios = {"x": {"y": {"zh": 1.5, "en": 4.0, "default": 4.0}}}
        # 16 ASCII chars / 4 = 4 tokens
        n = estimate_tokens_by_chars("hello world abcd", "x", "y",
                                     ratios=ratios)
        assert n == pytest.approx(4.0, rel=0.01)

    def test_unknown_model_falls_back_to_vendor_default(self):
        ratios = {"x": {"_default": {"default": 4.0}}}
        n = estimate_tokens_by_chars("abcdefgh", "x", "unknown-model",
                                     ratios=ratios)
        assert n == pytest.approx(2.0, rel=0.01)

    def test_unknown_vendor_falls_back_to_global(self):
        ratios = {"_default": {"_default": {"default": 4.0}}}
        n = estimate_tokens_by_chars("abcdefgh", "unknown", "unknown",
                                     ratios=ratios)
        assert n == pytest.approx(2.0, rel=0.01)


# ---------------------------------------------------------------------------
# Three required verdicts (DoD)
# ---------------------------------------------------------------------------

class TestL0Verdicts:
    def test_pricing_compliant_drift_zero(self):
        # English input, gpt-4o, 4 chars/token. 80 chars → ~20 tokens.
        # Reported tokens match exactly → 0% drift.
        ev = L0CharacterRatioEvaluator()
        sample = PricingSample(
            vendor="openai", model="gpt-4o-mini",
            input_text="a" * 80,
            output_text="b" * 40,
            reported_input_tokens=20,
            reported_output_tokens=10,
        )
        verdict = ev.evaluate(sample)
        assert verdict.severity is Severity.PRICING_COMPLIANT
        assert abs(verdict.drift_pct) < 5.0

    def test_token_drift_critical_200_pct(self):
        ev = L0CharacterRatioEvaluator()
        sample = PricingSample(
            vendor="openai", model="gpt-4o-mini",
            input_text="a" * 80,
            output_text="b" * 40,
            # Expected ~30 tokens; relay reports 90 → 200% drift
            reported_input_tokens=60,
            reported_output_tokens=30,
        )
        verdict = ev.evaluate(sample)
        assert verdict.severity is Severity.TOKEN_DRIFT_CRITICAL
        assert verdict.drift_pct >= 100.0

    def test_token_drift_warn_band(self):
        # ~35% drift → warn band (between 30% and 100%)
        ev = L0CharacterRatioEvaluator()
        sample = PricingSample(
            vendor="openai", model="gpt-4o-mini",
            input_text="a" * 80,
            output_text="b" * 40,
            reported_input_tokens=27,  # expected 20 → +35%
            reported_output_tokens=14,  # expected 10 → +40%
        )
        verdict = ev.evaluate(sample)
        assert verdict.severity is Severity.TOKEN_DRIFT_WARN

    def test_balance_drift_high_30_pct(self):
        # cost_local computed from prices.yaml at 1.0 / 1k input + 5.0 / 1k
        # output for fixture model. Reported balance delta diverges 30%
        # from local estimate.
        ev = L0CharacterRatioEvaluator()
        sample = PricingSample(
            vendor="openai", model="gpt-4o-mini",
            input_text="a" * 80,
            output_text="b" * 40,
            reported_input_tokens=20,
            reported_output_tokens=10,
            balance_before_usd=10.0,
            balance_after_usd=9.93,  # delta -0.07; expected ~-0.05 → +40% balance drift
            reported_cost_usd=0.05,
        )
        verdict = ev.evaluate(sample)
        # Token drift compliant, but balance drift > 25% threshold
        assert verdict.severity in {
            Severity.BALANCE_DRIFT_HIGH,
            # In future stories balance drift may be split out; for L0 it's
            # carried as a secondary detail. Accept the strongest finding.
        }
        assert verdict.drift_pct >= 25.0


class TestL0PrecisionFixture:
    """Synthetic 30-sample precision check (PRD §5.1 L1 ±20-30%)."""

    def test_p50_under_30_pct_p95_under_50_pct(self):
        ev = L0CharacterRatioEvaluator()
        # 30 samples of mixed lengths, vendor-honest token counts (= ratio
        # table prediction). With honest reporting, drift should be
        # near-zero across all samples.
        drifts: list[float] = []
        for n_chars in (40, 80, 120, 200, 400, 80, 80, 80, 80, 80,
                        160, 160, 160, 160, 160, 240, 240, 240, 240, 240,
                        320, 320, 320, 320, 320, 60, 60, 60, 60, 60):
            sample = PricingSample(
                vendor="openai", model="gpt-4o-mini",
                input_text="a" * n_chars,
                output_text="",
                reported_input_tokens=n_chars // 4,
                reported_output_tokens=0,
            )
            v = ev.evaluate(sample)
            drifts.append(abs(v.drift_pct))
        drifts.sort()
        n = len(drifts)
        p50 = drifts[n // 2]
        p95 = drifts[int(n * 0.95)]
        assert p50 <= 30.0, f"p50 drift {p50}% exceeds 30% threshold"
        assert p95 <= 50.0, f"p95 drift {p95}% exceeds 50% threshold"
