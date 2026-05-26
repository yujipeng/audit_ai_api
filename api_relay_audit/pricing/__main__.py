"""Shell entry point for ``audit pricing`` (Story TES-137 / S4-D, PRD §6.1.1).

Provides ``python -m api_relay_audit.pricing`` and the
``[project.scripts] audit-pricing`` console-script entry. Wires
:func:`pricing_cli.parse_args` to a real evaluator chain so a user can
invoke the subcommand from a shell without writing Python glue.

Sample-source contract:

* ``--samples-file <path>`` — JSON file holding a list of PricingSample
  field dicts. Used in tests and when replaying an offline capture.

The L0 character-ratio evaluator is the default detector chain because
it is the only zero-dependency layer (PRD §6.3.1 dual-distribution
invariant). L1 / L2 layers are added by the upstream live-audit driver
(Story S5) which has access to tokenizer extras and balance probes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterable

from api_relay_audit.pricing import pricing_cli
from api_relay_audit.pricing.config import load_audit_config
from api_relay_audit.pricing.evaluators.base import PricingSample
from api_relay_audit.pricing.evaluators.l0_character_ratio import (
    L0CharacterRatioEvaluator,
)
from api_relay_audit.pricing.pipeline import (
    AggregatedPricingVerdict,
    aggregate,
)


def _load_samples(path: Path) -> list[PricingSample]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, list):
        raise ValueError(
            f"--samples-file {path} must contain a JSON list of "
            f"PricingSample dicts; got {type(payload).__name__}"
        )
    samples: list[PricingSample] = []
    for entry in payload:
        samples.append(PricingSample(
            vendor=entry["vendor"],
            model=entry["model"],
            input_text=entry.get("input_text", ""),
            output_text=entry.get("output_text", ""),
            reported_input_tokens=int(entry["reported_input_tokens"]),
            reported_output_tokens=int(entry["reported_output_tokens"]),
            balance_before_usd=entry.get("balance_before_usd"),
            balance_after_usd=entry.get("balance_after_usd"),
            reported_cost_usd=entry.get("reported_cost_usd"),
            metadata=entry.get("metadata", {}) or {},
        ))
    return samples


def _build_evaluator():
    l0 = L0CharacterRatioEvaluator()

    def evaluate(sample: PricingSample) -> AggregatedPricingVerdict:
        return aggregate([l0.evaluate(sample)])

    return evaluate


def main(argv: list[str] | None = None) -> int:
    args = pricing_cli.parse_args(
        list(sys.argv[1:]) if argv is None else list(argv)
    )

    if not args.no_config:
        load_audit_config(args.config)

    samples: Iterable[PricingSample]
    if args.samples_file:
        samples = _load_samples(Path(args.samples_file))
    else:
        samples = []

    return pricing_cli.run(
        samples=samples,
        evaluate_fn=_build_evaluator(),
        run_id=args.run_id,
        provider=args.provider,
        model=args.model,
        artifact_dir=Path(args.reports_dir),
        verbosity=args.verbosity,
    )


if __name__ == "__main__":
    raise SystemExit(main())
