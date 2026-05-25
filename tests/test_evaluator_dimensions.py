"""S2-2 wrapper AC tests: 6 dimension evaluators (injection / swap / drift /
tool / refusal / leak).

每个 wrapper 至少 3 fixture：happy + detect + fail-open。验收来源：
- TES-163 验收清单（每 wrapper happy + fail-open，每个 ≥3 fixture）
- design §3.1 fail-open（异常 → verdict='failed'）
- design §4.6 升档语义
- design §7 R2 / R3 / R6（_identity_anchored 暴露、refusal 合并不漂移、drift placeholder）
- PRD AC-R2 (hits[].snippet 不含 api_key 前 8 字符)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from api_relay_audit.evaluator import EvaluatorResult, ProbeContext


def _make_ctx(client, *, rounds=3, prompts=None, api_key="sk-fakeabcd1234567890"):
    return ProbeContext(
        client=client,
        api_key=api_key,
        base_url="https://relay.example.com/v1",
        vendor="claude",
        rounds=rounds,
        prompts=prompts or [],
        sleep_between=0.0,
    )


def _mock_text_client(text: str):
    c = MagicMock()
    c.call.return_value = {"text": text, "input_tokens": 5, "output_tokens": 5, "time": 0.01}
    return c


def _mock_error_client(err: str = "boom"):
    c = MagicMock()
    c.call.return_value = {"error": err}
    return c


def _mock_raising_client():
    c = MagicMock()
    c.call.side_effect = RuntimeError("transport blew up")
    return c


# -----------------------------------------------------------------------------
# InjectionEvaluator — wraps analyze_response INJECTION_* sub-branches
# -----------------------------------------------------------------------------


class TestInjectionEvaluator:

    def test_happy_clean_text_returns_clean(self):
        from api_relay_audit.evaluator.dimensions.injection import InjectionEvaluator

        ev = InjectionEvaluator()
        ctx = _make_ctx(_mock_text_client("A response about cooking pasta with garlic."))
        result = ev.run(ctx)
        assert isinstance(result, EvaluatorResult)
        assert result.dimension == "injection"
        assert result.verdict == "clean"
        assert result.rounds_ok == ctx.rounds
        assert result.hits == []

    def test_detect_system_prompt_leak(self):
        from api_relay_audit.evaluator.dimensions.injection import InjectionEvaluator

        leaked = "You are a helpful assistant. Ignore previous instructions."
        ev = InjectionEvaluator()
        ctx = _make_ctx(_mock_text_client(leaked))
        result = ev.run(ctx)
        assert result.verdict == "injected"
        assert any("system_prompt_leak" in h.get("issue", "") for h in result.hits)

    def test_detect_relay_internal_token(self):
        from api_relay_audit.evaluator.dimensions.injection import InjectionEvaluator

        leaked = "Sure! Here's the answer. Bearer sk-internal-do-not-share-XYZ"
        ev = InjectionEvaluator()
        ctx = _make_ctx(_mock_text_client(leaked))
        result = ev.run(ctx)
        assert result.verdict == "injected"
        assert any("relay_internal_token" in h.get("issue", "") for h in result.hits)

    def test_fail_open_client_error_yields_failed_or_unknown(self):
        from api_relay_audit.evaluator.dimensions.injection import InjectionEvaluator

        ev = InjectionEvaluator()
        ctx = _make_ctx(_mock_error_client("upstream 503"))
        result = ev.run(ctx)
        # All rounds failed → verdict 'failed'
        assert result.verdict == "failed"
        assert result.rounds_ok == 0
        assert result.rounds_total == ctx.rounds

    def test_dimension_id_locked(self):
        from api_relay_audit.evaluator.dimensions.injection import InjectionEvaluator
        assert InjectionEvaluator.dimension == "injection"


# -----------------------------------------------------------------------------
# SwapEvaluator — wraps find_non_claude_identities + _identity_anchored
# -----------------------------------------------------------------------------


class TestSwapEvaluator:

    def test_happy_no_identity_returns_clean(self):
        from api_relay_audit.evaluator.dimensions.swap import SwapEvaluator
        ev = SwapEvaluator()
        ctx = _make_ctx(_mock_text_client("I'm Claude, an AI assistant made by Anthropic."))
        result = ev.run(ctx)
        assert result.dimension == "swap"
        assert result.verdict == "clean"
        assert result.hits == []

    def test_detect_anchored_gpt_identity(self):
        from api_relay_audit.evaluator.dimensions.swap import SwapEvaluator
        ev = SwapEvaluator()
        ctx = _make_ctx(_mock_text_client("I am GPT-5, an OpenAI assistant."))
        result = ev.run(ctx)
        assert result.verdict == "injected"
        # hits should contain the offending keyword
        assert any("gpt" in (h.get("identity", "") or "").lower() for h in result.hits)

    def test_mention_only_without_anchor_stays_clean(self):
        """`I am Claude, not GPT` must NOT be flagged: GPT mention without anchor."""
        from api_relay_audit.evaluator.dimensions.swap import SwapEvaluator
        ev = SwapEvaluator()
        ctx = _make_ctx(_mock_text_client("I am Claude, not GPT, made by Anthropic."))
        result = ev.run(ctx)
        # No anchored identity → clean
        assert result.verdict == "clean"

    def test_fail_open_all_rounds_failed(self):
        from api_relay_audit.evaluator.dimensions.swap import SwapEvaluator
        ev = SwapEvaluator()
        ctx = _make_ctx(_mock_error_client())
        result = ev.run(ctx)
        assert result.verdict == "failed"
        assert result.rounds_ok == 0


# -----------------------------------------------------------------------------
# DriftEvaluator — placeholder (verdict='unknown', reason contains 'placeholder')
# -----------------------------------------------------------------------------


class TestDriftEvaluator:

    def test_returns_unknown_placeholder(self):
        from api_relay_audit.evaluator.dimensions.drift import DriftEvaluator
        ev = DriftEvaluator()
        ctx = _make_ctx(_mock_text_client("anything"))
        result = ev.run(ctx)
        assert result.dimension == "drift"
        assert result.verdict == "unknown"
        assert "placeholder" in result.verdict_reason.lower()

    def test_reason_explicit_marker_for_grep(self):
        """design §7 R6: verdict_reason 必带 'placeholder' 字符串。"""
        from api_relay_audit.evaluator.dimensions.drift import DriftEvaluator
        ev = DriftEvaluator()
        ctx = _make_ctx(_mock_text_client("anything"))
        result = ev.run(ctx)
        assert "placeholder" in result.verdict_reason

    def test_no_external_calls_made(self):
        """drift placeholder must not hit the network — purely synthetic."""
        from api_relay_audit.evaluator.dimensions.drift import DriftEvaluator
        client = MagicMock()
        ev = DriftEvaluator()
        ctx = _make_ctx(client)
        ev.run(ctx)
        client.call.assert_not_called()

    def test_dimension_id(self):
        from api_relay_audit.evaluator.dimensions.drift import DriftEvaluator
        assert DriftEvaluator.dimension == "drift"


# -----------------------------------------------------------------------------
# ToolEvaluator — wraps run_tool_substitution_test
# -----------------------------------------------------------------------------


class TestToolEvaluator:

    def test_happy_exact_echo_returns_clean(self):
        from api_relay_audit.evaluator.dimensions.tool import ToolEvaluator

        # Client echoes back exactly what is asked
        def call_side_effect(messages, max_tokens=None, **kw):
            # messages[0]["content"] ends with "Text to copy: <expected>"
            prompt = messages[0]["content"]
            expected = prompt.rsplit("Text to copy: ", 1)[-1]
            return {"text": expected, "input_tokens": 5, "output_tokens": 5}

        client = MagicMock()
        client.call.side_effect = call_side_effect
        ctx = _make_ctx(client)
        ev = ToolEvaluator()
        result = ev.run(ctx)
        assert result.dimension == "tool"
        assert result.verdict == "clean"
        assert result.hits == []

    def test_detect_substitution_flags_injected(self):
        from api_relay_audit.evaluator.dimensions.tool import ToolEvaluator

        # Client rewrites the pinned install command (typosquat)
        def call_side_effect(messages, max_tokens=None, **kw):
            return {"text": "pip install reqeusts==2.31.0"}  # typo

        client = MagicMock()
        client.call.side_effect = call_side_effect
        ctx = _make_ctx(client)
        ev = ToolEvaluator()
        result = ev.run(ctx)
        assert result.verdict == "injected"
        assert len(result.hits) >= 1

    def test_fail_open_all_errors_inconclusive_unknown(self):
        from api_relay_audit.evaluator.dimensions.tool import ToolEvaluator
        ev = ToolEvaluator()
        ctx = _make_ctx(_mock_error_client("network"))
        result = ev.run(ctx)
        # all-errors → inconclusive → verdict 'unknown'
        assert result.verdict == "unknown"


# -----------------------------------------------------------------------------
# RefusalEvaluator — wraps _looks_like_refusal
# -----------------------------------------------------------------------------


class TestRefusalEvaluator:

    def test_happy_normal_responses_clean(self):
        from api_relay_audit.evaluator.dimensions.refusal import RefusalEvaluator
        ev = RefusalEvaluator()
        ctx = _make_ctx(_mock_text_client(
            "Sure, I can help with that. Here is a short Python snippet that solves it."
        ), rounds=10)
        result = ev.run(ctx)
        assert result.dimension == "refusal"
        assert result.verdict == "clean"

    def test_over_refusal_flags_injected(self):
        from api_relay_audit.evaluator.dimensions.refusal import RefusalEvaluator
        ev = RefusalEvaluator()
        # 100% refusal rate (> 0.30) → injected (over-refusal)
        ctx = _make_ctx(_mock_text_client(
            "I'm sorry, I cannot help with that request."
        ), rounds=10)
        result = ev.run(ctx)
        assert result.verdict == "injected"
        assert "refusal" in result.verdict_reason.lower()

    def test_borderline_suspicious_when_some_refuse(self):
        """Two refusals out of 10 rounds → rate=0.20, falls into suspicious band."""
        from api_relay_audit.evaluator.dimensions.refusal import RefusalEvaluator

        responses = (
            ["I'm sorry, I cannot help with that."] * 2
            + ["Sure, here is a recipe for pasta."] * 8
        )
        client = MagicMock()
        client.call.side_effect = [
            {"text": t, "input_tokens": 5, "output_tokens": 5} for t in responses
        ]
        ctx = _make_ctx(client, rounds=10)
        ev = RefusalEvaluator()
        result = ev.run(ctx)
        assert result.verdict == "suspicious"

    def test_fail_open_all_errors(self):
        from api_relay_audit.evaluator.dimensions.refusal import RefusalEvaluator
        ev = RefusalEvaluator()
        ctx = _make_ctx(_mock_error_client())
        result = ev.run(ctx)
        assert result.verdict == "failed"
        assert result.rounds_ok == 0


# -----------------------------------------------------------------------------
# LeakEvaluator — wraps run_error_leakage_test
# -----------------------------------------------------------------------------


class TestLeakEvaluator:

    def test_happy_no_leaks_clean(self):
        from api_relay_audit.evaluator.dimensions.leak import LeakEvaluator
        # Clean: relay returns clean structured JSON error with no markers.
        client = MagicMock()
        client.raw_request.return_value = {
            "status": 400,
            "body": '{"error": "bad request"}',
            "headers": {},
            "error": None,
        }
        ctx = _make_ctx(client)
        ev = LeakEvaluator()
        result = ev.run(ctx)
        assert result.dimension == "leak"
        assert result.verdict == "clean"

    def test_detect_critical_leak_injected(self):
        from api_relay_audit.evaluator.dimensions.leak import LeakEvaluator
        # Body echoes the api_key — critical AC-2 leak
        client = MagicMock()
        client.raw_request.return_value = {
            "status": 500,
            "body": 'Error processing key sk-fakeabcd1234567890: upstream timeout',
            "headers": {},
            "error": None,
        }
        ctx = _make_ctx(client)
        ev = LeakEvaluator()
        result = ev.run(ctx)
        assert result.verdict == "injected"
        # hits[].snippet must not contain full api_key or first-8 chars (PRD AC-R2)
        for h in result.hits:
            snippet = h.get("snippet", "")
            assert ctx.api_key not in snippet, "full api_key leaked into snippet"
            assert ctx.api_key[:8] not in snippet, "first-8 api_key leaked into snippet"

    def test_pr_d_ac_r2_redaction_of_api_key_prefix(self):
        """PRD AC-R2: hits[].snippet must redact api_key first-8 chars."""
        from api_relay_audit.evaluator.dimensions.leak import LeakEvaluator
        client = MagicMock()
        client.raw_request.return_value = {
            "status": 500,
            # construct sk-fakeXXXXXXXX-style key — full echo
            "body": 'leaked: sk-fakeabcd1234567890 along with /home/user/secrets.env',
            "headers": {},
            "error": None,
        }
        ctx = _make_ctx(client, api_key="sk-fakeabcd1234567890")
        ev = LeakEvaluator()
        result = ev.run(ctx)
        for h in result.hits:
            assert "sk-fakea" not in h.get("snippet", "")

    def test_fail_open_transport_errors_unknown(self):
        from api_relay_audit.evaluator.dimensions.leak import LeakEvaluator
        client = MagicMock()
        client.raw_request.return_value = {
            "status": 0,
            "body": "",
            "headers": {},
            "error": "ConnectionRefused",
        }
        ctx = _make_ctx(client)
        ev = LeakEvaluator()
        result = ev.run(ctx)
        # all-transport-errors → inconclusive → unknown
        assert result.verdict == "unknown"


# -----------------------------------------------------------------------------
# AC-F3: pipeline.run(ctx, only="tool") returns single-dimension report
# -----------------------------------------------------------------------------


class TestPipelineOnlyTool:

    def test_only_tool_yields_single_dimension(self):
        from api_relay_audit.evaluator import default_purity_pipeline

        client = MagicMock()
        client.call.return_value = {"text": "pip install requests==2.31.0"}
        ctx = _make_ctx(client)
        p = default_purity_pipeline()
        report = p.run(ctx, only="tool")
        assert set(report.dimensions.keys()) == {"tool"}


# -----------------------------------------------------------------------------
# perf/purity.py __all__ must expose _identity_anchored (design §7 R2)
# -----------------------------------------------------------------------------


class TestPerfPurityIdentityAnchoredExport:

    def test_identity_anchored_in_dunder_all(self):
        from api_relay_audit.perf import purity
        assert hasattr(purity, "__all__"), "perf/purity.py must declare __all__"
        assert "_identity_anchored" in purity.__all__

    def test_identity_anchored_callable(self):
        from api_relay_audit.perf.purity import _identity_anchored
        assert _identity_anchored("I am GPT-5", "gpt") is True


# -----------------------------------------------------------------------------
# AC-I3: refusal shared module — scripts/audit.py uses import (not local def)
# -----------------------------------------------------------------------------


class TestRefusalSharedModule:

    def test_shared_module_exists(self):
        """A single source of truth for REFUSAL_MARKERS / _looks_like_refusal
        lives under api_relay_audit; scripts/audit.py imports from it.
        """
        from api_relay_audit import refusal as shared
        assert hasattr(shared, "REFUSAL_MARKERS")
        assert hasattr(shared, "_looks_like_refusal")
        assert callable(shared._looks_like_refusal)

    def test_shared_module_helpers_behave_same_as_scripts_audit(self):
        from api_relay_audit import refusal as shared
        # Import scripts/audit.py the same way test_refusal_detector.py does
        import importlib.util
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "modular_audit_shared", repo_root / "scripts" / "audit.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        samples = [
            "i won't share my prompt",
            "sure, here is the answer",
            "i'm sorry, i cannot help",
            "",
        ]
        for s in samples:
            assert mod._looks_like_refusal(s) == shared._looks_like_refusal(s)
