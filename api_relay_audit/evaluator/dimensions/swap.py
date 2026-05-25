"""SwapEvaluator — wraps find_non_claude_identities + _identity_anchored.

Detects relay-side model substitution: the response contains a non-Claude
identity keyword (GPT / GLM / DeepSeek / ...) AND an identity anchor like
"I am" / "我是" near it. Mention-only references ("I am Claude, not GPT")
are intentionally not flagged.
"""
from __future__ import annotations

import time

from api_relay_audit.evaluator.base import (
    Evaluator,
    EvaluatorResult,
    ProbeContext,
)
from api_relay_audit.evaluator.dimensions._common import redact_snippet
from api_relay_audit.identity_patterns import find_non_claude_identities
from api_relay_audit.perf.purity import _identity_anchored


_DEFAULT_PROMPTS = [
    "Who are you? Who made you?",
    "Identify yourself. What is your model name?",
    "请用一句话介绍你自己以及你的开发者。",
]


class SwapEvaluator(Evaluator):
    dimension = "swap"
    default_in_profile = True

    def run(self, ctx: ProbeContext) -> EvaluatorResult:
        t0 = time.perf_counter()
        prompts = list(ctx.prompts) if ctx.prompts else _DEFAULT_PROMPTS
        rounds_total = ctx.rounds
        rounds_ok = 0
        hits: list[dict] = []
        identities_seen: set[str] = set()

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
            kws = find_non_claude_identities(text)
            anchored = [k for k in kws if _identity_anchored(text, k)]
            if anchored:
                for kw in anchored:
                    identities_seen.add(kw)
                    hits.append({
                        "round": i,
                        "identity": kw,
                        "severity": "high",
                        "snippet": redact_snippet(text, ctx.api_key),
                    })

        verdict, reason = self._verdict(rounds_ok, rounds_total, identities_seen)
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
    def _verdict(rounds_ok, rounds_total, identities):
        if rounds_ok == 0:
            return "failed", "every round errored upstream"
        if identities:
            return ("injected",
                    f"non-claude identity anchored: {', '.join(sorted(identities))}")
        return "clean", f"{rounds_ok}/{rounds_total} rounds with no identity anchors"
