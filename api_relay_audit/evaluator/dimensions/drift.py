"""DriftEvaluator — placeholder for identity-drift detection.

design §7 R6: drift 维度作为本 Story 的占位，verdict='unknown' 且
verdict_reason 必须包含 'placeholder' 字符串以便 grep 单测识别。

复用 swap hits 的轮廓但不发起网络调用；S3/S4 由独立 Story 替换为基于
"first reply identifies as Claude → later reply drifts to GPT" 的多轮检测。
"""
from __future__ import annotations

from api_relay_audit.evaluator.base import (
    Evaluator,
    EvaluatorResult,
    ProbeContext,
)


class DriftEvaluator(Evaluator):
    dimension = "drift"
    default_in_profile = True

    def run(self, ctx: ProbeContext) -> EvaluatorResult:
        return EvaluatorResult(
            dimension=self.dimension,
            verdict="unknown",
            verdict_reason=(
                "drift detection is a placeholder pending S3 multi-round "
                "implementation; reusing swap hits is not yet wired"
            ),
            rounds_total=ctx.rounds,
            rounds_ok=0,
            hits=[],
            duration_s=0.0,
        )
