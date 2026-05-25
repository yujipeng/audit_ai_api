"""Tests for the perf-bench streaming client chunk_timings capture.

S3-1 (TES-167) introduces three new ``StreamResult`` fields
(``chunk_intervals`` / ``chunk_timestamps`` / ``chunk_intervals_truncated``)
populated only when ``StreamingClient.stream`` is called with
``capture_chunk_timings=True``. The fields are bounded by
``MAX_CHUNK_RECORDS`` to keep memory safe under runaway streams.

The tests use a deterministic ``time.perf_counter`` and a fake
``httpx.stream`` context manager that yields a configurable list of SSE
lines, so we can assert exact timing values without touching the network.
"""

from __future__ import annotations

import json
from typing import Iterable
from unittest.mock import MagicMock, patch

import pytest

from api_relay_audit.perf import streaming as perf_streaming
from api_relay_audit.perf.streaming import StreamingClient, StreamResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse_openai_text(text: str) -> str:
    """One OpenAI-flavoured SSE line with a text delta."""
    return "data: " + json.dumps({
        "choices": [{"delta": {"content": text}}],
    })


def _sse_openai_finish(reason: str = "stop") -> str:
    return "data: " + json.dumps({
        "choices": [{"delta": {}, "finish_reason": reason}],
    })


def _sse_anthropic_text(text: str) -> str:
    return "data: " + json.dumps({
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": text},
    })


def _sse_anthropic_stop(reason: str = "end_turn") -> str:
    return "data: " + json.dumps({
        "type": "message_delta",
        "delta": {"stop_reason": reason},
    })


def _make_stream_cm(lines: Iterable[str], *, status_code: int = 200,
                    body_bytes: bytes = b"") -> MagicMock:
    """Build a fake ``httpx.stream(...)`` context manager.

    The returned response exposes ``status_code``, ``headers``, ``read``
    (used for the error path) and ``iter_lines`` (used for the SSE parse
    loop in :mod:`api_relay_audit.perf.streaming`).
    """
    response = MagicMock()
    response.status_code = status_code
    response.headers = {"content-type": "text/event-stream"}
    response.read = MagicMock(return_value=body_bytes)
    response.iter_lines = MagicMock(return_value=iter(list(lines) + ["data: [DONE]"]))

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=response)
    cm.__exit__ = MagicMock(return_value=None)
    return cm


def _deterministic_clock(step: float = 0.05):
    """Return a ``perf_counter`` replacement that advances ``step`` each call."""
    state = {"t": 0.0}

    def fn() -> float:
        t = state["t"]
        state["t"] += step
        return t

    return fn


# ---------------------------------------------------------------------------
# Default behaviour: capture_chunk_timings=False (the v1-compatible path)
# ---------------------------------------------------------------------------


def test_capture_chunk_timings_disabled_by_default_openai():
    """When the flag is unspecified the three new fields stay None / False."""
    client = StreamingClient("https://relay.example.com", "sk-test")
    lines = [_sse_openai_text("hi"), _sse_openai_text(" there"),
             _sse_openai_finish()]
    with patch.object(perf_streaming.httpx, "stream",
                      return_value=_make_stream_cm(lines)):
        result = client.stream(model="gpt-5.2", prompt="ping")

    assert result.ok is True
    assert result.chunk_count == 3
    assert result.text == "hi there"
    # The three new fields are absent (None / False) when the flag is off.
    assert result.chunk_intervals is None
    assert result.chunk_timestamps is None
    assert result.chunk_intervals_truncated is False


def test_capture_chunk_timings_disabled_by_default_anthropic():
    client = StreamingClient("https://relay.example.com", "sk-test",
                             format="anthropic")
    lines = [_sse_anthropic_text("hi"), _sse_anthropic_text(" there"),
             _sse_anthropic_stop()]
    with patch.object(perf_streaming.httpx, "stream",
                      return_value=_make_stream_cm(lines)):
        result = client.stream(model="claude-haiku-4-5", prompt="ping")

    assert result.ok is True
    assert result.chunk_intervals is None
    assert result.chunk_timestamps is None
    assert result.chunk_intervals_truncated is False


