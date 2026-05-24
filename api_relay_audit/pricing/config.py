"""``audit.yaml`` schema loader + billing safety invariants.

Implements PRD §4.1 (config surface), §6.1.2 (cost_cap_usd required), and
§6.3.3 (billing safety invariants). The loader rejects any audit run that
omits ``pricing.cost_cap_usd`` so a misconfigured user can never accidentally
run an unbounded burn against a pay-per-token endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


class PricingConfigError(ValueError):
    """Raised when audit.yaml fails pricing schema validation."""


class BillingSafetyError(RuntimeError):
    """Raised when an in-flight audit breaches a billing safety invariant."""


DEFAULT_MAX_RUNS = 100
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024  # 1MB


@dataclass
class PricingConfig:
    """Validated pricing block from audit.yaml."""

    cost_cap_usd: float
    max_runs: int = DEFAULT_MAX_RUNS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    evaluators: list[str] = field(default_factory=lambda: ["l0_character_ratio"])
    prices_file: Optional[str] = None
    ratio_table_file: Optional[str] = None
    fx: dict[str, float] = field(default_factory=dict)


@dataclass
class AuditConfig:
    """Full audit.yaml view (only ``pricing`` is owned by S4-A)."""

    pricing: PricingConfig
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class BillingSafetyResult:
    """Outcome of an in-flight billing safety check."""

    truncated: bool = False
    truncated_at: Optional[int] = None


def _require_positive_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool):  # bool is a subclass of int; reject explicitly
        raise PricingConfigError(f"{field_name} must be a positive number")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise PricingConfigError(f"{field_name} must be a positive number")
    if f <= 0 or f != f:  # NaN check via self-comparison
        raise PricingConfigError(f"{field_name} must be a positive number")
    return f


def load_audit_config(path: str) -> AuditConfig:
    """Load and validate ``audit.yaml`` from ``path``.

    Raises :class:`PricingConfigError` with the exact message
    ``pricing.cost_cap_usd is required`` (matched by the CLI to print to
    stderr and exit non-zero per PRD §6.1.2).
    """
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise PricingConfigError("audit.yaml root must be a mapping")

    pricing_block = raw.get("pricing")
    if not isinstance(pricing_block, dict) or "cost_cap_usd" not in pricing_block:
        raise PricingConfigError("pricing.cost_cap_usd is required")

    cost_cap = _require_positive_float(
        pricing_block["cost_cap_usd"], "pricing.cost_cap_usd"
    )

    max_runs = int(pricing_block.get("max_runs", DEFAULT_MAX_RUNS))
    if max_runs <= 0:
        raise PricingConfigError("pricing.max_runs must be a positive integer")

    max_response_bytes = int(
        pricing_block.get("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES)
    )
    if max_response_bytes <= 0:
        raise PricingConfigError(
            "pricing.max_response_bytes must be a positive integer"
        )

    evaluators_raw = pricing_block.get("evaluators", ["l0_character_ratio"])
    if not isinstance(evaluators_raw, list) or not all(
        isinstance(e, str) for e in evaluators_raw
    ):
        raise PricingConfigError("pricing.evaluators must be a list of strings")

    fx_raw = pricing_block.get("fx", {}) or {}
    if not isinstance(fx_raw, dict):
        raise PricingConfigError("pricing.fx must be a mapping")
    fx = {str(k): float(v) for k, v in fx_raw.items()}

    pricing = PricingConfig(
        cost_cap_usd=cost_cap,
        max_runs=max_runs,
        max_response_bytes=max_response_bytes,
        evaluators=list(evaluators_raw),
        prices_file=pricing_block.get("prices_file"),
        ratio_table_file=pricing_block.get("ratio_table_file"),
        fx=fx,
    )
    return AuditConfig(pricing=pricing, raw=raw)


def enforce_billing_safety(
    cfg: PricingConfig,
    *,
    accumulated_cost_usd: float,
    runs_so_far: int,
    response_bytes: int,
) -> BillingSafetyResult:
    """Three-invariant gate (PRD §6.3.3):

    1. ``accumulated_cost_usd`` ≤ ``cost_cap_usd`` — else abort.
    2. ``runs_so_far`` ≤ ``max_runs`` — else abort.
    3. ``response_bytes`` ≤ ``max_response_bytes`` — else mark for truncation
       (response is still returned, capped at the limit).
    """
    if accumulated_cost_usd > cfg.cost_cap_usd:
        raise BillingSafetyError(
            f"cost_cap_usd exceeded: ${accumulated_cost_usd:.4f} > "
            f"${cfg.cost_cap_usd:.4f}"
        )
    if runs_so_far > cfg.max_runs:
        raise BillingSafetyError(
            f"max_runs exceeded: {runs_so_far} > {cfg.max_runs}"
        )
    if response_bytes > cfg.max_response_bytes:
        return BillingSafetyResult(
            truncated=True, truncated_at=cfg.max_response_bytes
        )
    return BillingSafetyResult(truncated=False, truncated_at=None)


__all__ = [
    "AuditConfig",
    "BillingSafetyError",
    "BillingSafetyResult",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_MAX_RUNS",
    "PricingConfig",
    "PricingConfigError",
    "enforce_billing_safety",
    "load_audit_config",
]
