"""Web3InjectionEvaluator — wrap ``run_web3_injection_probes`` as an Evaluator.

design references:
- §4.1 公共 API：Web3 维度 default_in_profile=False，include 触发
- §7 R4：default profile 不能误注入 web3（CLI 默认 6 维报告）
- §3.1 fail-open：wrapper 不吞异常，由 pipeline._safe_run 包成 verdict='failed'

底层 ``run_web3_injection_probes`` 返回 ``(results, overall, inconclusive)`` 三元组，本
wrapper 仅做契约转换：

- ``overall == "clean"``        → ``verdict = "clean"``
- ``overall == "anomaly"``      → ``verdict = "injected"``
- ``overall == "inconclusive"`` → ``verdict = "unknown"``
"""
from __future__ import annotations

import time

from ..base import Evaluator, EvaluatorResult, ProbeContext
from ...web3 import run_web3_injection_probes


_OVERALL_TO_VERDICT = {
    "clean": "clean",
    "anomaly": "injected",
    "inconclusive": "unknown",
}


class Web3InjectionEvaluator(Evaluator):
    """Thin adapter over :func:`api_relay_audit.web3.run_web3_injection_probes`.

    The underlying detector fires 3 SlowMist-derived probes (ETH transfer
    guidance / sign-tx refusal / private-key leak refusal). Network calls go
    through ``ctx.client.call``; the wrapper does NOT swallow exceptions —
    that contract belongs to ``EvaluatorPipeline._safe_run``.
    """

    dimension = "web3_injection"
    default_in_profile = False

    def run(self, ctx: ProbeContext) -> EvaluatorResult:
        t0 = time.perf_counter()
        results, overall, _inconclusive = run_web3_injection_probes(
            ctx.client, sleep=ctx.sleep_between
        )
        duration = time.perf_counter() - t0

        rounds_ok = sum(1 for r in results if not r.error)
        verdict = _OVERALL_TO_VERDICT.get(overall, "unknown")
        hits = [_serialize_probe(r) for r in results]

        return EvaluatorResult(
            dimension=self.dimension,
            verdict=verdict,
            verdict_reason=_reason(overall, results),
            rounds_total=len(results),
            rounds_ok=rounds_ok,
            hits=hits,
            duration_s=duration,
        )


def _serialize_probe(r) -> dict:
    """Per-probe dict for ``EvaluatorResult.hits`` (reporter-renderable)."""
    return {
        "name": r.name,
        "verdict": r.verdict,
        "safe_markers": list(r.safe_markers_found),
        "unsafe_markers": list(r.unsafe_markers_found),
        "error": r.error,
        "response_preview": (r.response or "")[:200],
    }


def _reason(overall: str, results) -> str:
    if overall == "anomaly":
        bad = [r.name for r in results if r.verdict == "injected"]
        return f"web3 injection on {', '.join(bad)}"
    if overall == "clean":
        return "all 3 web3 probes refused safely"
    return "web3 probes returned inconclusive (errors or ambiguous)"
