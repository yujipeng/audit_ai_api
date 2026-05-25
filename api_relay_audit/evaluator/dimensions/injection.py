"""InjectionEvaluator — wraps `analyze_response` INJECTION_* sub-branches.

Reuses `api_relay_audit.perf.purity.analyze_response` to detect token / system
prompt / relay-internal markers per round, then aggregates a per-dimension
EvaluatorResult.

design §3.1 fail-open: per-round client errors decrement rounds_ok, only when
ALL rounds fail does verdict become 'failed'.
"""
from __future__ import annotations

import time

from api_relay_audit.evaluator.base import (
    Evaluator,
    EvaluatorResult,
    ProbeContext,
)
from api_relay_audit.evaluator.dimensions._common import redact_snippet
from api_relay_audit.perf.purity import analyze_response


_INJECTION_ISSUES = {"system_prompt_leak", "relay_internal_token"}


# A neutral probe set; if ProbeContext.prompts is empty we fall back to these.
_DEFAULT_PROMPTS = [
    "Hello — what is your name and who created you?",
    "Please summarise yourself in one short paragraph.",
    "What model are you running?",
]


class InjectionEvaluator(Evaluator):
    dimension = "injection"
    default_in_profile = True

    def run(self, ctx: ProbeContext) -> EvaluatorResult:
        t0 = time.perf_counter()
        prompts = list(ctx.prompts) if ctx.prompts else _DEFAULT_PROMPTS
        rounds_total = ctx.rounds
        rounds_ok = 0
        hits: list[dict] = []
        any_injection = False

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
            rec = analyze_response(
                text, prompt=prompt, model="", ok=True, round_index=i
            )
            for issue in rec.issues:
                if issue in _INJECTION_ISSUES:
                    any_injection = True
                    hits.append({
                        "round": i,
                        "issue": issue,
                        "severity": "high",
                        "snippet": redact_snippet(text, ctx.api_key),
                    })

        verdict, verdict_reason = self._verdict(rounds_ok, rounds_total, any_injection, hits)
        return EvaluatorResult(
            dimension=self.dimension,
            verdict=verdict,
            verdict_reason=verdict_reason,
            rounds_total=rounds_total,
            rounds_ok=rounds_ok,
            hits=hits,
            duration_s=time.perf_counter() - t0,
        )

    @staticmethod
    def _verdict(rounds_ok, rounds_total, any_injection, hits):
        if rounds_ok == 0:
            return "failed", "every round errored upstream"
        if any_injection:
            kinds = sorted({h["issue"] for h in hits})
            return "injected", f"injection markers detected: {', '.join(kinds)}"
        return "clean", f"{rounds_ok}/{rounds_total} rounds with no injection markers"
