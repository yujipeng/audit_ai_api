"""Pricing compliance module (Story TES-134 / S4-A).

Provides the core data model + Protocol surface for pricing-drift evaluators
(see design §4.2). Downstream evaluator stories (S4-B/C/D) implement the
:class:`PricingEvaluator` Protocol and reuse :class:`PricingSample`,
:class:`PricingVerdict`, :class:`PriceQuote`, and the closed
:class:`Severity` enum defined here.

The package layout::

    api_relay_audit/pricing/
      __init__.py            # public surface (this file)
      config.py              # audit.yaml schema + billing safety invariants
      prices.py              # prices.yaml loader returning PriceQuote dicts
      prices.yaml            # vendor / model unit-price catalogue
      ratio_table.yaml       # chars-per-token ratios (per vendor/model/lang)
      evaluators/
        __init__.py
        base.py              # PricingEvaluator Protocol + base helpers
        l0_character_ratio.py
"""

from __future__ import annotations

from api_relay_audit.pricing.evaluators.base import (
    PricingEvaluator,
    PricingSample,
    PricingVerdict,
    Severity,
)
from api_relay_audit.pricing.prices import PriceQuote, load_prices

__all__ = [
    "PriceQuote",
    "PricingEvaluator",
    "PricingSample",
    "PricingVerdict",
    "Severity",
    "load_prices",
]
