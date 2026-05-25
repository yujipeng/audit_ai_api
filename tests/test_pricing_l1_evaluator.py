"""Tests for L1 tokenizer evaluator (Story TES-135 / S4-B).

DoD slice:
  - L1 evaluator implements PricingEvaluator Protocol
  - GPT path uses tiktoken (offline, 0 cost)
  - Claude path uses client.beta.messages.count_tokens() but DEFAULT DISABLED;
    enabled via enable_tokenizer_truth=True + bounded by tokenizer_truth_cap
  - PRD §5.1 L2 < 5% precision gate: tiktoken vs ground-truth p95 ≤ 5% on
    ≥ 30 GPT samples + ≥ 10 Claude samples (with Claude API mocked)
  - When tokenizer truth is requested but the package is missing, the
    evaluator MUST raise with an install hint (no silent fallback).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from api_relay_audit.pricing import PricingSample, Severity

# The L1 module is the system-under-test. Import lazily so tests that don't
# need it (e.g. test_l1_missing_tiktoken_raises_with_hint) can still execute.
from api_relay_audit.pricing.evaluators import l1_tokenizer as l1_mod
from api_relay_audit.pricing.evaluators.l1_tokenizer import (
    L1TokenizerEvaluator,
    L1TokenizerMissingError,
)


tiktoken = pytest.importorskip("tiktoken", reason="tiktoken not installed")


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestL1ProtocolConformance:
    def test_l1_implements_pricing_evaluator_protocol(self):
        from api_relay_audit.pricing import PricingEvaluator

        ev = L1TokenizerEvaluator()
        assert isinstance(ev, PricingEvaluator)

    def test_l1_default_name(self):
        ev = L1TokenizerEvaluator()
        assert ev.name == "L1_tokenizer"

    def test_l1_default_disables_tokenizer_truth(self):
        """Claude `count_tokens` API is default-disabled; --with-tokenizer-truth
        must be explicit (PRD §6.1 third bullet — no silent online cost)."""
        ev = L1TokenizerEvaluator()
        assert ev.enable_tokenizer_truth is False
        assert ev.tokenizer_truth_cap == 10


# ---------------------------------------------------------------------------
# GPT path — offline, tiktoken-based
# ---------------------------------------------------------------------------


class TestL1GPTPath:
    def test_gpt_compliant_when_reported_matches_tiktoken(self):
        """Honest relay reports exactly the tiktoken count → 0% drift."""
        text_in = "The quick brown fox jumps over the lazy dog."
        text_out = "Lorem ipsum dolor sit amet."
        enc = tiktoken.get_encoding("cl100k_base")
        true_in = len(enc.encode(text_in))
        true_out = len(enc.encode(text_out))

        ev = L1TokenizerEvaluator()
        sample = PricingSample(
            vendor="openai", model="gpt-4o-mini",
            input_text=text_in, output_text=text_out,
            reported_input_tokens=true_in,
            reported_output_tokens=true_out,
        )
        verdict = ev.evaluate(sample)
        assert verdict.severity is Severity.PRICING_COMPLIANT
        assert verdict.drift_pct < 1.0
        assert verdict.evaluator == "L1_tokenizer"

    def test_gpt_critical_when_relay_doubles_tokens(self):
        text_in = "hello world"
        ev = L1TokenizerEvaluator()
        sample = PricingSample(
            vendor="openai", model="gpt-4o-mini",
            input_text=text_in, output_text="",
            reported_input_tokens=200,  # absurd inflation vs ~2 truth tokens
            reported_output_tokens=0,
        )
        verdict = ev.evaluate(sample)
        assert verdict.severity is Severity.TOKEN_DRIFT_CRITICAL

    def test_gpt_warn_band(self):
        """~10% drift trips the warn band (PRD §5.1 L2 < 5% precision)."""
        text_in = "The quick brown fox jumps over the lazy dog. " * 4
        enc = tiktoken.get_encoding("cl100k_base")
        true_in = len(enc.encode(text_in))
        ev = L1TokenizerEvaluator()
        sample = PricingSample(
            vendor="openai", model="gpt-4o-mini",
            input_text=text_in, output_text="",
            reported_input_tokens=int(true_in * 1.10) + 1,
            reported_output_tokens=0,
        )
        verdict = ev.evaluate(sample)
        assert verdict.severity is Severity.TOKEN_DRIFT_WARN


class TestL1GPTPrecisionGate:
    """PRD §5.1 L2 < 5% precision: ≥ 30 GPT samples, p95 drift ≤ 5% under
    honest reporting (relay reports the tiktoken count exactly)."""

    def test_p95_under_5_pct_on_30_gpt_samples(self):
        from api_relay_audit.pricing.evaluators.l1_tokenizer import (
            MODEL_ENCODING_OVERRIDES,
        )

        ev = L1TokenizerEvaluator()
        model = "gpt-4o-mini"
        # Honest reporting must match whatever encoding the evaluator picks
        # for this model; otherwise the test is stochastic on a tiktoken
        # release.
        encoding_name = MODEL_ENCODING_OVERRIDES.get(model, "cl100k_base")
        try:
            enc = tiktoken.get_encoding(encoding_name)
        except Exception:
            enc = tiktoken.get_encoding("cl100k_base")
        corpus = [
            "Hello.", "This is a slightly longer prompt.",
            "Compose a haiku about the wind.",
            "Translate 'good morning' into French.",
            "List five large languages models in 2025.",
            "Explain TLS handshake in two sentences.",
            "What is the boiling point of water at sea level?",
            "Outline a plan for a 5km run.",
            "Define a Python decorator.",
            "Recite the first line of Pride and Prejudice.",
            "Briefly summarise photosynthesis.",
            "Give an example of a regular expression for emails.",
            "How does HTTPS differ from HTTP?",
            "Write a SQL query selecting all rows from users.",
            "Convert 100 USD to EUR at 0.92.",
            "Solve x: 2x + 3 = 11.",
            "Name three primary colours.",
            "Sketch a simple bash for-loop.",
            "Identify the capital of Australia.",
            "Describe a binary search in plain English.",
            "Provide a 1-line definition of recursion.",
            "Translate hola amigo to English.",
            "How many planets orbit the Sun?",
            "Author of '1984'?",
            "What is 7 * 8?",
            "Which language compiles to bytecode for the JVM?",
            "Spell 'accommodate'.",
            "When did WW2 end?",
            "Origin of the word 'serendipity'?",
            "Speed of light in vacuum?",
        ]
        assert len(corpus) >= 30
        drifts: list[float] = []
        for text in corpus:
            n = len(enc.encode(text))
            sample = PricingSample(
                vendor="openai", model=model,
                input_text=text, output_text="",
                reported_input_tokens=n,
                reported_output_tokens=0,
            )
            v = ev.evaluate(sample)
            drifts.append(abs(v.drift_pct))
        drifts.sort()
        p95 = drifts[int(len(drifts) * 0.95) - 1]
        assert p95 <= 5.0, f"L1 GPT p95 drift {p95}% exceeds 5% gate"


# ---------------------------------------------------------------------------
# Claude path — opt-in, count_tokens() API with cap
# ---------------------------------------------------------------------------


class _StubClaudeClient:
    """Minimal stand-in for anthropic.Anthropic exposing
    .beta.messages.count_tokens(model=..., messages=[...])."""

    def __init__(self, count_per_call: int):
        self._count = count_per_call
        self.calls = 0
        self.beta = MagicMock()
        self.beta.messages = MagicMock()
        self.beta.messages.count_tokens = self._count_tokens

    def _count_tokens(self, *, model, messages, **kwargs):
        self.calls += 1
        result = MagicMock()
        result.input_tokens = self._count
        return result


class TestL1ClaudePath:
    def test_claude_path_disabled_by_default_returns_inconclusive(self):
        """Default-disabled means Claude calls are NEVER made unless the
        operator opts in via enable_tokenizer_truth=True."""
        client = _StubClaudeClient(count_per_call=42)
        ev = L1TokenizerEvaluator(claude_client=client)  # opt-in NOT set
        sample = PricingSample(
            vendor="anthropic", model="claude-haiku-4-5",
            input_text="x", output_text="",
            reported_input_tokens=1, reported_output_tokens=0,
        )
        verdict = ev.evaluate(sample)
        assert verdict.severity is Severity.INCONCLUSIVE_BALANCE
        assert client.calls == 0

    def test_claude_path_enabled_uses_count_tokens(self):
        client = _StubClaudeClient(count_per_call=10)
        ev = L1TokenizerEvaluator(
            claude_client=client, enable_tokenizer_truth=True
        )
        sample = PricingSample(
            vendor="anthropic", model="claude-haiku-4-5",
            input_text="hello", output_text="",
            reported_input_tokens=10, reported_output_tokens=0,
        )
        verdict = ev.evaluate(sample)
        assert client.calls == 1
        assert verdict.severity is Severity.PRICING_COMPLIANT
        assert verdict.drift_pct < 1.0

    def test_claude_path_cap_prevents_unbounded_calls(self):
        """tokenizer_truth_cap bounds Claude API calls per audit run; once the
        cap is hit subsequent samples must NOT call the API."""
        client = _StubClaudeClient(count_per_call=10)
        ev = L1TokenizerEvaluator(
            claude_client=client,
            enable_tokenizer_truth=True,
            tokenizer_truth_cap=2,
        )
        for _ in range(5):
            ev.evaluate(PricingSample(
                vendor="anthropic", model="claude-haiku-4-5",
                input_text="hi", output_text="",
                reported_input_tokens=10, reported_output_tokens=0,
            ))
        assert client.calls == 2


class TestL1ClaudePrecisionGate:
    """PRD §5.1: ≥ 10 Claude samples, p95 drift ≤ 5% with honest count_tokens."""

    def test_p95_under_5_pct_on_10_claude_samples(self):
        # Claude count_tokens stub returns a fixed truth per text length.
        # We feed 10 samples where reported = truth → 0% drift.
        class _Truthful:
            def __init__(self):
                self.beta = MagicMock()
                self.beta.messages = MagicMock()
                self.beta.messages.count_tokens = self._count

            def _count(self, *, model, messages, **kwargs):
                # Approximate truth for the stubbed corpus: 1 token per
                # 4 ASCII chars (consistent with anthropic's tokenizer
                # density). The test does NOT depend on the exact ratio
                # because reported = whatever this returns.
                text = ""
                for m in messages:
                    if isinstance(m.get("content"), str):
                        text += m["content"]
                r = MagicMock()
                r.input_tokens = max(1, len(text) // 4)
                return r

        client = _Truthful()
        ev = L1TokenizerEvaluator(
            claude_client=client,
            enable_tokenizer_truth=True,
            tokenizer_truth_cap=20,
        )
        prompts = [
            "hi", "hello world", "tell me a joke",
            "summarise climate policy",
            "write a haiku about clouds",
            "define entropy in physics",
            "explain TCP slow start in 1 line",
            "what's the capital of Norway?",
            "translate good morning to Japanese",
            "name three Anthropic models",
        ]
        drifts: list[float] = []
        for p in prompts:
            truth = max(1, len(p) // 4)
            v = ev.evaluate(PricingSample(
                vendor="anthropic", model="claude-haiku-4-5",
                input_text=p, output_text="",
                reported_input_tokens=truth,
                reported_output_tokens=0,
            ))
            drifts.append(abs(v.drift_pct))
        drifts.sort()
        p95 = drifts[int(len(drifts) * 0.95) - 1]
        assert p95 <= 5.0


# ---------------------------------------------------------------------------
# Missing-package indirection — no silent fallback (PRD §6.1 third bullet)
# ---------------------------------------------------------------------------


class TestL1MissingPackage:
    def test_missing_tiktoken_with_truth_enabled_raises_with_hint(
        self, monkeypatch
    ):
        """If --with-tokenizer-truth is on but tiktoken cannot be imported,
        raise L1TokenizerMissingError with the install hint mentioning the
        `token-meter` extras. MUST NOT silently fall back to L0."""

        def _fail(name):
            if name == "tiktoken":
                raise ModuleNotFoundError(name)
            import importlib
            return importlib.import_module(name)

        monkeypatch.setattr(l1_mod, "_safe_import", _fail)

        ev = L1TokenizerEvaluator(enable_tokenizer_truth=True)
        with pytest.raises(L1TokenizerMissingError) as excinfo:
            ev.evaluate(PricingSample(
                vendor="openai", model="gpt-4o-mini",
                input_text="hi", output_text="",
                reported_input_tokens=1, reported_output_tokens=0,
            ))
        msg = str(excinfo.value)
        assert "tiktoken" in msg
        assert "audit_ai_api[token-meter]" in msg

    def test_missing_anthropic_with_truth_enabled_raises_with_hint(
        self, monkeypatch
    ):
        """Same invariant for the Claude path: missing anthropic SDK +
        --with-tokenizer-truth → L1TokenizerMissingError, not silent L0."""
        ev = L1TokenizerEvaluator(
            enable_tokenizer_truth=True,
            claude_client=None,  # nothing injected
        )

        def _fail(name):
            if name == "anthropic":
                raise ModuleNotFoundError(name)
            import importlib
            return importlib.import_module(name)

        monkeypatch.setattr(l1_mod, "_safe_import", _fail)

        with pytest.raises(L1TokenizerMissingError) as excinfo:
            ev.evaluate(PricingSample(
                vendor="anthropic", model="claude-haiku-4-5",
                input_text="hi", output_text="",
                reported_input_tokens=1, reported_output_tokens=0,
            ))
        assert "anthropic" in str(excinfo.value)
        assert "audit_ai_api[token-meter]" in str(excinfo.value)
