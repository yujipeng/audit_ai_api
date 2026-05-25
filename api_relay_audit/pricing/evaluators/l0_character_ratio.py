"""L0 character-ratio evaluator (design §8 L0).

Cheapest pricing-drift detection layer:

1. Estimate expected token count from input/output character counts using the
   ``ratio_table.yaml`` entry for the (vendor, model, language) triple.
2. Compare expected vs reported tokens; classify drift into the closed
   :class:`Severity` enum.
3. Optionally, if the sample carries balance / cost fields, compute an
   estimated USD cost from ``prices.yaml`` and compare against the relay's
   reported cost or balance delta.

Bands (PRD §6.2):
- ``|drift_pct|`` < 30%   → ``pricing_compliant``
- 30% ≤ ``|drift_pct|`` < 100% → ``token_drift_warn``
- ``|drift_pct|`` ≥ 100%  → ``token_drift_critical``
- balance drift ≥ 25% (when reported) → ``balance_drift_high``

Precision-gate scope: the DoD precision gate against TES-94's ≥ 30 real-sample
corpus (p50 ≤ 30% / p95 ≤ 50%) is punted to Story-E (TES-94 follow-up). The
in-tree synthetic ``TestL0PrecisionFixture`` is a ratio-table-honest sanity
check only and must not be read as the empirical precision claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from api_relay_audit.pricing.evaluators.base import (
    PricingSample,
    PricingVerdict,
    Severity,
)
from api_relay_audit.pricing.prices import PriceQuote, load_prices

RATIO_TABLE_PATH = Path(__file__).parent.parent / "ratio_table.yaml"

# Drift bands. Conservative thresholds — tighter than PRD §5.1 ±20-30% L1
# precision so honest reports never trip the warn band.
TOKEN_DRIFT_WARN_PCT = 30.0
TOKEN_DRIFT_CRITICAL_PCT = 100.0
BALANCE_DRIFT_HIGH_PCT = 25.0


def load_ratio_table(path: Optional[Path] = None) -> dict:
    """Load ``ratio_table.yaml`` (default: bundled file)."""
    import yaml

    p = Path(path) if path else RATIO_TABLE_PATH
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _detect_lang(text: str) -> str:
    """Cheap heuristic: ``zh`` if any CJK code point is present, else ``en``.

    L0 only distinguishes the two extremes — finer language detection lives
    in Story-B / C if it's needed at all.
    """
    if not text:
        return "default"
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            return "zh"
    return "en"


def _lookup_ratio(ratios: dict, vendor: str, model: str, lang: str) -> float:
    """Walk ratios with vendor → model → lang fallback chain."""
    vendor_block = ratios.get(vendor) or ratios.get("_default") or {}
    model_block = vendor_block.get(model) or vendor_block.get("_default") or {}
    if not model_block:
        # Last-resort global default.
        model_block = (
            ratios.get("_default", {}).get("_default", {})
            or {"zh": 1.5, "en": 4.0, "default": 3.0}
        )
    return float(
        model_block.get(lang) or model_block.get("default") or 3.0
    )


def estimate_tokens_by_chars(
    text: str,
    vendor: str,
    model: str,
    *,
    ratios: Optional[dict] = None,
) -> float:
    """Return expected token count for ``text`` under (vendor, model)."""
    if not text:
        return 0.0
    if ratios is None:
        ratios = load_ratio_table()
    lang = _detect_lang(text)
    chars_per_token = _lookup_ratio(ratios, vendor, model, lang)
    if chars_per_token <= 0:
        chars_per_token = 3.0
    return len(text) / chars_per_token


def _drift_pct(expected: float, actual: float) -> float:
    if expected <= 0:
        # Can't compute a meaningful percentage when expected is zero —
        # treat any nonzero ``actual`` as 100% drift.
        return 100.0 if actual != 0 else 0.0
    return ((actual - expected) / expected) * 100.0


def _classify_token_severity(drift_pct: float) -> Severity:
    a = abs(drift_pct)
    if a < TOKEN_DRIFT_WARN_PCT:
        return Severity.PRICING_COMPLIANT
    if a < TOKEN_DRIFT_CRITICAL_PCT:
        return Severity.TOKEN_DRIFT_WARN
    return Severity.TOKEN_DRIFT_CRITICAL


@dataclass
class L0CharacterRatioEvaluator:
    """L0 evaluator — frozen-Protocol implementation."""

    name: str = "L0_character_ratio"
    ratio_table: Optional[dict] = None
    prices: Optional[dict] = None
    fx: Optional[dict[str, float]] = None

    def _ratios(self) -> dict:
        if self.ratio_table is None:
            self.ratio_table = load_ratio_table()
        return self.ratio_table

    def _prices(self) -> dict:
        if self.prices is None:
            try:
                self.prices = load_prices()
            except FileNotFoundError:
                self.prices = {}
        return self.prices

    def _fx_for(self, currency: str) -> float:
        if currency == "USD":
            return 1.0
        if self.fx and currency in self.fx:
            return float(self.fx[currency])
        # Sensible defaults if audit.yaml didn't set fx.
        return {"CNY": 0.14, "EUR": 1.08}.get(currency, 1.0)

    def _balance_finding(self, sample: PricingSample) -> Optional[tuple]:
        """If balance/cost data exists, return (drift_pct, expected_usd,
        reported_usd); else ``None``."""
        prices = self._prices()
        quote: Optional[PriceQuote] = None
        if sample.vendor in prices and sample.model in prices[sample.vendor]:
            quote = prices[sample.vendor][sample.model]
        if quote is None:
            return None

        fx = self._fx_for(quote.currency)
        expected_usd = quote.cost_for_tokens(
            sample.reported_input_tokens,
            sample.reported_output_tokens,
            fx_to_usd=fx,
        )
        if expected_usd <= 0:
            return None

        reported_usd: Optional[float] = sample.reported_cost_usd
        if reported_usd is None and sample.balance_delta_usd is not None:
            reported_usd = -sample.balance_delta_usd  # outflow = positive cost

        if reported_usd is None:
            return None

        drift = _drift_pct(expected_usd, reported_usd)
        return drift, expected_usd, reported_usd

    def evaluate(self, sample: PricingSample) -> PricingVerdict:
        ratios = self._ratios()

        expected_in = estimate_tokens_by_chars(
            sample.input_text, sample.vendor, sample.model, ratios=ratios
        )
        expected_out = estimate_tokens_by_chars(
            sample.output_text, sample.vendor, sample.model, ratios=ratios
        )
        expected_total = expected_in + expected_out
        reported_total = (
            sample.reported_input_tokens + sample.reported_output_tokens
        )
        token_drift = _drift_pct(expected_total, reported_total)
        token_severity = _classify_token_severity(token_drift)

        details: dict = {
            "expected_input_tokens": round(expected_in, 2),
            "expected_output_tokens": round(expected_out, 2),
            "expected_total_tokens": round(expected_total, 2),
            "reported_input_tokens": sample.reported_input_tokens,
            "reported_output_tokens": sample.reported_output_tokens,
            "reported_total_tokens": reported_total,
            "token_drift_pct": round(token_drift, 2),
        }

        balance = self._balance_finding(sample)
        if balance is not None:
            balance_drift, expected_usd, reported_usd = balance
            details.update({
                "expected_cost_usd": round(expected_usd, 6),
                "reported_cost_usd": round(reported_usd, 6),
                "balance_drift_pct": round(balance_drift, 2),
            })
            if abs(balance_drift) >= BALANCE_DRIFT_HIGH_PCT and (
                # Surface balance drift only if it's the strongest finding;
                # critical token drift outranks balance drift.
                token_severity is not Severity.TOKEN_DRIFT_CRITICAL
            ):
                return PricingVerdict(
                    severity=Severity.BALANCE_DRIFT_HIGH,
                    evaluator=self.name,
                    drift_pct=abs(balance_drift),
                    details=details,
                )

        return PricingVerdict(
            severity=token_severity,
            evaluator=self.name,
            drift_pct=abs(token_drift),
            details=details,
        )


__all__ = [
    "BALANCE_DRIFT_HIGH_PCT",
    "L0CharacterRatioEvaluator",
    "TOKEN_DRIFT_CRITICAL_PCT",
    "TOKEN_DRIFT_WARN_PCT",
    "estimate_tokens_by_chars",
    "load_ratio_table",
]
