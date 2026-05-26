"""Tests for the ``audit pricing`` CLI integration (Story TES-137 / S4-D).

PRD §6.1.1 requires the ``audit pricing`` subcommand:

  $ audit pricing --config audit.yaml --provider anthropic \\
                  --model claude-opus-4-7 --run-id <id>

Behaviour locked by these tests:

  * ``--verbose`` is a tri-state — implemented as count-based ``-v`` /
    ``-vv``: 0 = quiet (severity line only), 1 = normal (severity + per-
    layer summary), 2 = verbose (full evidence JSON).
  * Exit code follows PRD §6.5.1 matrix exactly: 0 for warn /
    inconclusive / compliant, 1 for critical / unit_price /
    balance_high.
  * The pricing CLI writes the JSON artifact to
    ``reports/pricing/<provider>/<run-id>.json`` and prints the path to
    stdout in normal / verbose modes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from api_relay_audit.pricing import pricing_cli
from api_relay_audit.pricing.evaluators.base import (
    PricingSample,
    PricingVerdict,
    Severity,
)
from api_relay_audit.pricing.pipeline import aggregate


def _evaluate_critical(_sample):
    """Stand-in evaluator that always returns a critical verdict."""
    return aggregate([
        PricingVerdict(
            severity=Severity.TOKEN_DRIFT_CRITICAL,
            evaluator="L0_character_ratio",
            drift_pct=120.0,
            details={"token_drift_pct": 120.0,
                     "expected_total_tokens": 100,
                     "reported_total_tokens": 220},
        ),
    ])


def _evaluate_warn(_sample):
    return aggregate([
        PricingVerdict(
            severity=Severity.TOKEN_DRIFT_WARN,
            evaluator="L0_character_ratio",
            drift_pct=35.0,
            details={"token_drift_pct": 35.0},
        ),
    ])


def _evaluate_compliant(_sample):
    return aggregate([
        PricingVerdict(
            severity=Severity.PRICING_COMPLIANT,
            evaluator="L0_character_ratio",
            drift_pct=4.0,
            details={"token_drift_pct": 4.0},
        ),
    ])


SAMPLE = PricingSample(
    vendor="anthropic", model="claude-opus-4-7",
    input_text="hello", output_text="world",
    reported_input_tokens=10, reported_output_tokens=10,
)


class TestExitCodeMatrix:
    def test_critical_returns_1(self, tmp_path):
        rc = pricing_cli.run(
            samples=[SAMPLE],
            evaluate_fn=_evaluate_critical,
            run_id="t",
            provider="anthropic",
            model="claude-opus-4-7",
            artifact_dir=tmp_path,
        )
        assert rc == 1

    def test_warn_returns_0(self, tmp_path):
        rc = pricing_cli.run(
            samples=[SAMPLE],
            evaluate_fn=_evaluate_warn,
            run_id="t",
            provider="anthropic",
            model="claude-opus-4-7",
            artifact_dir=tmp_path,
        )
        assert rc == 0

    def test_compliant_returns_0(self, tmp_path):
        rc = pricing_cli.run(
            samples=[SAMPLE],
            evaluate_fn=_evaluate_compliant,
            run_id="t",
            provider="anthropic",
            model="claude-opus-4-7",
            artifact_dir=tmp_path,
        )
        assert rc == 0

    def test_no_samples_returns_0(self, tmp_path):
        rc = pricing_cli.run(
            samples=[],
            evaluate_fn=_evaluate_compliant,
            run_id="t",
            provider="anthropic",
            model="claude-opus-4-7",
            artifact_dir=tmp_path,
        )
        assert rc == 0


class TestArtifactPath:
    def test_artifact_written_to_provider_run_id(self, tmp_path):
        pricing_cli.run(
            samples=[SAMPLE],
            evaluate_fn=_evaluate_compliant,
            run_id="abc-123",
            provider="anthropic",
            model="claude-opus-4-7",
            artifact_dir=tmp_path,
        )
        expected = tmp_path / "anthropic" / "abc-123.json"
        assert expected.exists()
        payload = json.loads(expected.read_text())
        assert payload["run_id"] == "abc-123"
        assert payload["provider"] == "anthropic"


class TestVerboseTriState:
    def _capture(self, level, evaluate_fn, tmp_path):
        from io import StringIO
        buf = StringIO()
        pricing_cli.run(
            samples=[SAMPLE],
            evaluate_fn=evaluate_fn,
            run_id="t",
            provider="anthropic",
            model="claude-opus-4-7",
            artifact_dir=tmp_path,
            verbosity=level,
            stdout=buf,
        )
        return buf.getvalue()

    def test_quiet_only_severity_line(self, tmp_path):
        out = self._capture(0, _evaluate_critical, tmp_path)
        # quiet = one line, the severity, nothing else
        assert "token_drift_critical" in out
        assert "expected_total_tokens" not in out  # no evidence in quiet
        assert "L0_character_ratio" not in out  # no per-layer summary

    def test_normal_includes_layer_summary(self, tmp_path):
        out = self._capture(1, _evaluate_critical, tmp_path)
        assert "token_drift_critical" in out
        assert "L0_character_ratio" in out
        # Normal does not dump full evidence JSON
        assert "expected_total_tokens" not in out

    def test_verbose_dumps_full_evidence(self, tmp_path):
        out = self._capture(2, _evaluate_critical, tmp_path)
        assert "token_drift_critical" in out
        assert "L0_character_ratio" in out
        assert "expected_total_tokens" in out  # full evidence

    def test_verbose_includes_artifact_path(self, tmp_path):
        out = self._capture(2, _evaluate_compliant, tmp_path)
        assert "anthropic" in out and ".json" in out


class TestArgparseIntegration:
    def test_parse_verbose_count(self):
        ns = pricing_cli.parse_args([
            "--config", "audit.yaml", "--provider", "anthropic",
            "--model", "claude-opus-4-7", "--run-id", "t",
            "-vv",
        ])
        assert ns.verbosity == 2

    def test_parse_quiet_default(self):
        ns = pricing_cli.parse_args([
            "--config", "audit.yaml", "--provider", "anthropic",
            "--model", "claude-opus-4-7", "--run-id", "t",
        ])
        assert ns.verbosity == 0

    def test_parse_normal(self):
        ns = pricing_cli.parse_args([
            "--config", "audit.yaml", "--provider", "anthropic",
            "--model", "claude-opus-4-7", "--run-id", "t",
            "-v",
        ])
        assert ns.verbosity == 1
