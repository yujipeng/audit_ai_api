"""Price quote dataclass + ``prices.yaml`` loader.

``prices.yaml`` schema (frozen, see TES-134 §跨 Story 不变量 #3)::

    <vendor>:
      <model>:
        input_per_1k: <float, currency-native>
        output_per_1k: <float>
        currency: USD|CNY|EUR    # default USD
        notes: <str, optional>

The loader returns ``dict[str, dict[str, PriceQuote]]`` so callers can do
``prices["anthropic"]["claude-haiku-4-5"].cost_for_tokens(...)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PRICES_YAML_PATH = Path(__file__).parent / "prices.yaml"


@dataclass
class PriceQuote:
    """Unit price for one (vendor, model) pair."""

    vendor: str
    model: str
    input_per_1k: float
    output_per_1k: float
    currency: str = "USD"
    notes: str = ""

    def cost_for_tokens(
        self,
        input_tokens: int,
        output_tokens: int,
        fx_to_usd: float = 1.0,
    ) -> float:
        """Return cost in USD. ``fx_to_usd`` converts native currency to USD."""
        native = (
            (input_tokens / 1000.0) * self.input_per_1k
            + (output_tokens / 1000.0) * self.output_per_1k
        )
        return native * fx_to_usd


def load_prices(path: Optional[Path] = None) -> dict[str, dict[str, PriceQuote]]:
    """Load ``prices.yaml`` from ``path`` (default: bundled file)."""
    import yaml  # PyYAML is in requirements.txt

    p = Path(path) if path else PRICES_YAML_PATH
    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    out: dict[str, dict[str, PriceQuote]] = {}
    for vendor, models in raw.items():
        if not isinstance(models, dict):
            continue
        out[vendor] = {}
        for model, spec in models.items():
            if not isinstance(spec, dict):
                continue
            out[vendor][model] = PriceQuote(
                vendor=vendor,
                model=model,
                input_per_1k=float(spec["input_per_1k"]),
                output_per_1k=float(spec["output_per_1k"]),
                currency=spec.get("currency", "USD"),
                notes=spec.get("notes", ""),
            )
    return out
