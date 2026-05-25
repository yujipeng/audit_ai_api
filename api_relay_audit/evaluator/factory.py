"""Factory — explicit pipeline assembly.

S2-2 装载 6 个真实 wrapper（injection/swap/drift/tool/refusal/leak），web3
维度仍是 NoOp 占位（由 S2-3 替换）。
"""
from __future__ import annotations

from .base import DimensionId, Evaluator, EvaluatorResult, ProbeContext
from .dimensions.drift import DriftEvaluator
from .dimensions.injection import InjectionEvaluator
from .dimensions.leak import LeakEvaluator
from .dimensions.refusal import RefusalEvaluator
from .dimensions.swap import SwapEvaluator
from .dimensions.tool import ToolEvaluator
from .pipeline import EvaluatorPipeline


class _NoOpEvaluator(Evaluator):
    """Placeholder evaluator (kept for web3_injection until S2-3)."""

    def __init__(self, dimension: DimensionId, default_in_profile: bool = True):
        self.dimension = dimension
        self.default_in_profile = default_in_profile

    def run(self, ctx: ProbeContext) -> EvaluatorResult:
        return EvaluatorResult(
            dimension=self.dimension,
            verdict="unknown",
            verdict_reason=f"{self.dimension} evaluator not yet implemented",
            rounds_total=ctx.rounds,
            rounds_ok=0,
            hits=[],
            duration_s=0.0,
        )


def default_purity_pipeline() -> EvaluatorPipeline:
    """Build the default pipeline with all dimensions registered."""
    return EvaluatorPipeline(
        [
            InjectionEvaluator(),
            SwapEvaluator(),
            DriftEvaluator(),
            ToolEvaluator(),
            RefusalEvaluator(),
            LeakEvaluator(),
            _NoOpEvaluator("web3_injection", default_in_profile=False),
        ]
    )
