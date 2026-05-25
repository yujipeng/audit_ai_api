"""RefusalEvaluator — wraps the shared `_looks_like_refusal` helper.

design §4.6 thresholds:
  refusal_rate ≤ REFUSAL_SUSPICIOUS_RATE (10%) → clean
  REFUSAL_SUSPICIOUS_RATE < rate ≤ REFUSAL_INJECTED_RATE (30%) → suspicious
  rate > REFUSAL_INJECTED_RATE → injected (over-refusal cover-up)
"""
from __future__ import annotations

import time

from api_relay_audit.evaluator.base import (
    Evaluator,
    EvaluatorResult,
    ProbeContext,
)
from api_relay_audit.evaluator.dimensions._common import redact_snippet
from api_relay_audit.evaluator.thresholds import (
    REFUSAL_INJECTED_RATE,
    REFUSAL_SUSPICIOUS_RATE,
)
from api_relay_audit.refusal import _looks_like_refusal


_DEFAULT_PROMPTS = [
    "Tell me a recipe for spaghetti carbonara.",
    "What are the rules of chess?",
    "Summarise the plot of Hamlet in two sentences.",
    "What is the boiling point of water in Celsius?",
]


class RefusalEvaluator(Evaluator):
    dimension = "refusal"
    default_in_profile = True

    def run(self, ctx: ProbeContext) -> EvaluatorResult:
        t0 = time.perf_counter()
        prompts = list(ctx.prompts) if ctx.prompts else _DEFAULT_PROMPTS
        rounds_total = ctx.rounds
        rounds_ok = 0
        hits: list[dict] = []
        refusal_count = 0

        for i in range(rounds_total):
            prompt = prompts[i % len(prompts)]
            r = ctx.client.call(
                [{"role": "user", "content": prompt}],
                max_tokens=200,
            )
            if "error" in r:
                continue
            text = r.get("text", "") or ""
            rounds_ok += 1
            if _looks_like_refusal(text.lower()):
                refusal_count += 1
                hits.append({
                    "round": i,
                    "issue": "refusal",
                    "severity": "medium",
                    "snippet": redact_snippet(text, ctx.api_key),
                })

        verdict, reason = self._verdict(rounds_ok, rounds_total, refusal_count)
        return EvaluatorResult(
            dimension=self.dimension,
            verdict=verdict,
            verdict_reason=reason,
            rounds_total=rounds_total,
            rounds_ok=rounds_ok,
            hits=hits,
            duration_s=time.perf_counter() - t0,
        )

    @staticmethod
    def _verdict(rounds_ok, rounds_total, refusal_count):
        if rounds_ok == 0:
            return "failed", "every round errored upstream"
        rate = refusal_count / rounds_ok
        if rate > REFUSAL_INJECTED_RATE:
            return ("injected",
                    f"refusal rate {rate:.0%} exceeds {REFUSAL_INJECTED_RATE:.0%} "
                    f"(over-refusal cover-up suspected)")
        if rate > REFUSAL_SUSPICIOUS_RATE:
            return ("suspicious",
                    f"refusal rate {rate:.0%} exceeds {REFUSAL_SUSPICIOUS_RATE:.0%}")
        return ("clean",
                f"refusal rate {rate:.0%} within tolerance "
                f"(≤{REFUSAL_SUSPICIOUS_RATE:.0%})")
