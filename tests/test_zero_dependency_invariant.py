"""Zero-dependency invariant (Story TES-135 / S4-B, R5 落地点 5).

PRD §6.1 third bullet + §6.3.2:
- Default install (without ``[token-meter]`` extras) MUST run
  ``audit pricing`` main path with the L0 evaluator only.
- ``--with-tokenizer-truth`` may be requested by the operator, but if the
  underlying packages (``tiktoken`` for GPT, ``anthropic`` for Claude) are
  absent, the program MUST surface an explicit error pointing at
  ``pip install audit_ai_api[token-meter]`` — silent fallback to L0 would
  be a covert downgrade of the audit's guarantees and is forbidden.

These tests run in the "default install" environment by simulating that the
optional extras are absent (monkey-patched ``_safe_import``) and then
re-running the L1 evaluator. Default behavior is then verified at the
``audit_ai_api.pricing`` boundary.
"""

from __future__ import annotations

import importlib
import pytest

from api_relay_audit.pricing import (
    PricingSample,
    Severity,
)
from api_relay_audit.pricing.evaluators import l0_character_ratio as l0_mod
from api_relay_audit.pricing.evaluators import l1_tokenizer as l1_mod
from api_relay_audit.pricing.evaluators.l1_tokenizer import (
    L1TokenizerEvaluator,
    L1TokenizerMissingError,
)


# ---------------------------------------------------------------------------
# Default install path: L0 evaluator + module imports stand alone
# ---------------------------------------------------------------------------


class TestDefaultInstallL0Path:
    def test_l0_imports_without_optional_extras(self, monkeypatch):
        """L0 evaluator chain must be importable even when tiktoken and
        anthropic are completely absent from the environment."""

        def _refuse(name):
            if name in ("tiktoken", "anthropic"):
                raise ModuleNotFoundError(name)
            return importlib.import_module(name)

        monkeypatch.setattr(l1_mod, "_safe_import", _refuse)

        # L0 evaluator must still work end-to-end on a vanilla sample.
        ev = l0_mod.L0CharacterRatioEvaluator()
        sample = PricingSample(
            vendor="openai", model="gpt-4o-mini",
            input_text="a" * 80, output_text="",
            reported_input_tokens=20, reported_output_tokens=0,
        )
        verdict = ev.evaluate(sample)
        assert verdict.severity is Severity.PRICING_COMPLIANT

    def test_l1_module_imports_without_optional_extras(self):
        """The L1 module itself must import lazily — it ships in the default
        distribution and only blows up when its ``evaluate()`` is called
        without the extras."""
        from api_relay_audit.pricing.evaluators import l1_tokenizer
        assert hasattr(l1_tokenizer, "L1TokenizerEvaluator")
        assert hasattr(l1_tokenizer, "L1TokenizerMissingError")


# ---------------------------------------------------------------------------
# --with-tokenizer-truth without extras: must raise, must NOT silently L0
# ---------------------------------------------------------------------------


class TestWithTokenizerTruthMissingExtras:
    def test_gpt_request_raises_with_install_hint(self, monkeypatch):
        def _refuse(name):
            if name == "tiktoken":
                raise ModuleNotFoundError(name)
            return importlib.import_module(name)

        monkeypatch.setattr(l1_mod, "_safe_import", _refuse)

        ev = L1TokenizerEvaluator(enable_tokenizer_truth=True)
        with pytest.raises(L1TokenizerMissingError) as excinfo:
            ev.evaluate(PricingSample(
                vendor="openai", model="gpt-4o-mini",
                input_text="hello", output_text="",
                reported_input_tokens=2, reported_output_tokens=0,
            ))
        msg = str(excinfo.value)
        assert "tiktoken" in msg
        assert "pip install" in msg
        assert "audit_ai_api[token-meter]" in msg

    def test_claude_request_raises_with_install_hint(self, monkeypatch):
        def _refuse(name):
            if name == "anthropic":
                raise ModuleNotFoundError(name)
            return importlib.import_module(name)

        monkeypatch.setattr(l1_mod, "_safe_import", _refuse)

        ev = L1TokenizerEvaluator(
            enable_tokenizer_truth=True, claude_client=None
        )
        with pytest.raises(L1TokenizerMissingError) as excinfo:
            ev.evaluate(PricingSample(
                vendor="anthropic", model="claude-haiku-4-5",
                input_text="hello", output_text="",
                reported_input_tokens=2, reported_output_tokens=0,
            ))
        msg = str(excinfo.value)
        assert "anthropic" in msg
        assert "audit_ai_api[token-meter]" in msg


# ---------------------------------------------------------------------------
# Extras declaration (token-meter)
# ---------------------------------------------------------------------------


class TestExtrasDeclaration:
    """``audit_ai_api[token-meter]`` MUST be a declared optional extra.

    pyproject.toml (when present) is the source of truth; absent that,
    setup.cfg / setup.py / extras manifest is acceptable. The point is that
    operators following the install-hint cannot fail with `unknown extra`.
    """

    def test_extras_token_meter_declared(self):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        candidates = [
            repo_root / "pyproject.toml",
            repo_root / "setup.cfg",
            repo_root / "setup.py",
            repo_root / "requirements-token-meter.txt",
        ]
        manifest_text = ""
        for c in candidates:
            if c.exists():
                manifest_text += c.read_text(encoding="utf-8")

        # Either pyproject extras, setup.cfg extras, requirements file,
        # or an explicit declaration in setup.py — any is acceptable as
        # long as `token-meter` (or `token_meter`) appears alongside both
        # tiktoken and anthropic.
        assert "tiktoken" in manifest_text, (
            "tiktoken must be declared in an extras manifest; install-hint "
            "would otherwise mislead users."
        )
        assert "anthropic" in manifest_text, (
            "anthropic must be declared in an extras manifest."
        )
        assert ("token-meter" in manifest_text
                or "token_meter" in manifest_text), (
            "`token-meter` extras name must appear in the manifest so that "
            "`pip install audit_ai_api[token-meter]` resolves."
        )
