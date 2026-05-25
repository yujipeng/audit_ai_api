"""ToolEvaluator — wraps run_tool_substitution_test (AC-1.a)."""
from __future__ import annotations

import time

from api_relay_audit.evaluator.base import (
    Evaluator,
    EvaluatorResult,
    ProbeContext,
)
from api_relay_audit.evaluator.dimensions._common import redact_snippet
from api_relay_audit.tool_substitution import run_tool_substitution_test


class ToolEvaluator(Evaluator):
    dimension = "tool"
    default_in_profile = True

    def run(self, ctx: ProbeContext) -> EvaluatorResult:
        t0 = time.perf_counter()
        results, detected, inconclusive = run_tool_substitution_test(
            ctx.client, sleep=ctx.sleep_between
        )
        rounds_total = len(results)
        rounds_ok = sum(1 for r in results if r.get("verdict") != "error")
        hits: list[dict] = []
        for r in results:
            if r.get("verdict") == "substituted":
                hits.append({
                    "manager": r.get("manager"),
                    "expected": r.get("expected"),
                    "received": redact_snippet(r.get("received", ""), ctx.api_key),
                    "severity": "high",
                })

        if inconclusive:
            verdict = "unknown"
            reason = "all tool-substitution probes errored (relay blocks plaintext echo)"
        elif detected:
            verdict = "injected"
            reason = f"tool-call substitution detected on {len(hits)} probe(s)"
        elif rounds_ok == 0:
            verdict = "failed"
            reason = "no successful tool-substitution probe"
        else:
            verdict = "clean"
            reason = f"{rounds_ok}/{rounds_total} probes returned exact echo"

        return EvaluatorResult(
            dimension=self.dimension,
            verdict=verdict,
            verdict_reason=reason,
            rounds_total=rounds_total,
            rounds_ok=rounds_ok,
            hits=hits,
            duration_s=time.perf_counter() - t0,
        )
