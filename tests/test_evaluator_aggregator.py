"""S2-1 aggregator AC tests: PRD §4.6 升档规则 + confidence + worst_dimensions.

验收口径来源：
- TES-162 验收：tests/test_evaluator_aggregator.py 覆盖 PRD §4.6 升档规则全 5 档（AC-R1）
- TES-162 验收：confidence 计算 = sum(rounds_ok)/sum(rounds_total)，0.0–1.0 闭区间
- design §4.5：升档语义 = 「最差子维度」；confidence 不参与升档
- design §4.5 _VERDICT_RANK：injected > suspicious > failed > unknown > clean
"""
from __future__ import annotations

import math

import pytest


def _ctx():
    from api_relay_audit.evaluator import ProbeContext

    return ProbeContext(
        client=object(),
        api_key="sk-test",
        base_url="https://relay.example.com",
        vendor="gpt",
        rounds=10,
    )


def _result(dimension: str, verdict: str, rounds_ok: int = 10, rounds_total: int = 10):
    from api_relay_audit.evaluator import EvaluatorResult

    return EvaluatorResult(
        dimension=dimension,
        verdict=verdict,
        verdict_reason=f"{dimension}={verdict}",
        rounds_total=rounds_total,
        rounds_ok=rounds_ok,
        hits=[],
        duration_s=0.1,
    )


# ---------- 升档 5 档完整笛卡儿覆盖 ----------


def test_promotion_all_clean_yields_clean():
    """PRD §4.6：全 clean → overall=clean。"""
    from api_relay_audit.evaluator import PurityAggregator

    results = [
        _result("injection", "clean"),
        _result("swap", "clean"),
        _result("drift", "clean"),
        _result("tool", "clean"),
        _result("refusal", "clean"),
        _result("leak", "clean"),
    ]
    report = PurityAggregator().aggregate(_ctx(), results)
    assert report.overall.verdict == "clean"
    assert report.overall.worst_dimensions == [
        "injection",
        "swap",
        "drift",
        "tool",
        "refusal",
        "leak",
    ]


def test_promotion_unknown_dominates_clean():
    """PRD §4.6 升档表：unknown > clean。"""
    from api_relay_audit.evaluator import PurityAggregator

    results = [
        _result("injection", "clean"),
        _result("swap", "unknown"),
        _result("drift", "clean"),
    ]
    report = PurityAggregator().aggregate(_ctx(), results)
    assert report.overall.verdict == "unknown"
    assert report.overall.worst_dimensions == ["swap"]


def test_promotion_failed_dominates_unknown():
    """PRD §4.6：failed > unknown."""
    from api_relay_audit.evaluator import PurityAggregator

    results = [
        _result("injection", "unknown"),
        _result("swap", "failed"),
        _result("drift", "clean"),
    ]
    report = PurityAggregator().aggregate(_ctx(), results)
    assert report.overall.verdict == "failed"
    assert report.overall.worst_dimensions == ["swap"]


def test_promotion_suspicious_dominates_failed():
    """PRD §4.6：suspicious > failed."""
    from api_relay_audit.evaluator import PurityAggregator

    results = [
        _result("injection", "failed"),
        _result("swap", "suspicious"),
        _result("drift", "unknown"),
        _result("tool", "clean"),
    ]
    report = PurityAggregator().aggregate(_ctx(), results)
    assert report.overall.verdict == "suspicious"
    assert report.overall.worst_dimensions == ["swap"]


def test_promotion_injected_dominates_all():
    """PRD §4.6：injected 最高级，必占顶。"""
    from api_relay_audit.evaluator import PurityAggregator

    results = [
        _result("injection", "injected"),
        _result("swap", "suspicious"),
        _result("drift", "failed"),
        _result("tool", "unknown"),
        _result("refusal", "clean"),
    ]
    report = PurityAggregator().aggregate(_ctx(), results)
    assert report.overall.verdict == "injected"
    assert report.overall.worst_dimensions == ["injection"]


def test_promotion_multiple_worst_dimensions_listed():
    """worst_dimensions 含全部并列最差维度（design §4.5 _VERDICT_RANK + Overall.worst_dimensions）."""
    from api_relay_audit.evaluator import PurityAggregator

    results = [
        _result("injection", "injected"),
        _result("swap", "injected"),
        _result("drift", "suspicious"),
        _result("tool", "clean"),
    ]
    report = PurityAggregator().aggregate(_ctx(), results)
    assert report.overall.verdict == "injected"
    assert set(report.overall.worst_dimensions) == {"injection", "swap"}


# ---------- confidence 范围与公式 ----------


