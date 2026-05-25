"""S2-1 skeleton AC tests: public API surface + ABC contract + Literal type lock.

验收口径来源：
- TES-162 验收 AC-F7：`from api_relay_audit.evaluator import ...` 全部可导入
- design §4.1 公开 Python API 清单
- design §8 契约稳定性承诺：DimensionId / Verdict / Severity Literal 字面值锁定
"""
from __future__ import annotations

import inspect
import typing

import pytest


def test_public_api_exports_present():
    """AC-F7：公共导入清单全部存在。"""
    from api_relay_audit.evaluator import (  # noqa: F401
        DimensionId,
        Evaluator,
        EvaluatorPipeline,
        EvaluatorResult,
        Overall,
        ProbeContext,
        PurityAggregator,
        PurityReportV2,
        Severity,
        Verdict,
        default_purity_pipeline,
    )


def test_dimension_id_literal_values_locked():
    """design §8 契约：DimensionId Literal 字面值锁定（不增不减、顺序不约束）。"""
    from api_relay_audit.evaluator import DimensionId

    expected = {
        "injection",
        "swap",
        "drift",
        "tool",
        "refusal",
        "leak",
        "web3_injection",
    }
    actual = set(typing.get_args(DimensionId))
    assert actual == expected, (
        f"DimensionId Literal 必须严格等于 7 个字面值；当前差异：缺 {expected - actual}，多 {actual - expected}"
    )


def test_verdict_literal_values_locked():
    """design §8 契约：Verdict 五档锁定。"""
    from api_relay_audit.evaluator import Verdict

    expected = {"clean", "suspicious", "injected", "failed", "unknown"}
    actual = set(typing.get_args(Verdict))
    assert actual == expected


def test_severity_literal_values_locked():
    """design §8 契约：Severity 三档锁定。"""
    from api_relay_audit.evaluator import Severity

    expected = {"high", "medium", "low"}
    actual = set(typing.get_args(Severity))
    assert actual == expected


def test_evaluator_is_abstract_base_class():
    """design §3.1 取舍：使用 ABC 而非 Protocol — 强约束 dimension/default_in_profile。"""
    import abc

    from api_relay_audit.evaluator import Evaluator

    assert inspect.isclass(Evaluator)
    assert issubclass(Evaluator, abc.ABC) or abc.ABC in Evaluator.__mro__, (
        "Evaluator 必须是 ABC 派生（非 Protocol）"
    )
    # run 是抽象方法
    abstract_methods = getattr(Evaluator, "__abstractmethods__", frozenset())
    assert "run" in abstract_methods, "Evaluator.run 必须 @abstractmethod"


def test_evaluator_cannot_instantiate_without_run():
    """缺 run 实现不可实例化。"""
    from api_relay_audit.evaluator import Evaluator

    with pytest.raises(TypeError):
        Evaluator()  # type: ignore[abstract]


def test_evaluator_subclass_must_declare_dimension():
    """子类必须 override `dimension` 类属性（design §3.1 ABC 契约）。"""
    from api_relay_audit.evaluator import Evaluator, EvaluatorResult, ProbeContext

    class _Concrete(Evaluator):
        dimension = "swap"

        def run(self, ctx: ProbeContext) -> EvaluatorResult:  # noqa: D401
            return EvaluatorResult(
                dimension=self.dimension,
                verdict="clean",
                verdict_reason="ok",
                rounds_total=1,
                rounds_ok=1,
                hits=[],
                duration_s=0.0,
            )

    inst = _Concrete()
    assert inst.dimension == "swap"
    # default_in_profile 默认 True（design §3.1 ABC 字段）
    assert _Concrete.default_in_profile is True


def test_probe_context_required_fields_and_defaults():
    """ProbeContext 字段与默认值（design §3.1）。"""
    from api_relay_audit.evaluator import ProbeContext

    ctx = ProbeContext(
        client=object(),
        api_key="sk-test",
        base_url="https://relay.example.com",
        vendor="gpt",
    )
    assert ctx.rounds == 10
    assert ctx.prompts == []
    assert ctx.sleep_between == 1.0
    assert ctx.keep_raw is False


