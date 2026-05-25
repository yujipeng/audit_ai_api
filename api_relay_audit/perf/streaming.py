"""Streaming chat-completion client that records first-token latency.

Supports both OpenAI ``/v1/chat/completions`` and Anthropic native
``/v1/messages`` over SSE. Used by the perf benchmark to measure
TTFT (time to first token), total latency, and capture the full
response text for downstream purity analysis.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

import httpx


# Hard cap on per-call chunk timing samples. Detectors only need a few
# hundred to judge slow-start / pseudo-stream patterns; this prevents a
# pathological 6h stream from pinning ~MB of timestamps in memory.
MAX_CHUNK_RECORDS = 10000


@dataclass
class StreamResult:
    """Outcome of one streaming chat call.

    Attributes:
        ok: Whether the call completed without a transport / HTTP error.
        ttft: Seconds from request start to first non-empty content chunk.
            ``None`` if the call errored before any content arrived.
        total_time: Wall-clock seconds from request start to end-of-stream
            (or to the error).
        text: Concatenated content text, empty string on early failure.
        chunk_count: Number of SSE data chunks observed.
        finish_reason: ``finish_reason`` / ``stop_reason`` from the stream
            tail event, when present.
        status_code: HTTP status code (``0`` if the request never reached
            the server, e.g. DNS/SSL failure).
        error: Short human-readable error string. ``None`` on success.
        format: ``"openai"`` or ``"anthropic"`` — the wire format used.
        model: Model id sent in the request body.
        chunk_intervals: Per-call gaps (seconds) between successive parsed
            SSE chunks, only populated when the caller passes
            ``capture_chunk_timings=True`` to :meth:`StreamingClient.stream`.
            ``None`` when the flag is off; ``[]`` when 0 or 1 chunk was
            observed.
        chunk_timestamps: Wall-clock-relative arrival time (seconds since
            request start) of each parsed SSE chunk, captured under the
            same flag as ``chunk_intervals``. ``None`` when the flag is off.
        chunk_intervals_truncated: ``True`` when the captured arrays were
            bounded by ``MAX_CHUNK_RECORDS`` and the tail of the stream
            was not recorded. ``chunk_count`` keeps the true total even
            when this flag is set.
    """

    ok: bool
    ttft: Optional[float]
    total_time: float
    text: str
    chunk_count: int
    finish_reason: Optional[str]
    status_code: int
    error: Optional[str]
    format: str
    model: str
    raw_first_chunk: Optional[str] = None
    response_headers: dict = field(default_factory=dict)
    chunk_intervals: Optional[list[float]] = None
    chunk_timestamps: Optional[list[float]] = None
    chunk_intervals_truncated: bool = False


class StreamingClient:
    """Minimal SSE streaming client for OpenAI / Anthropic compatible relays.

    Unlike the audit ``APIClient`` this client always streams, captures the
    first-token timestamp via ``time.perf_counter``, and never falls back
    to non-streaming. Format is configurable per call (default ``openai``).
    """

    def __init__(self, base_url: str, api_key: str, *,
                 timeout: float = 60.0, format: str = "openai") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.format = format

    # -- URL helpers ---------------------------------------------------------

    def _openai_url(self) -> str:
        base = self.base_url
        if not base.endswith("/v1"):
            base = base + "/v1"
        return base + "/chat/completions"

    def _anthropic_url(self) -> str:
        base = self.base_url
        if base.endswith("/v1"):
            base = base[:-3]
        return base + "/v1/messages"

    # -- Public API ----------------------------------------------------------

    def stream(self, model: str, prompt: str, *,
               system: Optional[str] = None,
               max_tokens: int = 512,
               temperature: Optional[float] = None,
               capture_chunk_timings: bool = False) -> StreamResult:
        """Send a streaming request and collect the result.

        Always returns a ``StreamResult`` — never raises on transport
        failure. Errors are surfaced through ``ok=False`` and ``error``.

        ``temperature=None`` means the field is omitted from the request
        body entirely. Some newer models (e.g. ``claude-opus-4-7``) reject
        any ``temperature`` value with HTTP 400, so omitting it is the
        most-compatible default.

        Pass ``capture_chunk_timings=True`` to additionally record per-chunk
        arrival timestamps and inter-chunk gaps on the returned
        :class:`StreamResult`. Capture is bounded by
        :data:`MAX_CHUNK_RECORDS`; once the cap is hit the remaining
        timing samples are discarded and ``chunk_intervals_truncated`` is
        set. ``chunk_count`` keeps the true total either way.
        """
        if self.format == "anthropic":
            return self._stream_anthropic(model, prompt, system,
                                          max_tokens, temperature,
                                          capture_chunk_timings)
        return self._stream_openai(model, prompt, system,
                                   max_tokens, temperature,
                                   capture_chunk_timings)

    # -- OpenAI flavour ------------------------------------------------------

    def _stream_openai(self, model, prompt, system, max_tokens,
                       temperature, capture_chunk_timings=False) -> StreamResult:
        url = self._openai_url()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if temperature is not None:
            body["temperature"] = temperature
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }

        start = time.perf_counter()
        ttft = None
        text_parts: list[str] = []
        chunk_count = 0
        finish_reason: Optional[str] = None
        first_chunk_raw = None
        chunk_timestamps: Optional[list[float]] = (
            [] if capture_chunk_timings else None)
        chunk_intervals_truncated = False
        try:
            with httpx.stream("POST", url, headers=headers, json=body,
                              timeout=self.timeout) as r:
                resp_headers = dict(r.headers)
                if r.status_code != 200:
                    body_text = r.read().decode("utf-8", errors="replace")[:400]
                    return StreamResult(
                        ok=False, ttft=None,
                        total_time=time.perf_counter() - start,
                        text="", chunk_count=0, finish_reason=None,
                        status_code=r.status_code,
                        error=f"HTTP {r.status_code}: {body_text}",
                        format="openai", model=model,
                        response_headers=resp_headers,
                    )
                for line in r.iter_lines():
                    if not line:
                        continue
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        evt = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    chunk_count += 1
                    if capture_chunk_timings:
                        if len(chunk_timestamps) < MAX_CHUNK_RECORDS:
                            chunk_timestamps.append(
                                time.perf_counter() - start)
                        else:
                            chunk_intervals_truncated = True
                    if first_chunk_raw is None:
                        first_chunk_raw = data[:500]
                    delta_text = _openai_delta_text(evt)
                    if delta_text:
                        if ttft is None:
                            ttft = time.perf_counter() - start
                        text_parts.append(delta_text)
                    fr = _openai_finish_reason(evt)
                    if fr:
                        finish_reason = fr
            total = time.perf_counter() - start
            chunk_intervals = (_intervals(chunk_timestamps)
                               if capture_chunk_timings else None)
            return StreamResult(
                ok=True, ttft=ttft, total_time=total,
                text="".join(text_parts), chunk_count=chunk_count,
                finish_reason=finish_reason, status_code=200, error=None,
                format="openai", model=model,
                raw_first_chunk=first_chunk_raw,
                response_headers=resp_headers,
                chunk_intervals=chunk_intervals,
                chunk_timestamps=chunk_timestamps,
                chunk_intervals_truncated=chunk_intervals_truncated,
            )
        except httpx.TimeoutException as e:
            return StreamResult(
                ok=False, ttft=ttft,
                total_time=time.perf_counter() - start,
                text="".join(text_parts), chunk_count=chunk_count,
                finish_reason=finish_reason, status_code=0,
                error=f"timeout: {e}", format="openai", model=model)
        except httpx.HTTPError as e:
            return StreamResult(
                ok=False, ttft=ttft,
                total_time=time.perf_counter() - start,
                text="".join(text_parts), chunk_count=chunk_count,
                finish_reason=finish_reason, status_code=0,
                error=f"http_error: {e}", format="openai", model=model)
        except Exception as e:
            return StreamResult(
                ok=False, ttft=ttft,
                total_time=time.perf_counter() - start,
                text="".join(text_parts), chunk_count=chunk_count,
                finish_reason=finish_reason, status_code=0,
                error=f"{type(e).__name__}: {e}",
                format="openai", model=model)

    # -- Anthropic flavour ---------------------------------------------------

    def _stream_anthropic(self, model, prompt, system, max_tokens,
                          temperature, capture_chunk_timings=False) -> StreamResult:
        url = self._anthropic_url()
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        }
        if temperature is not None:
            body["temperature"] = temperature
        if system:
            body["system"] = system
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }

        start = time.perf_counter()
        ttft = None
        text_parts: list[str] = []
        chunk_count = 0
        finish_reason: Optional[str] = None
        first_chunk_raw = None
        chunk_timestamps: Optional[list[float]] = (
            [] if capture_chunk_timings else None)
        chunk_intervals_truncated = False
        try:
            with httpx.stream("POST", url, headers=headers, json=body,
                              timeout=self.timeout) as r:
                resp_headers = dict(r.headers)
                if r.status_code != 200:
                    body_text = r.read().decode("utf-8", errors="replace")[:400]
                    return StreamResult(
                        ok=False, ttft=None,
                        total_time=time.perf_counter() - start,
                        text="", chunk_count=0, finish_reason=None,
                        status_code=r.status_code,
                        error=f"HTTP {r.status_code}: {body_text}",
                        format="anthropic", model=model,
                        response_headers=resp_headers,
                    )
                for line in r.iter_lines():
                    if not line:
                        continue
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        evt = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    chunk_count += 1
                    if capture_chunk_timings:
                        if len(chunk_timestamps) < MAX_CHUNK_RECORDS:
                            chunk_timestamps.append(
                                time.perf_counter() - start)
                        else:
                            chunk_intervals_truncated = True
                    if first_chunk_raw is None:
                        first_chunk_raw = data[:500]
                    if (evt.get("type") == "content_block_delta"
                            and evt.get("delta", {}).get("type") == "text_delta"):
                        chunk_text = evt["delta"].get("text", "")
                        if chunk_text:
                            if ttft is None:
                                ttft = time.perf_counter() - start
                            text_parts.append(chunk_text)
                    if evt.get("type") == "message_delta":
                        sr = evt.get("delta", {}).get("stop_reason")
                        if sr:
                            finish_reason = sr
            total = time.perf_counter() - start
            chunk_intervals = (_intervals(chunk_timestamps)
                               if capture_chunk_timings else None)
            return StreamResult(
                ok=True, ttft=ttft, total_time=total,
                text="".join(text_parts), chunk_count=chunk_count,
                finish_reason=finish_reason, status_code=200, error=None,
                format="anthropic", model=model,
                raw_first_chunk=first_chunk_raw,
                response_headers=resp_headers,
                chunk_intervals=chunk_intervals,
                chunk_timestamps=chunk_timestamps,
                chunk_intervals_truncated=chunk_intervals_truncated,
            )
        except httpx.TimeoutException as e:
            return StreamResult(
                ok=False, ttft=ttft,
                total_time=time.perf_counter() - start,
                text="".join(text_parts), chunk_count=chunk_count,
                finish_reason=finish_reason, status_code=0,
                error=f"timeout: {e}", format="anthropic", model=model)
        except Exception as e:
            return StreamResult(
                ok=False, ttft=ttft,
                total_time=time.perf_counter() - start,
                text="".join(text_parts), chunk_count=chunk_count,
                finish_reason=finish_reason, status_code=0,
                error=f"{type(e).__name__}: {e}",
                format="anthropic", model=model)


# -- SSE helpers -------------------------------------------------------------

def _intervals(timestamps: list[float]) -> list[float]:
    """Gaps between successive timestamps; ``[]`` when fewer than 2 entries."""
    return [timestamps[i] - timestamps[i - 1]
            for i in range(1, len(timestamps))]


def _openai_delta_text(evt: dict) -> str:
    """Extract text content from one OpenAI streaming chunk."""
    choices = evt.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # GPT-4o multipart streams sometimes use a list of {type,text} blocks
        out = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                out.append(block["text"])
        return "".join(out)
    # Some relays use ``message.content`` instead of ``delta.content``
    msg = choices[0].get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
        return msg["content"]
    return ""


def _openai_finish_reason(evt: dict) -> Optional[str]:
    choices = evt.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    return choices[0].get("finish_reason")


def fetch_models(base_url: str, api_key: str, *, timeout: float = 15.0
                 ) -> tuple[list[str], Optional[str]]:
    """Best-effort ``/v1/models`` lookup. Returns ``(model_ids, error)``.

    ``error`` is a short string when the lookup failed; otherwise ``None``.
    """
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base = base + "/v1"
    url = base + "/models"
    headers_options = [
        {"Authorization": f"Bearer {api_key}"},
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    ]
    last_err = None
    for headers in headers_options:
        try:
            r = httpx.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                payload = r.json()
                data = payload.get("data") or payload
                if isinstance(data, list):
                    ids = []
                    for item in data:
                        if isinstance(item, dict):
                            mid = item.get("id") or item.get("model")
                            if isinstance(mid, str):
                                ids.append(mid)
                        elif isinstance(item, str):
                            ids.append(item)
                    if ids:
                        return ids, None
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return [], last_err
