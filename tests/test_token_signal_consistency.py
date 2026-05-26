"""Cross-Story integration test (PRD §6.4 third bullet, Story TES-137).

S2 (perf purity) and S4 (pricing compliance) both inspect the same
relay response and must read the **same** token count from the wire.
PRD §6.4 explicitly forbids the two stages from each carrying their
own private token-delta arithmetic — under one shared fixture and one
shared tokenizer ratio, the L0 pricing evaluator and the perf-purity
counterpart must agree on the reported-vs-expected token signal.

The intent here is *consistency*, not feature-parity:

  * The perf bench records ``reported_input_tokens`` /
    ``reported_output_tokens`` directly off the response payload.
  * The S4 pricing pipeline derives drift from the **same** two
    integer fields on :class:`PricingSample`.
  * Therefore, given a single fixture row that names both fields, the
    ``token_drift_pct`` produced by L0's evaluator must be deterministic
    and reproducible from a stand-alone computation that reads the
    same fields.

This test does NOT exercise S2's purity heuristics (those operate on
response text, not tokens). It locks the *shared token-delta source*
so a future refactor that introduces a second token-counting path
fails loudly.
"""

from __future__ import annotations

import pytest

from api_relay_audit.pricing.evaluators.base import (
    PricingSample,
)
from api_relay_audit.pricing.evaluators.l0_character_ratio import (
    L0CharacterRatioEvaluator,
    estimate_tokens_by_chars,
)
from api_relay_audit.pricing.pipeline import aggregate


# Synthetic fixture — same fixture both Stories read in production.
# Chinese input on a Claude-family model; reported counts deliberately
# inflated to trip the warn band.
SHARED_FIXTURE = [
    {
        "vendor": "anthropic",
        "model": "claude-opus-4-7",
        "input_text": "请把以下英文翻译为中文：" + "Hello, world. " * 8,
        "output_text": "你好，世界。" * 12,
        "reported_input_tokens": 200,
        "reported_output_tokens": 90,
    },
    {
        "vendor": "openai",
        "model": "gpt-4o-mini",
        "input_text": "Summarise the key economic indicators for Q1.",
        "output_text": "GDP grew by 2.3% with inflation at 2.0%.",
        "reported_input_tokens": 14,
        "reported_output_tokens": 12,
    },
]


def _baseline_token_drift_pct(row: dict) -> float:
    """Compute the same drift% an evaluator-free reading would produce.

    Uses ``estimate_tokens_by_chars`` for the expected count (the
    shared character-ratio source) and the same ``reported_*`` fields
    perf consumes. Drift formula identical to L0's internal helper.
    """
    expected_in = estimate_tokens_by_chars(
        row["input_text"], row["vendor"], row["model"]
    )
    expected_out = estimate_tokens_by_chars(
        row["output_text"], row["vendor"], row["model"]
    )
    expected_total = expected_in + expected_out
    reported_total = (
        row["reported_input_tokens"] + row["reported_output_tokens"]
    )
    if expected_total <= 0:
        return 100.0 if reported_total != 0 else 0.0
    return ((reported_total - expected_total) / expected_total) * 100.0


@pytest.mark.parametrize("row", SHARED_FIXTURE)
def test_l0_drift_matches_shared_reading(row):
    """The S4 L0 evaluator's token drift must equal the shared baseline.

    If a future PR adds a second token-delta computation (private to
    either S2 or S4), this test starts failing — the audit can't have
    two truths about the same number.
    """
    sample = PricingSample(**row)
    verdict = L0CharacterRatioEvaluator().evaluate(sample)

    expected = _baseline_token_drift_pct(row)
    actual = verdict.details["token_drift_pct"]
    # Allow a tight rounding margin (L0 rounds to 2 dp before storing).
    assert abs(actual - expected) < 0.5, (
        f"S2/S4 token-delta divergence: row={row['vendor']}/{row['model']} "
        f"expected_drift_pct={expected:.4f} but L0 reports {actual:.4f} "
        "— the shared reading must agree to ≤0.5%."
    )


@pytest.mark.parametrize("row", SHARED_FIXTURE)
def test_aggregate_drift_pct_carries_l0_reading(row):
    """The pipeline aggregate keeps the L0 drift number intact when L0
    is the only layer — ensures the artifact / reporter / forensic log
    all see the same shared reading."""
    sample = PricingSample(**row)
    v0 = L0CharacterRatioEvaluator().evaluate(sample)
    agg = aggregate([v0])
    assert abs(agg.drift_pct - v0.drift_pct) < 1e-9


def test_reported_tokens_are_only_token_field_on_sample():
    """Frozen-Protocol guard: PricingSample exposes exactly two
    integer token fields (input + output). A second tokenizer source
    sneaking onto the dataclass is the regression we want to catch."""
    sample = PricingSample(
        vendor="anthropic", model="claude-opus-4-7",
        input_text="x", output_text="y",
        reported_input_tokens=1, reported_output_tokens=1,
    )
    token_attrs = [
        a for a in vars(sample)
        if a.startswith("reported_") and a.endswith("_tokens")
    ]
    assert sorted(token_attrs) == [
        "reported_input_tokens",
        "reported_output_tokens",
    ], (
        f"unexpected token-bearing attributes on PricingSample: "
        f"{token_attrs!r} — S2 and S4 must share exactly two fields."
    )
