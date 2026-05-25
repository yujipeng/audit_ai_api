"""EvaluatorPipeline — sequential, fail-open execution; parallel hook reserved for P1.

Design references:
- §3.1 方案 A pipeline.run 伪码 (顺序执行 + try/except → verdict=failed)
- §5.1 时序：CLI/SDK → pipeline.run → aggregator → reporter
- §5.2 P1 parallel hook 预留
"""
from __future__ import annotations

import time
from typing import Optional

from .aggregator import PurityAggregator, PurityReportV2
from .base import DimensionId, Evaluator, EvaluatorResult, ProbeContext


class EvaluatorPipeline:
    """顺序执行 evaluator 列表；fail-open 把异常转 verdict=failed。

    P1 改造路径声明在 design §5.2，本片仅接受 parallel=False (默认值)。
    """

    def __init__(self, evaluators: list[Evaluator]):
        self._evaluators = list(evaluators)

    def run(
        self,
        ctx: ProbeContext,
        *,
        only: Optional[DimensionId] = None,
        include: Optional[list[DimensionId]] = None,
        parallel: bool = False,
    ) -> PurityReportV2:
        if parallel:
            raise NotImplementedError(
                "parallel=True is reserved for P1 (design §5.2); "
                "set parallel=False or omit"
            )
        selected = self._select(only=only, include=include)
        results: list[EvaluatorResult] = [self._safe_run(ev, ctx) for ev in selected]
        return PurityAggregator().aggregate(ctx, results)

    def _select(
        self,
        only: Optional[DimensionId],
        include: Optional[list[DimensionId]],
    ) -> list[Evaluator]:
        if only is not None:
            return [e for e in self._evaluators if e.dimension == only]
        base = [e for e in self._evaluators if e.default_in_profile]
        if include:
            extra = [
                e
                for e in self._evaluators
                if (not e.default_in_profile) and (e.dimension in include)
            ]
            return base + extra
        return base

    @staticmethod
    def _safe_run(ev: Evaluator, ctx: ProbeContext) -> EvaluatorResult:
        t0 = time.perf_counter()
        try:
            return ev.run(ctx)
        except Exception as e:  # fail-open per design §3.1
            return EvaluatorResult(
                dimension=ev.dimension,
                verdict="failed",
                verdict_reason=f"evaluator raised {type(e).__name__}: {e}",
                rounds_total=ctx.rounds,
                rounds_ok=0,
                hits=[],
                duration_s=time.perf_counter() - t0,
            )
