"""Baseline diff hook — P1 placeholder (PM Q3 立场 [comment 7076cd68]).

本片不实现：MVP 不内置「已知诚实模型误报率」基线。
P1 通过 `--baseline <path>` 旁路 + tests/test_purity_baseline_*.py 集成回归落地。
"""
from __future__ import annotations

from typing import Any


def diff_against_baseline(
    current: Any,
    baseline_path: str,
) -> list[Any]:
    """对比当前 PurityReportV2 与基线 JSON，返回 hits 级别差异列表 (P1 placeholder).

    本签名是 P1 hook 占位，S2-1 不实现。调用即抛 NotImplementedError。

    Design §5.3 / Appendix B Q3：MVP 误报兜底机制 = hits.snippet ≤ 200 chars 必填 +
    verdict_reason 必填 + --raw-keep 留底。用户怀疑误报时人工复核第一手证据；
    --baseline 旁路在 P1 Story 落地。
    """
    raise NotImplementedError(
        "diff_against_baseline is reserved for P1 (design §5.3); "
        "MVP 不内置基线，请通过 verdict_reason + hits[].snippet + --raw-keep 人工复核"
    )
