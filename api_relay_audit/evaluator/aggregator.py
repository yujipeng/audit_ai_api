"""Aggregator — PRD §4.6 升档规则 + confidence + PurityReportV2.

Design references:
- §4.2 PurityReportV2 / EvaluatorResult JSON 形态
- §4.5 升档语义（最差子维度，confidence 不参与升档）
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Optional

from .base import DimensionId, EvaluatorResult, ProbeContext, Verdict

_VERDICT_RANK: dict[Verdict, int] = {
    "injected": 4,
    "suspicious": 3,
    "failed": 2,
    "unknown": 1,
    "clean": 0,
}


@dataclass
class Overall:
    verdict: Verdict
    verdict_reason: str
    confidence: float
    worst_dimensions: list[DimensionId]


@dataclass
class PurityReportV2:
    schema_version: str
    metadata: dict
    dimensions: dict
    overall: Overall


class PurityAggregator:
    """Aggregate per-dimension EvaluatorResult list into a PurityReportV2.

    升档规则严格按 PRD §4.6 / design §4.5：最差子维度决定 overall。
    confidence 不参与升档，仅作可靠度展示。
    """

    schema_version: str = "purity-v2.0"

    def aggregate(
        self,
        ctx: ProbeContext,
        results: list[EvaluatorResult],
    ) -> PurityReportV2:
        dims: dict[DimensionId, EvaluatorResult] = {r.dimension: r for r in results}

        if results:
            worst_rank = max(_VERDICT_RANK[r.verdict] for r in results)
            overall_verdict: Verdict = next(
                k for k, v in _VERDICT_RANK.items() if v == worst_rank
            )
            worst_dimensions: list[DimensionId] = [
                r.dimension for r in results if _VERDICT_RANK[r.verdict] == worst_rank
            ]
        else:
            overall_verdict = "clean"
            worst_dimensions = []

        confidence = self._confidence(results)
        verdict_reason = self._reason(overall_verdict, worst_dimensions, dims)

        return PurityReportV2(
            schema_version=self.schema_version,
            metadata=self._metadata(ctx, results),
            dimensions=dims,
            overall=Overall(
                verdict=overall_verdict,
                verdict_reason=verdict_reason,
                confidence=confidence,
                worst_dimensions=worst_dimensions,
            ),
        )

    @staticmethod
    def _confidence(results: list[EvaluatorResult]) -> float:
        total = sum(r.rounds_total for r in results)
        if total == 0:
            return 0.0
        ok = sum(r.rounds_ok for r in results)
        return ok / total

    @staticmethod
    def _reason(
        overall_verdict: Verdict,
        worst_dimensions: list[DimensionId],
        dims: dict,
    ) -> str:
        if not worst_dimensions:
            return "no evaluators ran"
        names = ", ".join(worst_dimensions)
        return f"{overall_verdict}: {names}"

    @staticmethod
    def _metadata(
        ctx: ProbeContext,
        results: list[EvaluatorResult],
    ) -> dict:
        return {
            "target_url": ctx.base_url,
            "api_key_fingerprint": _fingerprint(ctx.api_key),
            "vendor": ctx.vendor,
            "profile": "default",
            "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "duration_s": sum(r.duration_s for r in results),
            "rounds_requested": ctx.rounds,
            "evaluators_run": [r.dimension for r in results],
        }


def _fingerprint(api_key: str) -> str:
    if not api_key:
        return ""
    tail = api_key[-4:] if len(api_key) >= 4 else api_key
    return f"sk-...{tail}"
