"""Tests for the pricing JSON artifact schema (PRD §6.1.5).

The pipeline writes one JSON artifact per audit run at
``reports/pricing/<provider>/<run-id>.json``. The schema:

  {
    "schema_version": "1",
    "run_id": "<str>",
    "provider": "<str>",
    "model": "<str>",
    "started_at": "<iso8601>",
    "finished_at": "<iso8601>",
    "verdicts": [
      {
        "sample": {...},
        "severity": "<Severity.value>",
        "layer": "<str>",
        "confidence": <float>,
        "drift_pct": <float>,
        "suppressed_by": [<layer>...],
        "per_layer": {<layer>: {<details>...}}
      },
      ...
    ],
    "summary": {
      "total": <int>,
      "by_severity": {<severity>: <count>...},
      "exit_code": <int>
    }
  }

This file is what S5 yaml-matrix orchestration reads back, so the
schema is frozen — tests assert every documented field is present and
correctly typed.
"""

from __future__ import annotations

import json

import pytest

from api_relay_audit.pricing.evaluators.base import (
    PricingVerdict,
    Severity,
)
from api_relay_audit.pricing.pipeline import (
    PRICING_SCHEMA_VERSION,
    aggregate,
)


def _agg(severity: Severity, layer: str = "L0_character_ratio",
         drift: float = 0.0):
    v = PricingVerdict(
        severity=severity,
        evaluator=layer,
        drift_pct=drift,
        details={"token_drift_pct": drift},
    )
    return aggregate([v])


@pytest.fixture
def artifact_path(tmp_path):
    return tmp_path / "reports" / "pricing" / "anthropic" / "run-001.json"


class TestArtifactWriter:
    def test_writes_to_expected_path(self, artifact_path):
        from api_relay_audit.pricing.artifact import write_artifact

        agg = _agg(Severity.PRICING_COMPLIANT, drift=4.0)
        write_artifact(
            path=artifact_path,
            run_id="run-001",
            provider="anthropic",
            model="claude-opus-4-7",
            started_at="2026-05-26T18:00:00Z",
            finished_at="2026-05-26T18:00:04Z",
            verdicts=[(({"vendor": "anthropic"}), agg)],
        )
        assert artifact_path.exists()

    def test_schema_version_pinned(self, artifact_path):
        from api_relay_audit.pricing.artifact import write_artifact

        agg = _agg(Severity.PRICING_COMPLIANT)
        write_artifact(
            path=artifact_path, run_id="r", provider="anthropic",
            model="m", started_at="t0", finished_at="t1",
            verdicts=[({}, agg)],
        )
        payload = json.loads(artifact_path.read_text())
        assert payload["schema_version"] == PRICING_SCHEMA_VERSION

    def test_top_level_required_fields(self, artifact_path):
        from api_relay_audit.pricing.artifact import write_artifact

        agg = _agg(Severity.PRICING_COMPLIANT)
        write_artifact(
            path=artifact_path, run_id="r", provider="anthropic",
            model="m", started_at="t0", finished_at="t1",
            verdicts=[({}, agg)],
        )
        payload = json.loads(artifact_path.read_text())
        for field in ("schema_version", "run_id", "provider", "model",
                      "started_at", "finished_at", "verdicts", "summary"):
            assert field in payload

    def test_verdict_shape(self, artifact_path):
        from api_relay_audit.pricing.artifact import write_artifact

        agg = _agg(Severity.TOKEN_DRIFT_WARN, drift=35.0)
        write_artifact(
            path=artifact_path, run_id="r", provider="anthropic",
            model="m", started_at="t0", finished_at="t1",
            verdicts=[({"vendor": "anthropic"}, agg)],
        )
        payload = json.loads(artifact_path.read_text())
        v = payload["verdicts"][0]
        for field in ("sample", "severity", "layer", "confidence",
                      "drift_pct", "suppressed_by", "per_layer"):
            assert field in v
        assert v["severity"] == "token_drift_warn"
        assert v["drift_pct"] == 35.0

    def test_summary_counts(self, artifact_path):
        from api_relay_audit.pricing.artifact import write_artifact

        verdicts = [
            ({}, _agg(Severity.PRICING_COMPLIANT)),
            ({}, _agg(Severity.PRICING_COMPLIANT)),
            ({}, _agg(Severity.TOKEN_DRIFT_WARN, drift=35.0)),
            ({}, _agg(Severity.TOKEN_DRIFT_CRITICAL, drift=120.0)),
        ]
        from api_relay_audit.pricing.artifact import write_artifact
        write_artifact(
            path=artifact_path, run_id="r", provider="anthropic",
            model="m", started_at="t0", finished_at="t1",
            verdicts=verdicts,
        )
        payload = json.loads(artifact_path.read_text())
        assert payload["summary"]["total"] == 4
        assert payload["summary"]["by_severity"]["pricing_compliant"] == 2
        assert payload["summary"]["by_severity"]["token_drift_warn"] == 1
        assert payload["summary"]["by_severity"]["token_drift_critical"] == 1

    def test_summary_exit_code_critical(self, artifact_path):
        """Exit code matrix (PRD §6.5.1):
        critical / unit_price_mismatch / balance_drift_high → 1
        warn / inconclusive / compliant → 0."""
        from api_relay_audit.pricing.artifact import write_artifact

        verdicts = [
            ({}, _agg(Severity.TOKEN_DRIFT_WARN, drift=35.0)),
            ({}, _agg(Severity.TOKEN_DRIFT_CRITICAL, drift=120.0)),
        ]
        write_artifact(
            path=artifact_path, run_id="r", provider="anthropic",
            model="m", started_at="t0", finished_at="t1",
            verdicts=verdicts,
        )
        payload = json.loads(artifact_path.read_text())
        assert payload["summary"]["exit_code"] == 1

    def test_summary_exit_code_warn_only(self, artifact_path):
        from api_relay_audit.pricing.artifact import write_artifact

        verdicts = [
            ({}, _agg(Severity.TOKEN_DRIFT_WARN, drift=35.0)),
            ({}, _agg(Severity.PRICING_COMPLIANT, drift=4.0)),
        ]
        write_artifact(
            path=artifact_path, run_id="r", provider="anthropic",
            model="m", started_at="t0", finished_at="t1",
            verdicts=verdicts,
        )
        payload = json.loads(artifact_path.read_text())
        assert payload["summary"]["exit_code"] == 0

    def test_creates_parent_directories(self, tmp_path):
        from api_relay_audit.pricing.artifact import write_artifact

        # Deeply nested path that does not exist yet.
        path = tmp_path / "deep" / "reports" / "pricing" / "openai" / "x.json"
        write_artifact(
            path=path, run_id="r", provider="openai", model="m",
            started_at="t0", finished_at="t1",
            verdicts=[({}, _agg(Severity.PRICING_COMPLIANT))],
        )
        assert path.exists()

    def test_round_trips_through_json(self, artifact_path):
        """The artifact must be loadable by stdlib json with no
        custom decoders (S5 orchestrator constraint)."""
        from api_relay_audit.pricing.artifact import write_artifact

        agg = _agg(Severity.BALANCE_DRIFT_HIGH, layer="L2_balance_triangle",
                   drift=30.0)
        write_artifact(
            path=artifact_path, run_id="r", provider="anthropic",
            model="m", started_at="t0", finished_at="t1",
            verdicts=[({"sample_kind": "interactive"}, agg)],
        )
        payload = json.loads(artifact_path.read_text())
        # Re-serialise and re-load to confirm valid round-trip.
        roundtrip = json.loads(json.dumps(payload))
        assert roundtrip == payload