# ---------------------------------------------------------------------------
# Flag ON — happy path
# ---------------------------------------------------------------------------


def test_capture_chunk_timings_openai_records_timestamps():
    """OpenAI path records one timestamp per JSON-parsed SSE event."""
    client = StreamingClient("https://relay.example.com", "sk-test")
    lines = [_sse_openai_text("a"), _sse_openai_text("b"),
             _sse_openai_text("c"), _sse_openai_finish()]
    with patch.object(perf_streaming.time, "perf_counter",
                      side_effect=_deterministic_clock(step=0.05)), \
         patch.object(perf_streaming.httpx, "stream",
                      return_value=_make_stream_cm(lines)):
        result = client.stream(model="gpt-5.2", prompt="ping",
                               capture_chunk_timings=True)

    assert result.ok is True
    assert result.chunk_count == 4
    assert result.chunk_timestamps is not None
    assert len(result.chunk_timestamps) == 4
    # Timestamps are monotonically non-decreasing.
    assert all(b >= a for a, b in zip(result.chunk_timestamps,
                                       result.chunk_timestamps[1:]))
    # All positive (the test clock starts at 0 for the request, advances by
    # 0.05 on every perf_counter() call inside the stream loop).
    assert all(t > 0 for t in result.chunk_timestamps)

    # chunk_intervals = gaps between consecutive timestamps
    assert result.chunk_intervals is not None
    assert len(result.chunk_intervals) == result.chunk_count - 1
    assert all(d >= 0 for d in result.chunk_intervals)

    assert result.chunk_intervals_truncated is False


def test_capture_chunk_timings_anthropic_records_timestamps():
    """Anthropic path also populates chunk timings under the same flag."""
    client = StreamingClient("https://relay.example.com", "sk-test",
                             format="anthropic")
    lines = [_sse_anthropic_text("a"), _sse_anthropic_text("b"),
             _sse_anthropic_stop()]
    with patch.object(perf_streaming.time, "perf_counter",
                      side_effect=_deterministic_clock(step=0.1)), \
         patch.object(perf_streaming.httpx, "stream",
                      return_value=_make_stream_cm(lines)):
        result = client.stream(model="claude-haiku-4-5", prompt="ping",
                               capture_chunk_timings=True)

    assert result.ok is True
    assert result.chunk_count == 3
    assert result.chunk_timestamps is not None
    assert len(result.chunk_timestamps) == 3
    assert all(b >= a for a, b in zip(result.chunk_timestamps,
                                       result.chunk_timestamps[1:]))
    assert result.chunk_intervals is not None
    assert len(result.chunk_intervals) == 2
    assert all(d >= 0 for d in result.chunk_intervals)
    assert result.chunk_intervals_truncated is False


def test_chunk_intervals_length_equals_chunk_count_minus_one():
    """Acceptance criterion from TES-167 DoD."""
    client = StreamingClient("https://relay.example.com", "sk-test")
    lines = [_sse_openai_text(f"t{i}") for i in range(5)] + [_sse_openai_finish()]
    with patch.object(perf_streaming.httpx, "stream",
                      return_value=_make_stream_cm(lines)):
        result = client.stream(model="gpt-5.2", prompt="ping",
                               capture_chunk_timings=True)

    assert result.ok is True
    assert result.chunk_count == 6
    assert result.chunk_intervals is not None
    assert len(result.chunk_intervals) == result.chunk_count - 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_single_chunk_yields_empty_intervals_but_one_timestamp():
    """One SSE event → 1 timestamp, 0 intervals (no gap to measure)."""
    client = StreamingClient("https://relay.example.com", "sk-test")
    lines = [_sse_openai_text("only")]
    with patch.object(perf_streaming.httpx, "stream",
                      return_value=_make_stream_cm(lines)):
        result = client.stream(model="gpt-5.2", prompt="ping",
                               capture_chunk_timings=True)

    assert result.ok is True
    assert result.chunk_count == 1
    assert result.chunk_timestamps is not None
    assert len(result.chunk_timestamps) == 1
    assert result.chunk_intervals == []
    assert result.chunk_intervals_truncated is False


