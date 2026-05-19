"""Performance benchmarking extension for api-relay-audit.

Adds streaming-based latency benchmarks (TTFT, p95/p99) and a lightweight
purity/prompt-injection check that re-uses the same response corpus.
"""

from api_relay_audit.perf.streaming import StreamingClient, StreamResult
from api_relay_audit.perf.metrics import summarize_latencies
from api_relay_audit.perf.purity import analyze_purity
from api_relay_audit.perf.runner import run_benchmark, load_config
from api_relay_audit.perf.report import write_json_report, write_html_report

__all__ = [
    "StreamingClient",
    "StreamResult",
    "summarize_latencies",
    "analyze_purity",
    "run_benchmark",
    "load_config",
    "write_json_report",
    "write_html_report",
]
