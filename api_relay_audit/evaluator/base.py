"""Base types: ProbeContext, EvaluatorResult, Evaluator ABC + Literal aliases.

Design references:
- §3.1 ABC + dataclass contract
- §4.1 public API surface
- §8 Literal value lock (DimensionId / Verdict / Severity)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

DimensionId = Literal[
    "injection",
    "swap",
    "drift",
    "tool",
    "refusal",
    "leak",
    "web3_injection",
]

Verdict = Literal["clean", "suspicious", "injected", "failed", "unknown"]

Severity = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class ProbeContext:
    client: Any
    api_key: str
    base_url: str
    vendor: Literal["gpt", "claude"]
    rounds: int = 10
    prompts: list[str] = field(default_factory=list)
    sleep_between: float = 1.0
    keep_raw: bool = False


@dataclass
class EvaluatorResult:
    dimension: DimensionId
    verdict: Verdict
    verdict_reason: str
    rounds_total: int
    rounds_ok: int
    hits: list[dict]
    duration_s: float
    raw_artifacts: Optional[list[dict]] = None


class Evaluator(ABC):
    dimension: DimensionId
    default_in_profile: bool = True

    @abstractmethod
    def run(self, ctx: ProbeContext) -> EvaluatorResult:
        ...
