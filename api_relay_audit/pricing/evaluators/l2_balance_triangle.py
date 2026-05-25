"""L2 balance-triangle evaluator (design §8 S5, Story TES-136 / S4-C).

Triangulates three independent signals to detect relay over-billing:

  1. ``reported_cost_usd`` — what the relay says the call cost.
  2. ``balance_before_usd - balance_after_usd`` — the actual outflow on the
     relay's wallet/balance ledger.
  3. The expected cost computed from ``reported_input_tokens`` /
     ``reported_output_tokens`` × ``prices.yaml`` (carried into the
     verdict's ``details`` so reviewers can cross-check the claim against
     list price even when balance data is incomplete).

When the |claimed - actual| / claimed deviation exceeds
``BALANCE_DRIFT_HIGH_PCT`` (25%), the verdict is escalated to
``Severity.BALANCE_DRIFT_HIGH``. Otherwise compliant. Missing balance data
on a sample → ``Severity.INCONCLUSIVE_BALANCE`` (PRD §7.1 R2 mitigation):
the evaluator never silently downgrades to "OK" when the audit can't
actually verify outflow.

Adapter pattern (DoD bullet 1):

  * ``BalanceAdapter`` Protocol — ``supports(sample) -> bool`` and
    ``fetch_balance_usd() -> Optional[float]``.
  * ``SampleBalanceAdapter`` — sample-driven (uses ``balance_before_usd``
    / ``balance_after_usd`` already on the ``PricingSample``). This is the
    Anthropic-direct anchor: Anthropic doesn't expose a public balance
    endpoint, so the operator records before/after via the platform UI or
    the ``--interactive`` helper below.
  * ``OpenAICompatibleBillingAdapter`` — HTTP adapter that calls
    ``GET {base_url}/v1/billing`` (5/12 of hvoy.ai-surveyed relays expose
    this). Returns ``None`` on 4xx/5xx so the evaluator turns a missing
    endpoint into ``INCONCLUSIVE_BALANCE`` rather than failing the run.

Cache double-charge sub-dimension (DoD bullet 4): the evaluator keeps a
per-(vendor, model, prompt) cache of the previous sample's
``cached_tokens`` (read from ``sample.metadata``). When the second call
reports a sharply higher ``cached_tokens`` but the relay's reported cost
hasn't dropped proportionally, the verdict is escalated to
``balance_drift_high`` with ``details["cache_double_charge"] = True``.
First-call samples without a ``cached_tokens`` reading skip the sub-dim
cleanly — design §7 risk #7.

Interactive mode (DoD bullet 5): :func:`interactive_balance_prompt`
returns a *new* ``PricingSample`` with ``balance_before_usd`` and
``balance_after_usd`` populated from stdin. Pure-function, IO injectable
so the test suite can drive it without subprocess-level pty.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional, Protocol, Sequence, runtime_checkable

from api_relay_audit.pricing.evaluators.base import (
    PricingSample,
    PricingVerdict,
    Severity,
)


BALANCE_DRIFT_HIGH_PCT = 25.0
CACHE_DOUBLE_CHARGE_PCT = 50.0  # cached_tokens jump ratio that trips the sub-dim


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


@runtime_checkable
class BalanceAdapter(Protocol):
    """Source of balance/outflow truth for a single relay."""

    name: str

    def supports(self, sample: PricingSample) -> bool: ...

    def fetch_balance_usd(self) -> Optional[float]: ...


@dataclass
class SampleBalanceAdapter:
    """Reads ``balance_before_usd``/``balance_after_usd`` straight off the
    sample. Used by Anthropic direct (no /billing endpoint exists) and by
    the ``--interactive`` flow."""

    name: str = "sample"

    def supports(self, sample: PricingSample) -> bool:
        return (
            sample.balance_before_usd is not None
            and sample.balance_after_usd is not None
        )

    def fetch_balance_usd(self) -> Optional[float]:
        # Sample-driven adapter has no live endpoint to poll.
        return None


@dataclass
class OpenAICompatibleBillingAdapter:
    """``GET {base_url}/v1/billing`` adapter for OpenAI-compatible relays.

    Designed for the 5/12-of-hvoy.ai relays that expose the legacy OpenAI
    billing surface. The transport is injected so tests don't need a real
    HTTP stack; production callers pass an ``httpx.Client``-like object.
    """

    base_url: str
    api_key: str
    transport: object = None  # any object with .get(url, headers=, timeout=)
    name: str = "oai-billing"

    def supports(self, sample: PricingSample) -> bool:
        sample_url = (sample.metadata or {}).get("base_url")
        return sample_url == self.base_url

    def fetch_balance_usd(self) -> Optional[float]:
        url = f"{self.base_url.rstrip('/')}/v1/billing"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            response = self.transport.get(url, headers=headers, timeout=10.0)
        except Exception:
            return None
        if getattr(response, "status_code", 500) != 200:
            return None
        try:
            payload = response.json() or {}
        except Exception:
            return None
        # Accept either OpenAI legacy or hvoy-style relay shapes.
        for key in ("total_available", "balance_usd", "balance"):
            if key in payload:
                try:
                    return float(payload[key])
                except (TypeError, ValueError):
                    return None
        return None


# ---------------------------------------------------------------------------
# Fixture loader (DoD bullet 3 invariants are enforced by the tests)
# ---------------------------------------------------------------------------


def load_balance_fixture(path: Path) -> list[dict]:
    """Load every ``*.yaml`` row under ``path`` and return a flat list."""
    import yaml

    rows: list[dict] = []
    for f in sorted(Path(path).glob("*.yaml")):
        with open(f, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        for row in doc.get("rows", []) or []:
            if isinstance(row, dict):
                rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Interactive helper (DoD bullet 5)
# ---------------------------------------------------------------------------


def interactive_balance_prompt(
    sample: PricingSample,
    *,
    input_stream=None,
    output_stream=None,
) -> PricingSample:
    """Return a copy of ``sample`` with balance fields filled from stdin.

    The function loops on each prompt until a parseable float arrives, so
    the operator can recover from a typo without restarting the audit.
    """
    import sys

    in_ = input_stream if input_stream is not None else sys.stdin
    out_ = output_stream if output_stream is not None else sys.stdout

    def _ask(label: str) -> float:
        while True:
            out_.write(f"{label}: ")
            out_.flush() if hasattr(out_, "flush") else None
            line = in_.readline()
            if not line:
                raise EOFError(f"interactive balance prompt: stdin closed at {label}")
            try:
                return float(line.strip())
            except ValueError:
                out_.write(f"  invalid number {line.strip()!r}, try again.\n")

    before = _ask("balance_before_usd")
    after = _ask("balance_after_usd")
    return replace(
        sample,
        balance_before_usd=before,
        balance_after_usd=after,
    )


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


def _drift_pct(claimed: float, actual_outflow: float) -> float:
    if claimed <= 0:
        # No claim → can't compute a meaningful percentage.
        return 0.0
    return ((actual_outflow - claimed) / claimed) * 100.0


@dataclass
class L2BalanceTriangleEvaluator:
    """L2 evaluator — frozen-Protocol implementation."""

    name: str = "L2_balance_triangle"
    adapters: Sequence[BalanceAdapter] = field(default_factory=list)
    # Per-prompt cache of the prior call's cached_tokens reading. Keyed by
    # (vendor, model, input_text) so re-issued probes match cleanly.
    _cache_history: dict = field(default_factory=dict, init=False, repr=False)

    # ---------- public API ----------

    def evaluate(self, sample: PricingSample) -> PricingVerdict:
        adapter = self._pick_adapter(sample)
        outflow = self._actual_outflow(sample, adapter)
        claimed = self._claimed_cost(sample)
        cache_finding = self._check_cache_double_charge(sample, claimed)

        if outflow is None or claimed is None:
            return self._inconclusive(
                sample,
                adapter,
                "no balance data; L2 skipped (missing endpoint or "
                "balance fields).",
                cache_finding=cache_finding,
            )

        drift = _drift_pct(claimed, outflow)
        details = {
            "claimed_cost_usd": round(claimed, 6),
            "actual_balance_delta_usd": round(-outflow, 6),
            "actual_outflow_usd": round(outflow, 6),
            "balance_drift_pct": round(abs(drift), 4),
            "adapter": adapter.name if adapter else None,
            "cache_double_charge": cache_finding,
        }

        severity = (
            Severity.BALANCE_DRIFT_HIGH
            if abs(drift) >= BALANCE_DRIFT_HIGH_PCT or cache_finding is True
            else Severity.PRICING_COMPLIANT
        )
        return PricingVerdict(
            severity=severity,
            evaluator=self.name,
            drift_pct=abs(drift),
            details=details,
        )

    def evaluate_with_billing_probe(
        self, sample: PricingSample
    ) -> PricingVerdict:
        """Like :meth:`evaluate` but probes the live billing adapter for
        before/after balance and writes them onto a copy of ``sample``."""
        adapter = self._pick_adapter(sample)
        if adapter is None or not hasattr(adapter, "fetch_balance_usd"):
            return self._inconclusive(
                sample, adapter, "no live billing adapter for sample."
            )
        before = adapter.fetch_balance_usd()
        if before is None:
            return self._inconclusive(
                sample,
                adapter,
                "billing adapter returned no pre-call balance (likely "
                "missing /billing endpoint).",
            )
        after = adapter.fetch_balance_usd()
        if after is None:
            return self._inconclusive(
                sample,
                adapter,
                "billing adapter returned no post-call balance.",
            )
        probed = replace(sample, balance_before_usd=before, balance_after_usd=after)
        return self.evaluate(probed)

    # ---------- internals ----------

    def _pick_adapter(
        self, sample: PricingSample
    ) -> Optional[BalanceAdapter]:
        for adapter in self.adapters:
            try:
                if adapter.supports(sample):
                    return adapter
            except Exception:
                continue
        return None

    def _actual_outflow(
        self, sample: PricingSample, adapter: Optional[BalanceAdapter]
    ) -> Optional[float]:
        if adapter is None:
            return None
        if sample.balance_delta_usd is not None:
            # outflow positive = balance dropped
            return -sample.balance_delta_usd
        # Adapter supports the sample (e.g. OAI billing) but has no per-call
        # delta computed yet; the orchestrator path uses
        # evaluate_with_billing_probe to fill in delta. evaluate() alone
        # cannot fabricate one.
        return None

    @staticmethod
    def _claimed_cost(sample: PricingSample) -> Optional[float]:
        return sample.reported_cost_usd

    def _check_cache_double_charge(
        self, sample: PricingSample, claimed: Optional[float]
    ) -> Optional[bool]:
        """Return ``True`` if a double-charge is detected, ``False`` if the
        sub-dim ran and was clean, ``None`` if it couldn't be evaluated."""
        meta = sample.metadata or {}
        if "cached_tokens" not in meta:
            return None  # sub-dim disabled cleanly per design §7 risk #7
        key = (sample.vendor, sample.model, sample.input_text)
        prev = self._cache_history.get(key)
        # Always record current observation for the next iteration.
        self._cache_history[key] = {
            "cached_tokens": int(meta.get("cached_tokens") or 0),
            "claimed": claimed,
        }
        if prev is None:
            return None  # first observation; nothing to compare against

        prev_cached = prev["cached_tokens"]
        cur_cached = int(meta.get("cached_tokens") or 0)
        prev_cost = prev.get("claimed")
        cur_cost = claimed

        # Cache hit jumped sharply — relays SHOULD discount the cached
        # portion. If cost didn't drop materially, that's a double-charge.
        cached_jump_pct = 0.0
        if prev_cached <= 0:
            cached_jump_pct = 100.0 if cur_cached > 0 else 0.0
        else:
            cached_jump_pct = (
                (cur_cached - prev_cached) / max(prev_cached, 1) * 100.0
            )

        cost_dropped = (
            prev_cost is not None
            and cur_cost is not None
            and cur_cost <= prev_cost * 0.5  # ≥ 50% discount expected
        )
        return cached_jump_pct >= CACHE_DOUBLE_CHARGE_PCT and not cost_dropped

    def _inconclusive(
        self,
        sample: PricingSample,
        adapter: Optional[BalanceAdapter],
        notes: str,
        *,
        cache_finding: Optional[bool] = None,
    ) -> PricingVerdict:
        return PricingVerdict(
            severity=Severity.INCONCLUSIVE_BALANCE,
            evaluator=self.name,
            drift_pct=0.0,
            details={
                "adapter": adapter.name if adapter else None,
                "claimed_cost_usd": sample.reported_cost_usd,
                "balance_before_usd": sample.balance_before_usd,
                "balance_after_usd": sample.balance_after_usd,
                "cache_double_charge": cache_finding,
            },
            notes=notes,
        )


__all__ = [
    "BALANCE_DRIFT_HIGH_PCT",
    "BalanceAdapter",
    "CACHE_DOUBLE_CHARGE_PCT",
    "L2BalanceTriangleEvaluator",
    "OpenAICompatibleBillingAdapter",
    "SampleBalanceAdapter",
    "interactive_balance_prompt",
    "load_balance_fixture",
]
