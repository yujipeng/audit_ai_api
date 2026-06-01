"""Runner integration for chunk metrics + detector triggering + warmup
剔除 (S3-3 / TES-169).

Validates design §4.6 / §5.2 / §10 Q1 锚点：

- ``metrics.itl_seconds`` / ``throughput_chars_s`` / ``first_chunk_ratio``
  全部从 ``chunk_intervals`` 聚合而来，关闭 capture 时为 ``None``。
- ``metrics.warmup_rounds_count`` 与 ``metrics.steady_state_metrics`` 对应
  design §4.6 字段表 — 关闭 warmup 时 count=0、steady=None。
- ``detectors`` 子节点 (``pseudo_stream`` / ``slow_start``) 在开启对应
  flag 时由 runner 触发并写回 result，关闭时为 None。
- v1 cap 字段 (``schema_version=2`` / round v1 字段) 不被打破。

Mocks ``_execute_one`` 以避开网络；行级断言锚点 design §4.6。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from api_relay_audit.perf import runner
from api_relay_audit.perf.streaming import StreamResult


def _stream_result(*, chunk_intervals=None, output_chars=580,
                   ttft=0.4, total=4.0, ok=True,
                   chunk_count=None, status_code=200,
                   error=None) -> StreamResult:
    """Build a StreamResult mirroring what StreamingClient would emit."""
    if chunk_intervals is not None and chunk_count is None:
        chunk_count = len(chunk_intervals) + 1
    timestamps = None
    if chunk_intervals is not None:
        # Reconstruct timestamps from intervals + ttft (best-effort, runner
        # uses chunk_intervals only).
        timestamps = [ttft]
        for gap in chunk_intervals:
            timestamps.append(timestamps[-1] + gap)
    text = "x" * output_chars
    return StreamResult(
        ok=ok,
        ttft=ttft if ok else None,
        total_time=total,
        text=text if ok else "",
        chunk_count=chunk_count or 0,
        finish_reason="stop" if ok else None,
        status_code=status_code,
        error=error,
        format="openai",
        model="m",
        chunk_intervals=chunk_intervals,
        chunk_timestamps=timestamps,
        chunk_intervals_truncated=False,
    )


def _cfg(*, capture: bool = True,
         detect_pseudo: bool = False,
         detect_slow_start: bool = False,
         warmup_rounds: int = 0,
         rounds: int = 5) -> dict:
    return {
        "test": {
            "prompt": "ping",
            "rounds": rounds,
            "timeout": 5,
            "max_tokens": 16,
            "temperature": None,
            "concurrency": 1,
            "format": "openai",
            "system": None,
            "prompts": None,
            "capture_chunk_timings": capture,
            "detect_pseudo_stream": detect_pseudo,
            "detect_slow_start": detect_slow_start,
            "warmup_rounds": warmup_rounds,
        },
        "default_models": ["m"],
        "endpoints": [{
            "name": "ep",
            "base_url": "https://relay.example.com",
            "api_key": "k",
            "models": ["m"],
            "format": "openai",
            "vendor": None,
        }],
    }


def _run(cfg, stream_results):
    """Run benchmark with a sequence of fake stream results (1-per-round)."""
    it = iter(stream_results)

    def fake_execute(client, **kwargs):
        return next(it)

    with patch.object(runner, "_execute_one", side_effect=fake_execute), \
         patch.object(runner, "fetch_models", return_value=([], None)):
        return runner.run_benchmark(cfg, log_fn=lambda _m: None)


# ---- chunk metrics: itl_seconds / throughput_chars_s / first_chunk_ratio --

def test_metrics_itl_seconds_from_chunk_intervals():
    """capture on → metrics.itl_seconds is summarize_latencies over flattened intervals."""
    intervals = [0.02, 0.018, 0.022, 0.020, 0.019]
    cfg = _cfg(capture=True, rounds=3)
    results = [_stream_result(chunk_intervals=list(intervals)) for _ in range(3)]
    out = _run(cfg, results)
    metrics = out["endpoints"][0]["results"][0]["metrics"]
    itl = metrics["itl_seconds"]
    assert itl is not None
    assert itl["count"] == len(intervals) * 3   # flattened over rounds
    assert itl["p50"] == pytest.approx(0.020, rel=0.05)


def test_metrics_throughput_chars_s_per_round():
    """throughput_chars_s = output_chars / total_seconds, summarized per ok round."""
    cfg = _cfg(capture=True, rounds=3)
    results = [_stream_result(chunk_intervals=[0.02] * 4,
                              output_chars=400, total=2.0) for _ in range(3)]
    out = _run(cfg, results)
    metrics = out["endpoints"][0]["results"][0]["metrics"]
    tp = metrics["throughput_chars_s"]
    assert tp is not None
    assert tp["count"] == 3
    assert tp["p50"] == pytest.approx(200.0, rel=0.01)


def test_metrics_first_chunk_ratio_per_round():
    """first_chunk_ratio = chunk_intervals[0] / median(chunk_intervals[1:])."""
    intervals = [0.10, 0.02, 0.02, 0.02, 0.02]   # first_chunk_ratio = 5.0
    cfg = _cfg(capture=True, rounds=3)
    results = [_stream_result(chunk_intervals=list(intervals)) for _ in range(3)]
    out = _run(cfg, results)
    metrics = out["endpoints"][0]["results"][0]["metrics"]
    fcr = metrics["first_chunk_ratio"]
    assert fcr is not None
    assert fcr["count"] == 3
    assert fcr["p50"] == pytest.approx(5.0, rel=0.01)


def test_chunk_metrics_null_when_capture_off():
    """capture off → all v2 chunk-derived metrics are null (design §4.6)."""
    cfg = _cfg(capture=False, rounds=3)
    results = [_stream_result(chunk_intervals=None, chunk_count=3) for _ in range(3)]
    out = _run(cfg, results)
    metrics = out["endpoints"][0]["results"][0]["metrics"]
    assert metrics["itl_seconds"] is None
    assert metrics["throughput_chars_s"] is None
    assert metrics["first_chunk_ratio"] is None


def test_chunk_metrics_skip_failed_rounds():
    """Failed rounds (ok=False) must not contribute to chunk metrics."""
    cfg = _cfg(capture=True, rounds=4)
    good = [_stream_result(chunk_intervals=[0.02] * 4, output_chars=400,
                           total=2.0) for _ in range(2)]
    bad = [_stream_result(ok=False, status_code=500, error="HTTP 500",
                          chunk_intervals=None, chunk_count=0,
                          output_chars=0, total=0.5) for _ in range(2)]
    out = _run(cfg, good + bad)
    metrics = out["endpoints"][0]["results"][0]["metrics"]
    # 2 ok rows, 4 intervals each → 8 samples
    assert metrics["itl_seconds"]["count"] == 8
    assert metrics["throughput_chars_s"]["count"] == 2
    assert metrics["first_chunk_ratio"]["count"] == 2


# ---- warmup_rounds / steady_state_metrics ---------------------------------

def test_warmup_rounds_count_and_steady_metrics():
    """warmup_rounds=3 → count=3, steady_state_metrics summarizes rounds[3:]."""
    cfg = _cfg(capture=True, warmup_rounds=3, rounds=10)
    # Warmup: TTFT 2.0s; steady: TTFT 0.5s. Steady metrics should reflect 0.5.
    warmup = [_stream_result(chunk_intervals=[0.02] * 4, output_chars=400,
                             ttft=2.0, total=4.0) for _ in range(3)]
    steady = [_stream_result(chunk_intervals=[0.02] * 4, output_chars=400,
                             ttft=0.5, total=2.0) for _ in range(7)]
    out = _run(cfg, warmup + steady)
    metrics = out["endpoints"][0]["results"][0]["metrics"]
    assert metrics["warmup_rounds_count"] == 3
    steady_metrics = metrics["steady_state_metrics"]
    assert steady_metrics is not None
    assert steady_metrics["ttft_seconds"]["count"] == 7
    assert steady_metrics["ttft_seconds"]["avg"] == pytest.approx(0.5)


def test_warmup_rounds_zero_default_no_steady():
    """warmup_rounds=0 (default) → count=0 and steady_state_metrics is None."""
    cfg = _cfg(capture=False, warmup_rounds=0, rounds=5)
    results = [_stream_result(chunk_intervals=None, chunk_count=3,
                              ttft=0.5, total=2.0) for _ in range(5)]
    out = _run(cfg, results)
    metrics = out["endpoints"][0]["results"][0]["metrics"]
    assert metrics["warmup_rounds_count"] == 0
    assert metrics["steady_state_metrics"] is None


def test_warmup_rounds_capped_at_total_rounds():
    """warmup_rounds >= rounds → steady empty → steady_state_metrics is None."""
    cfg = _cfg(capture=True, warmup_rounds=5, rounds=5)
    results = [_stream_result(chunk_intervals=[0.02] * 4) for _ in range(5)]
    out = _run(cfg, results)
    metrics = out["endpoints"][0]["results"][0]["metrics"]
    assert metrics["warmup_rounds_count"] == 5
    assert metrics["steady_state_metrics"] is None


# ---- detector wiring ------------------------------------------------------

def test_detectors_node_present_in_result():
    """Result always carries a `detectors` node with both subkeys (None when off)."""
    cfg = _cfg(capture=False, detect_pseudo=False, detect_slow_start=False)
    results = [_stream_result(chunk_intervals=None, chunk_count=3) for _ in range(5)]
    out = _run(cfg, results)
    detectors = out["endpoints"][0]["results"][0]["detectors"]
    assert detectors == {"pseudo_stream": None, "slow_start": None}


def test_pseudo_stream_detector_triggers_when_enabled():
    """detect_pseudo=True + capture=True → detectors.pseudo_stream verdict ∈ {real, pseudo, inconclusive}."""
    cfg = _cfg(capture=True, detect_pseudo=True, rounds=5)
    results = [_stream_result(chunk_intervals=[0.02] * 86) for _ in range(5)]
    out = _run(cfg, results)
    pseudo = out["endpoints"][0]["results"][0]["detectors"]["pseudo_stream"]
    assert pseudo is not None
    assert pseudo["verdict"] in {"real", "pseudo", "inconclusive"}
    assert "evidence" in pseudo


def test_slow_start_detector_triggers_when_enabled():
    """detect_slow_start=True → detectors.slow_start verdict populated."""
    cfg = _cfg(capture=True, detect_slow_start=True, rounds=10)
    warmup = [_stream_result(chunk_intervals=[0.02] * 4, ttft=2.0)
              for _ in range(3)]
    steady = [_stream_result(chunk_intervals=[0.02] * 4, ttft=0.5)
              for _ in range(7)]
    out = _run(cfg, warmup + steady)
    ss = out["endpoints"][0]["results"][0]["detectors"]["slow_start"]
    assert ss is not None
    assert ss["verdict"] == "slow_start"


def test_pseudo_stream_detector_off_when_capture_off():
    """detect_pseudo=True but capture=False → still inconclusive (no intervals)."""
    cfg = _cfg(capture=False, detect_pseudo=True, rounds=5)
    results = [_stream_result(chunk_intervals=None, chunk_count=3) for _ in range(5)]
    out = _run(cfg, results)
    pseudo = out["endpoints"][0]["results"][0]["detectors"]["pseudo_stream"]
    assert pseudo is not None
    assert pseudo["verdict"] == "inconclusive"


# ---- schema integration ---------------------------------------------------

def test_schema_v2_metrics_carry_all_v2_keys_even_when_capture_off():
    """design §4.6: v2 keys always present (null), v1 keys never break."""
    cfg = _cfg(capture=False, rounds=3)
    results = [_stream_result(chunk_intervals=None, chunk_count=3) for _ in range(3)]
    out = _run(cfg, results)
    metrics = out["endpoints"][0]["results"][0]["metrics"]
    for v1_key in ["ttft_seconds", "total_seconds", "output_chars",
                   "success_rate", "successful_rounds", "failed_rounds"]:
        assert v1_key in metrics, f"v1 metrics field {v1_key!r} dropped"
    for v2_key in ["itl_seconds", "throughput_chars_s", "first_chunk_ratio",
                   "warmup_rounds_count", "steady_state_metrics"]:
        assert v2_key in metrics, f"v2 metrics field {v2_key!r} missing"


# ---- load_config wiring ---------------------------------------------------

def test_load_config_normalises_detect_and_warmup_flags(tmp_path):
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        "test:\n"
        "  prompt: hi\n"
        "  capture_chunk_timings: true\n"
        "  detect_pseudo_stream: true\n"
        "  detect_slow_start: true\n"
        "  warmup_rounds: 3\n"
        "endpoints:\n"
        "  - name: ep\n"
        "    base_url: https://relay.example.com\n"
        "    api_key: k\n"
        "    models: [m]\n",
        encoding="utf-8",
    )
    cfg = runner.load_config(str(cfg_path))
    assert cfg["test"]["capture_chunk_timings"] is True
    assert cfg["test"]["detect_pseudo_stream"] is True
    assert cfg["test"]["detect_slow_start"] is True
    assert cfg["test"]["warmup_rounds"] == 3


def test_load_config_defaults_for_new_v2_flags(tmp_path):
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        "test:\n"
        "  prompt: hi\n"
        "endpoints:\n"
        "  - name: ep\n"
        "    base_url: https://relay.example.com\n"
        "    api_key: k\n"
        "    models: [m]\n",
        encoding="utf-8",
    )
    cfg = runner.load_config(str(cfg_path))
    assert cfg["test"]["detect_pseudo_stream"] is False
    assert cfg["test"]["detect_slow_start"] is False
    assert cfg["test"]["warmup_rounds"] == 0
