"""Story-6 (TES-154 / S1-aggregator) verdict aggregation regression tests.

Acceptance mapping:

- design §4.3 verdict rules — 12 combinations frozen here:
    fail rules (4):
        F1  reachability.status == "error"
        F2  auth_sniff.classification == "broken"
        F3  auth_sniff.classification == "permissive"
        F4  models_diff.status == "error" AND aggregator opts include model_required=True

    warn rules (5):
        W1  reachability.fallback_to_curl == True
        W2  models_diff.suspicious_aliases != []
        W3  models_diff.claimed_model_match == "alias"
        W4  rate_limit.compliance == "absent"
        W5  any ProbeResult.status == "degraded"

    corner cases (3):
        C1  pass — all status="ok" and no warn condition
        C2  fail wins over warn — F1 + W1 still fail
        C3  rate_limit.probe_disabled=True (compliance="unknown") MUST NOT trip W4 warn

These tests exercise ONLY ``api_relay_audit.probe.aggregator.compute_verdict``;
the two-stage parallel ``run_probes`` body is covered separately in
``tests/test_probe_integration.py``.
"""

from __future__ import annotations

import pytest

from api_relay_audit.probe.aggregator import compute_verdict
from api_relay_audit.probe.types import (
    AuthSniffResult,
    ModelsDiffResult,
    ProbeError,
    ProbeReport,
    RateLimitResult,
    ReachabilityResult,
)


# ---------------------------------------------------------------------------
# Helpers — build a baseline "all green" ProbeReport so each test only mutates
# the field under exam. compute_verdict() must be a pure function over the
# report; baseline below is the §4.3 "pass" fixture.
# ---------------------------------------------------------------------------


def _ok_reachability() -> ReachabilityResult:
    return ReachabilityResult(
        status="ok",
        tcp_ok=True,
        tls_ok=True,
        dns_resolves=True,
        http_status_root=200,
        latency_ms=42,
        fallback_to_curl=False,
    )


def _ok_auth_sniff() -> AuthSniffResult:
    return AuthSniffResult(
        status="ok",
        accepted_schemes=["bearer"],
        envelope_401="openai",
        envelope_403="openai",
        key_position="Authorization",
        classification="strict",
    )


def _ok_models_diff() -> ModelsDiffResult:
    return ModelsDiffResult(
        status="ok",
        declared=["gpt-4o", "gpt-4-turbo"],
        declared_count=2,
        official_reference=["gpt-4o", "gpt-4-turbo"],
        catalog_version="2026-05-24",
        extra_in_relay=[],
        missing_in_relay=[],
        suspicious_aliases=[],
        claimed_model_match="exact",
        vendor_breakdown={"openai": 2},
    )


def _ok_rate_limit() -> RateLimitResult:
    return RateLimitResult(
        status="ok",
        rpm_observed=60,
        headers_seen=["x-ratelimit-remaining-requests"],
        envelope_429="openai",
        retry_after_pattern="seconds",
        burst_window_s=60,
        triggered_429=False,
        samples_to_429=None,
        compliance="openai",
        probe_disabled=False,
    )


def _ok_report() -> ProbeReport:
    return ProbeReport(
        schema_version="1.0",
        generated_at="2026-05-26T22:00:00Z",
        input_base_url="https://relay.example.com/v1",
        input_key_fingerprint="abc12345",
        input_vendor_hint="openai",
        verdict="pass",  # baseline; tests overwrite per §4.3
        reachability=_ok_reachability(),
        auth_sniff=_ok_auth_sniff(),
        models_diff=_ok_models_diff(),
        rate_limit=_ok_rate_limit(),
        infra_hint=None,
        total_http_calls=20,
    )


# ---------------------------------------------------------------------------
# 4 FAIL rules
# ---------------------------------------------------------------------------


def test_fail_when_reachability_error_F1():
    report = _ok_report()
    report.reachability = ReachabilityResult(
        status="error",
        tcp_ok=False,
        tls_ok=False,
        dns_resolves=False,
        signals=["reachability:dns-failed"],
        error=ProbeError(code="dns_resolution_failed", message="lookup failed"),
    )
    assert compute_verdict(report) == "fail"