def test_zero_chunks_yields_empty_lists():
    """A stream that finishes with no data events → both lists empty."""
    client = StreamingClient("https://relay.example.com", "sk-test")
    with patch.object(perf_streaming.httpx, "stream",
                      return_value=_make_stream_cm([])):
        result = client.stream(model="gpt-5.2", prompt="ping",
                               capture_chunk_timings=True)

    assert result.ok is True
    assert result.chunk_count == 0
    assert result.chunk_timestamps == []
    assert result.chunk_intervals == []
    assert result.chunk_intervals_truncated is False


def test_http_error_leaves_chunk_timings_null():
    """Non-200 errors short-circuit before the parse loop — fields stay None."""
    client = StreamingClient("https://relay.example.com", "sk-test")
    error_cm = _make_stream_cm([], status_code=403,
                               body_bytes=b'{"error":"forbidden"}')
    with patch.object(perf_streaming.httpx, "stream", return_value=error_cm):
        result = client.stream(model="gpt-5.2", prompt="ping",
                               capture_chunk_timings=True)

    assert result.ok is False
    assert result.status_code == 403
    assert result.chunk_intervals is None
    assert result.chunk_timestamps is None
    assert result.chunk_intervals_truncated is False


# ---------------------------------------------------------------------------
# Memory protection (MAX_CHUNK_RECORDS)
# ---------------------------------------------------------------------------


def test_truncation_flag_set_when_chunks_exceed_max_records(monkeypatch):
    """When chunk_count > MAX_CHUNK_RECORDS the lists are bounded and a flag is set."""
    # Shrink the cap so the test stays fast.
    monkeypatch.setattr(perf_streaming, "MAX_CHUNK_RECORDS", 5)

    client = StreamingClient("https://relay.example.com", "sk-test")
    # 12 text chunks → above the patched cap of 5
    lines = [_sse_openai_text(f"t{i}") for i in range(12)]
    with patch.object(perf_streaming.httpx, "stream",
                      return_value=_make_stream_cm(lines)):
        result = client.stream(model="gpt-5.2", prompt="ping",
                               capture_chunk_timings=True)

    assert result.ok is True
    assert result.chunk_count == 12
    # All chunks are still counted, but the captured arrays are bounded.
    assert result.chunk_intervals_truncated is True
    assert result.chunk_timestamps is not None
    assert result.chunk_intervals is not None
    assert len(result.chunk_timestamps) <= 5
    # intervals stay at most one fewer than the bounded timestamps
    assert len(result.chunk_intervals) <= len(result.chunk_timestamps)


def test_truncation_flag_not_set_at_exact_max_records(monkeypatch):
    """At exactly MAX_CHUNK_RECORDS we record all and DO NOT flag truncation."""
    monkeypatch.setattr(perf_streaming, "MAX_CHUNK_RECORDS", 4)

    client = StreamingClient("https://relay.example.com", "sk-test")
    lines = [_sse_openai_text(f"t{i}") for i in range(4)]
    with patch.object(perf_streaming.httpx, "stream",
                      return_value=_make_stream_cm(lines)):
        result = client.stream(model="gpt-5.2", prompt="ping",
                               capture_chunk_timings=True)

    assert result.ok is True
    assert result.chunk_count == 4
    assert result.chunk_intervals_truncated is False
    assert len(result.chunk_timestamps) == 4
    assert len(result.chunk_intervals) == 3