def test_evaluator_result_dataclass_fields():
    """EvaluatorResult 字段集（design §4.2）。"""
    from dataclasses import fields

    from api_relay_audit.evaluator import EvaluatorResult

    names = {f.name for f in fields(EvaluatorResult)}
    expected = {
        "dimension",
        "verdict",
        "verdict_reason",
        "rounds_total",
        "rounds_ok",
        "hits",
        "duration_s",
        "raw_artifacts",
    }
    assert names == expected, f"EvaluatorResult 字段不匹配：差异 {names ^ expected}"


def test_purity_report_v2_schema_version_locked():
    """AC-R1 锁定：schema_version 必须等于 'purity-v2.0' 字面值。"""
    from api_relay_audit.evaluator import (
        Overall,
        PurityAggregator,
        PurityReportV2,
        ProbeContext,
    )

    agg = PurityAggregator()
    ctx = ProbeContext(
        client=object(), api_key="sk-x", base_url="http://x", vendor="gpt"
    )
    report = agg.aggregate(ctx, [])
    assert isinstance(report, PurityReportV2)
    assert report.schema_version == "purity-v2.0"


def test_default_purity_pipeline_returns_pipeline_with_evaluators():
    """factory 占位：default_purity_pipeline() 返回内含 NoOp 占位的 EvaluatorPipeline。

    S2-1 仅交付占位，由 S2-2/3 替换为真实 wrapper（design §9 Story 拆分）。
    """
    from api_relay_audit.evaluator import (
        DimensionId,
        EvaluatorPipeline,
        default_purity_pipeline,
    )

    p = default_purity_pipeline()
    assert isinstance(p, EvaluatorPipeline)
    # 默认 profile 至少包含 6 个 default-on 维度（web3_injection default_in_profile=False）
    selected = p._select(only=None, include=None)
    selected_dims = {ev.dimension for ev in selected}
    assert selected_dims == {"injection", "swap", "drift", "tool", "refusal", "leak"}


def test_default_purity_pipeline_include_web3_appends_dimension():
    """AC-F4：include=['web3_injection'] 把 default-off 维度加入。"""
    from api_relay_audit.evaluator import default_purity_pipeline

    p = default_purity_pipeline()
    selected = p._select(only=None, include=["web3_injection"])
    dims = {ev.dimension for ev in selected}
    assert "web3_injection" in dims


def test_default_purity_pipeline_only_filters_single_dimension():
    """AC-F3：only='swap' 仅返回 1 个 evaluator。"""
    from api_relay_audit.evaluator import default_purity_pipeline

    p = default_purity_pipeline()
    selected = p._select(only="swap", include=None)
    assert len(selected) == 1
    assert selected[0].dimension == "swap"


def test_baseline_module_exists_with_signature_only():
    """PM Q3 P1 hook：baseline.py 只有签名 + docstring，不实现。"""
    from api_relay_audit.evaluator import baseline

    assert hasattr(baseline, "diff_against_baseline")
    fn = baseline.diff_against_baseline
    # 必须有 docstring 标注 P1 占位
    assert fn.__doc__ is not None
    assert "P1" in fn.__doc__ or "placeholder" in fn.__doc__.lower()


def test_thresholds_module_exports_defaults():
    """阈值常量集中在 thresholds.py（design §4.6 末段）。"""
    from api_relay_audit.evaluator import thresholds

    # 至少包含 refusal 的两个阈值（design §4.6 表格）
    assert hasattr(thresholds, "REFUSAL_SUSPICIOUS_RATE")
    assert hasattr(thresholds, "REFUSAL_INJECTED_RATE")
    assert 0.0 < thresholds.REFUSAL_SUSPICIOUS_RATE < thresholds.REFUSAL_INJECTED_RATE < 1.0
