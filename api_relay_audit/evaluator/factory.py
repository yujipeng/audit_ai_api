"""Factory — explicit pipeline assembly.

S2-1 用 NoOp 占位整张 pipeline；S2-2 / S2-3 起逐一替换为真实 wrapper。
本片：S2-3 已替 web3 占位为 ``Web3InjectionEvaluator``；其余 6 维仍为 NoOp（S2-2 接管）。
"""
from __future__ import annotations

from .base import DimensionId, Evaluator, EvaluatorResult, ProbeContext
from .dimensions.web3_injection import Web3InjectionEvaluator
from .pipeline import EvaluatorPipeline


class _NoOpEvaluator(Evaluator):
    """Placeholder evaluator: returns verdict='unknown' with reason 'not implemented'.

    S2-2/3/4/5 各自替换对应 dimension 的 wrapper 后，本类不再被引用。
    选择 verdict='unknown' 而非 'clean'：避免在 wrapper 未上线前给出虚假 clean 结论。
    """

    def __init__(self, dimension: DimensionId, default_in_profile: bool = True):
        self.dimension = dimension
        self.default_in_profile = default_in_profile

    def run(self, ctx: ProbeContext) -> EvaluatorResult:
        return EvaluatorResult(
            dimension=self.dimension,
            verdict="unknown",
            verdict_reason=f"{self.dimension} evaluator not yet implemented (S2-1 skeleton)",
            rounds_total=ctx.rounds,
            rounds_ok=0,
            hits=[],
            duration_s=0.0,
        )


def default_purity_pipeline() -> EvaluatorPipeline:
    """Build the default pipeline with all dimensions registered."""
    return EvaluatorPipeline(
        [
            _NoOpEvaluator("injection"),
            _NoOpEvaluator("swap"),
            _NoOpEvaluator("drift"),
            _NoOpEvaluator("tool"),
            _NoOpEvaluator("refusal"),
            _NoOpEvaluator("leak"),
            Web3InjectionEvaluator(),
        ]
    )
