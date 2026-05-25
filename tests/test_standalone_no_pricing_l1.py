"""Standalone parity guard for pricing L1 (Story TES-135 / S4-B, R5 落地点 4).

PRD §6.3 first bullet:
- The standalone ``audit.py`` (zero-dep, curl-only distribution) MUST NOT
  expose any L1 / pricing tokenizer code. It surfaces only the L0 verdict
  (which is character-ratio based, no external deps).
- This test enforces that invariant via static analysis of ``audit.py``:
  no ``tiktoken`` / ``anthropic`` imports, no ``count_tokens`` calls, no
  ``L1TokenizerEvaluator`` references.

Rationale: the standalone distribution's value proposition is "drop one
file, run anywhere with stdlib only". Pulling in ``tiktoken`` (a 100MB
binary wheel) or the ``anthropic`` SDK breaks that. The modular package
(``api_relay_audit/pricing/evaluators/l1_tokenizer.py``) is where L1
lives.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def standalone_text() -> str:
    return (REPO_ROOT / "audit.py").read_text(encoding="utf-8")


class TestStandaloneNoPricingL1:
    """Static analysis: standalone audit.py forbids L1 tokenizer code."""

    def test_no_tiktoken_import(self, standalone_text):
        """``import tiktoken`` / ``from tiktoken ...`` must not appear."""
        # Allow the keyword to appear inside a string (e.g. a help message
        # pointing operators to the modular evaluator) but never as an
        # actual import statement.
        for line in standalone_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not re.match(r"^\s*(import|from)\s+tiktoken\b", stripped), (
                f"standalone audit.py imports tiktoken: {stripped!r} — "
                "L1 tokenizer code must live in the modular distribution only."
            )

    def test_no_anthropic_sdk_import(self, standalone_text):
        """``import anthropic`` / ``from anthropic ...`` must not appear."""
        for line in standalone_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not re.match(r"^\s*(import|from)\s+anthropic\b", stripped), (
                f"standalone audit.py imports anthropic SDK: {stripped!r} — "
                "Claude count_tokens must stay in the modular L1 path."
            )

    def test_no_count_tokens_call(self, standalone_text):
        """The Claude count_tokens API surface must not appear in
        standalone code (it'd require the SDK)."""
        # We forbid the actual API call shape, not the docstring / help
        # message. The check tolerates string literals containing the token
        # for documentation but flags any callable form.
        callable_patterns = [
            r"\.count_tokens\s*\(",
            r"messages\.count_tokens",
            r"beta\.messages\.count_tokens",
        ]
        for pat in callable_patterns:
            assert not re.search(pat, standalone_text), (
                f"standalone audit.py contains count_tokens callable "
                f"({pat!r}); restricted to modular L1."
            )

    def test_no_l1_evaluator_class(self, standalone_text):
        """The L1 evaluator class itself must not be embedded standalone.
        Allowed: a help line / comment naming it. Forbidden: a class
        definition or a constructor call."""
        assert not re.search(
            r"^\s*class\s+L1TokenizerEvaluator\b", standalone_text,
            flags=re.MULTILINE,
        ), "standalone audit.py defines L1TokenizerEvaluator — forbidden."
        assert not re.search(r"L1TokenizerEvaluator\s*\(", standalone_text), (
            "standalone audit.py constructs L1TokenizerEvaluator — forbidden."
        )

    def test_l0_loc_budget_within_80(self, standalone_text):
        """If a pricing L0 sliver is embedded into standalone, it must be
        ≤ 80 LOC and styled like the existing identity-pattern embed.

        Detection heuristic: look for any pricing-related symbol embedded
        directly. If none are present, the budget is trivially satisfied.
        """
        markers = [
            "L0CharacterRatioEvaluator",
            "TOKEN_DRIFT_WARN_PCT",
            "TOKEN_DRIFT_CRITICAL_PCT",
            "BALANCE_DRIFT_HIGH_PCT",
        ]
        embedded = any(m in standalone_text for m in markers)
        if not embedded:
            return  # Nothing pricing-related embedded — pass trivially.

        # Heuristic LOC count: lines mentioning any pricing marker plus 80
        # lines around them. We bound the local pricing block by counting
        # the smallest contiguous span containing all markers.
        lines = standalone_text.splitlines()
        hits = [i for i, ln in enumerate(lines) if any(m in ln for m in markers)]
        if hits:
            span = max(hits) - min(hits) + 1
            assert span <= 80, (
                f"Embedded pricing L0 block spans {span} LOC; budget is 80. "
                "Trim or move logic into api_relay_audit/pricing/."
            )


class TestStandaloneL0VerdictOnly:
    """Smoke: when the standalone audit is loaded as a module, no symbol
    named ``L1TokenizerEvaluator`` should leak into its namespace."""

    def test_loaded_module_lacks_l1_symbols(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_standalone_for_pricing_parity", REPO_ROOT / "audit.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for forbidden in ("L1TokenizerEvaluator", "L1TokenizerMissingError"):
            assert not hasattr(module, forbidden), (
                f"standalone audit.py exposes {forbidden!r} — must remain "
                "modular-only."
            )
