"""``audit pricing`` CLI surface (Story TES-137 / S4-D, PRD §6.1.1 / §6.5).

Pure function ``run(samples, evaluate_fn, ...)`` so the unit tests can
drive every code path without touching real argparse / IO. The argparse
wrapper :func:`parse_args` is exercised separately for the
``-v / -vv`` tri-state.

Exit-code contract (PRD §6.5.1):

  * critical / unit_price_mismatch / balance_drift_high → 1
  * warn / inconclusive_balance / pricing_compliant → 0 (warn still
    logs WARN-level lines)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, TextIO

from api_relay_audit.pricing.artifact import compute_exit_code, write_artifact
from api_relay_audit.pricing.evaluators.base import PricingSample
from api_relay_audit.pricing.pipeline import AggregatedPricingVerdict


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse ``audit pricing`` CLI flags.

    ``-v`` raises verbosity by one step (0 = quiet, 1 = normal,
    2 = verbose). Higher counts are clamped at 2.
    """
    p = argparse.ArgumentParser(prog="audit pricing")
    p.add_argument("--config", required=True, help="Path to audit.yaml")
    p.add_argument("--provider", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--reports-dir", default="reports/pricing",
                   help="Root directory for artifact output.")
    p.add_argument("-v", "--verbose", dest="verbosity", action="count",
                   default=0,
                   help="Increase verbosity (-v normal, -vv verbose). "
                        "Default: quiet.")
    ns = p.parse_args(argv)
    ns.verbosity = min(ns.verbosity, 2)
    return ns


def run(
    *,
    samples: Iterable[PricingSample],
    evaluate_fn: Callable[[PricingSample], AggregatedPricingVerdict],
    run_id: str,
    provider: str,
    model: str,
    artifact_dir: Path,
    verbosity: int = 1,
    stdout: Optional[TextIO] = None,
) -> int:
    """Execute one pricing audit and return the process exit code."""
    out = stdout if stdout is not None else sys.stdout
    started = datetime.now(timezone.utc).isoformat()

    pairs: list[tuple[dict, AggregatedPricingVerdict]] = []
    for sample in samples:
        agg = evaluate_fn(sample)
        sample_payload = {
            "vendor": sample.vendor,
            "model": sample.model,
            "reported_input_tokens": sample.reported_input_tokens,
            "reported_output_tokens": sample.reported_output_tokens,
        }
        pairs.append((sample_payload, agg))

    finished = datetime.now(timezone.utc).isoformat()

    artifact_path = Path(artifact_dir) / provider / f"{run_id}.json"
    write_artifact(
        path=artifact_path,
        run_id=run_id,
        provider=provider,
        model=model,
        started_at=started,
        finished_at=finished,
        verdicts=pairs,
    )

    aggs = [a for _, a in pairs]
    exit_code = compute_exit_code(aggs)
    _emit(out, pairs, artifact_path, exit_code, verbosity)
    return exit_code


def _emit(
    out: TextIO,
    pairs: list[tuple[dict, AggregatedPricingVerdict]],
    artifact_path: Path,
    exit_code: int,
    verbosity: int,
) -> None:
    if not pairs:
        if verbosity >= 1:
            out.write("pricing: no samples evaluated (exit_code=0)\n")
        return

    severities = [a.severity.value for _, a in pairs]
    worst = max(severities, key=_severity_rank)
    out.write(f"pricing: {worst} (exit_code={exit_code})\n")
    if verbosity == 0:
        return

    out.write(f"pricing: artifact={artifact_path}\n")
    for sample, agg in pairs:
        out.write(
            f"  sample={sample.get('vendor', '?')}/"
            f"{sample.get('model', '?')} "
            f"severity={agg.severity.value} "
            f"layer={agg.layer} "
            f"confidence={agg.confidence}\n"
        )
    if verbosity < 2:
        return

    for _, agg in pairs:
        out.write("  evidence:\n")
        out.write(
            "    " + json.dumps(agg.per_layer, ensure_ascii=False,
                                  sort_keys=True) + "\n"
        )


_SEVERITY_RANK_BY_VALUE = {
    "inconclusive_balance": 0,
    "pricing_compliant": 1,
    "token_drift_warn": 2,
    "unit_price_mismatch": 3,
    "balance_drift_high": 4,
    "token_drift_critical": 5,
}


def _severity_rank(value: str) -> int:
    return _SEVERITY_RANK_BY_VALUE.get(value, 0)


__all__ = ["parse_args", "run"]
