"""Evaluator base contract: dataclasses + Protocol surface.

Cross-Story invariants (per TES-134 description §跨 Story 不变量):

1. ``PricingEvaluator`` Protocol signature is frozen — Story-B/C/D implement it.
2. ``Severity`` enum is closed — exactly six members, no extras.
3. ``prices.yaml`` / ``ratio_table.yaml`` schemas are frozen; future scrapers
   (Story-E) must conform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol, runtime_checkable


class Severity(str, Enum):
    """Closed pricing-verdict severity (PRD §6.2)."""

    PRICING_COMPLIANT = "pricing_compliant"
    TOKEN_DRIFT_WARN = "token_drift_warn"
    TOKEN_DRIFT_CRITICAL = "token_drift_critical"
    UNIT_PRICE_MISMATCH = "unit_price_mismatch"
    BALANCE_DRIFT_HIGH = "balance_drift_high"
    INCONCLUSIVE_BALANCE = "inconclusive_balance"


@dataclass
class PricingSample:
    """A single observed call: input/output text + relay-reported tokens.

    ``input_text`` / ``output_text`` are the actual strings that crossed the
    wire. Char counts are derived from them so Story-E scrapers don't have to
    duplicate the field. ``balance_*`` and ``reported_cost_usd`` are optional
    and only populated by relays that expose either a balance endpoint or a
    cost field.
    """

    vendor: str
    model: str
    input_text: str
    output_text: str
    reported_input_tokens: int
    reported_output_tokens: int
    balance_before_usd: Optional[float] = None
    balance_after_usd: Optional[float] = None
    reported_cost_usd: Optional[float] = None
    metadata: dict = field(default_factory=dict)

    @property
    def input_chars(self) -> int:
        return len(self.input_text)

    @property
    def output_chars(self) -> int:
        return len(self.output_text)

    @property
    def balance_delta_usd(self) -> Optional[float]:
        if self.balance_before_usd is None or self.balance_after_usd is None:
            return None
        return self.balance_after_usd - self.balance_before_usd


# Severity → exit code mapping (PRD §6.5.1).
# critical / high → 1, warn / inconclusive / compliant → 0 (warn logs).
_EXIT_CODE_BY_SEVERITY: dict[Severity, int] = {
    Severity.PRICING_COMPLIANT: 0,
    Severity.TOKEN_DRIFT_WARN: 0,
    Severity.TOKEN_DRIFT_CRITICAL: 1,
    Severity.UNIT_PRICE_MISMATCH: 1,
    Severity.BALANCE_DRIFT_HIGH: 1,
    Severity.INCONCLUSIVE_BALANCE: 0,
}


@dataclass
class PricingVerdict:
    """Result of a single evaluator pass over a :class:`PricingSample`."""

    severity: Severity
    evaluator: str
    drift_pct: float = 0.0
    details: dict = field(default_factory=dict)
    notes: str = ""

    def is_pass(self) -> bool:
        return self.severity is Severity.PRICING_COMPLIANT

    def exit_code(self) -> int:
        return _EXIT_CODE_BY_SEVERITY[self.severity]


@runtime_checkable
class PricingEvaluator(Protocol):
    """Frozen Protocol — implementations live in ``evaluators/``."""

    name: str

    def evaluate(self, sample: PricingSample) -> PricingVerdict: ...