def test_fail_when_auth_classification_broken_F2():
    report = _ok_report()
    report.auth_sniff = AuthSniffResult(
        status="ok",
        classification="broken",
    )
    assert compute_verdict(report) == "fail"


def test_fail_when_auth_classification_permissive_F3():
    report = _ok_report()
    report.auth_sniff = AuthSniffResult(
        status="ok",
        classification="permissive",
        signals=["auth:permissive", "auth:red_flag_no_real_authentication"],
    )
    assert compute_verdict(report) == "fail"


def test_fail_when_models_diff_error_and_model_required_F4():
    report = _ok_report()
    report.models_diff = ModelsDiffResult(
        status="error",
        error=ProbeError(code="models_list_unavailable", message="503"),
    )
    assert compute_verdict(report, model_required=True) == "fail"


def test_NOT_fail_when_models_diff_error_but_model_NOT_required():
    """F4 only triggers when audit run pinned a specific --model. Without
    that pin, models_diff.error is at most a degraded signal."""
    report = _ok_report()
    report.models_diff = ModelsDiffResult(
        status="error",
        error=ProbeError(code="models_list_unavailable", message="503"),
    )
    assert compute_verdict(report, model_required=False) != "fail"


# ---------------------------------------------------------------------------
# 5 WARN rules
# ---------------------------------------------------------------------------


def test_warn_when_reachability_fallback_to_curl_W1():
    report = _ok_report()
    report.reachability = ReachabilityResult(
        status="ok",
        tcp_ok=True,
        tls_ok=False,
        fallback_to_curl=True,
        http_status_root=200,
        signals=["reachability:tls-fallback-curl"],
    )
    assert compute_verdict(report) == "warn"


def test_warn_when_models_diff_suspicious_aliases_W2():
    report = _ok_report()
    report.models_diff = ModelsDiffResult(
        status="ok",
        suspicious_aliases=["gpt-4-omni-pro-max"],
        claimed_model_match="exact",
    )
    assert compute_verdict(report) == "warn"


def test_warn_when_claimed_model_match_alias_W3():
    report = _ok_report()
    report.models_diff = ModelsDiffResult(
        status="ok",
        claimed_model_match="alias",
    )
    assert compute_verdict(report) == "warn"


def test_warn_when_rate_limit_compliance_absent_W4():
    report = _ok_report()
    report.rate_limit = RateLimitResult(
        status="ok",
        compliance="absent",
    )
    assert compute_verdict(report) == "warn"


def test_warn_when_any_probe_status_degraded_W5():
    report = _ok_report()
    report.rate_limit = RateLimitResult(
        status="degraded",
        compliance="openai",
    )
    assert compute_verdict(report) == "warn"


# ---------------------------------------------------------------------------
# 3 CORNER cases
# ---------------------------------------------------------------------------


def test_pass_when_all_ok_and_no_warn_signals_C1():
    report = _ok_report()
    assert compute_verdict(report) == "pass"


def test_fail_wins_over_warn_C2():
    """F1 (fail) co-occurring with W1 (warn) — fail must win per §4.3
    rule precedence (fail rules listed first, evaluated as short-circuit)."""
    report = _ok_report()
    report.reachability = ReachabilityResult(
        status="error",
        fallback_to_curl=True,  # would otherwise trip W1
        signals=["reachability:tls-fallback-curl"],
        error=ProbeError(code="tcp_refused", message="ECONNREFUSED"),
    )
    assert compute_verdict(report) == "fail"


def test_rate_limit_probe_disabled_does_NOT_warn_C3():
    """When P4 is skipped via ``--no-rate-limit-probe`` (PRD §6.2 A7),
    compliance stays at default ``"unknown"`` and probe_disabled=True.
    This MUST NOT be confused with W4 (compliance == ``"absent"``)."""
    report = _ok_report()
    report.rate_limit = RateLimitResult(
        status="ok",
        compliance="unknown",
        probe_disabled=True,
    )
    assert compute_verdict(report) == "pass"
