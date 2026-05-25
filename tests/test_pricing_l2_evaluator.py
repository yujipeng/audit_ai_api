"""Tests for L2 balance-triangle evaluator (TES-136 / S4-C).

Covers DoD items from the issue description:

  1. ``evaluators/l2_balance_triangle.py`` + adapter interface; ≥ 2 adapters
     (Anthropic-anchor sample-driven + 1 OpenAI-compatible relay /billing).
  2. Missing balance endpoint → graceful skip with
     ``Severity.INCONCLUSIVE_BALANCE`` (PRD §7.1 R2 mitigation).
  3. Default fixture ``tests/fixtures/pricing/balance/`` with ≥ 2 vendor
     entries, ≥ 1 ``balance_drift_pct ≠ 0`` row, ≥ 1 ``/billing`` exposure.
  4. Cache double-charge sub-dimension: same prompt re-issued with markedly
     higher ``cached_tokens`` → ``balance_drift_high`` (design §7 risk #7);
     missing ``cached_tokens`` on first call disables the sub-dim cleanly.
  5. ``--interactive`` mode accepts manual before/after balance entry
     (PRD §7.2 U3) — covered by a thin helper that maps stdin inputs into
     ``PricingSample.balance_before_usd / balance_after_usd``.
  6. Headline fixture hit: claimed accumulated $1.00, actual balance drops
     $1.30 → ``balance_drift_pct = 30%`` and verdict carries
     ``balance_drift_high`` (PRD §6.2 fourth row).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from api_relay_audit.pricing import PricingSample, Severity


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pricing" / "balance"


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------

def test_module_exposes_public_surface():
    from api_relay_audit.pricing.evaluators import l2_balance_triangle as mod

    for attr in (
        "L2BalanceTriangleEvaluator",
        "BalanceAdapter",
        "SampleBalanceAdapter",
        "OpenAICompatibleBillingAdapter",
        "BALANCE_DRIFT_HIGH_PCT",
        "CACHE_DOUBLE_CHARGE_PCT",
        "interactive_balance_prompt",
    ):
        assert hasattr(mod, attr), f"l2_balance_triangle missing {attr!r}"


def test_evaluator_implements_pricing_evaluator_protocol():
    from api_relay_audit.pricing import PricingEvaluator
    from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
        L2BalanceTriangleEvaluator,
    )

    ev = L2BalanceTriangleEvaluator()
    assert isinstance(ev, PricingEvaluator)
    assert ev.name == "L2_balance_triangle"


# ---------------------------------------------------------------------------
# Fixture invariants (DoD bullet 3)
# ---------------------------------------------------------------------------

class TestFixtureInvariants:
    def test_fixture_directory_exists(self):
        assert FIXTURE_DIR.is_dir(), (
            f"Default fixture directory {FIXTURE_DIR} not present; DoD requires "
            "tests/fixtures/pricing/balance/ to ship with the L2 evaluator."
        )

    def test_fixture_has_at_least_two_vendors(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            load_balance_fixture,
        )

        rows = load_balance_fixture(FIXTURE_DIR)
        vendors = {row["vendor"] for row in rows}
        assert len(vendors) >= 2, (
            f"Fixture covers only {sorted(vendors)}; DoD requires ≥ 2 vendors."
        )

    def test_fixture_has_at_least_one_nonzero_drift_row(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            load_balance_fixture,
        )

        rows = load_balance_fixture(FIXTURE_DIR)
        nonzero = [r for r in rows if abs(r.get("balance_drift_pct", 0)) > 0]
        assert nonzero, (
            "DoD requires ≥ 1 row with balance_drift_pct ≠ 0 to regress "
            "balance_drift_high."
        )

    def test_fixture_has_at_least_one_billing_exposed_relay(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            load_balance_fixture,
        )

        rows = load_balance_fixture(FIXTURE_DIR)
        billing_rows = [r for r in rows if r.get("billing_endpoint")]
        assert billing_rows, (
            "DoD requires ≥ 1 row exposing /billing (hvoy 5/12 集合)."
        )


# ---------------------------------------------------------------------------
# Headline DoD scenario (bullet 6)
# ---------------------------------------------------------------------------

class TestHeadlineBalanceDriftHigh:
    """Claimed $1.00 accumulated, actual balance dropped $1.30 → 30% drift."""

    def test_thirty_pct_drift_yields_balance_drift_high(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            L2BalanceTriangleEvaluator,
            SampleBalanceAdapter,
        )

        sample = PricingSample(
            vendor="anthropic",
            model="claude-haiku-4-5",
            input_text="hello",
            output_text="world",
            reported_input_tokens=100,
            reported_output_tokens=100,
            balance_before_usd=10.00,
            balance_after_usd=8.70,           # actual outflow $1.30
            reported_cost_usd=1.00,            # claimed $1.00
        )
        ev = L2BalanceTriangleEvaluator(adapters=[SampleBalanceAdapter()])
        verdict = ev.evaluate(sample)

        assert verdict.severity is Severity.BALANCE_DRIFT_HIGH
        assert verdict.drift_pct == pytest.approx(30.0, rel=0.01)
        assert verdict.details["balance_drift_pct"] == pytest.approx(
            30.0, rel=0.01
        )
        assert verdict.details["claimed_cost_usd"] == pytest.approx(1.00)
        assert verdict.details["actual_balance_delta_usd"] == pytest.approx(
            -1.30, rel=0.01
        )


# ---------------------------------------------------------------------------
# Compliant scenario — small drift stays compliant
# ---------------------------------------------------------------------------

class TestCompliantSmallDrift:
    def test_small_drift_compliant(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            L2BalanceTriangleEvaluator,
            SampleBalanceAdapter,
        )

        sample = PricingSample(
            vendor="anthropic",
            model="claude-haiku-4-5",
            input_text="x",
            output_text="y",
            reported_input_tokens=100,
            reported_output_tokens=100,
            balance_before_usd=10.00,
            balance_after_usd=8.95,           # actual outflow $1.05 vs claim $1.00
            reported_cost_usd=1.00,
        )
        ev = L2BalanceTriangleEvaluator(adapters=[SampleBalanceAdapter()])
        verdict = ev.evaluate(sample)

        # 5% drift < 25% threshold → compliant
        assert verdict.severity is Severity.PRICING_COMPLIANT
        assert verdict.drift_pct < 25.0


# ---------------------------------------------------------------------------
# Graceful skip (bullet 2)
# ---------------------------------------------------------------------------

class TestGracefulSkip:
    def test_no_balance_data_yields_inconclusive(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            L2BalanceTriangleEvaluator,
            SampleBalanceAdapter,
        )

        sample = PricingSample(
            vendor="openai",
            model="gpt-4o-mini",
            input_text="hi",
            output_text="ho",
            reported_input_tokens=10,
            reported_output_tokens=5,
            # balance_* and reported_cost_usd intentionally missing
        )
        ev = L2BalanceTriangleEvaluator(adapters=[SampleBalanceAdapter()])
        verdict = ev.evaluate(sample)

        assert verdict.severity is Severity.INCONCLUSIVE_BALANCE
        assert "skip" in verdict.notes.lower() or "no balance" in verdict.notes.lower()
        # Inconclusive must NOT pollute main verdict — exit code 0
        assert verdict.exit_code() == 0

    def test_no_adapters_yields_inconclusive(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            L2BalanceTriangleEvaluator,
        )

        sample = PricingSample(
            vendor="openai",
            model="gpt-4o-mini",
            input_text="hi",
            output_text="ho",
            reported_input_tokens=10,
            reported_output_tokens=5,
            balance_before_usd=5.0,
            balance_after_usd=4.9,
        )
        ev = L2BalanceTriangleEvaluator(adapters=[])
        verdict = ev.evaluate(sample)
        assert verdict.severity is Severity.INCONCLUSIVE_BALANCE


# ---------------------------------------------------------------------------
# Adapter contract (bullet 1)
# ---------------------------------------------------------------------------

class TestAdapters:
    def test_sample_balance_adapter_supports_when_balance_present(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            SampleBalanceAdapter,
        )

        sample_with = PricingSample(
            vendor="anthropic", model="claude-haiku-4-5",
            input_text="a", output_text="b",
            reported_input_tokens=1, reported_output_tokens=1,
            balance_before_usd=1.0, balance_after_usd=0.9,
        )
        sample_without = PricingSample(
            vendor="anthropic", model="claude-haiku-4-5",
            input_text="a", output_text="b",
            reported_input_tokens=1, reported_output_tokens=1,
        )
        adapter = SampleBalanceAdapter()
        assert adapter.supports(sample_with) is True
        assert adapter.supports(sample_without) is False

    def test_oai_billing_adapter_supports_only_with_endpoint(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            OpenAICompatibleBillingAdapter,
        )

        adapter = OpenAICompatibleBillingAdapter(
            base_url="https://example-relay.test",
            api_key="sk-test",
            transport=_StubTransport(
                {"https://example-relay.test/v1/billing": {
                    "total_available": 9.50,
                    "total_granted": 10.00,
                    "total_used": 0.50,
                }}
            ),
        )
        sample = PricingSample(
            vendor="openai-compatible",
            model="gpt-4o-mini",
            input_text="x", output_text="y",
            reported_input_tokens=10, reported_output_tokens=5,
            metadata={"base_url": "https://example-relay.test"},
        )
        assert adapter.supports(sample) is True

        # Different base_url → adapter declines.
        other = PricingSample(
            vendor="openai-compatible",
            model="gpt-4o-mini",
            input_text="x", output_text="y",
            reported_input_tokens=10, reported_output_tokens=5,
            metadata={"base_url": "https://different-relay.test"},
        )
        assert adapter.supports(other) is False

    def test_oai_billing_adapter_fetches_balance(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            OpenAICompatibleBillingAdapter,
        )

        transport = _StubTransport(
            {"https://relay.test/v1/billing": {
                "total_available": 8.20,
            }}
        )
        adapter = OpenAICompatibleBillingAdapter(
            base_url="https://relay.test",
            api_key="sk-x",
            transport=transport,
        )
        balance = adapter.fetch_balance_usd()
        assert balance == pytest.approx(8.20)
        assert transport.calls and "Authorization" in transport.calls[0][1]

    def test_oai_billing_adapter_graceful_on_404(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            OpenAICompatibleBillingAdapter,
        )

        adapter = OpenAICompatibleBillingAdapter(
            base_url="https://no-billing.test",
            api_key="sk-x",
            transport=_StubTransport({}, default_status=404),
        )
        # Returning None signals graceful skip → evaluator turns it into
        # INCONCLUSIVE_BALANCE (covered by the orchestrator-level test).
        assert adapter.fetch_balance_usd() is None


class TestEndToEndWithOAIBilling:
    """Drive the evaluator through the OAI-compatible billing adapter end-
    to-end (relay reports a fake $1.00 cost; /billing shows $1.30 outflow)."""

    def test_oai_billing_path_flags_balance_drift(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            L2BalanceTriangleEvaluator,
            OpenAICompatibleBillingAdapter,
        )

        # Two sequential calls on the /billing endpoint: before then after.
        transport = _StubTransport({
            "https://relay.test/v1/billing": [
                {"total_available": 10.00},  # before
                {"total_available": 8.70},   # after — outflow 1.30
            ]
        })
        adapter = OpenAICompatibleBillingAdapter(
            base_url="https://relay.test", api_key="sk-x", transport=transport,
        )

        sample_pre = PricingSample(
            vendor="openai-compatible", model="gpt-4o-mini",
            input_text="hello", output_text="world",
            reported_input_tokens=100, reported_output_tokens=100,
            reported_cost_usd=1.00,
            metadata={"base_url": "https://relay.test", "stage": "pre"},
        )
        ev = L2BalanceTriangleEvaluator(adapters=[adapter])
        # Use the orchestration helper to capture before/after balance
        # automatically.
        verdict = ev.evaluate_with_billing_probe(sample_pre)
        assert verdict.severity is Severity.BALANCE_DRIFT_HIGH
        assert verdict.drift_pct == pytest.approx(30.0, rel=0.01)


# ---------------------------------------------------------------------------
# Cache double-charge sub-dimension (bullet 4)
# ---------------------------------------------------------------------------

class TestCacheDoubleCharge:
    def test_second_call_cached_tokens_jump_flags_double_charge(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            L2BalanceTriangleEvaluator,
            SampleBalanceAdapter,
        )

        ev = L2BalanceTriangleEvaluator(adapters=[SampleBalanceAdapter()])
        prompt = "what is 2+2?"

        first = PricingSample(
            vendor="anthropic", model="claude-haiku-4-5",
            input_text=prompt, output_text="4",
            reported_input_tokens=20, reported_output_tokens=2,
            balance_before_usd=10.0, balance_after_usd=9.99,
            reported_cost_usd=0.01,
            metadata={"cached_tokens": 0},
        )
        second = PricingSample(
            vendor="anthropic", model="claude-haiku-4-5",
            input_text=prompt, output_text="4",
            reported_input_tokens=20, reported_output_tokens=2,
            balance_before_usd=9.99, balance_after_usd=9.95,
            reported_cost_usd=0.01,
            # Second call reports a large jump in cached_tokens but cost
            # didn't drop — relay is double-charging the cache hit.
            metadata={"cached_tokens": 18},
        )

        # First sample — sub-dim disabled (no prior cached_tokens reading).
        v1 = ev.evaluate(first)
        # First call by itself is compliant on balance terms.
        assert v1.severity in (
            Severity.PRICING_COMPLIANT, Severity.INCONCLUSIVE_BALANCE
        )

        v2 = ev.evaluate(second)
        assert v2.severity is Severity.BALANCE_DRIFT_HIGH
        assert v2.details.get("cache_double_charge") is True

    def test_first_call_without_cached_tokens_skips_subdim(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            L2BalanceTriangleEvaluator,
            SampleBalanceAdapter,
        )

        sample = PricingSample(
            vendor="anthropic", model="claude-haiku-4-5",
            input_text="solo", output_text="ok",
            reported_input_tokens=10, reported_output_tokens=2,
            balance_before_usd=10.0, balance_after_usd=9.99,
            reported_cost_usd=0.01,
            # No cached_tokens key at all.
        )
        ev = L2BalanceTriangleEvaluator(adapters=[SampleBalanceAdapter()])
        verdict = ev.evaluate(sample)
        # Sub-dim must not raise / must be reported as not-evaluated.
        assert verdict.details.get("cache_double_charge") in (False, None)


# ---------------------------------------------------------------------------
# Interactive mode (bullet 5)
# ---------------------------------------------------------------------------

class TestInteractiveBalancePrompt:
    def test_interactive_helper_populates_balance_fields(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            interactive_balance_prompt,
        )

        sample = PricingSample(
            vendor="anthropic", model="claude-haiku-4-5",
            input_text="x", output_text="y",
            reported_input_tokens=1, reported_output_tokens=1,
        )
        # Simulate a user typing "10.00\n8.70\n" on stdin.
        stdin = io.StringIO("10.00\n8.70\n")
        stdout = io.StringIO()
        updated = interactive_balance_prompt(
            sample, input_stream=stdin, output_stream=stdout,
        )
        assert updated.balance_before_usd == pytest.approx(10.00)
        assert updated.balance_after_usd == pytest.approx(8.70)
        # Original sample left untouched (returned a new instance).
        assert sample.balance_before_usd is None

    def test_interactive_helper_rejects_garbage(self):
        from api_relay_audit.pricing.evaluators.l2_balance_triangle import (
            interactive_balance_prompt,
        )

        sample = PricingSample(
            vendor="anthropic", model="claude-haiku-4-5",
            input_text="x", output_text="y",
            reported_input_tokens=1, reported_output_tokens=1,
        )
        # First "abc" is rejected, then valid numbers are accepted.
        stdin = io.StringIO("abc\n10.0\n9.0\n")
        stdout = io.StringIO()
        updated = interactive_balance_prompt(
            sample, input_stream=stdin, output_stream=stdout,
        )
        assert updated.balance_before_usd == pytest.approx(10.0)
        assert updated.balance_after_usd == pytest.approx(9.0)
        # And user was warned at least once.
        assert "invalid" in stdout.getvalue().lower() or "?" in stdout.getvalue()


# ---------------------------------------------------------------------------
# Stub HTTP transport for the OAI billing adapter
# ---------------------------------------------------------------------------

class _StubTransport:
    """Tiny stand-in for httpx.Client; supports per-URL queues of responses."""

    def __init__(self, payloads, *, default_status: int = 200):
        # payloads: dict[url, dict | list[dict]]
        self._payloads = {
            url: list(v) if isinstance(v, list) else [v]
            for url, v in payloads.items()
        }
        self._default_status = default_status
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, *, headers: dict | None = None, timeout: float = 10.0):
        self.calls.append((url, headers or {}))
        if url not in self._payloads or not self._payloads[url]:
            return _StubResponse(status_code=self._default_status, payload={})
        nxt = self._payloads[url].pop(0)
        return _StubResponse(status_code=200, payload=nxt)


class _StubResponse:
    def __init__(self, *, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload
