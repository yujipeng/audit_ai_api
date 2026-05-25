"""evaluator subsystem — purity-v2 evaluation pipeline.

Public API (design §4.1, AC-F7):
- ProbeContext, EvaluatorResult, Evaluator
- EvaluatorPipeline, PurityAggregator, PurityReportV2, Overall
- default_purity_pipeline
- DimensionId, Verdict, Severity (Literal types, design §8 contract)
"""
from .aggregator import Overall, PurityAggregator, PurityReportV2
from .base import (
    DimensionId,
    Evaluator,
    EvaluatorResult,
    ProbeContext,
    Severity,
    Verdict,
)
from .factory import default_purity_pipeline
from .pipeline import EvaluatorPipeline

__all__ = [
    "ProbeContext",
    "EvaluatorResult",
    "Evaluator",
    "EvaluatorPipeline",
    "PurityAggregator",
    "PurityReportV2",
    "Overall",
    "default_purity_pipeline",
    "DimensionId",
    "Verdict",
    "Severity",
]
