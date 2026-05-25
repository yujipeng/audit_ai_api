"""LeakEvaluator — wraps run_error_leakage_test (AC-2 adjacent).

Severity → verdict mapping (design §4.6 align):
  critical / high → injected
  medium          → suspicious
  none + no run   → unknown / failed
PRD AC-R2: hits[].snippet must redact api_key full + first-8 prefix.
"""
from __future__ import annotations

import time

from api_relay_audit.error_leakage import run_error_leakage_test
from api_relay_audit.evaluator.base import (
    Evaluator,
    EvaluatorResult,
    ProbeContext,
)
from api_relay_audit.evaluator.dimensions._common import redact_snippet


_SEVERITY_TO_VERDICT = {
    "critical": "injected",
    "high": "injected",
    "medium": "suspicious",
    "none": "clean",
}


class LeakEvaluator(Evaluator):
    dimension = "leak"
    default_in_profile = True

    def run(self, ctx: ProbeContext) -> EvaluatorResult:
        t0 = time.perf_counter()
        results, severity, inconclusive = run_error_leakage_test(
            ctx.client, api_key=ctx.api_key, base_url=ctx.base_url, aggressive=False,
        )
        rounds_total = len(results)
        rounds_ok = sum(1 for r in results if r.get("error") is None and r.get("status") != 0)

        hits: list[dict] = []
        for r in results:
            for h in r.get("hits", []):
                hits.append({
                    "trigger": r.get("trigger"),
                    "kind": h.get("kind"),
                    "severity": h.get("severity"),
                    "snippet": redact_snippet(h.get("snippet", ""), ctx.api_key),
                })

        if inconclusive:
            verdict = "unknown"
            reason = "all error probes inconclusive (no error surface or all transport errors)"
        elif rounds_ok == 0:
            verdict = "failed"
            reason = "no error probe got a real HTTP response"
        else:
            verdict = _SEVERITY_TO_VERDICT.get(severity, "unknown")
            reason = f"highest-severity leak: {severity}"

        return EvaluatorResult(
            dimension=self.dimension,
            verdict=verdict,
            verdict_reason=reason,
            rounds_total=rounds_total,
            rounds_ok=rounds_ok,
            hits=hits,
            duration_s=time.perf_counter() - t0,
        )
