"""Tests for the perf-bench runner wiring of capture_chunk_timings (S3-1 / TES-167).

Validates that:
- ``schema_version`` was bumped to 2 once chunk-timing capture became part
  of the result schema.
- The ``test.capture_chunk_timings`` flag flows from config into
  ``StreamingClient.stream`` and the per-round payload.
- v1 round fields stay unchanged when capture is off (regression guard).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from api_relay_audit.perf import runner
from api_relay_audit.perf.streaming import StreamResult


def _fake_stream_result(*, chunk_count: int = 3,
                        capture: bool = False) -> StreamResult:
    timestamps = [0.05 * i for i in range(1, chunk_count + 1)] if capture else None
    intervals = (
        [timestamps[i + 1] - timestamps[i] for i in range(chunk_count - 1)]
        if capture else None)
    return StreamResult(
        ok=True, ttft=0.05, total_time=0.20, text="hello",
        chunk_count=chunk_count, finish_reason="stop", status_code=200,
        error=None, format="openai", model="m",
        chunk_timestamps=timestamps,
        chunk_intervals=intervals,
        chunk_intervals_truncated=False,
    )


def _minimal_cfg(*, capture: bool) -> dict:
    return {
        "test": {
            "prompt": "ping",
            "rounds": 2,
            "timeout": 5,
            "max_tokens": 16,
            "temperature": None,
            "concurrency": 1,
            "format": "openai",
            "system": None,
            "prompts": None,
            "capture_chunk_timings": capture,
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


def test_runner_schema_version_is_two():
    """schema_version bumped to 2 now that chunk-timing capture is part of the schema."""
    cfg = _minimal_cfg(capture=False)
    with patch.object(runner, "_execute_one",
                      return_value=_fake_stream_result()), \
         patch.object(runner, "fetch_models", return_value=([], None)):
        result = runner.run_benchmark(cfg, log_fn=lambda _m: None)
    assert result["schema_version"] == 2


def test_round_payload_omits_chunk_timing_fields_when_disabled():
    """v1-compatibility: capture off → chunk_intervals / _timestamps absent or null."""
    cfg = _minimal_cfg(capture=False)
    with patch.object(runner, "_execute_one",
                      return_value=_fake_stream_result(capture=False)), \
         patch.object(runner, "fetch_models", return_value=([], None)):
        result = runner.run_benchmark(cfg, log_fn=lambda _m: None)
    rounds = result["endpoints"][0]["results"][0]["rounds"]
    assert rounds, "expected at least one round"
    for row in rounds:
        # All v1 fields still present
        for key in ["round", "ok", "status_code", "ttft_seconds",
                    "total_seconds", "output_chars", "chunk_count",
                    "finish_reason", "error", "format", "text_preview"]:
            assert key in row, f"v1 field {key!r} missing from round payload"
        # Capture-off: the new fields are null (preserved as keys for schema
        # stability, but carry no data).
        assert row.get("chunk_intervals") is None
        assert row.get("chunk_timestamps") is None
        assert row.get("chunk_intervals_truncated") is False


def test_round_payload_includes_chunk_timing_fields_when_enabled():
    cfg = _minimal_cfg(capture=True)
    with patch.object(runner, "_execute_one",
                      return_value=_fake_stream_result(chunk_count=4,
                                                       capture=True)), \
         patch.object(runner, "fetch_models", return_value=([], None)):
        result = runner.run_benchmark(cfg, log_fn=lambda _m: None)
    rounds = result["endpoints"][0]["results"][0]["rounds"]
    for row in rounds:
        assert isinstance(row["chunk_timestamps"], list)
        assert isinstance(row["chunk_intervals"], list)
        # DoD invariant: len(intervals) == chunk_count - 1
        assert len(row["chunk_intervals"]) == row["chunk_count"] - 1
        assert row["chunk_intervals_truncated"] is False


def test_capture_flag_threads_through_to_streaming_client():
    """When config sets capture_chunk_timings=True, _execute_one must pass it."""
    cfg = _minimal_cfg(capture=True)
    captured_kwargs: list[dict] = []

    def fake_execute_one(client, **kwargs):
        captured_kwargs.append(kwargs)
        return _fake_stream_result(capture=True)

    with patch.object(runner, "_execute_one", side_effect=fake_execute_one), \
         patch.object(runner, "fetch_models", return_value=([], None)):
        runner.run_benchmark(cfg, log_fn=lambda _m: None)

    assert captured_kwargs, "_execute_one was not called"
    for kw in captured_kwargs:
        assert kw.get("capture_chunk_timings") is True


def test_capture_flag_default_off_when_unset():
    """A config without capture_chunk_timings must NOT enable capture."""
    cfg = _minimal_cfg(capture=False)
    cfg["test"].pop("capture_chunk_timings", None)
    captured_kwargs: list[dict] = []

    def fake_execute_one(client, **kwargs):
        captured_kwargs.append(kwargs)
        return _fake_stream_result(capture=False)

    with patch.object(runner, "_execute_one", side_effect=fake_execute_one), \
         patch.object(runner, "fetch_models", return_value=([], None)):
        runner.run_benchmark(cfg, log_fn=lambda _m: None)

    assert captured_kwargs
    for kw in captured_kwargs:
        assert kw.get("capture_chunk_timings", False) is False


def test_load_config_normalises_capture_chunk_timings(tmp_path):
    """YAML round-trip preserves the new test-level flag (defaults to False)."""
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(
        "test:\n"
        "  prompt: hi\n"
        "  capture_chunk_timings: true\n"
        "endpoints:\n"
        "  - name: ep\n"
        "    base_url: https://relay.example.com\n"
        "    api_key: k\n"
        "    models: [m]\n",
        encoding="utf-8",
    )
    cfg = runner.load_config(str(cfg_path))
    assert cfg["test"]["capture_chunk_timings"] is True

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
    cfg2 = runner.load_config(str(cfg_path))
    assert cfg2["test"]["capture_chunk_timings"] is False
