"""Tests for prices.yaml + ratio_table.yaml fixture content + FX folding."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from api_relay_audit.pricing import PriceQuote, load_prices


PRICING_DIR = Path(__file__).parent.parent / "api_relay_audit" / "pricing"


class TestPricesYaml:
    def test_prices_yaml_exists(self):
        assert (PRICING_DIR / "prices.yaml").exists()

    def test_required_anthropic_models_present(self):
        prices = load_prices()
        assert "anthropic" in prices
        for model in ("claude-haiku-4-5", "claude-sonnet-4-6",
                      "claude-opus-4-7"):
            assert model in prices["anthropic"], f"missing {model}"

    def test_required_openai_models_present(self):
        prices = load_prices()
        assert "openai" in prices
        for model in ("gpt-4o-mini", "gpt-4o"):
            assert model in prices["openai"], f"missing {model}"

    def test_deepseek_entry_present(self):
        prices = load_prices()
        assert "deepseek" in prices
        assert len(prices["deepseek"]) >= 1

    def test_load_prices_returns_quote_objects(self):
        prices = load_prices()
        haiku = prices["anthropic"]["claude-haiku-4-5"]
        assert isinstance(haiku, PriceQuote)
        assert haiku.input_per_1k > 0
        assert haiku.output_per_1k > 0
        assert haiku.currency in ("USD", "CNY", "EUR")


class TestRatioTableYaml:
    def test_ratio_table_yaml_exists(self):
        assert (PRICING_DIR / "ratio_table.yaml").exists()

    def test_ratio_table_loads_as_dict(self):
        with open(PRICING_DIR / "ratio_table.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "anthropic" in data or "_default" in data


class TestFxFolding:
    def test_cny_quote_folds_via_fx(self):
        # Synthetic CNY-priced model: 10 CNY per 1k input + 20 CNY per 1k output.
        # FX 0.14 → 30 CNY for 1k+1k tokens = $4.20.
        q = PriceQuote(
            vendor="deepseek", model="deepseek-chat",
            input_per_1k=10.0, output_per_1k=20.0,
            currency="CNY",
        )
        usd = q.cost_for_tokens(1000, 1000, fx_to_usd=0.14)
        assert usd == pytest.approx(4.2, rel=1e-3)
