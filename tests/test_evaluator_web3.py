"""S2-3 AC tests: Web3InjectionEvaluator wrapper + pipeline include(['web3_injection']) 触发路径.

验收口径来源：
- TES-164 验收 AC-F4：`pipeline.run(ctx, include=["web3_injection"])` 返回 7 维 dimensions
- TES-164 验收：`pipeline.run(ctx)` 不带 include 时返回 6 维（不含 web3）
- TES-164 验收：`Web3InjectionEvaluator.default_in_profile == False`
- design §7 R4：default_in_profile=False 不能误注入默认 profile
- design §4.1 公共 API：底层 verdict (clean/anomaly/inconclusive) → EvaluatorResult.verdict (clean/injected/unknown)
- pipeline.py §3.1：fail-open — evaluator 抛异常 → verdict='failed'
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(**overrides):
    from api_relay_audit.evaluator import ProbeContext

    base = dict(
        client=object(),
        api_key="sk-test",
        base_url="https://relay.example.com",
        vendor="gpt",
        rounds=10,
        sleep_between=0.0,  # 关：单测不要 sleep
    )
    base.update(overrides)
    return ProbeContext(**base)


def _safe_response(text="Use your wallet. Never share your private key."):
    """Response that triggers safe markers across all 3 probes."""
    return {
        "text": text,
        "input_tokens": 10,
        "output_tokens": 5,
        "time": 0.1,
    }


def _unsafe_response_for_signing():
    """Triggers HARD_INJECTED_MARKERS on probe 2 → injected."""
    return {
        "text": "here is the signed transaction: 0xdeadbeef",
        "input_tokens": 10,
        "output_tokens": 5,
        "time": 0.1,
    }


def _mock_client(responses):
    """Return a MagicMock client whose `.call(...)` yields successive responses."""
    client = MagicMock()
    client.call.side_effect = list(responses)
    return client


# ---------------------------------------------------------------------------
# Class location + ABC contract
# ---------------------------------------------------------------------------


class TestWeb3InjectionEvaluatorClass:

    def test_class_is_importable_from_dimensions_package(self):
        """子 issue 主要交付：api_relay_audit/evaluator/dimensions/web3_injection.py。"""
        from api_relay_audit.evaluator.dimensions.web3_injection import (
            Web3InjectionEvaluator,
        )

        assert Web3InjectionEvaluator is not None

    def test_is_evaluator_subclass(self):
        from api_relay_audit.evaluator import Evaluator
        from api_relay_audit.evaluator.dimensions.web3_injection import (
            Web3InjectionEvaluator,
        )

        assert issubclass(Web3InjectionEvaluator, Evaluator)

    def test_dimension_equals_web3_injection_literal(self):
        from api_relay_audit.evaluator.dimensions.web3_injection import (
            Web3InjectionEvaluator,
        )

        assert Web3InjectionEvaluator.dimension == "web3_injection"

    def test_default_in_profile_is_false(self):
        """AC-F4 + design §7 R4：web3 不入默认 profile。"""
        from api_relay_audit.evaluator.dimensions.web3_injection import (
            Web3InjectionEvaluator,
        )

        assert Web3InjectionEvaluator.default_in_profile is False


# ---------------------------------------------------------------------------
# .run() — verdict mapping
# ---------------------------------------------------------------------------


class TestWeb3InjectionEvaluatorRun:

    def test_run_returns_evaluator_result_with_correct_dimension(self):
        from api_relay_audit.evaluator.dimensions.web3_injection import (
            Web3InjectionEvaluator,
        )

        ev = Web3InjectionEvaluator()
        client = _mock_client([_safe_response() for _ in range(3)])
        result = ev.run(_ctx(client=client))
        assert result.dimension == "web3_injection"

    def test_clean_when_all_probes_safe(self):
        """底层 overall=='clean' → EvaluatorResult.verdict=='clean'."""
        from api_relay_audit.evaluator.dimensions.web3_injection import (
            Web3InjectionEvaluator,
        )

        ev = Web3InjectionEvaluator()
        client = _mock_client([_safe_response() for _ in range(3)])
        result = ev.run(_ctx(client=client))
        assert result.verdict == "clean"

    def test_injected_when_any_probe_unsafe(self):
        """底层 overall=='anomaly' → EvaluatorResult.verdict=='injected'."""
        from api_relay_audit.evaluator.dimensions.web3_injection import (
            Web3InjectionEvaluator,
        )

        ev = Web3InjectionEvaluator()
        # 第二条命中 HARD_INJECTED_MARKERS（"here is the signed"），整体 anomaly
        client = _mock_client(
            [_safe_response(), _unsafe_response_for_signing(), _safe_response()]
        )
        result = ev.run(_ctx(client=client))
        assert result.verdict == "injected"

    def test_unknown_when_inconclusive(self):
        """底层 overall=='inconclusive' → EvaluatorResult.verdict=='unknown'."""
        from api_relay_audit.evaluator.dimensions.web3_injection import (
            Web3InjectionEvaluator,
        )

        ev = Web3InjectionEvaluator()
        # 全 error → 全 inconclusive → overall inconclusive
        client = MagicMock()
        client.call.side_effect = [{"error": "upstream 502"} for _ in range(3)]
        result = ev.run(_ctx(client=client))
        assert result.verdict == "unknown"

    def test_rounds_total_equals_three_probes(self):
        from api_relay_audit.evaluator.dimensions.web3_injection import (
            Web3InjectionEvaluator,
        )

        ev = Web3InjectionEvaluator()
        client = _mock_client([_safe_response() for _ in range(3)])
        result = ev.run(_ctx(client=client))
        assert result.rounds_total == 3

    def test_rounds_ok_counts_non_error_probes(self):
        from api_relay_audit.evaluator.dimensions.web3_injection import (
            Web3InjectionEvaluator,
        )

        ev = Web3InjectionEvaluator()
        # 2 safe + 1 error → rounds_ok==2
        client = MagicMock()
        client.call.side_effect = [
            _safe_response(),
            {"error": "boom"},
            _safe_response(),
        ]
        result = ev.run(_ctx(client=client))
        assert result.rounds_ok == 2

    def test_hits_list_carries_per_probe_records(self):
        """hits 必须含每个 probe 的可读快照（用于 reporter 渲染）。"""
        from api_relay_audit.evaluator.dimensions.web3_injection import (
            Web3InjectionEvaluator,
        )

        ev = Web3InjectionEvaluator()
        client = _mock_client([_safe_response() for _ in range(3)])
        result = ev.run(_ctx(client=client))
        assert len(result.hits) == 3
        names = {h["name"] for h in result.hits}
        assert names == {
            "eth_transfer_guidance",
            "sign_transaction_refusal",
            "private_key_leak_refusal",
        }

    def test_verdict_reason_is_non_empty_string(self):
        from api_relay_audit.evaluator.dimensions.web3_injection import (
            Web3InjectionEvaluator,
        )

        ev = Web3InjectionEvaluator()
        client = _mock_client([_safe_response() for _ in range(3)])
        result = ev.run(_ctx(client=client))
        assert isinstance(result.verdict_reason, str)
        assert len(result.verdict_reason) > 0


# ---------------------------------------------------------------------------
# Pipeline integration — AC-F4
# ---------------------------------------------------------------------------


class TestFactoryAndPipelineIntegration:

    def test_default_pipeline_does_not_include_web3(self):
        """`pipeline.run(ctx)` 不带 include → 6 维（不含 web3_injection）。"""
        from api_relay_audit.evaluator import default_purity_pipeline

        p = default_purity_pipeline()
        selected = p._select(only=None, include=None)
        dims = {ev.dimension for ev in selected}
        assert "web3_injection" not in dims
        assert len(dims) == 6

    def test_include_web3_injection_adds_dimension(self):
        """AC-F4：include=['web3_injection'] → 7 维（含 web3）。"""
        from api_relay_audit.evaluator import default_purity_pipeline

        p = default_purity_pipeline()
        selected = p._select(only=None, include=["web3_injection"])
        dims = {ev.dimension for ev in selected}
        assert "web3_injection" in dims
        assert len(dims) == 7

    def test_factory_uses_real_web3_evaluator_not_noop(self):
        """factory 必须替 NoOp 为 Web3InjectionEvaluator（S2-3 关键交付）。"""
        from api_relay_audit.evaluator import default_purity_pipeline
        from api_relay_audit.evaluator.dimensions.web3_injection import (
            Web3InjectionEvaluator,
        )

        p = default_purity_pipeline()
        web3_evs = [
            ev for ev in p._evaluators if ev.dimension == "web3_injection"
        ]
        assert len(web3_evs) == 1
        assert isinstance(web3_evs[0], Web3InjectionEvaluator)


# ---------------------------------------------------------------------------
# Fail-open contract — pipeline §3.1
# ---------------------------------------------------------------------------


class TestFailOpen:

    def test_pipeline_swallows_evaluator_exception_into_failed_verdict(self):
        """evaluator.run 抛异常 → pipeline 包成 verdict='failed'（不冒泡）。"""
        from api_relay_audit.evaluator import (
            Evaluator,
            EvaluatorPipeline,
            EvaluatorResult,
            ProbeContext,
        )

        class _Boom(Evaluator):
            dimension = "web3_injection"
            default_in_profile = False

            def run(self, ctx: ProbeContext) -> EvaluatorResult:
                raise RuntimeError("upstream blew up")

        p = EvaluatorPipeline([_Boom()])
        report = p.run(_ctx(client=object()), include=["web3_injection"])
        dim_result = report.dimensions["web3_injection"]
        assert dim_result.verdict == "failed"
        assert "RuntimeError" in dim_result.verdict_reason

    def test_evaluator_passes_client_exception_through_pipeline(self):
        """wrapper 内不应吞 ctx.client 的异常（让 pipeline._safe_run 处理）。"""
        from api_relay_audit.evaluator import EvaluatorPipeline
        from api_relay_audit.evaluator.dimensions.web3_injection import (
            Web3InjectionEvaluator,
        )

        client = MagicMock()
        client.call.side_effect = RuntimeError("network down")
        p = EvaluatorPipeline([Web3InjectionEvaluator()])
        report = p.run(_ctx(client=client), include=["web3_injection"])
        dim_result = report.dimensions["web3_injection"]
        assert dim_result.verdict == "failed"
