"""S1 probe-core verdict aggregator (TES-154 / Story-6).

Implements the pure ``compute_verdict`` function over a fully-populated
``ProbeReport``. Rules are frozen in
``docs/design/2026-05-24-audit-ai-relay-design-probe-core.md`` §4.3 and
exercised by ``tests/test_probe_aggregator.py`` (12 combinations).

Rule order matters: fail rules short-circuit before warn rules, so a
report that trips both F1 and W1 lands at ``"fail"`` (corner case C2).
"""

from __future__ import annotations

from .types import ProbeReport


def compute_verdict(report: ProbeReport, *, model_required: bool = False) -> str:
    """Return ``"pass" | "warn" | "fail"`` for a probe report.

    ``model_required`` is set by the caller when the parent audit run was
    invoked with ``--model <name>``. In that mode an unreachable
    ``/v1/models`` endpoint is fatal (F4); without the flag, the same
    error degrades but does not fail.
    """
    if report.reachability.status == "error":
        return "fail"
    if report.auth_sniff.classification in ("broken", "permissive"):
        return "fail"
    if model_required and report.models_diff.status == "error":
        return "fail"

    if report.reachability.fallback_to_curl:
        return "warn"
    if report.models_diff.suspicious_aliases:
        return "warn"
    if report.models_diff.claimed_model_match == "alias":
        return "warn"
    if (
        report.rate_limit.compliance == "absent"
        and not report.rate_limit.probe_disabled
    ):
        return "warn"
    for result in (
        report.reachability,
        report.auth_sniff,
        report.models_diff,
        report.rate_limit,
    ):
        if result.status == "degraded":
            return "warn"

    return "pass"


__all__ = ["compute_verdict"]