def test_confidence_full_rounds_ok_equals_one():
    """所有 rounds_ok == rounds_total → confidence = 1.0。"""
    from api_relay_audit.evaluator import PurityAggregator

    results = [
        _result("injection", "clean", rounds_ok=10, rounds_total=10),
        _result("swap", "clean", rounds_ok=10, rounds_total=10),
    ]
    report = PurityAggregator().aggregate(_ctx(), results)
    assert report.overall.confidence == pytest.approx(1.0)


def test_confidence_no_rounds_ok_equals_zero():
    """所有 rounds_ok == 0 → confidence = 0.0。"""
    from api_relay_audit.evaluator import PurityAggregator

    results = [
        _result("injection", "failed", rounds_ok=0, rounds_total=10),
        _result("swap", "failed", rounds_ok=0, rounds_total=10),
    ]
    report = PurityAggregator().aggregate(_ctx(), results)
    assert report.overall.confidence == pytest.approx(0.0)


def test_confidence_formula_sum_rounds_ok_over_sum_rounds_total():
    """confidence = sum(rounds_ok) / sum(rounds_total)（TES-162 验收）.

    7 + 3 = 10 ok 出 20 total = 0.5
    """
    from api_relay_audit.evaluator import PurityAggregator

    results = [
        _result("injection", "clean", rounds_ok=7, rounds_total=10),
        _result("swap", "suspicious", rounds_ok=3, rounds_total=10),
    ]
    report = PurityAggregator().aggregate(_ctx(), results)
    assert report.overall.confidence == pytest.approx(0.5)


def test_confidence_bounded_in_unit_interval():
    """confidence 必在 [0.0, 1.0] 闭区间（TES-162 验收）。"""
    from api_relay_audit.evaluator import PurityAggregator

    cases = [
        [_result("injection", "clean", rounds_ok=10, rounds_total=10)],  # 1.0
        [_result("injection", "failed", rounds_ok=0, rounds_total=10)],  # 0.0
        [
            _result("injection", "suspicious", rounds_ok=5, rounds_total=10),
            _result("swap", "clean", rounds_ok=5, rounds_total=10),
        ],  # 0.5
    ]
    for results in cases:
        report = PurityAggregator().aggregate(_ctx(), results)
        assert 0.0 <= report.overall.confidence <= 1.0


def test_confidence_zero_total_is_zero_not_division_error():
    """边界：sum(rounds_total)=0 时不抛除零，返回 0.0。"""
    from api_relay_audit.evaluator import PurityAggregator

    results = [
        _result("injection", "failed", rounds_ok=0, rounds_total=0),
    ]
    # 不应抛 ZeroDivisionError
    report = PurityAggregator().aggregate(_ctx(), results)
    assert report.overall.confidence == pytest.approx(0.0)
    assert math.isfinite(report.overall.confidence)


# ---------- schema_version / dimensions 索引 ----------


def test_report_schema_version_locked():
    """schema_version 字面值锁定（TES-162 验收）。"""
    from api_relay_audit.evaluator import PurityAggregator

    report = PurityAggregator().aggregate(_ctx(), [])
    assert report.schema_version == "purity-v2.0"


def test_report_dimensions_indexed_by_id():
    """dimensions 字典按 DimensionId 索引（design §4.2 JSON 形态）。"""
    from api_relay_audit.evaluator import PurityAggregator

    results = [
        _result("injection", "clean"),
        _result("swap", "suspicious"),
    ]
    report = PurityAggregator().aggregate(_ctx(), results)
    assert set(report.dimensions.keys()) == {"injection", "swap"}
    assert report.dimensions["injection"].verdict == "clean"
    assert report.dimensions["swap"].verdict == "suspicious"


def test_report_metadata_includes_vendor_and_rounds():
    """metadata 含 vendor / rounds_requested / evaluators_run（design §4.2）。"""
    from api_relay_audit.evaluator import PurityAggregator

    results = [
        _result("injection", "clean"),
        _result("swap", "clean"),
    ]
    report = PurityAggregator().aggregate(_ctx(), results)
    assert report.metadata["vendor"] == "gpt"
    assert report.metadata["rounds_requested"] == 10
    assert report.metadata["evaluators_run"] == ["injection", "swap"]


def test_empty_results_yields_clean_with_zero_confidence():
    """边界：results=[] → overall=clean, confidence=0.0, worst_dimensions=[]。

    设计选择：无 result 时不抛异常（pipeline 在 `--only` 不匹配时可能空 selected），
    但 confidence 必须可计算。clean 是「最低排名」语义自然兜底。
    """
    from api_relay_audit.evaluator import PurityAggregator

    report = PurityAggregator().aggregate(_ctx(), [])
    assert report.overall.verdict == "clean"
    assert report.overall.confidence == pytest.approx(0.0)
    assert report.overall.worst_dimensions == []
    assert report.dimensions == {}
