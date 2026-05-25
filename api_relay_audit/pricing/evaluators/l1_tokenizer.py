"""L1 tokenizer evaluator (design §8 L1, Story TES-135 / S4-B).

Two paths:

  - **GPT path** — uses ``tiktoken`` locally. Offline, 0 incremental cost. The
    default tokenizer is ``cl100k_base`` (GPT-4 / GPT-4o family); the
    ``MODEL_ENCODING_OVERRIDES`` table allows per-model overrides without a
    new release.

  - **Claude path** — uses ``client.beta.messages.count_tokens()`` against an
    anthropic.Anthropic instance the operator provides. This costs API quota
    (one count_tokens call per audited sample), so it is **off by default**.
    The operator must set ``enable_tokenizer_truth=True`` AND the audit run
    enforces ``tokenizer_truth_cap`` (default 10) calls per
    evaluator instance — once the cap is hit, further Claude samples return
    :attr:`Severity.INCONCLUSIVE_BALANCE` without contacting the API.

Missing-package contract (PRD §6.1.3 third bullet + §6.3.2):

  - The module ships in the default distribution; it imports cleanly even
    when ``tiktoken``/``anthropic`` are not installed (lazy import via
    :func:`_safe_import`).
  - When :meth:`L1TokenizerEvaluator.evaluate` is called WITH
    ``enable_tokenizer_truth=True`` and the required package is absent, the
    evaluator raises :class:`L1TokenizerMissingError` with an install hint
    pointing at the ``token-meter`` extras. **It MUST NOT silently fall back
    to the L0 evaluator** — a covert downgrade would defeat the audit
    contract.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Optional

from api_relay_audit.pricing.evaluators.base import (
    PricingSample,
    PricingVerdict,
    Severity,
)


TOKEN_DRIFT_WARN_PCT = 5.0   # PRD §5.1 L2 < 5% precision gate
TOKEN_DRIFT_CRITICAL_PCT = 30.0


MODEL_ENCODING_OVERRIDES: dict[str, str] = {
    # tiktoken-known encodings; falls back to cl100k_base otherwise.
    "gpt-4o-mini": "o200k_base",
    "gpt-4o": "o200k_base",
    "gpt-4-turbo": "cl100k_base",
    "gpt-3.5-turbo": "cl100k_base",
}


class L1TokenizerMissingError(RuntimeError):
    """Raised when the L1 path is requested but its package is absent.

    The error message always contains the missing package name AND the
    install hint ``pip install audit_ai_api[token-meter]`` so operators
    have a single actionable next step.
    """


def _safe_import(name: str):
    """Indirection point so tests can pretend a package is missing."""
    return importlib.import_module(name)


def _missing_pkg_msg(pkg: str) -> str:
    return (
        f"L1 tokenizer evaluator requires the '{pkg}' package, which is not "
        "installed. Run: pip install audit_ai_api[token-meter] "
        "(or pip install " + pkg + " directly). "
        "L1 will NOT silently fall back to L0 — see PRD §6.1.3."
    )


def _drift_pct(expected: float, actual: float) -> float:
    if expected <= 0:
        return 100.0 if actual != 0 else 0.0
    return ((actual - expected) / expected) * 100.0


def _classify(drift_pct: float) -> Severity:
    a = abs(drift_pct)
    if a < TOKEN_DRIFT_WARN_PCT:
        return Severity.PRICING_COMPLIANT
    if a < TOKEN_DRIFT_CRITICAL_PCT:
        return Severity.TOKEN_DRIFT_WARN
    return Severity.TOKEN_DRIFT_CRITICAL


# ---------------------------------------------------------------------------
# GPT path — tiktoken (offline)
# ---------------------------------------------------------------------------


def _gpt_count(text: str, model: str) -> int:
    """Return tiktoken's exact token count for ``text`` under ``model``.

    Raises :class:`L1TokenizerMissingError` if tiktoken is not installed.
    """
    try:
        tiktoken = _safe_import("tiktoken")
    except ModuleNotFoundError:
        raise L1TokenizerMissingError(_missing_pkg_msg("tiktoken"))

    encoding_name = MODEL_ENCODING_OVERRIDES.get(model)
    if encoding_name:
        try:
            enc = tiktoken.get_encoding(encoding_name)
        except Exception:
            enc = tiktoken.get_encoding("cl100k_base")
    else:
        # Try encoding_for_model, then fall back to cl100k_base.
        try:
            enc = tiktoken.encoding_for_model(model)
        except Exception:
            enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text or ""))


# ---------------------------------------------------------------------------
# Claude path — count_tokens (opt-in, capped)
# ---------------------------------------------------------------------------


def _build_claude_client() -> Any:
    """Construct a default ``anthropic.Anthropic`` client.

    Used only when ``enable_tokenizer_truth=True`` and the caller did NOT
    inject a client. Raises :class:`L1TokenizerMissingError` on missing SDK.
    """
    try:
        anthropic = _safe_import("anthropic")
    except ModuleNotFoundError:
        raise L1TokenizerMissingError(_missing_pkg_msg("anthropic"))
    return anthropic.Anthropic()


def _claude_count(client: Any, model: str, text: str) -> int:
    """Call ``client.beta.messages.count_tokens(model=model, messages=[...])``
    and return the integer ``input_tokens`` field.

    The shape mirrors the public Claude SDK; tests inject a stub client.
    """
    response = client.beta.messages.count_tokens(
        model=model,
        messages=[{"role": "user", "content": text or ""}],
    )
    return int(response.input_tokens)


# ---------------------------------------------------------------------------
# Public evaluator
# ---------------------------------------------------------------------------


@dataclass
class L1TokenizerEvaluator:
    """L1 tokenizer-truth evaluator — frozen-Protocol implementation."""

    name: str = "L1_tokenizer"
    enable_tokenizer_truth: bool = False
    tokenizer_truth_cap: int = 10
    claude_client: Optional[Any] = None
    _claude_calls: int = field(default=0, init=False, repr=False)

    # ---------- routing ----------

    def _is_anthropic(self, sample: PricingSample) -> bool:
        return (sample.vendor or "").lower() == "anthropic"

    # ---------- core ----------

    def evaluate(self, sample: PricingSample) -> PricingVerdict:
        if self._is_anthropic(sample):
            return self._evaluate_anthropic(sample)
        return self._evaluate_openai_family(sample)

    # ---------- GPT-family ----------

    def _evaluate_openai_family(self, sample: PricingSample) -> PricingVerdict:
        # GPT path uses tiktoken offline; this is "free" (no API spend), so
        # we run it for OpenAI samples regardless of enable_tokenizer_truth
        # IF tiktoken is available. If the operator explicitly asked for
        # truth and tiktoken is missing, raise. Otherwise be silent and
        # return INCONCLUSIVE so the orchestrator can downgrade to L0.
        try:
            true_in = _gpt_count(sample.input_text, sample.model)
            true_out = _gpt_count(sample.output_text, sample.model)
        except L1TokenizerMissingError:
            if self.enable_tokenizer_truth:
                raise
            return self._inconclusive(
                sample,
                "tiktoken not installed; L1 unavailable. "
                "Install audit_ai_api[token-meter] to enable L1.",
            )

        expected_total = true_in + true_out
        reported_total = (
            sample.reported_input_tokens + sample.reported_output_tokens
        )
        drift = _drift_pct(expected_total, reported_total)
        severity = _classify(drift)
        return PricingVerdict(
            severity=severity,
            evaluator=self.name,
            drift_pct=abs(drift),
            details={
                "tokenizer": "tiktoken",
                "true_input_tokens": true_in,
                "true_output_tokens": true_out,
                "true_total_tokens": expected_total,
                "reported_input_tokens": sample.reported_input_tokens,
                "reported_output_tokens": sample.reported_output_tokens,
                "reported_total_tokens": reported_total,
                "token_drift_pct": round(drift, 4),
            },
        )

    # ---------- Anthropic ----------

    def _evaluate_anthropic(self, sample: PricingSample) -> PricingVerdict:
        if not self.enable_tokenizer_truth:
            return self._inconclusive(
                sample,
                "Claude count_tokens disabled (opt in via "
                "--with-tokenizer-truth).",
            )
        if self._claude_calls >= self.tokenizer_truth_cap:
            return self._inconclusive(
                sample,
                f"Claude count_tokens cap reached "
                f"({self.tokenizer_truth_cap}); further samples skipped.",
            )

        client = self.claude_client
        if client is None:
            client = _build_claude_client()  # raises if anthropic missing
            self.claude_client = client

        try:
            true_in = (
                _claude_count(client, sample.model, sample.input_text)
                if sample.input_text else 0
            )
            if sample.input_text:
                self._claude_calls += 1
            true_out = (
                _claude_count(client, sample.model, sample.output_text)
                if sample.output_text else 0
            )
            if sample.output_text:
                self._claude_calls += 1
        except L1TokenizerMissingError:
            raise  # Always surface missing-SDK errors.

        expected_total = true_in + true_out
        reported_total = (
            sample.reported_input_tokens + sample.reported_output_tokens
        )
        drift = _drift_pct(expected_total, reported_total)
        severity = _classify(drift)
        return PricingVerdict(
            severity=severity,
            evaluator=self.name,
            drift_pct=abs(drift),
            details={
                "tokenizer": "anthropic.count_tokens",
                "true_input_tokens": true_in,
                "true_output_tokens": true_out,
                "true_total_tokens": expected_total,
                "reported_input_tokens": sample.reported_input_tokens,
                "reported_output_tokens": sample.reported_output_tokens,
                "reported_total_tokens": reported_total,
                "token_drift_pct": round(drift, 4),
                "tokenizer_truth_calls": self._claude_calls,
                "tokenizer_truth_cap": self.tokenizer_truth_cap,
            },
        )

    # ---------- helpers ----------

    def _inconclusive(
        self, sample: PricingSample, notes: str
    ) -> PricingVerdict:
        return PricingVerdict(
            severity=Severity.INCONCLUSIVE_BALANCE,
            evaluator=self.name,
            drift_pct=0.0,
            details={
                "tokenizer": None,
                "reported_input_tokens": sample.reported_input_tokens,
                "reported_output_tokens": sample.reported_output_tokens,
            },
            notes=notes,
        )


__all__ = [
    "L1TokenizerEvaluator",
    "L1TokenizerMissingError",
    "MODEL_ENCODING_OVERRIDES",
    "TOKEN_DRIFT_CRITICAL_PCT",
    "TOKEN_DRIFT_WARN_PCT",
]
