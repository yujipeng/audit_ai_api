"""Business smoke for TES-167 DoD: drive scripts/perf-bench.py end-to-end
with a mocked httpx so the CLI flag --capture-chunk-timings is exercised
through argparse -> runner -> StreamingClient -> JSON report.

Verifies:
    - When --capture-chunk-timings is on, rounds[0].chunk_intervals has
      length chunk_count - 1.
    - When the flag is off, rounds[0].chunk_intervals is null.
    - schema_version == 2.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _make_sse_stream_cm():
    lines = []
    for i in range(5):
        evt = {"choices": [{"delta": {"content": f"t{i}"}}]}
        lines.append("data: " + json.dumps(evt))
    lines.append('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}')
    lines.append("data: [DONE]")

    response = MagicMock()
    response.status_code = 200
    response.headers = {"content-type": "text/event-stream"}
    response.iter_lines = MagicMock(side_effect=lambda: iter(list(lines)))
    response.read = MagicMock(return_value=b"")

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=response)
    cm.__exit__ = MagicMock(return_value=None)
    return cm


def _run_perf_bench(extra_args: list[str]) -> dict:
    out_dir = Path(tempfile.mkdtemp(prefix="perf-bench-smoke-"))
    json_path = out_dir / "report.json"
    html_path = out_dir / "report.html"
    argv = [
        "scripts/perf-bench.py",
        "https://relay.example.com",
        "sk-test",
        "gpt",
        "--rounds", "2",
        "--model", "gpt-5.2",
        "--output", str(html_path),
        "--json", str(json_path),
        "--quiet",
    ] + extra_args

    from api_relay_audit.perf import streaming as perf_streaming

    with patch.object(perf_streaming.httpx, "stream",
                      side_effect=lambda *a, **kw: _make_sse_stream_cm()), \
         patch.object(perf_streaming, "fetch_models",
                      return_value=([], None)):
        old_argv = sys.argv
        sys.argv = argv
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "perf_bench_module",
                str(ROOT / "scripts" / "perf-bench.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.main()
        finally:
            sys.argv = old_argv

    return json.loads(json_path.read_text(encoding="utf-8"))


def main() -> int:
    # Case A: capture enabled
    result_on = _run_perf_bench(["--capture-chunk-timings"])
    assert result_on["schema_version"] == 2, \
        f"schema_version expected 2, got {result_on['schema_version']!r}"
    rounds_on = result_on["endpoints"][0]["results"][0]["rounds"]
    assert rounds_on, "no rounds emitted"
    r0 = rounds_on[0]
    assert isinstance(r0["chunk_intervals"], list), \
        f"chunk_intervals should be a list when capture on, got {r0['chunk_intervals']!r}"
    assert len(r0["chunk_intervals"]) == r0["chunk_count"] - 1, \
        (f"DoD invariant: len(chunk_intervals)={len(r0['chunk_intervals'])} "
         f"vs chunk_count-1={r0['chunk_count'] - 1}")
    assert r0["chunk_intervals_truncated"] is False
    print("[smoke] capture=ON  chunk_count={}, len(chunk_intervals)={}, "
          "schema_version={}".format(
              r0["chunk_count"], len(r0["chunk_intervals"]),
              result_on["schema_version"]))

    # Case B: capture disabled (default)
    result_off = _run_perf_bench([])
    assert result_off["schema_version"] == 2, \
        f"schema_version expected 2, got {result_off['schema_version']!r}"
    r0 = result_off["endpoints"][0]["results"][0]["rounds"][0]
    assert r0["chunk_intervals"] is None, \
        f"chunk_intervals must be null when capture off, got {r0['chunk_intervals']!r}"
    assert r0["chunk_timestamps"] is None
    assert r0["chunk_intervals_truncated"] is False
    print("[smoke] capture=OFF chunk_intervals={!r}, schema_version={}".format(
        r0["chunk_intervals"], result_off["schema_version"]))

    print("[smoke] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
