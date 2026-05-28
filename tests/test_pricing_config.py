"""Tests for audit.yaml schema loading + cost_cap_usd validation
(PRD §4.1, §6.1.2, §6.3.3 billing safety invariants)."""

from __future__ import annotations

from pathlib import Path

import pytest

from api_relay_audit.pricing.config import (
    BillingSafetyError,
    PricingConfig,
    PricingConfigError,
    enforce_billing_safety,
    load_audit_config,
)


def write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "audit.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Schema acceptance (PRD §4.1)
# ---------------------------------------------------------------------------

class TestSchemaAcceptance:
    def test_full_schema_loads(self, tmp_path):
        p = write_yaml(tmp_path, """
pricing:
  cost_cap_usd: 1.5
  max_runs: 50
  max_response_bytes: 524288
  evaluators:
    - l0_character_ratio
  prices_file: prices.yaml
  ratio_table_file: ratio_table.yaml
  fx:
    CNY: 0.14
""")
        cfg = load_audit_config(str(p))
        assert isinstance(cfg.pricing, PricingConfig)
        assert cfg.pricing.cost_cap_usd == 1.5
        assert cfg.pricing.max_runs == 50
        assert cfg.pricing.max_response_bytes == 524288
        assert cfg.pricing.evaluators == ["l0_character_ratio"]
        assert cfg.pricing.fx == {"CNY": 0.14}

    def test_cost_cap_required_missing_block(self, tmp_path):
        p = write_yaml(tmp_path, "pricing:\n  evaluators: [l0_character_ratio]\n")
        with pytest.raises(PricingConfigError) as excinfo:
            load_audit_config(str(p))
        assert "pricing.cost_cap_usd is required" in str(excinfo.value)

    def test_cost_cap_required_no_pricing_block(self, tmp_path):
        p = write_yaml(tmp_path, "test:\n  rounds: 5\n")
        with pytest.raises(PricingConfigError) as excinfo:
            load_audit_config(str(p))
        assert "pricing.cost_cap_usd is required" in str(excinfo.value)

    def test_cost_cap_must_be_positive(self, tmp_path):
        p = write_yaml(tmp_path, "pricing:\n  cost_cap_usd: 0\n")
        with pytest.raises(PricingConfigError):
            load_audit_config(str(p))

    def test_cost_cap_must_be_numeric(self, tmp_path):
        p = write_yaml(tmp_path, "pricing:\n  cost_cap_usd: 'one dollar'\n")
        with pytest.raises(PricingConfigError):
            load_audit_config(str(p))

    def test_defaults_applied_for_optional_fields(self, tmp_path):
        p = write_yaml(tmp_path, "pricing:\n  cost_cap_usd: 0.5\n")
        cfg = load_audit_config(str(p))
        # max_runs default 100, max_response_bytes default 1MB
        assert cfg.pricing.max_runs == 100
        assert cfg.pricing.max_response_bytes == 1024 * 1024


# ---------------------------------------------------------------------------
# Billing safety invariants (PRD §6.3.3)
# ---------------------------------------------------------------------------

class TestBillingSafety:
    def test_cost_cap_exceeded_aborts(self):
        cfg = PricingConfig(cost_cap_usd=1.0)
        with pytest.raises(BillingSafetyError) as excinfo:
            enforce_billing_safety(cfg, accumulated_cost_usd=1.01,
                                   runs_so_far=1, response_bytes=100)
        assert "cost_cap_usd" in str(excinfo.value)

    def test_runs_over_cap_aborts(self):
        cfg = PricingConfig(cost_cap_usd=10.0, max_runs=100)
        with pytest.raises(BillingSafetyError) as excinfo:
            enforce_billing_safety(cfg, accumulated_cost_usd=0.0,
                                   runs_so_far=101, response_bytes=100)
        assert "max_runs" in str(excinfo.value)

    def test_response_truncated_above_max(self):
        cfg = PricingConfig(cost_cap_usd=10.0, max_response_bytes=1024 * 1024)
        # 1MB+1 byte should signal a truncate request, not an abort.
        result = enforce_billing_safety(
            cfg, accumulated_cost_usd=0.0,
            runs_so_far=1, response_bytes=1024 * 1024 + 1,
        )
        assert result.truncated is True
        assert result.truncated_at == 1024 * 1024

    def test_within_limits_does_not_raise(self):
        cfg = PricingConfig(cost_cap_usd=1.0)
        result = enforce_billing_safety(cfg, accumulated_cost_usd=0.5,
                                        runs_so_far=10, response_bytes=2048)
        assert result.truncated is False
        assert result.truncated_at is None
